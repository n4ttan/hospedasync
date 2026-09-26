from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx
import os
import uuid
import time
import random
from pathlib import Path

app = FastAPI(title="HospedaSync API Gateway", version="1.0.0")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Service Registry (Esquema de Nomeação Dinâmico em Memória)
ACTIVE_WORKERS = {}

class RegisterWorkerRequest(BaseModel):
    worker_id: str
    endpoint: str

class TriggerJobRequest(BaseModel):
    hotel_name: str
    checkin_date: str
    checkout_date: str

@app.get("/")
async def read_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "HospedaSync API Gateway online."}

@app.get("/health")
def health():
    return {"status": "UP", "service": "api-gateway", "registered_workers": len(ACTIVE_WORKERS)}

# --- ESQUEMA DE NOMEAÇÃO: Service Registry ---
@app.post("/registry/register")
def register_worker(req: RegisterWorkerRequest):
    """Registra ou atualiza a presença do Worker (Service Discovery)"""
    ACTIVE_WORKERS[req.worker_id] = {
        "endpoint": req.endpoint,
        "last_seen": time.time()
    }
    return {"status": "REGISTERED", "worker_id": req.worker_id}

@app.get("/registry/nodes")
def list_active_nodes():
    """Lista todos os nós nomeados e ativos"""
    now = time.time()
    # Expira nós que não mandam heartbeat há mais de 15s
    active = {k: v for k, v in ACTIVE_WORKERS.items() if now - v["last_seen"] < 15}
    return {"active_nodes": active, "count": len(active)}

@app.post("/jobs/dispatch")
async def dispatch_job(req: TriggerJobRequest):
    # Remove nós inativos (timeout de 15 segundos)
    now = time.time()
    valid_workers = {k: v for k, v in ACTIVE_WORKERS.items() if now - v["last_seen"] < 15}

    # Fallback se nenhum worker tiver mandado heartbeat ainda (ex: Docker local)
    default_worker_url = os.getenv("WORKER_SERVICE_URL", "http://worker-node:8001")
    
    if valid_workers:
        # Escolhe um Worker ativo usando o registro dinâmico (Esquema de Nomeação)
        selected_id, node_info = random.choice(list(valid_workers.items()))
        target_url = node_info["endpoint"]
    else:
        selected_id = "default-worker"
        target_url = default_worker_url

    job_id = f"job-{uuid.uuid4().hex[:8]}"
    payload = {
        "job_id": job_id,
        "hotel_name": req.hotel_name,
        "checkin_date": req.checkin_date,
        "checkout_date": req.checkout_date
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(f"{target_url}/scrape", json=payload)
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail="Erro retornado pelo no worker.")
            worker_data = response.json()
            
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503, 
            detail=f"Falha ao conectar ao Worker ({target_url}): {str(exc)}"
        )
        
    return {
        "gateway_status": "SUCCESS",
        "dispatched_to": target_url,
        "selected_worker_id": selected_id,
        "result": worker_data
    }