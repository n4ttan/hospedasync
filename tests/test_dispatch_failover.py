"""Failover do dispatch entre workers e circuit breaker por worker."""
import json

import httpx
from fastapi.testclient import TestClient

from conftest import signed_headers
from test_dispatch_mutex import RealAsyncClient, body, gw  # noqa: F401  (fixture reutilizada)


def register(c, *ids):
    for w in ids:
        ep = f"http://{w}"
        assert c.post("/registry/register", json={"worker_id": w, "endpoint": ep},
                      headers=signed_headers(w, ep)).status_code == 200


def install(gw, monkeypatch, dead):
    """Workers em `dead` recusam conexao; os demais respondem 200. Devolve a lista de hosts chamados."""
    hosts = []

    async def handler(request: httpx.Request):
        hosts.append(request.url.host)
        if request.url.host in dead:
            raise httpx.ConnectError("fora do ar", request=request)
        b = json.loads(request.content)
        return httpx.Response(200, json={
            "job_id": b["job_id"], "hotel_key": b["hotel_key"], "hotel_name": "H", "currency": "BRL",
            "rates": [{"code": "BookingCom", "name": "Booking.com", "rate": 90.0}], "min_rate": 90.0,
            "daily_rate": 90.0, "source": "live", "fetched_at": "2026-01-01T00:00:00Z",
            "tripadvisor_url": None, "processed_by": request.url.host, "processing_time_ms": 1.0,
            "checkin_date": b["checkin_date"], "checkout_date": b["checkout_date"]})

    monkeypatch.setattr(gw.httpx, "AsyncClient",
                        lambda *a, **kw: RealAsyncClient(*a, transport=httpx.MockTransport(handler),
                                                         **{k: v for k, v in kw.items() if k != "transport"}))
    return hosts


def test_failover_to_second_worker_when_first_is_dead(gw, monkeypatch):
    hosts = install(gw, monkeypatch, dead={"w-dead"})
    monkeypatch.setattr(gw.random, "shuffle", lambda lst: lst.sort(key=lambda t: t[0] != "w-dead"))  # w-dead 1o
    with TestClient(gw.app) as c:
        register(c, "w-dead", "w-ok")
        r = c.post("/jobs/dispatch", json=body("Hotel F"))
    assert r.status_code == 200 and r.json()["gateway_status"] == "SUCCESS"
    assert r.json()["selected_worker_id"] == "w-ok"
    assert hosts == ["w-dead", "w-ok"]
    assert gw.METRICS["dispatch_failover_total"] == 1


def test_breaker_opens_after_repeated_failures_and_skips_dead_worker(gw, monkeypatch):
    hosts = install(gw, monkeypatch, dead={"w-dead"})
    monkeypatch.setattr(gw.random, "shuffle", lambda lst: lst.sort(key=lambda t: t[0] != "w-dead"))
    with TestClient(gw.app) as c:
        register(c, "w-dead", "w-ok")
        for i in range(4):
            assert c.post("/jobs/dispatch", json=body(f"Hotel {i}")).status_code == 200
    assert gw._breaker("w-dead").state == "open"
    # depois de abrir, o worker morto deixa de ser chamado
    assert hosts.count("w-dead") == 3


def test_all_workers_dead_returns_503(gw, monkeypatch):
    install(gw, monkeypatch, dead={"a", "b"})
    with TestClient(gw.app) as c:
        register(c, "a", "b")
        r = c.post("/jobs/dispatch", json=body("Hotel Z"))
    assert r.status_code == 503


def test_metrics_endpoint_exposes_counters_and_breakers(gw, monkeypatch):
    install(gw, monkeypatch, dead=set())
    with TestClient(gw.app) as c:
        register(c, "m1")
        c.post("/jobs/dispatch", json=body("Hotel M"))
        text = c.get("/metrics").text
    assert "hospeda_dispatch_total 1" in text and "hospeda_workers_active 1" in text
    assert 'hospeda_worker_breaker_state{worker="m1"} 0' in text


def test_worker_502_upstream_outage_does_not_open_gateway_breaker(gw, monkeypatch):
    """502 do worker = fonte de precos fora (worker vivo). Nao pode contar como falha do worker no gateway."""
    async def handler(request: httpx.Request):
        return httpx.Response(502, json={"detail": {"error": "upstream_unavailable"}})

    monkeypatch.setattr(gw.httpx, "AsyncClient",
                        lambda *a, **kw: RealAsyncClient(*a, transport=httpx.MockTransport(handler),
                                                         **{k: v for k, v in kw.items() if k != "transport"}))
    with TestClient(gw.app) as c:
        register(c, "w-up")
        for i in range(6):
            r = c.post("/jobs/dispatch", json=body(f"Hotel U{i}"))
            assert r.status_code == 503  # sem cache previo: erro claro
    assert gw._breaker("w-up").state == "closed"
