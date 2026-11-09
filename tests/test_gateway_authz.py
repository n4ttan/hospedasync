"""Prova que as rotas REAIS do gateway exigem autenticacao (sem o override de auth dos outros testes)."""
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import signed_headers

GATEWAY = Path(__file__).resolve().parent.parent / "api-gateway" / "main.py"

ANALYST_ROUTES = [("GET", "/hotels"), ("GET", "/history"), ("GET", "/registry/nodes"),
                  ("POST", "/jobs/dispatch"), ("POST", "/sweeps"), ("GET", "/sweeps/x")]
ADMIN_ROUTES = [("GET", "/locks"), ("GET", "/locks/abc"), ("GET", "/coordination/status"),
                ("GET", "/coordination/events"), ("POST", "/hotels/refresh")]


@pytest.fixture
def real_app(monkeypatch):
    import auth
    import sweeps
    spec = importlib.util.spec_from_file_location("hs_gateway_authz", GATEWAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "init_db", lambda: None)

    async def no_consumer():
        return None
    monkeypatch.setattr(sweeps, "run_results_consumer", no_consumer)
    mod.app.dependency_overrides.pop(auth.current_user, None)  # auth REAL
    return mod


@pytest.mark.parametrize("method,path", ANALYST_ROUTES + ADMIN_ROUTES)
def test_protected_routes_reject_anonymous(real_app, method, path):
    with TestClient(real_app.app) as c:
        r = c.request(method, path, json={} if method == "POST" else None)
    assert r.status_code == 401, f"{method} {path} deveria exigir token"


@pytest.mark.parametrize("path", ["/health", "/"])
def test_public_routes_stay_public(real_app, path):
    with TestClient(real_app.app) as c:
        assert c.get(path).status_code == 200


def test_registry_rejects_unsigned_node(real_app):
    body = {"worker_id": "evil", "endpoint": "http://6.6.6.6:8001"}
    with TestClient(real_app.app) as c:
        assert c.post("/registry/register", json=body).status_code == 401
        bad = signed_headers("outro", "http://6.6.6.6:8001")  # assinatura de outro no
        assert c.post("/registry/register", json=body, headers=bad).status_code == 401
        assert real_app.ACTIVE_WORKERS == {}


def test_registry_accepts_signed_node(real_app):
    body = {"worker_id": "w9", "endpoint": "http://w9:8001"}
    with TestClient(real_app.app) as c:
        r = c.post("/registry/register", json=body, headers=signed_headers("w9", "http://w9:8001"))
    assert r.status_code == 200 and "w9" in real_app.ACTIVE_WORKERS
