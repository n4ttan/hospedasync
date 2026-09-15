from fastapi import FastAPI
from pydantic import BaseModel
import datetime
import random
import time

app = FastAPI(title="HospedaSync Worker Node", version="1.0.0")

class ScrapeRequest(BaseModel):
    job_id: str
    hotel_name: str
    checkin_date: str
    checkout_date: str

class ScrapeResponse(BaseModel):
    job_id: str
    hotel_name: str
    daily_rate: float
    currency: str
    collected_at: str
    processing_time_ms: float

@app.get("/health")
def health_check():
    return {"status": "UP", "service": "worker-node"}

@app.post("/scrape", response_model=ScrapeResponse)
def execute_scrape(payload: ScrapeRequest):
    start_time = time.time()
    
    # Latência simulada de rede/raspagem da OTA
    time.sleep(random.uniform(0.2, 0.5))
    
    # Simulação da tarifa média do compset de Poços de Caldas
    simulated_price = round(random.uniform(190.0, 420.0), 2)
    elapsed = round((time.time() - start_time) * 1000, 2)
    
    return ScrapeResponse(
        job_id=payload.job_id,
        hotel_name=payload.hotel_name,
        daily_rate=simulated_price,
        currency="BRL",
        collected_at=datetime.datetime.utcnow().isoformat(),
        processing_time_ms=elapsed
    )