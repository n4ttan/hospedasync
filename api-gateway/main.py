from fastapi import FastAPI, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
import httpx
import os
import uuid
import time
import random
import asyncio
import json
import datetime
from typing import Optional
from pathlib import Path

from database import init_db, SessionLocal, JobHistory, Hotel
from hotels import seed_if_empty, refresh_from_xotelo
from distlock import acquire_lock, release_lock, is_locked, check_connection, list_locks

app = FastAPI(title="HospedaSync API Gateway", version="1.0.0")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

ACTIVE_WORKERS = {}

class RegisterWorkerRequest(BaseModel):
    worker_id: str
    endpoint: str

class TriggerJobRequest(BaseModel):
    hotel_key: Optional[str] = None
    hotel_name: Optional[str] = None
    checkin_date: str
    checkout_date: str
    currency: Optional[str] = "BRL"

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.on_event("startup")
def startup_event():
    # Inicializa as tabelas no PostgreSQL ao subir o Gateway
    try:
        init_db()
        db = SessionLocal()
        try:
            seed_if_empty(db)
        finally:
            db.close()
    except Exception as e:
        print(f"Aguardando conexão com o banco de dados... ({e})")

@app.get("/")
async def read_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "HospedaSync API Gateway online."}

@app.get("/health")
def health():
    redis_ok = check_connection()
    return {
        "status": "UP",
        "service": "api-gateway",
        "registered_workers": len(ACTIVE_WORKERS),
        "redis_lock_connection": "UP" if redis_ok else "DOWN"
    }

@app.get("/locks/{hotel_name}")
def get_lock_status(hotel_name: str):
    """Endpoint de diagnostico: informa se o hotel esta com lock ativo."""
    return {"hotel_name": hotel_name, "locked": is_locked(hotel_name)}

@app.get("/locks")
def get_active_locks():
    """Diagnostico: todos os locks de hotel atualmente ativos no Redis."""
    try:
        return {"locks": list_locks()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Redis indisponivel: {exc}")

@app.get("/coordination/status")
async def coordination_status():
    """
    Visao de coordenacao do cluster: pergunta a cada worker registrado quem ele
    considera o lider (Bully) e informa se ha consenso, alem dos locks ativos.
    """
    now = time.time()
    workers = {k: v for k, v in ACTIVE_WORKERS.items() if now - v["last_seen"] < 15}

    async def ask(client, worker_id, info):
        try:
            resp = await client.get(f"{info['endpoint']}/leader")
            return worker_id, resp.json().get("leader_id")
        except Exception:
            return worker_id, "unreachable"

    async with httpx.AsyncClient(timeout=1.5) as client:
        answers = await asyncio.gather(*(ask(client, k, v) for k, v in workers.items()))

    views = dict(answers)
    votes = {v for v in views.values() if v not in (None, "unreachable")}
    try:
        locks = list_locks()
    except Exception:
        locks = None
    return {
        "leader_id": next(iter(votes)) if len(votes) == 1 else None,
        "consensus": len(votes) == 1,
        "worker_views": views,
        "active_locks": locks,
    }

@app.post("/registry/register")
def register_worker(req: RegisterWorkerRequest):
    ACTIVE_WORKERS[req.worker_id] = {
        "endpoint": req.endpoint,
        "last_seen": time.time()
    }
    return {"status": "REGISTERED", "worker_id": req.worker_id}

@app.get("/registry/nodes")
def get_registered_nodes():
    """Retorna os nós ativos registrados no Service Registry em tempo real (para a apresentação do Rafael)"""
    now = time.time()
    valid_workers = {
        k: v for k, v in ACTIVE_WORKERS.items() 
        if now - v["last_seen"] < 15
    }
    return {
        "active_nodes": valid_workers,
        "count": len(valid_workers)
    }

def _loads_rates(raw):
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []

def _iso_utc(dt):
    return dt.replace(tzinfo=None).isoformat() + "Z" if dt else None

def _job_to_dict(r: JobHistory) -> dict:
    return {
        "job_id": r.job_id,
        "hotel_key": r.hotel_key,
        "hotel_name": r.hotel_name,
        "checkin_date": r.checkin_date,
        "checkout_date": r.checkout_date,
        "currency": r.currency,
        "rates": _loads_rates(r.rates),
        "min_rate": r.min_rate,
        "daily_rate": r.daily_rate,
        "source": r.source,
        "fetched_at": r.fetched_at,
        "processed_by": r.processed_by,
        "processing_time_ms": r.processing_time_ms,
        "created_at": _iso_utc(r.created_at),
    }

def _hotel_to_dict(h: Hotel) -> dict:
    return {c: getattr(h, c) for c in (
        "hotel_key", "name", "location_key", "rating", "review_count",
        "latitude", "longitude", "tripadvisor_url", "accommodation_type")}

@app.get("/hotels")
def list_hotels(q: str = "", limit: int = 50, db: Session = Depends(get_db)):
    """Busca hoteis do catalogo por nome, ordenados por numero de avaliacoes."""
    limit = max(1, min(limit, 500))
    query = db.query(Hotel)
    if q.strip():
        query = query.filter(Hotel.name.ilike(f"%{q.strip()}%"))
    rows = query.order_by(Hotel.review_count.desc().nullslast()).limit(limit).all()
    return [_hotel_to_dict(h) for h in rows]

@app.post("/hotels/refresh")
def refresh_hotels(db: Session = Depends(get_db)):
    """Atualiza o catalogo a partir da API Xotelo (mantem o existente em caso de falha)."""
    result = refresh_from_xotelo(db)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result)
    return result

@app.get("/history")
def get_history(db: Session = Depends(get_db)):
    """Retorna o histórico persistido no banco de dados"""
    records = db.query(JobHistory).order_by(JobHistory.created_at.desc()).limit(50).all()
    return [_job_to_dict(r) for r in records]

def _resolve_hotel(db, req: TriggerJobRequest):
    """Retorna (hotel_key, hotel_name). 404 se so o nome veio e nao ha no catalogo."""
    if req.hotel_key:
        name = req.hotel_name
        if not name:
            try:
                h = db.get(Hotel, req.hotel_key)
                name = h.name if h else req.hotel_key
            except Exception:
                name = req.hotel_key
        return req.hotel_key, name
    if not req.hotel_name or not req.hotel_name.strip():
        raise HTTPException(status_code=422, detail="Informe hotel_key ou hotel_name.")
    term = req.hotel_name.strip()
    try:
        exact = db.query(Hotel).filter(Hotel.name.ilike(term)).first()
        found = exact or (db.query(Hotel).filter(Hotel.name.ilike(f"%{term}%"))
                          .order_by(Hotel.review_count.desc().nullslast()).first())
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Catalogo de hoteis indisponivel: {exc}")
    if not found:
        raise HTTPException(status_code=404, detail=f"Hotel '{term}' nao encontrado no catalogo. Use GET /hotels?q= para buscar.")
    return found.hotel_key, found.name

def _cached_quote(db, hotel_key, checkin, checkout):
    """Ultima cotacao do hotel: mesmas datas se houver, senao a mais recente."""
    try:
        base = db.query(JobHistory).filter(JobHistory.hotel_key == hotel_key, JobHistory.rates.isnot(None))
        rec = (base.filter(JobHistory.checkin_date == checkin, JobHistory.checkout_date == checkout)
               .order_by(JobHistory.created_at.desc()).first()
               or base.order_by(JobHistory.created_at.desc()).first())
    except Exception as exc:
        print(f"Erro ao consultar cache: {exc}")
        return None
    if rec is None:
        return None
    d = _job_to_dict(rec)
    d.pop("created_at", None)
    d["source"] = "cache"
    d["stale"] = True
    d["fetched_at"] = rec.fetched_at or _iso_utc(rec.created_at)
    d["tripadvisor_url"] = None
    try:
        h = db.get(Hotel, hotel_key)
        d["tripadvisor_url"] = h.tripadvisor_url if h else None
    except Exception:
        pass
    return d

@app.post("/jobs/dispatch")
async def dispatch_job(req: TriggerJobRequest, db: Session = Depends(get_db)):
    hotel_key, hotel_name = _resolve_hotel(db, req)
    currency = req.currency or "BRL"

    now = time.time()
    valid_workers = {k: v for k, v in ACTIVE_WORKERS.items() if now - v["last_seen"] < 15}
    default_worker_url = os.getenv("WORKER_SERVICE_URL", "http://worker-node:8001")

    if valid_workers:
        selected_id, node_info = random.choice(list(valid_workers.items()))
        target_url = node_info["endpoint"]
    else:
        selected_id = "default-worker"
        target_url = default_worker_url

    job_id = f"job-{uuid.uuid4().hex[:8]}"
    payload = {
        "job_id": job_id,
        "hotel_key": hotel_key,
        "hotel_name": hotel_name,
        "checkin_date": req.checkin_date,
        "checkout_date": req.checkout_date,
        "currency": currency,
    }

    # Exclusao mutua por hotel_key; owner_id = job_id (so quem adquiriu libera).
    if not acquire_lock(hotel_key, owner_id=job_id, ttl=30):
        raise HTTPException(
            status_code=409,
            detail=f"O hotel '{hotel_name}' ja esta sendo processado por outra requisicao. Tente novamente em instantes."
        )

    try:
        worker_data = None
        failure = None  # (status, detail) quando o worker esta indisponivel
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(f"{target_url}/scrape", json=payload)
            if response.status_code == 200:
                worker_data = response.json()
            elif response.status_code == 404:
                try:
                    detail = response.json().get("detail", {"error": "no_rates"})
                except Exception:
                    detail = {"error": "no_rates"}
                raise HTTPException(status_code=404, detail=detail)
            elif response.status_code in (502, 503):
                failure = (503, f"Worker indisponivel ou fonte de precos fora do ar (HTTP {response.status_code}).")
            else:
                raise HTTPException(status_code=502, detail="Erro retornado pelo no worker.")
        except httpx.RequestError as exc:
            failure = (503, f"Falha ao conectar ao Worker ({target_url}): {str(exc)}")

        if worker_data is None:
            cached = _cached_quote(db, hotel_key, req.checkin_date, req.checkout_date)
            if cached is None:
                raise HTTPException(status_code=failure[0], detail=failure[1])
            return {
                "gateway_status": "DEGRADED",
                "dispatched_to": target_url,
                "selected_worker_id": selected_id,
                "persisted": False,
                "stale": True,
                "result": cached,
            }

        # Persistência no Banco
        persisted = True
        try:
            rates = worker_data.get("rates") or []
            min_rate = worker_data.get("min_rate", worker_data["daily_rate"])
            db.add(JobHistory(
                job_id=worker_data["job_id"],
                hotel_key=worker_data.get("hotel_key", hotel_key),
                hotel_name=worker_data["hotel_name"],
                currency=worker_data.get("currency", currency),
                rates=json.dumps(rates),
                min_rate=min_rate,
                daily_rate=worker_data["daily_rate"],
                source=worker_data.get("source"),
                fetched_at=worker_data.get("fetched_at"),
                processed_by=worker_data["processed_by"],
                processing_time_ms=worker_data["processing_time_ms"],
                checkin_date=worker_data["checkin_date"],
                checkout_date=worker_data["checkout_date"],
            ))
            db.commit()
        except Exception as e:
            persisted = False
            try:
                db.rollback()
            except Exception:
                pass
            print(f"Erro ao salvar no banco: {e}")

        return {
            "gateway_status": "SUCCESS" if persisted else "DEGRADED",
            "dispatched_to": target_url,
            "selected_worker_id": selected_id,
            "persisted": persisted,
            "result": worker_data,
        }
    finally:
        release_lock(hotel_key, owner_id=job_id)
