import os
import time
from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime, Text, text
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
    # Cotacao real (Xotelo). rates e uma lista JSON serializada em Text.
    hotel_key = Column(String, nullable=True, index=True)
    currency = Column(String, nullable=True)
    rates = Column(Text, nullable=True)
    min_rate = Column(Float, nullable=True)
    source = Column(String, nullable=True)
    fetched_at = Column(String, nullable=True)


class Hotel(Base):
    __tablename__ = "hotels"

    hotel_key = Column(String, primary_key=True)
    name = Column(String, nullable=False, index=True)
    location_key = Column(String, nullable=True)
    rating = Column(Float, nullable=True)
    review_count = Column(Integer, nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    tripadvisor_url = Column(String, nullable=True)
    accommodation_type = Column(String, nullable=True)


NEW_JOB_COLUMNS = {
    "hotel_key": "VARCHAR",
    "currency": "VARCHAR",
    "rates": "TEXT",
    "min_rate": "DOUBLE PRECISION",
    "source": "VARCHAR",
    "fetched_at": "VARCHAR",
}


def migrate_job_history():
    """create_all nao altera tabelas existentes: adiciona colunas novas (Postgres)."""
    if engine.dialect.name != "postgresql":
        return
    try:
        with engine.begin() as conn:
            for col, typ in NEW_JOB_COLUMNS.items():
                conn.execute(text(f"ALTER TABLE job_history ADD COLUMN IF NOT EXISTS {col} {typ}"))
    except Exception as e:
        print(f"Migracao de job_history ignorada: {e}")

def init_db():
    retries = 10
    while retries > 0:
        try:
            Base.metadata.create_all(bind=engine)
            migrate_job_history()
            print("Conexão com PostgreSQL estabelecida com sucesso!")
            break
        except Exception as e:
            retries -= 1
            print(f"Aguardando PostgreSQL iniciar... ({retries} tentativas restantes). Erro: {e}")
            time.sleep(3)