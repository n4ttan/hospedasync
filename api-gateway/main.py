from fastapi import FastAPI, HTTPException, Depends, Response, Query, Request
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

from database import (init_db, SessionLocal, JobHistory, Hotel, get_db, get_read_db,
                      replica_lag_ms, consistency_status)
from lamport import merge_events
from resilience import CircuitBreaker
import sweeps
import servicetoken
import auth
from auth import router as auth_router, require_role, check_service_headers
from sweeps import EVENTS, router as sweeps_router
from hotels import seed_if_empty, refresh_from_xotelo
from distlock import acquire_lock, release_lock, is_locked, check_connection, list_locks

app = FastAPI(title="HospedaSync API Gateway", version="1.0.0")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

ANALYST = [Depends(require_role("analyst"))]
ADMIN = [Depends(require_role("admin"))]

app.include_router(auth_router)
app.include_router(sweeps_router, dependencies=ANALYST)

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

_consumer_task = None

@app.on_event("startup")
async def startup_event():
    global _consumer_task
    # Inicializa as tabelas no PostgreSQL ao subir o Gateway
    try:
        init_db()
        db = SessionLocal()
        try:
            auth.seed_admin(db)
            seed_if_empty(db)
        finally:
            db.close()
    except Exception as e:
        print(f"Aguardando conexão com o banco de dados... ({e})")
    # Consumidor de resultados em background: falha de broker nunca derruba o gateway.
    _consumer_task = asyncio.create_task(sweeps.run_results_consumer())

@app.on_event("shutdown")
async def shutdown_event():
    if _consumer_task is not None:
        _consumer_task.cancel()
        try:
            await _consumer_task
        except BaseException:
            pass

MAX_DISPATCH_ATTEMPTS = int(os.getenv("MAX_DISPATCH_ATTEMPTS", "3"))
_BREAKERS = {}
METRICS = {"dispatch_total": 0, "dispatch_ok_total": 0, "dispatch_degraded_total": 0,
           "dispatch_conflict_total": 0, "dispatch_failover_total": 0}


def _peer_headers(method: str, path: str, extra=None) -> dict:
    """Assina chamada interna gateway->worker (id de servico 'api-gateway')."""
    secret = os.getenv("SERVICE_SECRET", "")
    # sem secret nao assina: o worker rejeita com 401 (falha fechada)
    h = servicetoken.sign_request(secret, "api-gateway", method, path) if secret else {}
    if extra:
        h.update(extra)
    return h


def _breaker(worker_id: str) -> CircuitBreaker:
    b = _BREAKERS.get(worker_id)
    if b is None:
        b = _BREAKERS[worker_id] = CircuitBreaker(f"worker:{worker_id}", failure_threshold=3, recovery_timeout=20)
    return b


@app.get("/metrics", dependencies=ADMIN)
def metrics():
    """Metricas no formato texto do Prometheus (admin). Contadores do dispatch, workers ativos e circuitos."""
    now = time.time()
    active = sum(1 for v in ACTIVE_WORKERS.values() if now - v["last_seen"] < 15)
    lines = [f"hospeda_{k} {v}" for k, v in METRICS.items()]
    lines.append(f"hospeda_workers_active {active}")
    for wid, b in _BREAKERS.items():
        state = {"closed": 0, "half_open": 1, "open": 2}.get(b.state, -1)
        lines.append(f'hospeda_worker_breaker_state{{worker="{wid}"}} {state}')
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


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
        "redis_lock_connection": "UP" if redis_ok else "DOWN",
        "rabbitmq": "UP" if sweeps.consumer_connected() else "DOWN",
    }

@app.get("/locks/{hotel_name}", dependencies=ADMIN)
def get_lock_status(hotel_name: str):
    """Endpoint de diagnostico: informa se o hotel esta com lock ativo."""
    return {"hotel_name": hotel_name, "locked": is_locked(hotel_name)}

@app.get("/locks", dependencies=ADMIN)
def get_active_locks():
    """Diagnostico: todos os locks de hotel atualmente ativos no Redis."""
    try:
        return {"locks": list_locks()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Redis indisponivel: {exc}")

@app.get("/coordination/status", dependencies=ADMIN)
async def coordination_status():
    """
    Visao de coordenacao do cluster: pergunta a cada worker registrado quem ele
    considera o lider (Bully) e informa se ha consenso, alem dos locks ativos.
    """
    now = time.time()
    workers = {k: v for k, v in ACTIVE_WORKERS.items() if now - v["last_seen"] < 15}

    async def ask(client, worker_id, info):
        try:
            resp = await client.get(f"{info['endpoint']}/leader", headers=_peer_headers("GET", "/leader"))
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

@app.get("/coordination/events", dependencies=ADMIN)
async def coordination_events(limit: int = 100):
    """Eventos de todos os nos (workers ativos + gateway) fundidos em ordem de Lamport."""
    limit = max(1, min(limit, 1000))
    now = time.time()
    workers = {k: v for k, v in ACTIVE_WORKERS.items() if now - v["last_seen"] < 15}

    async def fetch(client, worker_id, info):
        try:
            resp = await client.get(f"{info['endpoint']}/events", headers=_peer_headers("GET", "/events"))
            resp.raise_for_status()
            data = resp.json()
            events = data.get("events", []) if isinstance(data, dict) else data
            return worker_id, [e for e in events if isinstance(e, dict) and "lamport" in e and "node" in e]
        except Exception:
            return worker_id, None

    async with httpx.AsyncClient(timeout=1.5) as client:
        answers = await asyncio.gather(*(fetch(client, k, v) for k, v in workers.items()))

    reached = [w for w, ev in answers if ev is not None]
    failed = [w for w, ev in answers if ev is None]
    merged = merge_events(EVENTS.snapshot(), *[ev for _, ev in answers if ev])
    return {"events": merged[-limit:], "nodes_reached": reached, "nodes_failed": failed}

@app.post("/registry/register")
def register_worker(req: RegisterWorkerRequest, request: Request):
    # Somente nos com assinatura HMAC valida entram no registry (impede no falso).
    check_service_headers(request.headers, req.worker_id, req.endpoint)
    ACTIVE_WORKERS[req.worker_id] = {
        "endpoint": req.endpoint,
        "last_seen": time.time()
    }
    return {"status": "REGISTERED", "worker_id": req.worker_id}

@app.get("/registry/nodes", dependencies=ANALYST)
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
        "sweep_id": getattr(r, "sweep_id", None),
        "created_at": _iso_utc(r.created_at),
    }

def _hotel_to_dict(h: Hotel) -> dict:
    return {c: getattr(h, c) for c in (
        "hotel_key", "name", "location_key", "rating", "review_count",
        "latitude", "longitude", "tripadvisor_url", "accommodation_type")}

@app.get("/hotels", dependencies=ANALYST)
def list_hotels(q: str = "", limit: int = 50, db: Session = Depends(get_db)):
    """Busca hoteis do catalogo por nome, ordenados por numero de avaliacoes."""
    limit = max(1, min(limit, 500))
    query = db.query(Hotel)
    if q.strip():
        query = query.filter(Hotel.name.ilike(f"%{q.strip()}%"))
    rows = query.order_by(Hotel.review_count.desc().nullslast()).limit(limit).all()
    return [_hotel_to_dict(h) for h in rows]

@app.get("/internal/hotels")
def internal_hotels(request: Request, limit: int = 5, db: Session = Depends(get_db)):
    """Catalogo para os workers (agendador de varreduras). Autenticado por assinatura HMAC de servico, nao por JWT."""
    check_service_headers(request.headers, request.headers.get("X-Service-Id", ""), "internal:hotels")
    rows = db.query(Hotel).order_by(Hotel.review_count.desc().nullslast()).limit(max(1, min(limit, 50))).all()
    return [_hotel_to_dict(h) for h in rows]

@app.post("/hotels/refresh", dependencies=ADMIN)
def refresh_hotels(db: Session = Depends(get_db)):
    """Atualiza o catalogo a partir da API Xotelo (mantem o existente em caso de falha)."""
    result = refresh_from_xotelo(db)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result)
    return result

@app.get("/history", dependencies=ANALYST)
def get_history(response: Response, consistency: str = Query("strong", pattern="^(strong|eventual)$"),
                pdb: Session = Depends(get_db)):
    """Historico persistido. strong (padrao) le do primario; eventual le da replica
    (pode estar defasada) e cai para o primario se ela estiver fora. Modo e origem vao em headers."""
    db, served_by, fallback = get_read_db(consistency, pdb)
    try:
        try:
            records = db.query(JobHistory).order_by(JobHistory.created_at.desc()).limit(50).all()
        except Exception:
            if served_by != "replica":
                raise
            db.close()
            db, served_by, fallback = pdb, "primary", True
            records = db.query(JobHistory).order_by(JobHistory.created_at.desc()).limit(50).all()
        response.headers["X-Consistency"] = consistency
        response.headers["X-Served-By"] = served_by
        if fallback:
            response.headers["X-Replica-Fallback"] = "true"
        if served_by == "replica":
            try:
                lag = replica_lag_ms(db)
                if lag is not None:
                    response.headers["X-Replication-Lag-Ms"] = str(lag)
            except Exception:
                pass
        return [_job_to_dict(r) for r in records]
    finally:
        if db is not pdb:
            db.close()


@app.get("/consistency/status", dependencies=ANALYST)
def get_consistency_status():
    """Estado da replicacao: lag (ms e bytes), modo sincrono e alcancabilidade da replica."""
    return consistency_status()

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

@app.post("/jobs/dispatch", dependencies=ANALYST)
async def dispatch_job(req: TriggerJobRequest, db: Session = Depends(get_db)):
    hotel_key, hotel_name = _resolve_hotel(db, req)
    currency = req.currency or "BRL"
    METRICS["dispatch_total"] += 1

    now = time.time()
    valid_workers = {k: v for k, v in ACTIVE_WORKERS.items() if now - v["last_seen"] < 15}
    default_worker_url = os.getenv("WORKER_SERVICE_URL", "http://worker-node:8001")

    if valid_workers:
        candidates = [(k, v["endpoint"]) for k, v in valid_workers.items()]
        random.shuffle(candidates)
    else:
        candidates = [("default-worker", default_worker_url)]
    selected_id, target_url = candidates[0]

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
        EVENTS.log("lock_conflict", job_id=job_id, hotel_key=hotel_key)
        METRICS["dispatch_conflict_total"] += 1
        raise HTTPException(
            status_code=409,
            detail=f"O hotel '{hotel_name}' ja esta sendo processado por outra requisicao. Tente novamente em instantes."
        )

    try:
        worker_data = None
        failure = None  # (status, detail) quando nenhum worker respondeu
        # Failover: tenta ate MAX_DISPATCH_ATTEMPTS workers; cada um com seu circuit breaker.
        for selected_id, target_url in candidates[:MAX_DISPATCH_ATTEMPTS]:
            breaker = _breaker(selected_id)
            if not breaker.allow():
                failure = (503, f"Worker {selected_id} com circuito aberto.")
                EVENTS.log("dispatch_skipped_breaker_open", job_id=job_id, worker=selected_id)
                continue
            try:
                sent = EVENTS.log("dispatch_sent", job_id=job_id, hotel_key=hotel_key, worker=selected_id)
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.post(f"{target_url}/scrape", json=payload,
                                                 headers=_peer_headers("POST", "/scrape", {"X-Lamport": str(sent["lamport"])}))
            except httpx.RequestError as exc:
                breaker.record_failure()
                failure = (503, f"Falha ao conectar ao Worker ({target_url}): {str(exc)}")
                EVENTS.log("dispatch_failover", job_id=job_id, worker=selected_id, reason="connect_error")
                METRICS["dispatch_failover_total"] += 1
                continue
            EVENTS.log("dispatch_done", received=response.headers.get("X-Lamport"),
                       job_id=job_id, worker=selected_id, http_status=response.status_code)
            if response.status_code in (502, 503):
                # 502/503 do worker = fonte de precos fora; tentar outro worker nao ajuda, mas conta para o breaker
                breaker.record_failure()
                failure = (503, f"Worker indisponivel ou fonte de precos fora do ar (HTTP {response.status_code}).")
                break
            breaker.record_success()
            if response.status_code == 200:
                worker_data = response.json()
            elif response.status_code == 404:
                try:
                    detail = response.json().get("detail", {"error": "no_rates"})
                except Exception:
                    detail = {"error": "no_rates"}
                raise HTTPException(status_code=404, detail=detail)
            else:
                raise HTTPException(status_code=502, detail="Erro retornado pelo no worker.")
            break
        else:
            if failure is None:
                failure = (503, "Nenhum worker disponivel.")

        if worker_data is None:
            cached = _cached_quote(db, hotel_key, req.checkin_date, req.checkout_date)
            if cached is None:
                raise HTTPException(status_code=failure[0], detail=failure[1])
            METRICS["dispatch_degraded_total"] += 1
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

        METRICS["dispatch_ok_total"] += 1
        return {
            "gateway_status": "SUCCESS" if persisted else "DEGRADED",
            "dispatched_to": target_url,
            "selected_worker_id": selected_id,
            "persisted": persisted,
            "result": worker_data,
        }
    finally:
        release_lock(hotel_key, owner_id=job_id)
