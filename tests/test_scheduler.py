import asyncio
import os
import sys
from datetime import date
from pathlib import Path

import pytest

WORKERS = Path(__file__).resolve().parent.parent / "workers"


@pytest.fixture(scope="module")
def sch():
    os.environ.setdefault("WORKER_ID", "worker-02")
    os.environ.setdefault("SELF_URL", "http://worker-02:8002")
    sys.path.insert(0, str(WORKERS))
    try:
        import scheduler
    finally:
        sys.path.remove(str(WORKERS))
    return sch_mod(scheduler)


def sch_mod(m):
    return m


HOTELS = [{"hotel_key": "g1-d1", "name": "A"}, {"hotel_key": "g2-d2", "name": "B"}]


class FakeHttp:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url, params=None, headers=None):
        class R:
            def raise_for_status(self): pass
            def json(self): return HOTELS
        FakeHttp.calls.append((url, params))
        FakeHttp.last_headers = headers
        return R()


@pytest.fixture
def env(sch, monkeypatch):
    st = {"published": []}
    FakeHttp.calls = []

    async def pub(channel, rk, payload, exchange=None, headers=None):
        st["published"].append((rk, payload, exchange))

    async def ensure():
        return object(), {"events": "EVENTS_EX"}

    monkeypatch.setattr(sch, "publish_json", pub)
    monkeypatch.setattr(sch, "_ensure_channel", ensure)
    monkeypatch.setattr(sch.httpx, "AsyncClient", FakeHttp)
    monkeypatch.setattr(sch, "SWEEP_DATE_OFFSETS", [7, 14])
    monkeypatch.setattr(sch, "SWEEP_NIGHTS", 2)
    monkeypatch.setattr(sch, "SWEEP_HOTELS", 5)
    return st


def test_sweep_publishes_jobs_and_event(sch, env):
    sid = asyncio.run(sch.run_sweep())
    assert sid.startswith("sweep-") and len(sid) == len("sweep-") + 8
    jobs = [p for rk, p, ex in env["published"] if rk == sch.JOBS_QUEUE]
    assert len(jobs) == 4
    today = date.today()
    first = jobs[0]
    assert first["sweep_id"] == sid and first["enqueued_by"] == sch.election.WORKER_ID
    assert (date.fromisoformat(first["checkin_date"]) - today).days == 7
    assert (date.fromisoformat(first["checkout_date"]) - date.fromisoformat(first["checkin_date"])).days == 2
    assert {j["hotel_key"] for j in jobs} == {"g1-d1", "g2-d2"}
    assert len({j["job_id"] for j in jobs}) == 4
    assert all(j["lamport"] > 0 and j["enqueued_at"] for j in jobs)
    ev = [(p, ex) for rk, p, ex in env["published"] if ex == "EVENTS_EX"]
    assert ev[0][0]["type"] == "sweep_scheduled"
    assert ev[0][0]["detail"] == {"sweep_id": sid, "jobs": 4, "leader": sch.election.MY_ID}
    assert FakeHttp.calls[0][1] == {"limit": 5}
    assert FakeHttp.calls[0][0].endswith("/internal/hotels")
    assert FakeHttp.last_headers["X-Service-Id"] == sch.election.WORKER_ID and FakeHttp.last_headers["X-Service-Signature"]


def _run_one_cycle(sch, monkeypatch):
    async def go():
        calls = {"n": 0}
        real_sleep = asyncio.sleep

        async def fast_sleep(_):
            calls["n"] += 1
            if calls["n"] > 1:
                raise asyncio.CancelledError
            await real_sleep(0)

        monkeypatch.setattr(sch.asyncio, "sleep", fast_sleep)
        try:
            await sch.run_scheduler()
        except asyncio.CancelledError:
            pass
    asyncio.run(go())


def test_only_leader_sweeps(sch, env, monkeypatch):
    monkeypatch.setattr(sch.election, "CURRENT_LEADER_ID", sch.election.MY_ID)
    _run_one_cycle(sch, monkeypatch)
    assert any(rk == sch.JOBS_QUEUE for rk, _, _ in env["published"])


def test_follower_does_not_publish(sch, env, monkeypatch):
    monkeypatch.setattr(sch.election, "CURRENT_LEADER_ID", sch.election.MY_ID + 1)
    _run_one_cycle(sch, monkeypatch)
    assert env["published"] == [] and FakeHttp.calls == []


def test_gateway_failure_does_not_kill_scheduler(sch, env, monkeypatch):
    monkeypatch.setattr(sch.election, "CURRENT_LEADER_ID", sch.election.MY_ID)

    async def boom():
        raise RuntimeError("gateway down")

    monkeypatch.setattr(sch, "run_sweep", boom)
    _run_one_cycle(sch, monkeypatch)  # nao propaga
    assert env["published"] == []


def test_leader_hook_publishes_leader_elected(sch, env):
    asyncio.run(sch.announce_leader())
    (rk, payload, ex), = env["published"]
    assert ex == "EVENTS_EX" and payload["type"] == "leader_elected"
    assert payload["detail"]["leader"] == sch.election.MY_ID


def test_cluster_event_broadcast_is_logged_with_lamport(sch):
    """Regressao: o listener do fanout quebrava (kwarg event_type colidia com EventLog.log)."""
    import json

    class Msg:
        body = json.dumps({"type": "sweep_scheduled", "node": "worker-03", "lamport": 41,
                           "detail": {"sweep_id": "sweep-x"}}).encode()

        def process(self):
            class Ctx:
                async def __aenter__(self): return self
                async def __aexit__(self, *a): return False
            return Ctx()

    before = sch.election.EVENTS.clock.time
    asyncio.run(sch.handle_cluster_event(Msg()))
    last = sch.election.EVENTS.snapshot()[-1]
    assert last["type"] == "cluster_event"
    assert last["detail"]["kind"] == "sweep_scheduled"
    assert last["lamport"] > 41 and sch.election.EVENTS.clock.time > before
