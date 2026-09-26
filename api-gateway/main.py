from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx
import os
import uuid

app = FastAPI(title="HospedaSync API Gateway", version="1.0.0")

# Servir a pasta static
app.mount("/static", StaticFiles(directory="api-gateway/static"), name="static")

WORKER_URL = os.getenv("WORKER_SERVICE_URL", "http://127.0.0.1:8001")

class TriggerJobRequest(BaseModel):
    hotel_name: str
    checkin_date: str
    checkout_date: str

@app.get("/")
async def read_index():
    return FileResponse("api-gateway/static/index.html")

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