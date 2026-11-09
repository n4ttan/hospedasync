import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

GATEWAY = Path(__file__).resolve().parent.parent / "api-gateway" / "main.py"
RealAsyncClient = httpx.AsyncClient


@pytest.fixture
def env(clean_locks, monkeypatch, tmp_path):
    import database
    import hotels
    spec = importlib.util.spec_from_file_location("hs_gateway_catalog", GATEWAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "init_db", lambda: None)
    eng = create_engine(f"sqlite:///{tmp_path / 'cat.db'}", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(bind=eng)
    Session = sessionmaker(bind=eng, autoflush=False)
    monkeypatch.setattr(mod, "SessionLocal", Session)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    mod.app.dependency_overrides[mod.get_db] = _get_db
    mod.ACTIVE_WORKERS.clear()
    yield mod, Session, hotels, database
    mod.app.dependency_overrides.clear()


def install_worker(mod, monkeypatch, status=200, connect_error=False):
    sent = []

    def handler(request: httpx.Request):
        b = json.loads(request.content)
        sent.append(b)
        if connect_error:
            raise httpx.ConnectError("fora do ar", request=request)
        if status != 200:
            return httpx.Response(status, json={"detail": {"error": "no_rates" if status == 404 else "upstream_unavailable"}})
        return httpx.Response(200, json={
            **{k: b[k] for k in ("job_id", "hotel_key", "hotel_name", "checkin_date", "checkout_date", "currency")},
            "rates": [{"code": "A", "name": "A", "rate": 300.0}, {"code": "B", "name": "B", "rate": 250.0}],
            "min_rate": 250.0, "daily_rate": 250.0, "source": "live",
            "fetched_at": "2026-02-02T10:00:00Z", "tripadvisor_url": "http://t",
            "processed_by": "mock", "processing_time_ms": 2.0,
        })

    monkeypatch.setattr(mod.httpx, "AsyncClient",
                        lambda *a, **kw: RealAsyncClient(*a, transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"}))
    return sent


def dispatch(c, **kw):
    body = {"checkin_date": "2026-03-01", "checkout_date": "2026-03-02", **kw}
    return c.post("/jobs/dispatch", json=body)


def test_seed_idempotent_and_upsert(env):
    mod, Session, hotels, database = env
    with Session() as db:
        n = hotels.seed_if_empty(db)
        assert n == 100
        assert hotels.seed_if_empty(db) == 0
        assert db.query(database.Hotel).count() == 100
        # upsert: reaplicar um item nao duplica
        item = json.loads(hotels.DATA_FILE.read_text(encoding="utf-8"))[0]
        item["name"] = "Renomeado"
        hotels._upsert(db, item)
        db.commit()
        assert db.query(database.Hotel).count() == 100
        assert db.get(database.Hotel, item["hotel_key"]).name == "Renomeado"


def test_get_hotels_search_and_order(env):
    mod, Session, hotels, _ = env
    with Session() as db:
        hotels.seed_if_empty(db)
    with TestClient(mod.app) as c:
        all_ = c.get("/hotels?limit=10").json()
        assert len(all_) == 10
        counts = [h["review_count"] for h in all_]
        assert counts == sorted(counts, reverse=True)
        found = c.get("/hotels", params={"q": "santa teresa"}).json()
        assert found and all("santa teresa" in h["name"].lower() for h in found)


def test_resolve_by_name_and_404(env, monkeypatch):
    mod, Session, hotels, _ = env
    with Session() as db:
        hotels.seed_if_empty(db)
    sent = install_worker(mod, monkeypatch)
    with TestClient(mod.app) as c:
        r = dispatch(c, hotel_name="Santa Teresa Hotel RJ")
        assert r.status_code == 200, r.text
        assert sent[0]["hotel_key"] == "g303506-d1200117"
        assert sent[0]["currency"] == "BRL"
        assert r.json()["persisted"] is True
        assert dispatch(c, hotel_name="Hotel Que Nao Existe XYZ").status_code == 404
        assert dispatch(c).status_code == 422


def test_persist_and_history_rates_as_list(env, monkeypatch):
    mod, *_ = env
    install_worker(mod, monkeypatch)
    with TestClient(mod.app) as c:
        r = dispatch(c, hotel_key="k-1", hotel_name="Hotel K")
        assert r.json()["gateway_status"] == "SUCCESS"
        h = c.get("/history").json()[0]
        assert h["hotel_key"] == "k-1" and h["min_rate"] == 250.0
        assert isinstance(h["rates"], list) and len(h["rates"]) == 2
        assert h["source"] == "live" and h["fetched_at"] == "2026-02-02T10:00:00Z"


@pytest.mark.parametrize("mode", ["connect", 502, 503])
def test_fallback_to_cache(env, monkeypatch, mode):
    mod, *_ = env
    install_worker(mod, monkeypatch)
    with TestClient(mod.app) as c:
        assert dispatch(c, hotel_key="k-2", hotel_name="Hotel Z").status_code == 200
        if mode == "connect":
            install_worker(mod, monkeypatch, connect_error=True)
        else:
            install_worker(mod, monkeypatch, status=mode)
        # datas diferentes: cai na cotacao mais recente do hotel
        r = dispatch(c, hotel_key="k-2", checkin_date="2026-09-01", checkout_date="2026-09-03")
        assert r.status_code == 200
        res = r.json()["result"]
        assert res["source"] == "cache" and res["stale"] is True
        assert res["fetched_at"] == "2026-02-02T10:00:00Z"
        assert res["min_rate"] == 250.0 and len(res["rates"]) == 2
        # sem historico para outro hotel: 503
        assert dispatch(c, hotel_key="k-none", hotel_name="X").status_code == 503
        assert c.get("/locks").json()["locks"] == []


def test_worker_404_passthrough(env, monkeypatch):
    mod, *_ = env
    install_worker(mod, monkeypatch, status=404)
    with TestClient(mod.app) as c:
        r = dispatch(c, hotel_key="k-3", hotel_name="H")
        assert r.status_code == 404
        assert r.json()["detail"] == {"error": "no_rates"}


def test_persisted_false_on_db_failure(env, monkeypatch):
    mod, Session, hotels, database = env
    install_worker(mod, monkeypatch)

    class BrokenDB:
        def get(self, *a, **k): return None
        def add(self, o): pass
        def commit(self): raise RuntimeError("db down")
        def rollback(self): pass
        def close(self): pass

    mod.app.dependency_overrides[mod.get_db] = lambda: BrokenDB()
    with TestClient(mod.app) as c:
        r = dispatch(c, hotel_key="k-4", hotel_name="H")
        assert r.status_code == 200
        j = r.json()
        assert j["persisted"] is False and j["gateway_status"] == "DEGRADED"
        assert j["result"]["min_rate"] == 250.0
