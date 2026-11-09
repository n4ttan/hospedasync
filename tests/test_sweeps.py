import asyncio
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

GATEWAY = Path(__file__).resolve().parent.parent / "api-gateway" / "main.py"


class FakeRedis:
    def __init__(self):
        self.h, self.s = {}, {}

    def hset(self, key, mapping=None, **kw):
        self.h.setdefault(key, {}).update({k: str(v) for k, v in (mapping or {}).items()})

    def hgetall(self, key):
        return dict(self.h.get(key, {}))

    def hincrby(self, key, f, n):
        d = self.h.setdefault(key, {})
        d[f] = str(int(d.get(f, 0)) + n)

    def sadd(self, key, v):
        st = self.s.setdefault(key, set())
        if v in st:
            return 0
        st.add(v)
        return 1

    def srem(self, key, v):
        self.s.get(key, set()).discard(v)

    def expire(self, *a):
        pass

    def delete(self, key):
        self.h.pop(key, None)


@pytest.fixture
def env(monkeypatch, tmp_path):
    import database
    import sweeps
    spec = importlib.util.spec_from_file_location("hs_gateway_sweeps", GATEWAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "init_db", lambda: None)

    async def no_consumer():
        return None
    monkeypatch.setattr(sweeps, "run_results_consumer", no_consumer)
    eng = create_engine(f"sqlite:///{tmp_path / 's.db'}", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(bind=eng)
    Session = sessionmaker(bind=eng, autoflush=False)
    with Session() as s:
        for i, rc in enumerate([10, 500, 300, 50, 400, 20, 5]):
            s.add(database.Hotel(hotel_key=f"h{i}", name=f"Hotel {i}", review_count=rc))
        s.commit()

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    mod.app.dependency_overrides[database.get_db] = _get_db
    fake = FakeRedis()
    monkeypatch.setattr(sweeps, "get_redis", lambda: fake)
    published = []

    async def fake_publish(jobs):
        published.extend(jobs)
    monkeypatch.setattr(sweeps, "publish_jobs", fake_publish)
    yield mod, sweeps, database, Session, fake, published
    mod.app.dependency_overrides.clear()


def test_post_sweep_top_n_and_jobs(env):
    mod, sweeps, db, Session, fake, published = env
    with TestClient(mod.app) as c:
        r = c.post("/sweeps", json={"top_n": 3, "date_offsets": [7, 14], "nights": 2})
    assert r.status_code == 202
    body = r.json()
    assert body["jobs_enqueued"] == 6 and body["sweep_id"].startswith("sweep-")
    assert {j["hotel_key"] for j in published} == {"h1", "h4", "h2"}  # maior review_count
    j = published[0]
    assert j["sweep_id"] == body["sweep_id"] and j["enqueued_by"] == "api-gateway"
    assert j["lamport"] >= 1 and j["currency"] == "BRL"
    assert len({x["job_id"] for x in published}) == 6
    assert fake.hgetall(f"sweep:{body['sweep_id']}")["total"] == "6"


def test_post_sweep_explicit_keys(env):
    mod, sweeps, db, Session, fake, published = env
    with TestClient(mod.app) as c:
        r = c.post("/sweeps", json={"hotel_keys": ["h0", "zzz"], "date_offsets": [3]})
    assert r.status_code == 202 and r.json()["jobs_enqueued"] == 2
    assert {j["hotel_name"] for j in published} == {"Hotel 0", "zzz"}


@pytest.mark.parametrize("payload", [
    {"top_n": 21},
    {"top_n": 0},
    {"date_offsets": []},
    {"hotel_keys": [f"k{i}" for i in range(20)], "date_offsets": [1, 2, 3, 4]},  # 80 jobs
    {"hotel_keys": [f"k{i}" for i in range(7)], "date_offsets": list(range(1, 11))},  # 70 jobs
])
def test_post_sweep_limits(env, payload):
    mod, *_, published = env
    with TestClient(mod.app) as c:
        assert c.post("/sweeps", json=payload).status_code == 422
    assert published == []


def test_post_sweep_broker_down_503(env, monkeypatch):
    mod, sweeps, db, Session, fake, published = env

    async def boom(jobs):
        raise ConnectionError("rabbit fora")
    monkeypatch.setattr(sweeps, "publish_jobs", boom)
    with TestClient(mod.app) as c:
        r = c.post("/sweeps", json={"top_n": 1})
    assert r.status_code == 503 and "RabbitMQ" in r.json()["detail"]
    assert fake.h == {}  # nao deixa varredura fantasma


def test_get_sweep_and_404(env):
    mod, sweeps, db, Session, fake, published = env
    with TestClient(mod.app) as c:
        assert c.get("/sweeps/sweep-nope").status_code == 404
        fake.hset("sweep:sweep-x", mapping={"total": 4, "done": 2, "errors": 1, "no_rates": 0})
        s = c.get("/sweeps/sweep-x").json()
        assert s == {"sweep_id": "sweep-x", "total": 4, "done": 2, "errors": 1,
                     "no_rates": 0, "pending": 1, "finished": False}
        fake.hincrby("sweep:sweep-x", "no_rates", 1)
        assert c.get("/sweeps/sweep-x").json()["finished"] is True


def _ok(job_id="job-1", sweep="sweep-a"):
    return {"job_id": job_id, "sweep_id": sweep, "status": "ok", "processed_by": "w1", "lamport": 5,
            "error": None, "result": {
                "hotel_key": "h1", "hotel_name": "Hotel 1", "checkin_date": "2026-12-01",
                "checkout_date": "2026-12-03", "currency": "BRL", "rates": [{"code": "x", "rate": 90}],
                "min_rate": 90.0, "daily_rate": 90.0, "source": "live", "fetched_at": "t",
                "processing_time_ms": 12.0}}


def test_handle_result_ok_idempotent(env):
    mod, sweeps, db, Session, fake, _ = env
    fake.hset("sweep:sweep-a", mapping={"total": 1, "done": 0, "errors": 0, "no_rates": 0})
    assert asyncio.run(sweeps.handle_result(_ok(), Session, fake)) == "counted"
    assert asyncio.run(sweeps.handle_result(_ok(), Session, fake)) == "duplicate"
    with Session() as s:
        rows = s.query(db.JobHistory).all()
        assert len(rows) == 1 and rows[0].sweep_id == "sweep-a" and rows[0].processed_by == "w1"
    assert fake.hgetall("sweep:sweep-a")["done"] == "1"


def test_handle_result_error_and_no_rates(env):
    mod, sweeps, db, Session, fake, _ = env
    fake.hset("sweep:sweep-a", mapping={"total": 2})
    asyncio.run(sweeps.handle_result({"job_id": "j1", "sweep_id": "sweep-a", "status": "error",
                                      "result": None, "error": "x"}, Session, fake))
    asyncio.run(sweeps.handle_result({"job_id": "j2", "sweep_id": "sweep-a", "status": "no_rates",
                                      "result": None, "error": None}, Session, fake))
    h = fake.hgetall("sweep:sweep-a")
    assert h["errors"] == "1" and h["no_rates"] == "1" and h.get("done", "0") == "0"
    with Session() as s:
        assert s.query(db.JobHistory).count() == 0


def test_handle_result_db_failure_raises_for_requeue(env):
    mod, sweeps, db, Session, fake, _ = env

    def broken():
        raise RuntimeError("banco fora")
    with pytest.raises(RuntimeError):
        asyncio.run(sweeps.handle_result(_ok(), broken, fake))
    assert "sweep:sweep-a:seen" not in fake.s  # nada contado: a redelivery contara


def test_handle_result_malformed(env):
    mod, sweeps, db, Session, fake, _ = env
    for bad in ({"status": "ok"}, {"job_id": "j", "status": "weird"}, {"job_id": "j", "status": "ok", "result": None},
                {"job_id": "j", "status": "ok", "result": {"hotel_name": "x"}}, "lixo"):
        with pytest.raises(sweeps.MalformedResult):
            asyncio.run(sweeps.handle_result(bad, Session, fake))


def test_health_has_rabbitmq(env):
    mod, sweeps, *_ = env
    with TestClient(mod.app) as c:
        assert c.get("/health").json()["rabbitmq"] in ("UP", "DOWN")
