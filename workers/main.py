import os
import time
import uuid
import asyncio
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx

import election
import scraper
import consumer
import scheduler

app = FastAPI(title="HospedaSync Worker Node", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(election.router)

# Identidade única do Worker
WORKER_ID = election.WORKER_ID
WORKER_PORT = os.getenv("PORT", "8001")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000")
SELF_URL = os.getenv("SELF_URL", f"http://127.0.0.1:{WORKER_PORT}")

class ScrapeRequest(BaseModel):
    job_id: str
    hotel_key: str = Field(min_length=1)
    hotel_name: str = ""
    checkin_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    checkout_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    currency: str = "BRL"

@app.on_event("startup")
async def register_with_gateway():
    """Envia heartbeat/registro ao Gateway ao iniciar e participa da eleição de líder"""
    asyncio.create_task(send_periodic_heartbeat())
    asyncio.create_task(election.initial_election_delay())
    asyncio.create_task(election.monitor_leader())
    for factory in (consumer.run_consumer, scheduler.run_scheduler, scheduler.run_event_listener):
        asyncio.create_task(_guarded(factory))


async def _guarded(factory):
    """Mantém a task viva: qualquer exceção vira log e a corrotina é reiniciada após 5 s."""
    while True:
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[{WORKER_ID}] {factory.__name__} caiu ({type(exc).__name__}: {exc}); reiniciando em 5s", flush=True)
            await asyncio.sleep(5)


async def send_periodic_heartbeat():
    while True:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(f"{GATEWAY_URL}/registry/register", json={
                    "worker_id": WORKER_ID,
                    "endpoint": SELF_URL
                })
        except Exception as e:
            print(f"[{WORKER_ID}] Tentando registrar no Gateway... ({e})")
        await asyncio.sleep(5)  # Heartbeat a cada 5 segundos

@app.get("/health")
def health():
    return {"status": "UP", "worker_id": WORKER_ID, "leader_id": election.CURRENT_LEADER_ID,
            "lamport": election.CLOCK.time}

@app.post("/scrape")
async def execute_scrape(req: ScrapeRequest, request: Request, response: Response):
    start_time = time.time()
    received = request.headers.get("x-lamport")
    try:
        received = int(received) if received is not None else None
    except ValueError:
        received = None
    election.EVENTS.log("scrape_http", received=received, job_id=req.job_id, hotel_key=req.hotel_key)
    lamport_hdr = {"X-Lamport": str(election.CLOCK.tick())}
    response.headers.update(lamport_hdr)
    try:
        data = await scraper.fetch_rates(req.hotel_key, req.checkin_date, req.checkout_date, req.currency)
    except scraper.NoRates:
        raise HTTPException(status_code=404, detail={"error": "no_rates"}, headers=lamport_hdr)
    except scraper.UpstreamError as exc:
        raise HTTPException(status_code=502, detail={"error": "upstream_unavailable", "message": str(exc)}, headers=lamport_hdr)

    return {
        "job_id": req.job_id,
        "processed_by": WORKER_ID,
        "hotel_key": req.hotel_key,
        "hotel_name": req.hotel_name,
        "checkin_date": req.checkin_date,
        "checkout_date": req.checkout_date,
        "currency": req.currency,
        "rates": data["rates"],
        "min_rate": data["min_rate"],
        "daily_rate": data["min_rate"],
        "source": data["source"],
        "fetched_at": data["fetched_at"],
        "tripadvisor_url": data["tripadvisor_url"],
        "processing_time_ms": round((time.time() - start_time) * 1000, 2),
    }
