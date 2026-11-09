import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

WORKERS = Path(__file__).resolve().parent.parent / "workers"


@pytest.fixture(scope="module")
def cons():
    os.environ.setdefault("WORKER_ID", "worker-02")
    os.environ.setdefault("SELF_URL", "http://worker-02:8002")
    sys.path.insert(0, str(WORKERS))
    try:
        import consumer
    finally:
        sys.path.remove(str(WORKERS))
    return consumer


class Msg:
    def __init__(self, job, headers=None, body=None):
        self.body = body if body is not None else json.dumps(job).encode()
        self.headers = headers or {}
        self.acked = 0
        self.nacked = []

    async def ack(self):
        self.acked += 1

    async def nack(self, requeue=True):
        self.nacked.append(requeue)


JOB = {"job_id": "j1", "sweep_id": "s1", "hotel_key": "g1-d1", "hotel_name": "H",
       "checkin_date": "2026-12-18", "checkout_date": "2026-12-20", "currency": "BRL", "lamport": 40}
DATA = {"rates": [{"code": "A", "name": "A", "rate": 100.0}], "min_rate": 100.0, "source": "live",
        "fetched_at": "x", "tripadvisor_url": None}


@pytest.fixture
def env(cons, monkeypatch):
    st = {"published": [], "locks": [], "released": [], "free": True, "fetch": None}

    async def fake_publish(channel, rk, payload, exchange=None, headers=None):
        st["published"].append((rk, payload, headers))

    def acquire(res, owner, ttl):
        st["locks"].append((res, owner, ttl))
        return st["free"]

    def release(res, owner):
        st["released"].append((res, owner))
        return True

    async def fetch(*a):
        if isinstance(st["fetch"], Exception):
            raise st["fetch"]
        return st["fetch"] if st["fetch"] is not None else dict(DATA)

    async def nosleep(_):
        pass

    monkeypatch.setattr(cons, "publish_json", fake_publish)
    monkeypatch.setattr(cons.distlock, "acquire_lock", acquire)
    monkeypatch.setattr(cons.distlock, "release_lock", release)
    monkeypatch.setattr(cons.scraper, "fetch_rates", fetch)
    monkeypatch.setattr(cons.asyncio, "sleep", nosleep)
    return st


def run(cons, msg):
    asyncio.run(cons.handle_message(msg, object()))


def test_ok_publishes_result_and_acks(cons, env):
    m = Msg(JOB)
    run(cons, m)
    (rk, payload, _), = env["published"]
    assert rk == cons.RESULTS_QUEUE
    assert payload["status"] == "ok" and payload["job_id"] == "j1" and payload["sweep_id"] == "s1"
    assert payload["processed_by"] == cons.election.WORKER_ID and payload["lamport"] > 40
    assert payload["result"]["min_rate"] == 100.0 and payload["error"] is None
    assert m.acked == 1 and not m.nacked
    assert env["locks"] == [("g1-d1", f"{cons.election.WORKER_ID}:j1", 60)]
    assert env["released"] == [("g1-d1", f"{cons.election.WORKER_ID}:j1")]


def test_lock_busy_republishes_with_header_and_acks(cons, env):
    env["free"] = False
    m = Msg(JOB, headers={"x-lock-conflicts": 3})
    run(cons, m)
    (rk, payload, headers), = env["published"]
    assert rk == cons.JOBS_QUEUE and payload == JOB and headers == {"x-lock-conflicts": 4}
    assert m.acked == 1
    assert env["released"] == []  # nao adquiriu, nao libera


def test_lock_timeout_after_30_conflicts(cons, env):
    env["free"] = False
    m = Msg(JOB, headers={"x-lock-conflicts": 30})
    run(cons, m)
    (rk, payload, _), = env["published"]
    assert rk == cons.RESULTS_QUEUE and payload["status"] == "error" and payload["error"] == "lock_timeout"
    assert m.acked == 1


def test_upstream_error_publishes_error_result(cons, env):
    env["fetch"] = cons.scraper.UpstreamError("boom")
    m = Msg(JOB)
    run(cons, m)
    (_, payload, _), = env["published"]
    assert payload["status"] == "error" and "boom" in payload["error"] and payload["result"] is None
    assert m.acked == 1 and len(env["released"]) == 1


def test_no_rates(cons, env):
    env["fetch"] = cons.scraper.NoRates()
    m = Msg(JOB)
    run(cons, m)
    assert env["published"][0][1]["status"] == "no_rates" and m.acked == 1


def test_unexpected_exception_nacks_without_requeue_and_releases(cons, env):
    env["fetch"] = RuntimeError("bug")
    m = Msg(JOB)
    run(cons, m)
    assert m.nacked == [False] and m.acked == 0 and env["published"] == []
    assert len(env["released"]) == 1


def test_invalid_body_nacks(cons, env):
    m = Msg(None, body=b"not json")
    run(cons, m)
    assert m.nacked == [False]
