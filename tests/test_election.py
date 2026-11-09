import asyncio
import importlib.util
import os
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
    assert r == {"status": "OK", "responder_id": 2}
    assert client.started == [1]  # dispara a propria eleicao
    for cand in (2, 3):
        r = client.post("/election", json={"candidate_id": cand, "term": 1.0}).json()
        assert r["status"] == "IGNORED"
    assert client.started == [1]


def test_coordinator_accepts_and_sets_leader(client, el):
    r = client.post("/coordinator", json={"leader_id": 3, "term": 100.0}).json()
    assert r == {"status": "ACK", "worker_id": 2}
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
