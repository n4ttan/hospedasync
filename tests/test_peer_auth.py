"""Autenticacao HMAC de chamadas internas (gateway->worker, worker->worker), com falha fechada."""
import json
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import servicetoken
from conftest import SignedClient
from test_dispatch_mutex import RealAsyncClient, body, gw  # noqa: F401
from test_dispatch_failover import register
from test_election import el, reset  # noqa: F401  (fixtures)
from test_scraper import mods  # noqa: F401  (fixture)

SECRET = "test-service-secret"
SCRAPE = {"job_id": "j1", "hotel_key": "h1", "checkin_date": "2026-01-01", "checkout_date": "2026-01-02"}


@pytest.fixture
def app(el, monkeypatch):  # noqa: F811
    async def fake_start():
        pass
    monkeypatch.setattr(el, "start_election", fake_start)
    a = FastAPI()
    a.include_router(el.router)

    @a.get("/health")
    def health():
        return {"status": "UP"}
    return a


ROUTES = [("GET", "/leader", None), ("GET", "/events", None),
          ("POST", "/election", {"candidate_id": 1, "term": 1.0}),
          ("POST", "/coordinator", {"leader_id": 3, "term": 1.0})]


def call(c, method, path, payload, headers=None):
    return c.request(method, path, json=payload, headers=headers or {})


def sig(method, path, **kw):
    return servicetoken.sign_request(kw.pop("secret", SECRET), "api-gateway", method, path, **kw)


@pytest.mark.parametrize("method,path,payload", ROUTES)
def test_unsigned_is_401(app, method, path, payload):
    assert call(TestClient(app), method, path, payload).status_code == 401


@pytest.mark.parametrize("method,path,payload", ROUTES)
def test_valid_signature_is_200(app, method, path, payload):
    assert call(TestClient(app), method, path, payload, sig(method, path)).status_code == 200


@pytest.mark.parametrize("method,path,payload", ROUTES)
def test_wrong_secret_is_401(app, method, path, payload):
    h = sig(method, path, secret="outro-segredo")
    assert call(TestClient(app), method, path, payload, h).status_code == 401


def test_signature_for_other_path_is_401(app):
    c = TestClient(app)
    assert call(c, "GET", "/events", None, sig("GET", "/leader")).status_code == 401
    assert call(c, "POST", "/coordinator", ROUTES[3][2], sig("POST", "/election")).status_code == 401


def test_signature_for_other_method_is_401(app):
    assert call(TestClient(app), "POST", "/election", ROUTES[2][2], sig("GET", "/election")).status_code == 401


def test_expired_signature_is_401(app):
    h = sig("GET", "/leader", ts=time.time() - 120)
    assert call(TestClient(app), "GET", "/leader", None, h).status_code == 401


def test_tampered_service_id_is_401(app):
    h = sig("GET", "/leader")
    h[servicetoken.H_ID] = "worker-99"
    assert call(TestClient(app), "GET", "/leader", None, h).status_code == 401


def test_health_stays_public(app):
    assert TestClient(app).get("/health").status_code == 200


@pytest.mark.parametrize("method,path,payload", ROUTES)
def test_empty_secret_closes_everything(app, monkeypatch, method, path, payload):
    h = sig(method, path)  # assinado com o secret valido...
    monkeypatch.setenv("SERVICE_SECRET", "")  # ...mas o no esta sem secret: nada passa
    assert call(TestClient(app), method, path, payload, h).status_code == 401
    assert call(TestClient(app), method, path, payload, sig(method, path, secret="x")).status_code == 401


def test_verify_request_empty_secret_never_accepts():
    h = servicetoken.sign_request("s", "a", "GET", "/x")
    assert servicetoken.verify_request("", h, "GET", "/x") is False
    assert servicetoken.verify_request("s", h, "GET", "/x") is True
    assert servicetoken.verify_request("s", h, "POST", "/x") is False
    with pytest.raises(ValueError):
        servicetoken.sign_request("", "a", "GET", "/x")


def test_forged_coordinator_does_not_change_leader(app, el):  # noqa: F811
    c = TestClient(app)
    el.CURRENT_LEADER_ID = 3
    forged = {"leader_id": 1, "term": 9e12}
    assert call(c, "POST", "/coordinator", forged).status_code == 401
    assert call(c, "POST", "/coordinator", forged, sig("GET", "/leader")).status_code == 401
    assert el.CURRENT_LEADER_ID == 3
    assert call(c, "POST", "/coordinator", forged, sig("POST", "/coordinator")).status_code == 200
    assert el.CURRENT_LEADER_ID == 1  # so quem tem o secret consegue mudar o lider


def test_worker_scrape_requires_signature(mods):  # noqa: F811
    main, _ = mods
    c = TestClient(main.app)
    assert c.post("/scrape", json=SCRAPE).status_code == 401
    assert c.post("/scrape", json=SCRAPE, headers=sig("POST", "/leader")).status_code == 401
    assert c.get("/health").status_code == 200


def test_worker_election_calls_are_signed(el, monkeypatch):  # noqa: F811
    seen = []

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, json=None, headers=None, **kw):
            seen.append((url.rsplit("/", 1)[1], headers))
            raise httpx.ConnectError("x")

    monkeypatch.setattr(el.httpx, "AsyncClient", FakeClient)
    import asyncio
    asyncio.run(el.start_election())   # peer maior inacessivel -> vira lider e avisa /coordinator
    paths = {p for p, _ in seen}
    assert {"election", "coordinator"} <= paths
    for p, h in seen:
        assert servicetoken.verify_request(SECRET, h, "POST", f"/{p}")
        assert h[servicetoken.H_ID] == el.WORKER_ID


def test_gateway_sends_signed_headers_to_worker(gw, monkeypatch):  # noqa: F811
    reqs = []

    async def handler(request: httpx.Request):
        reqs.append(request)
        if request.url.path == "/scrape":
            b = json.loads(request.content)
            return httpx.Response(200, json={
                "job_id": b["job_id"], "hotel_key": b["hotel_key"], "hotel_name": "H", "currency": "BRL",
                "rates": [{"code": "BookingCom", "name": "Booking.com", "rate": 90.0}], "min_rate": 90.0,
                "daily_rate": 90.0, "source": "live", "fetched_at": "2026-01-01T00:00:00Z",
                "tripadvisor_url": None, "processed_by": "w1", "processing_time_ms": 1.0,
                "checkin_date": b["checkin_date"], "checkout_date": b["checkout_date"]})
        if request.url.path == "/leader":
            return httpx.Response(200, json={"leader_id": 1})
        return httpx.Response(200, json={"node": "w1", "lamport": 1, "events": []})

    monkeypatch.setattr(gw.httpx, "AsyncClient",
                        lambda *a, **kw: RealAsyncClient(*a, transport=httpx.MockTransport(handler),
                                                         **{k: v for k, v in kw.items() if k != "transport"}))
    with TestClient(gw.app) as c:
        register(c, "w1")
        assert c.post("/jobs/dispatch", json=body("Hotel Sig")).status_code == 200
        c.get("/coordination/status")
        c.get("/coordination/events")
    by_path = {r.url.path: r for r in reqs}
    assert {"/scrape", "/leader", "/events"} <= set(by_path)
    for path, r in by_path.items():
        assert r.headers[servicetoken.H_ID] == "api-gateway"
        assert servicetoken.verify_request(SECRET, r.headers, r.method, path), path
