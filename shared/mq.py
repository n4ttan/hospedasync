"""
Topologia e helpers do RabbitMQ (aio-pika), compartilhados por Gateway e Workers.

  hospeda.scrape.jobs     fila duravel de trabalhos (Gateway/lider publicam, Workers consomem)
  hospeda.scrape.results  fila duravel de resultados (Workers publicam, Gateway consome e grava no banco)
  hospeda.scrape.dead     dead-letter: mensagens rejeitadas sem requeue vao para ca (via exchange hospeda.dlx)
  hospeda.cluster.events  exchange FANOUT: broadcast de eventos do cluster (lider eleito, varredura agendada)

Entrega at-least-once: o consumidor so da ack depois de concluir; se o worker morrer com a mensagem em voo, o
broker reentrega a outro worker. Por isso a gravacao dos resultados e idempotente (job_id e chave primaria).
"""

import asyncio
import json
import os

import aio_pika

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://hospeda:hospeda-mq-pass@rabbitmq:5672/")

JOBS_QUEUE = "hospeda.scrape.jobs"
RESULTS_QUEUE = "hospeda.scrape.results"
DEAD_QUEUE = "hospeda.scrape.dead"
DLX_EXCHANGE = "hospeda.dlx"
EVENTS_EXCHANGE = "hospeda.cluster.events"


async def connect(url: str = None, retries: int = 30, delay: float = 2.0):
    """Conexao robusta (reconecta sozinha). Tenta ate `retries` vezes na subida, pois o broker pode demorar."""
    url = url or RABBITMQ_URL
    last = None
    for _ in range(retries):
        try:
            return await aio_pika.connect_robust(url)
        except Exception as exc:  # broker ainda subindo
            last = exc
            await asyncio.sleep(delay)
    raise ConnectionError(f"RabbitMQ indisponivel em {url}: {last}")


async def declare_topology(channel) -> dict:
    """Declara (idempotente) filas, dead-letter e exchange de broadcast. Devolve os objetos."""
    dlx = await channel.declare_exchange(DLX_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
    dead = await channel.declare_queue(DEAD_QUEUE, durable=True)
    await dead.bind(dlx, routing_key="dead")
    jobs = await channel.declare_queue(
        JOBS_QUEUE,
        durable=True,
        arguments={"x-dead-letter-exchange": DLX_EXCHANGE, "x-dead-letter-routing-key": "dead"},
    )
    results = await channel.declare_queue(RESULTS_QUEUE, durable=True)
    events = await channel.declare_exchange(EVENTS_EXCHANGE, aio_pika.ExchangeType.FANOUT, durable=True)
    return {"jobs": jobs, "results": results, "dead": dead, "events": events, "dlx": dlx}


async def publish_json(channel, routing_key: str, payload: dict, exchange=None, headers: dict = None):
    """Publica mensagem JSON persistente. Sem `exchange`, usa a default (routing_key = nome da fila)."""
    message = aio_pika.Message(
        body=json.dumps(payload, ensure_ascii=False).encode(),
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        headers=headers or {},
    )
    target = exchange or channel.default_exchange
    await target.publish(message, routing_key=routing_key)
