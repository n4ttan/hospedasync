import asyncio
import importlib.util
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

GATEWAY = Path(__file__).resolve().parent.parent / "api-gateway" / "main.py"
RealAsyncClient = httpx.AsyncClient


@pytest.fixture
def gw(clean_locks, monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("hs_gateway_main", GATEWAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "init_db", lambda: None)
    import database
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    eng = create_engine(f"sqlite:///{tmp_path / 'gw.db'}", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(bind=eng)
    Session = sessionmaker(bind=eng, autoflush=False)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    mod.app.dependency_overrides[mod.get_db] = _get_db
    mod.ACTIVE_WORKERS.clear()
    yield mod
    mod.app.dependency_overrides.clear()


def install_worker(gw, monkeypatch, delay=0.0, fail=False):
    calls = []

    async def handler(request: httpx.Request):
        import json
        body = json.loads(request.content)
        calls.append(body["hotel_key"])
        if fail:
            raise httpx.ConnectError("worker fora do ar", request=request)
        await asyncio.sleep(delay)
        return httpx.Response(200, json={
            "job_id": body["job_id"], "hotel_key": body["hotel_key"],
            "hotel_name": body["hotel_name"], "currency": body["currency"],
            "rates": [{"code": "BookingCom", "name": "Booking.com", "rate": 100.0}],
            "min_rate": 100.0, "daily_rate": 100.0, "source": "live",
            "fetched_at": "2026-01-01T00:00:00Z", "tripadvisor_url": None,
            "processed_by": "mock-worker", "processing_time_ms": 1.0,
            "checkin_date": body["checkin_date"], "checkout_date": body["checkout_date"],
        })

    def factory(*a, **kw):
        kw.pop("transport", None)
        return RealAsyncClient(*a, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(gw.httpx, "AsyncClient", factory)
    return calls


def body(hotel):
    return {"hotel_key": hotel, "checkin_date": "2026-01-01", "checkout_date": "2026-01-02"}


def concurrent_post(gw, hotels, stagger=0.15):
    results = [None] * len(hotels)

    def run(i, h):
        with TestClient(gw.app) as c:  # startup usa init_db mockado
            results[i] = c.post("/jobs/dispatch", json=body(h)).status_code

    threads = []
    for i, h in enumerate(hotels):
        t = threading.Thread(target=run, args=(i, h))
        threads.append(t)
        t.start()
        threading.Event().wait(stagger)
    for t in threads:
        t.join(10)
    return results


def test_same_hotel_concurrent_one_200_one_409(gw, monkeypatch):
    calls = install_worker(gw, monkeypatch, delay=1.0)
    res = concurrent_post(gw, ["Hotel X", "Hotel X"])
    assert sorted(res) == [200, 409]
    assert calls == ["Hotel X"]  # o 409 nem chegou ao worker


def test_different_hotels_both_200(gw, monkeypatch):
    install_worker(gw, monkeypatch, delay=1.0)
    assert concurrent_post(gw, ["Hotel A", "Hotel B"]) == [200, 200]


def test_lock_released_after_success(gw, monkeypatch):
    install_worker(gw, monkeypatch)
    with TestClient(gw.app) as c:
        assert c.post("/jobs/dispatch", json=body("H")).status_code == 200
        assert c.get("/locks/H").json()["locked"] is False
        assert c.post("/jobs/dispatch", json=body("H")).status_code == 200


def test_lock_released_when_worker_fails_then_retry_ok(gw, monkeypatch):
    install_worker(gw, monkeypatch, fail=True)
    with TestClient(gw.app) as c:
        assert c.post("/jobs/dispatch", json=body("H")).status_code == 503  # sem cache
        assert c.get("/locks").json()["locks"] == []
        install_worker(gw, monkeypatch)  # worker volta
        assert c.post("/jobs/dispatch", json=body("H")).status_code == 200


def test_locks_endpoint_shows_active_lock(gw, monkeypatch):
    gw.acquire_lock("H", "job-x")
    with TestClient(gw.app) as c:
        assert c.get("/locks/H").json() == {"hotel_name": "H", "locked": True}
        locks = c.get("/locks").json()["locks"]
        assert [l["resource"] for l in locks] == ["H"]
        assert c.post("/jobs/dispatch", json=body("H")).status_code == 409


def test_registry_and_nodes(gw):
    with TestClient(gw.app) as c:
        r = c.post("/registry/register", json={"worker_id": "w1", "endpoint": "http://w1:8001"})
        assert r.json()["status"] == "REGISTERED"
        n = c.get("/registry/nodes").json()
        assert n["count"] == 1 and "w1" in n["active_nodes"]


def test_coordination_status_consensus(gw, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"leader_id": 3})

    monkeypatch.setattr(gw.httpx, "AsyncClient",
                        lambda *a, **kw: RealAsyncClient(*a, transport=httpx.MockTransport(handler), **kw))
    with TestClient(gw.app) as c:
        for w in ("w1", "w2"):
            c.post("/registry/register", json={"worker_id": w, "endpoint": f"http://{w}"})
        s = c.get("/coordination/status").json()
    assert s["consensus"] is True and s["leader_id"] == 3
    assert s["active_locks"] == []


def test_coordination_status_split_view(gw, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"leader_id": 3 if "w1" in str(request.url) else 2})

    monkeypatch.setattr(gw.httpx, "AsyncClient",
                        lambda *a, **kw: RealAsyncClient(*a, transport=httpx.MockTransport(handler), **kw))
    with TestClient(gw.app) as c:
        for w in ("w1", "w2"):
            c.post("/registry/register", json={"worker_id": w, "endpoint": f"http://{w}"})
        s = c.get("/coordination/status").json()
    assert s["consensus"] is False and s["leader_id"] is None
