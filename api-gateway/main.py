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
from pathlib import Path

from database import init_db, SessionLocal, JobHistory

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
    hotel_name: str
    checkin_date: str
    checkout_date: str

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
    return {"status": "UP", "service": "api-gateway", "registered_workers": len(ACTIVE_WORKERS)}

@app.post("/registry/register")
def register_worker(req: RegisterWorkerRequest):
    ACTIVE_WORKERS[req.worker_id] = {
        "endpoint": req.endpoint,
        "last_seen": time.time()
    }
    return {"status": "REGISTERED", "worker_id": req.worker_id}

@app.get("/history")
def get_history(db: Session = Depends(get_db)):
    """Retorna o histórico persistido no banco de dados"""
    records = db.query(JobHistory).order_by(JobHistory.created_at.desc()).limit(50).all()
    return records

@app.post("/jobs/dispatch")
async def dispatch_job(req: TriggerJobRequest, db: Session = Depends(get_db)):
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

    # Persistência no Banco PostgreSQL
    try:
        db_job = JobHistory(
            job_id=worker_data["job_id"],
            hotel_name=worker_data["hotel_name"],
            daily_rate=worker_data["daily_rate"],
            processed_by=worker_data["processed_by"],
            processing_time_ms=worker_data["processing_time_ms"],
            checkin_date=worker_data["checkin_date"],
            checkout_date=worker_data["checkout_date"]
        )
        db.add(db_job)
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Erro ao salvar no banco: {e}")

    return {
        "gateway_status": "SUCCESS",
        "dispatched_to": target_url,
        "selected_worker_id": selected_id,
        "result": worker_data
    }