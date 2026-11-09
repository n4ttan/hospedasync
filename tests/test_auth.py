import time

import jwt
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
import servicetoken


@pytest.fixture
def ctx(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(bind=eng, autoflush=False)
    auth.User.__table__.create(bind=eng)
    auth.reset_rate_limit()
    app = FastAPI()
    app.include_router(auth.router)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[auth.get_session] = override

    @app.get("/an")
    def an(_=Depends(auth.require_role("analyst"))):
        return {"ok": 1}

    @app.get("/ad")
    def ad(_=Depends(auth.require_role("admin"))):
        return {"ok": 1}

    @app.post("/reg")
    def reg(body: dict, request: Request):
        auth.check_service_headers(request.headers, body["worker_id"], body["endpoint"])
        return {"ok": 1}

    @app.post("/reg2", dependencies=[Depends(auth.verify_service_request)])
    def reg2(body: dict):
        return {"ok": 1}

    db = Session()
    for u, r in (("root", "admin"), ("ana", "analyst")):
        db.add(auth.User(username=u, password_hash=auth.hash_password("senha-longa-123"), role=r))
    db.commit()
    db.close()
    c = TestClient(app)
    c.Session = Session
    return c


def login(c, u="ana", p="senha-longa-123"):
    return c.post("/auth/login", json={"username": u, "password": p})


def hdr(c, u="ana"):
    return {"Authorization": "Bearer " + login(c, u).json()["access_token"]}


def test_login_ok(ctx):
    r = login(ctx)
    j = r.json()
    assert r.status_code == 200 and j["token_type"] == "bearer" and j["role"] == "analyst"
    assert j["expires_in"] == auth.JWT_TTL_MINUTES * 60
    claims = jwt.decode(j["access_token"], auth.JWT_SECRET, algorithms=["HS256"])
    assert {"sub", "role", "exp", "iat", "jti"} <= claims.keys()
    assert ctx.get("/auth/me", headers=hdr(ctx)).json()["username"] == "ana"


def test_login_error_identical(ctx):
    a = login(ctx, "ana", "errada-errada")
    b = login(ctx, "fantasma", "errada-errada")
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_no_token_and_bad_token(ctx):
    assert ctx.get("/an").status_code == 401
    assert ctx.get("/an", headers={"Authorization": "Bearer lixo"}).status_code == 401


def test_expired_token(ctx):
    now = int(time.time())
    t = jwt.encode({"sub": "ana", "role": "analyst", "iat": now - 100, "exp": now - 10, "jti": "x"},
                   auth.JWT_SECRET, algorithm="HS256")
    assert ctx.get("/an", headers={"Authorization": f"Bearer {t}"}).status_code == 401


def test_roles(ctx):
    assert ctx.get("/an", headers=hdr(ctx, "ana")).status_code == 200
    assert ctx.get("/ad", headers=hdr(ctx, "ana")).status_code == 403
    assert ctx.get("/an", headers=hdr(ctx, "root")).status_code == 200
    assert ctx.get("/ad", headers=hdr(ctx, "root")).status_code == 200


def test_rate_limit(ctx):
    for _ in range(5):
        assert login(ctx, "ana", "errada-errada").status_code == 401
    r = login(ctx, "ana", "senha-longa-123")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_seed_admin_idempotent(ctx, monkeypatch):
    db = ctx.Session()
    monkeypatch.delenv("ADMIN_USER", raising=False)
    assert auth.seed_admin(db) is False
    monkeypatch.setenv("ADMIN_USER", "novo")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-senha-longa")
    assert auth.seed_admin(db) is True
    assert auth.seed_admin(db) is False
    assert db.query(auth.User).filter_by(username="novo").count() == 1
    assert login(ctx, "novo", "admin-senha-longa").json()["role"] == "admin"


def test_create_user(ctx):
    body = {"username": "bia", "password": "outra-senha-123", "role": "analyst"}
    assert ctx.post("/auth/users", json=body).status_code == 401
    assert ctx.post("/auth/users", json=body, headers=hdr(ctx, "ana")).status_code == 403
    adm = hdr(ctx, "root")
    assert ctx.post("/auth/users", json={**body, "password": "curta"}, headers=adm).status_code == 422
    assert ctx.post("/auth/users", json={**body, "role": "x"}, headers=adm).status_code == 422
    assert ctx.post("/auth/users", json=body, headers=adm).status_code == 201
    assert ctx.post("/auth/users", json=body, headers=adm).status_code == 409
    assert login(ctx, "bia", "outra-senha-123").status_code == 200


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("SERVICE_SECRET", "s3gredo-de-teste")
    return "s3gredo-de-teste"


BODY = {"worker_id": "w1", "endpoint": "http://w1:8001"}


@pytest.mark.parametrize("path", ["/reg", "/reg2"])
def test_hmac_valid_and_invalid(ctx, secret, path):
    h = servicetoken.sign(secret, "w1", "http://w1:8001")
    assert ctx.post(path, json=BODY, headers=h).status_code == 200
    assert ctx.post(path, json=BODY).status_code == 401  # ausente
    bad = servicetoken.sign("outro-segredo", "w1", "http://w1:8001")
    assert ctx.post(path, json=BODY, headers=bad).status_code == 401
    # adulterado: endpoint trocado no corpo
    assert ctx.post(path, json={**BODY, "endpoint": "http://evil:1"}, headers=h).status_code == 401
    # expirado
    old = servicetoken.sign(secret, "w1", "http://w1:8001", ts=time.time() - 300)
    assert ctx.post(path, json=BODY, headers=old).status_code == 401


def test_hmac_unit(secret):
    h = servicetoken.sign(secret, "w1", "e")
    assert servicetoken.verify(secret, h, "w1", "e")
    assert not servicetoken.verify(secret, h, "w2", "e")
    assert not servicetoken.verify("", h, "w1", "e")
    h2 = dict(h); h2["X-Service-Signature"] = "0" * 64
    assert not servicetoken.verify(secret, h2, "w1", "e")
