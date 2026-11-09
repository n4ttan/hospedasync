import importlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database
from database import Base, JobHistory


def _mk(rows):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    with S() as s:
        for r in rows:
            s.add(JobHistory(job_id=r, hotel_name=r, daily_rate=1, processed_by="w",
                             processing_time_ms=1, checkin_date="a", checkout_date="b"))
        s.commit()
    return S


@pytest.fixture
def setup(monkeypatch):
    primary = _mk(["P1", "P2"])
    replica = _mk(["R1"])
    monkeypatch.setattr(database, "SessionLocal", primary)
    monkeypatch.setattr(database, "ReplicaSessionLocal", replica)
    import main
    monkeypatch.setattr(main, "SessionLocal", primary)

    def _get_db():
        s = primary()
        try:
            yield s
        finally:
            s.close()

    main.app.dependency_overrides[main.get_db] = _get_db
    yield main, primary, replica
    main.app.dependency_overrides.clear()


def _ids(r):
    return {x["job_id"] for x in r.json()}


def test_strong_reads_primary(setup):
    main, *_ = setup
    r = TestClient(main.app).get("/history")
    assert r.status_code == 200 and _ids(r) == {"P1", "P2"}
    assert r.headers["X-Served-By"] == "primary" and r.headers["X-Consistency"] == "strong"


def test_eventual_reads_replica(setup):
    main, *_ = setup
    r = TestClient(main.app).get("/history?consistency=eventual")
    assert _ids(r) == {"R1"}
    assert r.headers["X-Served-By"] == "replica" and r.headers["X-Consistency"] == "eventual"


def test_eventual_fallback_when_replica_down(setup, monkeypatch):
    main, *_ = setup
    def broken():
        raise RuntimeError("replica fora")
    monkeypatch.setattr(database, "ReplicaSessionLocal", broken)
    r = TestClient(main.app).get("/history?consistency=eventual")
    assert _ids(r) == {"P1", "P2"}
    assert r.headers["X-Served-By"] == "primary" and r.headers["X-Replica-Fallback"] == "true"


def test_eventual_without_replica_configured(setup, monkeypatch):
    main, *_ = setup
    monkeypatch.setattr(database, "ReplicaSessionLocal", None)
    r = TestClient(main.app).get("/history?consistency=eventual")
    assert r.headers["X-Served-By"] == "primary" and r.headers["X-Replica-Fallback"] == "true"


def test_invalid_consistency_422(setup):
    main, *_ = setup
    assert TestClient(main.app).get("/history?consistency=xyz").status_code == 422


def test_get_read_db_routing(setup):
    _, primary, replica = setup
    assert database.get_read_db("strong")[1] == "primary"
    db, who, fb = database.get_read_db("eventual")
    db.close()
    assert (who, fb) == ("replica", False)


def test_status_without_replica(setup, monkeypatch):
    main, *_ = setup
    monkeypatch.setattr(database, "replica_engine", None)
    st = TestClient(main.app).get("/consistency/status").json()
    assert st["replica_configured"] is False and st["replica_reachable"] is False
    assert st["primary_in_recovery"] is False
