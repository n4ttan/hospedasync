import asyncio
import importlib.util
import os
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from conftest import SignedClient as TestClient

ELECTION = Path(__file__).resolve().parent.parent / "workers" / "election.py"


@pytest.fixture(scope="module")
def el():
    saved = {k: os.environ.get(k) for k in ("WORKER_ID", "SELF_URL", "PEER_WORKERS")}
    os.environ["WORKER_ID"] = "worker-02"
    os.environ["SELF_URL"] = "http://worker-02:8002"
    os.environ["PEER_WORKERS"] = "http://worker-01:8001,http://worker-03:8003"
    try:
        spec = importlib.util.spec_from_file_location("hs_election", ELECTION)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


@pytest.fixture(autouse=True)
def reset(el):
    def r():
        el.CURRENT_LEADER_ID = None
        el.election_term = 0.0
        el.election_in_progress = False
        el._last_coordinator_term_by_leader.clear()
    r()
    yield
    r()


@pytest.fixture
def client(el, monkeypatch):
    started = []

    async def fake_start():
        started.append(1)

    monkeypatch.setattr(el, "start_election", fake_start)
    app = FastAPI()
    app.include_router(el.router)
    c = TestClient(app)
    c.started = started
    return c


class FakeResp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._d = data or {}

    def json(self):
        return self._d


def fake_httpx(monkeypatch, el, behavior):
    """behavior: {url_prefix: FakeResp | Exception}. Registra posts em .posts."""
    posts = []

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, json=None, **kw):
            posts.append((url, json))
            for prefix, b in behavior.items():
                if url.startswith(prefix):
                    if isinstance(b, Exception):
                        raise b
                    return b
            raise httpx.ConnectError("sem rota")

    monkeypatch.setattr(el.httpx, "AsyncClient", FakeClient)
    return posts


def test_module_config(el):
    assert el.MY_ID == 2
    assert el.PEERS == {1: "http://worker-01:8001", 2: "http://worker-02:8002", 3: "http://worker-03:8003"}


@pytest.mark.parametrize("url,expected", [
    ("http://worker-02:8002", 2),
    ("http://worker-10:8001/x", 10),
    ("worker-7", 7),
])
def test_extract_id_valid(el, url, expected):
    assert el.extract_id_from_url(url) == expected


@pytest.mark.parametrize("url", ["http://worker:8001", "http://127.0.0.x", "http://localhost"])
def test_extract_id_invalid(el, url):
    with pytest.raises(ValueError):
        el.extract_id_from_url(url)


def test_election_ok_only_if_my_id_greater(client):
    r = client.post("/election", json={"candidate_id": 1, "term": 1.0}).json()
    assert r["status"] == "OK" and r["responder_id"] == 2
    assert client.started == [1]  # dispara a propria eleicao
    for cand in (2, 3):
        r = client.post("/election", json={"candidate_id": cand, "term": 1.0}).json()
        assert r["status"] == "IGNORED"
    assert client.started == [1]


def test_coordinator_accepts_and_sets_leader(client, el):
    r = client.post("/coordinator", json={"leader_id": 3, "term": 100.0}).json()
    assert r["status"] == "ACK" and r["worker_id"] == 2
    assert client.get("/leader").json() == {"leader_id": 3, "am_i_leader": False}


def test_coordinator_discards_stale_term_same_leader(client, el):
    client.post("/coordinator", json={"leader_id": 3, "term": 100.0})
    r = client.post("/coordinator", json={"leader_id": 3, "term": 50.0}).json()
    assert r["status"] == "STALE"
    assert el._last_coordinator_term_by_leader[3] == 100.0
    # mesmo term e term maior sao aceitos
    assert client.post("/coordinator", json={"leader_id": 3, "term": 100.0}).json()["status"] == "ACK"
    assert client.post("/coordinator", json={"leader_id": 3, "term": 200.0}).json()["status"] == "ACK"


def test_stale_term_does_not_change_leader(client, el):
    client.post("/coordinator", json={"leader_id": 3, "term": 100.0})
    client.post("/coordinator", json={"leader_id": 1, "term": 10.0})  # outro lider, 1o term dele
    assert el.CURRENT_LEADER_ID == 1
    client.post("/coordinator", json={"leader_id": 3, "term": 90.0})  # obsoleto de 3
    assert el.CURRENT_LEADER_ID == 1


def test_leader_endpoint_when_self(client, el):
    el.CURRENT_LEADER_ID = el.MY_ID
    assert client.get("/leader").json() == {"leader_id": 2, "am_i_leader": True}
    el.CURRENT_LEADER_ID = None
    assert client.get("/leader").json() == {"leader_id": None, "am_i_leader": False}


def test_start_election_no_higher_peers_becomes_leader(el, monkeypatch):
    monkeypatch.setattr(el, "PEERS", {1: "http://worker-01:8001", 2: "http://worker-02:8002"})
    posts = fake_httpx(monkeypatch, el, {"http://worker-01": FakeResp(200, {"status": "ACK"})})
    asyncio.run(el.start_election())
    assert el.CURRENT_LEADER_ID == 2
    assert el.election_in_progress is False
    assert [u for u, _ in posts] == ["http://worker-01:8001/coordinator"]
    assert posts[0][1]["leader_id"] == 2


def test_start_election_higher_peer_ok_not_leader(el, monkeypatch):
    posts = fake_httpx(monkeypatch, el, {"http://worker-03": FakeResp(200, {"status": "OK"})})
    asyncio.run(el.start_election())
    assert el.CURRENT_LEADER_ID is None
    assert [u for u, _ in posts] == ["http://worker-03:8003/election"]
    assert el.election_in_progress is False


def test_start_election_higher_peer_down_becomes_leader(el, monkeypatch):
    posts = fake_httpx(monkeypatch, el, {
        "http://worker-03:8003/election": httpx.ConnectError("down"),
        "http://worker-01": FakeResp(200, {"status": "ACK"}),
    })
    asyncio.run(el.start_election())
    assert el.CURRENT_LEADER_ID == 2
    urls = [u for u, _ in posts]
    assert "http://worker-01:8001/coordinator" in urls
    assert "http://worker-03:8003/coordinator" in urls  # tenta avisar mesmo o fora do ar


def test_start_election_ignored_response_means_leader(el, monkeypatch):
    fake_httpx(monkeypatch, el, {"http://worker-03:8003/election": FakeResp(200, {"status": "IGNORED"}),
                                 "http://worker-01": FakeResp(200)})
    asyncio.run(el.start_election())
    assert el.CURRENT_LEADER_ID == 2


def test_start_election_skipped_if_already_in_progress(el, monkeypatch):
    posts = fake_httpx(monkeypatch, el, {})
    el.election_in_progress = True
    asyncio.run(el.start_election())
    assert posts == [] and el.CURRENT_LEADER_ID is None


# ---------- ID, peers e relogio de Lamport ----------

def _load(monkeypatch, **env):
    for k in ("WORKER_ID", "SELF_URL", "PEER_WORKERS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("hs_election_x", ELECTION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_my_id_from_worker_id(monkeypatch):
    m = _load(monkeypatch, WORKER_ID="worker-02", SELF_URL="http://10.0.1.7:8001")
    assert m.MY_ID == 2 and m.WORKER_ID == "worker-02"


def test_my_id_falls_back_to_self_url(monkeypatch):
    m = _load(monkeypatch, WORKER_ID="alpha", SELF_URL="http://worker-05:8001")
    assert m.MY_ID == 5
    m = _load(monkeypatch, SELF_URL="http://worker-03:8001")
    assert m.MY_ID == 3


def test_peer_workers_both_formats(monkeypatch):
    m = _load(monkeypatch, WORKER_ID="worker-02", SELF_URL="http://10.0.1.2:8002",
              PEER_WORKERS="1=http://10.0.1.5:8001, http://worker-03:8003")
    assert m.PEERS == {2: "http://10.0.1.2:8002", 1: "http://10.0.1.5:8001", 3: "http://worker-03:8003"}


def test_lamport_in_responses_and_events(client, el):
    r = client.post("/election", json={"candidate_id": 1, "term": 1.0, "lamport": 50}).json()
    assert r["lamport"] > 50
    r = client.post("/coordinator", json={"leader_id": 3, "term": 5.0, "lamport": 200}).json()
    assert r["lamport"] > 200
    ev = client.get("/events").json()
    assert ev["node"] == "worker-02" and ev["lamport"] >= r["lamport"]
    types = [e["type"] for e in ev["events"]]
    assert "coordinator_received" in types
    assert "election_received" in types


def test_lamport_optional_in_requests(client):
    assert client.post("/election", json={"candidate_id": 1, "term": 1.0}).status_code == 200


def test_become_leader_runs_hooks_and_logs(el, monkeypatch):
    called = []

    async def hook():
        called.append(1)

    monkeypatch.setattr(el, "on_became_leader", [hook])
    posts = fake_httpx(monkeypatch, el, {"http://worker-0": FakeResp(200, {"status": "ACK", "lamport": 999})})
    asyncio.run(el.become_leader(1.0))
    assert called == [1]
    assert all("lamport" in body for _, body in posts)
    assert el.CLOCK.time > 999
    assert "became_leader" in [e["type"] for e in el.EVENTS.snapshot()]
