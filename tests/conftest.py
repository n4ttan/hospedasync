import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, str(ROOT / "api-gateway"))  # database (nao colide com workers/main.py)
sys.path.insert(0, str(ROOT / "shared"))  # distlock, lamport, mq
sys.path.append(str(ROOT / "workers"))  # security (no fim: nao sombreia main.py do gateway)


@pytest.fixture(scope="session")
def distlock_mod():
    import distlock
    try:
        distlock._client.ping()
    except Exception as exc:
        pytest.skip(f"Redis indisponivel em {distlock.REDIS_URL}: {exc}")
    return distlock


@pytest.fixture
def clean_locks(distlock_mod):
    def wipe():
        for k in distlock_mod._client.scan_iter(match=f"{distlock_mod.LOCK_PREFIX}*"):
            distlock_mod._client.delete(k)
    wipe()
    yield distlock_mod
    wipe()


# --- Autenticacao nos testes do Gateway -------------------------------------
# Producao exige JWT e assinatura HMAC; nos testes de rota (nao nos de auth) o gateway
# roda com um usuario admin fixo via dependency_overrides, sem nenhum bypass no codigo.
os.environ.setdefault("SERVICE_SECRET", "test-service-secret")


def signed_headers(worker_id: str, endpoint: str) -> dict:
    import servicetoken
    return servicetoken.sign(os.environ["SERVICE_SECRET"], worker_id, endpoint)


def _install_gateway_auth_override():
    import fastapi

    import auth

    original_init = fastapi.FastAPI.__init__

    class _FakeAdmin:
        id, username, role, disabled = 0, "test-admin", "admin", False

    def patched(self, *a, **kw):
        original_init(self, *a, **kw)
        if kw.get("title") == "HospedaSync API Gateway":
            self.dependency_overrides[auth.current_user] = lambda: _FakeAdmin()

    fastapi.FastAPI.__init__ = patched


_install_gateway_auth_override()


from fastapi.testclient import TestClient  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402


class SignedClient(TestClient):
    """TestClient que assina cada requisicao como um peer interno (HMAC de servico)."""
    service_id = "api-gateway"

    def request(self, method, url, **kw):
        import servicetoken
        headers = dict(kw.pop("headers", None) or {})
        if "X-Service-Signature" not in headers:
            headers.update(servicetoken.sign_request(
                os.environ["SERVICE_SECRET"], self.service_id, method, urlsplit(str(url)).path))
        return super().request(method, url, headers=headers, **kw)
