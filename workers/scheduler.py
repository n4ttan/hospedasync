"""Varredura periodica (so o lider) e listener de eventos do cluster (broadcast via fanout)."""
import asyncio
import os
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx

import election
from mq import EVENTS_EXCHANGE, JOBS_QUEUE, connect, declare_topology, publish_json

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000")
SWEEP_INTERVAL_SECONDS = float(os.getenv("SWEEP_INTERVAL_SECONDS", "120"))
SWEEP_HOTELS = int(os.getenv("SWEEP_HOTELS", "5"))
SWEEP_DATE_OFFSETS = [int(x) for x in os.getenv("SWEEP_DATE_OFFSETS", "7,14").split(",") if x.strip()]
SWEEP_NIGHTS = int(os.getenv("SWEEP_NIGHTS", "2"))

_channel = None
_topology = None


async def _ensure_channel():
    """Canal/topologia compartilhados do scheduler (reabre se ainda nao existir)."""
    global _channel, _topology
    if _channel is None or getattr(_channel, "is_closed", False):
        connection = await connect()
        _channel = await connection.channel()
        _topology = await declare_topology(_channel)
    return _channel, _topology


async def publish_cluster_event(event_type: str, **detail):
    channel, topology = await _ensure_channel()
    payload = {"type": event_type, "node": election.WORKER_ID, "lamport": election.CLOCK.tick(), "detail": detail}
    await publish_json(channel, "", payload, exchange=topology["events"])


def build_jobs(hotels, sweep_id, today=None):
    today = today or date.today()
    jobs = []
    for hotel in hotels:
        for offset in SWEEP_DATE_OFFSETS:
            checkin = today + timedelta(days=offset)
            jobs.append({
                "job_id": f"job-{uuid.uuid4().hex[:12]}",
                "sweep_id": sweep_id,
                "hotel_key": hotel["hotel_key"],
                "hotel_name": hotel.get("name") or hotel.get("hotel_name") or "",
                "checkin_date": checkin.isoformat(),
                "checkout_date": (checkin + timedelta(days=SWEEP_NIGHTS)).isoformat(),
                "currency": "BRL",
                "enqueued_by": election.WORKER_ID,
            })
    return jobs


async def run_sweep():
    """Uma varredura: busca hoteis no Gateway, publica jobs e anuncia no fanout. Devolve o sweep_id ou None."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{GATEWAY_URL}/hotels", params={"limit": SWEEP_HOTELS})
        resp.raise_for_status()
        hotels = resp.json()
    if not hotels:
        return None
    sweep_id = f"sweep-{uuid.uuid4().hex[:8]}"
    channel, _ = await _ensure_channel()
    jobs = build_jobs(hotels, sweep_id)
    for job in jobs:
        job["lamport"] = election.CLOCK.tick()
        job["enqueued_at"] = datetime.now(timezone.utc).isoformat()
        await publish_json(channel, JOBS_QUEUE, job)
    election.EVENTS.log("sweep_scheduled", sweep_id=sweep_id, jobs=len(jobs), leader=election.MY_ID)
    await publish_cluster_event("sweep_scheduled", sweep_id=sweep_id, jobs=len(jobs), leader=election.MY_ID)
    return sweep_id


async def announce_leader():
    await publish_cluster_event("leader_elected", leader=election.MY_ID, worker_id=election.WORKER_ID)


async def _safe_announce_leader():
    try:
        await announce_leader()
    except Exception as exc:
        print(f"[{election.WORKER_ID}] falha ao anunciar lideranca no fanout: {exc}", flush=True)


async def run_scheduler():
    if _safe_announce_leader not in election.on_became_leader:
        election.on_became_leader.append(_safe_announce_leader)
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        if election.CURRENT_LEADER_ID != election.MY_ID:
            continue
        try:
            await run_sweep()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[{election.WORKER_ID}] varredura falhou ({type(exc).__name__}: {exc}); tenta no proximo ciclo", flush=True)


async def handle_cluster_event(message):
    import json
    async with message.process():
        try:
            event = json.loads(message.body)
        except ValueError:
            return
        election.EVENTS.log("cluster_event", received=event.get("lamport", 0),
                            kind=event.get("type"), origin=event.get("node"), detail=event.get("detail"))


async def run_event_listener():
    """Cada worker liga uma fila exclusiva/anonima ao fanout: todos recebem todos os eventos (broadcast)."""
    while True:
        try:
            connection = await connect()
            channel = await connection.channel()
            topology = await declare_topology(channel)
            queue = await channel.declare_queue("", exclusive=True, auto_delete=True)
            await queue.bind(topology["events"])
            await queue.consume(handle_cluster_event)
            print(f"[{election.WORKER_ID}] ouvindo {EVENTS_EXCHANGE}", flush=True)
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[{election.WORKER_ID}] listener de eventos falhou ({exc}); nova tentativa em 5s", flush=True)
            await asyncio.sleep(5)
