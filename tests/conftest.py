import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, str(ROOT / "api-gateway"))  # distlock, database (nao colide com workers/main.py)


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
