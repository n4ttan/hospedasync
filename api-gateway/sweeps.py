"""
Varreduras em lote: o Gateway publica um JOB por hotel x data no RabbitMQ e consome os RESULTs.

  POST /sweeps          enfileira os jobs, cria os contadores em Redis (hash sweep:<id>, TTL 24 h)
  GET  /sweeps/{id}     progresso (total/done/errors/no_rates/pending/finished)
  run_results_consumer  consome hospeda.scrape.results e grava em job_history (idempotente por job_id)

Entrega at-least-once: ack so apos o commit; redelivery nao duplica linha nem recontagem.
"""

import asyncio
import datetime
import json
import os
import uuid
from typing import List, Optional

import aio_pika
import redis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import database
from database import JobHistory, Hotel, get_db
from lamport import EventLog
import mq

router = APIRouter()

EVENTS = EventLog("api-gateway")  # relogio de Lamport unico do gateway
SWEEP_TTL = 24 * 3600
MAX_JOBS = 60
MAX_TOP_N = 20
REQUEUE_DELAY = 2.0

_consumer_up = False
_redis = None


def consumer_connected() -> bool:
    return _consumer_up


def get_redis():
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis-lock:6379/0"), decode_responses=True)
    return _redis


class SweepRequest(BaseModel):
    hotel_keys: Optional[List[str]] = None
    top_n: int = Field(5, ge=1, le=MAX_TOP_N)
    date_offsets: List[int] = Field(default_factory=lambda: [7, 14], min_length=1)
    nights: int = Field(2, ge=1, le=30)
    currency: str = "BRL"


async def _open_connection():
    """Conexao simples (nao-robust): connect_robust engole o cancelamento durante a conexao inicial e
    trava o shutdown; aqui o proprio loop do consumidor cuida de reconectar."""
    return await aio_pika.connect(mq.RABBITMQ_URL, timeout=5)


async def publish_jobs(jobs: list):
    """Publica os jobs no broker (conexao curta). Levanta excecao se o broker estiver fora."""
    conn = await asyncio.wait_for(_open_connection(), timeout=8)
    try:
        channel = await conn.channel()
        await mq.declare_topology(channel)
        for job in jobs:
            await mq.publish_json(channel, mq.JOBS_QUEUE, job)
    finally:
        await conn.close()


@router.post("/sweeps", status_code=202)
async def create_sweep(req: SweepRequest, db: Session = Depends(get_db)):
    if any(o < 0 or o > 365 for o in req.date_offsets):
        raise HTTPException(status_code=422, detail="date_offsets deve estar entre 0 e 365.")
    offsets = list(dict.fromkeys(req.date_offsets))

    try:
        if req.hotel_keys:
            keys = list(dict.fromkeys(req.hotel_keys))
            hotels = []
            for k in keys:
                h = db.get(Hotel, k)
                hotels.append((k, h.name if h else k))
        else:
            rows = (db.query(Hotel).order_by(Hotel.review_count.desc().nullslast())
                    .limit(req.top_n).all())
            hotels = [(h.hotel_key, h.name) for h in rows]
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Catalogo de hoteis indisponivel: {exc}")
    if not hotels:
        raise HTTPException(status_code=422, detail="Nenhum hotel no catalogo para varrer.")

    total = len(hotels) * len(offsets)
    if total > MAX_JOBS:
        raise HTTPException(status_code=422, detail=f"Varredura geraria {total} jobs; o maximo e {MAX_JOBS}.")

    sweep_id = f"sweep-{uuid.uuid4().hex[:8]}"
    today = datetime.date.today()
    now_iso = datetime.datetime.utcnow().isoformat() + "Z"
    jobs = []
    for key, name in hotels:
        for off in offsets:
            checkin = today + datetime.timedelta(days=off)
            jobs.append({
                "job_id": f"job-{uuid.uuid4().hex[:8]}",
                "sweep_id": sweep_id,
                "hotel_key": key,
                "hotel_name": name,
                "checkin_date": checkin.isoformat(),
                "checkout_date": (checkin + datetime.timedelta(days=req.nights)).isoformat(),
                "currency": req.currency,
                "lamport": EVENTS.clock.tick(),
                "enqueued_by": "api-gateway",
                "enqueued_at": now_iso,
            })

    try:
        r = get_redis()
        r.hset(f"sweep:{sweep_id}", mapping={"total": total, "done": 0, "errors": 0,
                                             "no_rates": 0, "created_at": now_iso})
        r.expire(f"sweep:{sweep_id}", SWEEP_TTL)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Redis indisponivel: {exc}")

    try:
        await publish_jobs(jobs)
    except Exception as exc:
        try:
            get_redis().delete(f"sweep:{sweep_id}")
        except Exception:
            pass
        raise HTTPException(status_code=503, detail=f"RabbitMQ indisponivel, varredura nao enfileirada: {exc}")

    EVENTS.log("sweep_enqueued", sweep_id=sweep_id, jobs=total)
    return {"sweep_id": sweep_id, "jobs_enqueued": total}


@router.get("/sweeps/{sweep_id}")
def get_sweep(sweep_id: str):
    try:
        h = get_redis().hgetall(f"sweep:{sweep_id}")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Redis indisponivel: {exc}")
    if not h:
        raise HTTPException(status_code=404, detail="Varredura nao encontrada (ou expirada).")
    total, done, errors, no_rates = (int(h.get(k, 0)) for k in ("total", "done", "errors", "no_rates"))
    pending = max(0, total - done - errors - no_rates)
    return {"sweep_id": sweep_id, "total": total, "done": done, "errors": errors,
            "no_rates": no_rates, "pending": pending, "finished": pending == 0}


# ---------------------------------------------------------------- consumidor

class MalformedResult(ValueError):
    pass


_COUNTER = {"ok": "done", "error": "errors", "no_rates": "no_rates"}


def _insert_ok(db, payload, res, job_id, sweep_id) -> None:
    if db.get(JobHistory, job_id) is not None:
        return
    try:
        daily = res.get("daily_rate", res.get("min_rate"))
        db.add(JobHistory(
            job_id=job_id,
            sweep_id=sweep_id,
            hotel_key=res.get("hotel_key"),
            hotel_name=res["hotel_name"],
            currency=res.get("currency"),
            rates=json.dumps(res.get("rates") or []),
            min_rate=res.get("min_rate", daily),
            daily_rate=float(daily),
            source=res.get("source"),
            fetched_at=res.get("fetched_at"),
            processed_by=payload.get("processed_by") or res.get("processed_by") or "unknown",
            processing_time_ms=float(res.get("processing_time_ms") or 0),
            checkin_date=res["checkin_date"],
            checkout_date=res["checkout_date"],
        ))
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedResult(f"result invalido: {exc}")
    db.commit()


async def handle_result(payload, session_factory, redis_client) -> str:
    """
    Persiste um RESULT. Idempotente por job_id. Devolve "counted" ou "duplicate".
    Levanta MalformedResult (nack sem requeue) ou qualquer outra excecao (nack com requeue).
    """
    if not isinstance(payload, dict) or not payload.get("job_id") or payload.get("status") not in _COUNTER:
        raise MalformedResult("job_id/status ausente ou invalido")
    job_id, status = payload["job_id"], payload["status"]
    sweep_id = payload.get("sweep_id")
    res = payload.get("result")

    if status == "ok":
        if not isinstance(res, dict):
            raise MalformedResult("status ok sem result")
        db = session_factory()
        try:
            try:
                await asyncio.to_thread(_insert_ok, db, payload, res, job_id, sweep_id)
            except Exception:
                db.rollback()
                raise
        finally:
            db.close()

    EVENTS.log("result_received", received=payload.get("lamport"),
               job_id=job_id, sweep_id=sweep_id, status=status, processed_by=payload.get("processed_by"))

    if not sweep_id:
        return "counted"
    seen_key = f"sweep:{sweep_id}:seen"
    if not redis_client.sadd(seen_key, job_id):
        return "duplicate"
    try:
        redis_client.hincrby(f"sweep:{sweep_id}", _COUNTER[status], 1)
        redis_client.expire(seen_key, SWEEP_TTL)
        redis_client.expire(f"sweep:{sweep_id}", SWEEP_TTL)
    except Exception:
        try:
            redis_client.srem(seen_key, job_id)
        except Exception:
            pass
        raise
    return "counted"


async def _consume_once():
    global _consumer_up
    conn = await _open_connection()
    try:
        channel = await conn.channel()
        await channel.set_qos(prefetch_count=10)
        queues = await mq.declare_topology(channel)
        _consumer_up = True
        async with queues["results"].iterator() as it:
            async for message in it:
                try:
                    payload = json.loads(message.body)
                    await handle_result(payload, database.SessionLocal, get_redis())
                except (ValueError, MalformedResult) as exc:  # JSON invalido ou contrato violado
                    print(f"[api-gateway] resultado malformado descartado: {exc}", flush=True)
                    await message.nack(requeue=False)
                except Exception as exc:  # banco/Redis fora: tenta de novo
                    print(f"[api-gateway] falha ao gravar resultado, requeue: {exc}", flush=True)
                    await asyncio.sleep(REQUEUE_DELAY)
                    await message.nack(requeue=True)
                else:
                    await message.ack()
    finally:
        _consumer_up = False
        try:
            await conn.close()
        except Exception:
            pass


async def run_results_consumer():
    """Loop de background com backoff; nunca propaga excecao (exceto cancelamento)."""
    delay = 2.0
    while True:
        try:
            await _consume_once()
            delay = 2.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[api-gateway] consumidor de resultados fora ({exc}); nova tentativa em {delay:.0f}s", flush=True)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)
