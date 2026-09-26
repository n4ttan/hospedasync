import os
import time
from sqlalchemy import create_engine, Column, String, Float, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import datetime

# Adicionado +psycopg2 para forçar o driver correto instalado no requirements.txt
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql+psycopg2://postgres:postgrespassword@postgres-db:5432/hospedasync_db"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class JobHistory(Base):
    __tablename__ = "job_history"

    job_id = Column(String, primary_key=True, index=True)
    hotel_name = Column(String, nullable=False)
    daily_rate = Column(Float, nullable=False)
    processed_by = Column(String, nullable=False)
    processing_time_ms = Column(Float, nullable=False)
    checkin_date = Column(String, nullable=False)
    checkout_date = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

def init_db():
    retries = 10
    while retries > 0:
        try:
            Base.metadata.create_all(bind=engine)
            print("Conexão com PostgreSQL estabelecida com sucesso!")
            break
        except Exception as e:
            retries -= 1
            print(f"Aguardando PostgreSQL iniciar... ({retries} tentativas restantes). Erro: {e}")
            time.sleep(3)