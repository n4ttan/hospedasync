import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
import mq  # noqa: E402


def test_safe_url_remove_credenciais():
    out = mq.safe_url("amqp://usuario:S3nh4Secreta@rabbitmq:5672/")
    assert "S3nh4Secreta" not in out and "usuario" not in out
    assert out == "amqp://rabbitmq:5672/"


def test_erro_de_conexao_nao_vaza_senha(monkeypatch):
    async def falha(url):
        raise OSError("recusado " + url)

    monkeypatch.setattr(mq.aio_pika, "connect_robust", falha)
    with pytest.raises(ConnectionError) as e:
        asyncio.run(mq.connect("amqp://u:SegredoXYZ@host:5672/", retries=2, delay=0))
    assert "SegredoXYZ" not in str(e.value)
