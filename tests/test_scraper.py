import importlib.util
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

WORKERS = Path(__file__).resolve().parent.parent / "workers"
BODY = {"job_id": "j1", "hotel_key": "g1-d1", "hotel_name": "Hotel X",
        "checkin_date": "2026-12-18", "checkout_date": "2026-12-20"}
OK = {"error": None, "result": {"rates": [
    {"code": "A", "name": "A.com", "rate": 300, "tax": None},
    {"code": "B", "name": "B.com", "rate": 250.5, "tax": 10}]}}


@pytest.fixture(scope="module")
def mods():
    saved = {k: os.environ.get(k) for k in ("WORKER_ID", "SELF_URL", "PEER_WORKERS")}
    os.environ.update(WORKER_ID="worker-02", SELF_URL="http://worker-02:8002",
                      PEER_WORKERS="http://worker-01:8001,http://worker-03:8003")
    sys.path.insert(0, str(WORKERS))
    try:
        spec = importlib.util.spec_from_file_location("hs_worker_main", WORKERS / "main.py")
        main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main)
        import scraper
    finally:
        sys.path.remove(str(WORKERS))
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return main, scraper


@pytest.fixture
def env(mods):
    main, scraper = mods
    scraper.clear_cache()
    scraper._redis_down_until = float("inf")  # sem Redis nos testes: usa o cache em memoria
    scraper.BACKOFF_BASE = 0
    calls = []

    def install(handler):
        def wrapped(request):
            calls.append(request)
            return handler(request)
        scraper._transport = httpx.MockTransport(wrapped)

    yield TestClient(main.app), scraper, install, calls
    scraper._transport = None
    scraper._redis_down_until = 0.0
    scraper.clear_cache()


def test_success(env):
    client, _, install, calls = env
    install(lambda r: httpx.Response(200, json=OK))
    r = client.post("/scrape", json=BODY)
    assert r.status_code == 200
    d = r.json()
    assert d["processed_by"] == "worker-02" and d["source"] == "live"
    assert d["min_rate"] == d["daily_rate"] == 250.5
    assert d["currency"] == "BRL" and len(d["rates"]) == 2
    assert d["rates"][0] == {"code": "A", "name": "A.com", "rate": 300.0}
    assert d["fetched_at"] and "tripadvisor_url" in d and "processing_time_ms" in d
    q = calls[0].url.params
    assert q["hotel_key"] == "g1-d1" and q["chk_in"] == "2026-12-18" and q["currency"] == "BRL"
    assert "HospedaSync" in calls[0].headers["user-agent"]


def test_cache_hit(env):
    client, _, install, calls = env
    install(lambda r: httpx.Response(200, json=OK))
    assert client.post("/scrape", json=BODY).json()["source"] == "live"
    assert client.post("/scrape", json=BODY).json()["source"] == "cache"
    assert len(calls) == 1


def test_retry_503_then_success(env):
    client, _, install, calls = env
    install(lambda r: httpx.Response(503) if len(calls) < 2 else httpx.Response(200, json=OK))
    r = client.post("/scrape", json=BODY)
    assert r.status_code == 200 and len(calls) == 2


def test_total_failure_502(env):
    client, _, install, calls = env
    install(lambda r: httpx.Response(503))
    r = client.post("/scrape", json=BODY)
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "upstream_unavailable"
    assert len(calls) == 3


def test_upstream_error_field_502_no_retry(env):
    client, _, install, calls = env
    install(lambda r: httpx.Response(200, json={"error": {"status_code": 400, "message": "x"}, "result": None}))
    r = client.post("/scrape", json=BODY)
    assert r.status_code == 502 and len(calls) == 1


def test_timeout_502(env):
    client, _, install, calls = env

    def boom(r):
        raise httpx.ConnectTimeout("t")
    install(boom)
    assert client.post("/scrape", json=BODY).status_code == 502
    assert len(calls) == 3


def test_empty_rates_404(env):
    client, _, install, _ = env
    install(lambda r: httpx.Response(200, json={"error": None, "result": {"rates": []}}))
    r = client.post("/scrape", json=BODY)
    assert r.status_code == 404 and r.json()["detail"] == {"error": "no_rates"}


@pytest.mark.parametrize("patch", [
    {"hotel_key": None}, {"checkin_date": "18/12/2026"}, {"checkout_date": None}, {"job_id": None}])
def test_validation(env, patch):
    client, _, install, calls = env
    install(lambda r: httpx.Response(200, json=OK))
    body = {**BODY, **patch}
    body = {k: v for k, v in body.items() if v is not None}
    assert client.post("/scrape", json=body).status_code == 422
    assert not calls


def test_breaker_opens_and_fails_fast(env):
    client, scraper, install, calls = env
    from resilience import CircuitBreaker
    clock = [0.0]
    old = scraper.breaker
    scraper.breaker = CircuitBreaker("xotelo", 2, 30, clock=lambda: clock[0])
    try:
        install(lambda r: httpx.Response(503))
        for _ in range(2):
            assert client.post("/scrape", json=BODY).status_code == 502
        n = len(calls)
        r = client.post("/scrape", json=BODY)
        assert r.status_code == 502 and r.json()["detail"]["error"] == "upstream_unavailable"
        assert len(calls) == n  # sem chamar o upstream
        assert scraper.breaker_state()["state"] == "open"
        clock[0] = 30
        install(lambda r: httpx.Response(200, json=OK))
        assert client.post("/scrape", json=BODY).status_code == 200
        assert scraper.breaker_state()["state"] == "closed"
    finally:
        scraper.breaker = old
