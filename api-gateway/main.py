from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx
import os
import uuid
from pathlib import Path

app = FastAPI(title="HospedaSync API Gateway", version="1.0.0")

# Caminhos absolutos/dinâmicos para arquivos estáticos
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

WORKER_URL = os.getenv("WORKER_SERVICE_URL", "http://127.0.0.1:8001")

class TriggerJobRequest(BaseModel):
    hotel_name: str
    checkin_date: str
    checkout_date: str

@app.get("/")
async def read_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "HospedaSync API Gateway em execução. Interface estática não localizada."}

@app.get("/health")
def health():
    return {"status": "UP", "service": "api-gateway"}

@app.post("/jobs/dispatch")
async def dispatch_job(req: TriggerJobRequest):
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    
    payload = {
        "job_id": job_id,
        "hotel_name": req.hotel_name,
        "checkin_date": req.checkin_date,
        "checkout_date": req.checkout_date
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(f"{WORKER_URL}/scrape", json=payload)
            
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail="Erro retornado pelo no worker.")
                
            worker_data = response.json()
            
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503, 
            detail=f"Falha de comunicacao distribuida com o Worker em {WORKER_URL}: {str(exc)}"
        )
        
    return {
        "gateway_status": "SUCCESS",
        "dispatched_to": WORKER_URL,
        "result": worker_data
    }