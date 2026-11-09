from conftest import signed_headers
import importlib.util
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

GATEWAY = Path(__file__).resolve().parent.parent / "api-gateway" / "main.py"
RealAsyncClient = httpx.AsyncClient


@pytest.fixture
def gw(monkeypatch):
    import sweeps
    spec = importlib.util.spec_from_file_location("hs_gateway_events", GATEWAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "init_db", lambda: None)

    async def no_consumer():
        return None
    monkeypatch.setattr(sweeps, "run_results_consumer", no_consumer)
    mod.ACTIVE_WORKERS.clear()
    mod.EVENTS._events.clear()
    mod.EVENTS.clock._time = 0
    yield mod
    mod.app.dependency_overrides.clear()


def ev(node, lamport, t="x"):
    return {"node": node, "lamport": lamport, "wall_time": 0, "type": t, "detail": {}}


def test_events_merged_by_lamport_and_failures(gw, monkeypatch):
    gw.EVENTS.clock.update(10)  # gateway em L=11
    gw.EVENTS.log("local")  # L=12

    def handler(request):
        url = str(request.url)
        if "w3" in url:
            raise httpx.ConnectError("fora", request=request)
        if "w1" in url:
            return httpx.Response(200, json={"events": [ev("w1", 20), ev("w1", 3)]})
        return httpx.Response(200, json={"events": [ev("w2", 3), ev("w2", 15)]})

    monkeypatch.setattr(gw.httpx, "AsyncClient",
                        lambda *a, **kw: RealAsyncClient(*a, transport=httpx.MockTransport(handler), **kw))
    with TestClient(gw.app) as c:
        for w in ("w1", "w2", "w3"):
            c.post("/registry/register", json={"worker_id": w, "endpoint": f"http://{w}"}, headers=signed_headers(w, f"http://{w}"))
        r = c.get("/coordination/events").json()
        limited = c.get("/coordination/events?limit=2").json()
    assert [(e["lamport"], e["node"]) for e in r["events"]] == [
        (3, "w1"), (3, "w2"), (12, "api-gateway"), (15, "w2"), (20, "w1")]
    assert sorted(r["nodes_reached"]) == ["w1", "w2"] and r["nodes_failed"] == ["w3"]
    assert [e["lamport"] for e in limited["events"]] == [15, 20]


def test_dispatch_sends_lamport_header_and_logs(gw, monkeypatch):
    seen = {}

    def handler(request):
        import json
        seen["hdr"] = request.headers.get("X-Lamport")
        b = json.loads(request.content)
        return httpx.Response(200, headers={"X-Lamport": "50"}, json={
            "job_id": b["job_id"], "hotel_key": b["hotel_key"], "hotel_name": "H", "currency": "BRL",
            "rates": [], "min_rate": 1.0, "daily_rate": 1.0, "processed_by": "w", "processing_time_ms": 1.0,
            "checkin_date": b["checkin_date"], "checkout_date": b["checkout_date"]})

    monkeypatch.setattr(gw.httpx, "AsyncClient",
                        lambda *a, **kw: RealAsyncClient(*a, transport=httpx.MockTransport(handler), **kw))
    gw.app.dependency_overrides[gw.get_db] = lambda: None
    monkeypatch.setattr(gw, "acquire_lock", lambda *a, **k: True)
    monkeypatch.setattr(gw, "release_lock", lambda *a, **k: None)
    with TestClient(gw.app) as c:
        r = c.post("/jobs/dispatch", json={"hotel_key": "H", "hotel_name": "H", "checkin_date": "2026-01-01",
                                           "checkout_date": "2026-01-02"})
    assert r.status_code == 200
    assert int(seen["hdr"]) >= 1
    types = [e["type"] for e in gw.EVENTS.snapshot()]
    assert types == ["dispatch_sent", "dispatch_done"]
    assert gw.EVENTS.snapshot()[-1]["lamport"] == 51  # max(L, 50) + 1


def test_lock_conflict_event(gw, monkeypatch):
    gw.app.dependency_overrides[gw.get_db] = lambda: None
    monkeypatch.setattr(gw, "acquire_lock", lambda *a, **k: False)
    with TestClient(gw.app) as c:
        r = c.post("/jobs/dispatch", json={"hotel_key": "H", "hotel_name": "H", "checkin_date": "2026-01-01",
                                           "checkout_date": "2026-01-02"})
    assert r.status_code == 409
    assert [e["type"] for e in gw.EVENTS.snapshot()] == ["lock_conflict"]
