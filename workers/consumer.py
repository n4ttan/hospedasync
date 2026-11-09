"""Consumidor de jobs (RabbitMQ): lock distribuido por hotel, scrape e publicacao do resultado."""
import asyncio
import json
import time

import distlock
import election
import scraper
from mq import JOBS_QUEUE, RESULTS_QUEUE, connect, declare_topology, publish_json

LOCK_TTL = 60
LOCK_RETRY_DELAY = 1.0
MAX_LOCK_CONFLICTS = 30
CONFLICT_HEADER = "x-lock-conflicts"
STARTUP_RETRY_DELAY = 5.0


def _result_msg(job, status, result=None, error=None):
    return {
        "job_id": job.get("job_id"),
        "sweep_id": job.get("sweep_id"),
        "status": status,
        "processed_by": election.WORKER_ID,
        "lamport": election.CLOCK.tick(),
        "result": result,
        "error": error,
    }


def build_scrape_payload(job, data, started):
    return {
        "job_id": job["job_id"],
        "processed_by": election.WORKER_ID,
        "hotel_key": job["hotel_key"],
        "hotel_name": job.get("hotel_name", ""),
        "checkin_date": job["checkin_date"],
        "checkout_date": job["checkout_date"],
        "currency": job.get("currency", "BRL"),
        "rates": data["rates"],
        "min_rate": data["min_rate"],
        "daily_rate": data["min_rate"],
        "source": data["source"],
        "fetched_at": data["fetched_at"],
        "tripadvisor_url": data["tripadvisor_url"],
        "processing_time_ms": round((time.time() - started) * 1000, 2),
    }


async def handle_message(message, channel):
    """Processa uma mensagem de job. Ack so depois de publicar o resultado; erro inesperado -> nack sem requeue (DLQ)."""
    owner = None
    resource = None
    acquired = False
    try:
        job = json.loads(message.body)
        election.EVENTS.log("job_received", received=job.get("lamport", 0),
                            job_id=job.get("job_id"), hotel_key=job.get("hotel_key"))
        resource = job["hotel_key"]
        owner = f"{election.WORKER_ID}:{job['job_id']}"
        acquired = await asyncio.to_thread(distlock.acquire_lock, resource, owner, LOCK_TTL)

        if not acquired:
            conflicts = int((message.headers or {}).get(CONFLICT_HEADER, 0) or 0)
            if conflicts >= MAX_LOCK_CONFLICTS:
                election.EVENTS.log("job_lock_timeout", job_id=job["job_id"], hotel_key=resource)
                await publish_json(channel, RESULTS_QUEUE, _result_msg(job, "error", error="lock_timeout"))
            else:
                election.EVENTS.log("job_lock_conflict", job_id=job["job_id"], hotel_key=resource,
                                    conflicts=conflicts + 1)
                await asyncio.sleep(LOCK_RETRY_DELAY)
                await publish_json(channel, JOBS_QUEUE, job, headers={CONFLICT_HEADER: conflicts + 1})
            await message.ack()
            return

        started = time.time()
        try:
            data = await scraper.fetch_rates(
                job["hotel_key"], job["checkin_date"], job["checkout_date"], job.get("currency", "BRL")
            )
            out = _result_msg(job, "ok", result=build_scrape_payload(job, data, started))
        except scraper.NoRates:
            out = _result_msg(job, "no_rates", error="no_rates")
        except scraper.UpstreamError as exc:
            out = _result_msg(job, "error", error=str(exc))
        election.EVENTS.log("job_done", job_id=job["job_id"], status=out["status"])
        await publish_json(channel, RESULTS_QUEUE, out)
        await message.ack()
    except Exception as exc:
        print(f"[{election.WORKER_ID}] erro inesperado no job: {type(exc).__name__}: {exc}", flush=True)
        try:
            await message.nack(requeue=False)
        except Exception as nack_exc:
            print(f"[{election.WORKER_ID}] falha no nack: {nack_exc}", flush=True)
    finally:
        if acquired:
            try:
                await asyncio.to_thread(distlock.release_lock, resource, owner)
            except Exception as exc:
                print(f"[{election.WORKER_ID}] falha ao liberar lock: {exc}", flush=True)


async def run_consumer():
    """Loop de vida longa: conecta, consome e, se algo falhar na subida, tenta de novo apos 5 s."""
    while True:
        try:
            connection = await connect()
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=1)
            topology = await declare_topology(channel)

            async def on_message(message):
                await handle_message(message, channel)

            await topology["jobs"].consume(on_message)
            print(f"[{election.WORKER_ID}] consumidor de {JOBS_QUEUE} ativo", flush=True)
            await asyncio.Event().wait()  # aio-pika robust reconecta e restaura o consumo
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[{election.WORKER_ID}] consumidor falhou ({exc}); nova tentativa em {STARTUP_RETRY_DELAY}s", flush=True)
            await asyncio.sleep(STARTUP_RETRY_DELAY)
