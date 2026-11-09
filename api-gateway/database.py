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

# Replica de leitura (hot standby) opcional. Sem DATABASE_REPLICA_URL, leituras vao ao primario.
DATABASE_REPLICA_URL = os.getenv("DATABASE_REPLICA_URL", "")
replica_engine = (
    create_engine(DATABASE_REPLICA_URL, pool_pre_ping=True, connect_args=(
        {"connect_timeout": 2} if DATABASE_REPLICA_URL.startswith("postgresql") else {}))
    if DATABASE_REPLICA_URL else None
)
ReplicaSessionLocal = (
    sessionmaker(autocommit=False, autoflush=False, bind=replica_engine) if replica_engine else None
)

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
    # Varredura em lote (POST /sweeps) que originou o job; nulo para despachos avulsos.
    sweep_id = Column(String, nullable=True, index=True)


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
    "sweep_id": "VARCHAR",
}


def migrate_job_history():
    """create_all nao altera tabelas existentes: adiciona colunas novas (Postgres)."""
    if engine.dialect.name != "postgresql":
        return
    try:
        with engine.begin() as conn:
            for col, typ in NEW_JOB_COLUMNS.items():
                conn.execute(text(f"ALTER TABLE job_history ADD COLUMN IF NOT EXISTS {col} {typ}"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_job_history_sweep_id ON job_history (sweep_id)"))
    except Exception as e:
        print(f"Migracao de job_history ignorada: {e}")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_read_db(consistency: str = "strong", primary_db=None):
    """Sessao para leitura. Retorna (session, served_by, fallback).

    strong  -> primario (le o que foi confirmado, sem defasagem).
    eventual -> replica (pode estar atrasada); se ausente/indisponivel cai para o primario
                (served_by="primary", fallback=True).
    primary_db: sessao do primario ja aberta (ex.: Depends(get_db)); se omitida abre uma nova.
    Quem chama fecha a sessao retornada apenas se ela nao for primary_db.
    """
    def primary():
        return primary_db if primary_db is not None else SessionLocal()

    if consistency == "eventual":
        if ReplicaSessionLocal is None:
            return primary(), "primary", True
        rdb = None
        try:
            rdb = ReplicaSessionLocal()
            rdb.execute(text("SELECT 1"))
            return rdb, "replica", False
        except Exception:
            if rdb is not None:
                rdb.close()
            return primary(), "primary", True
    return primary(), "primary", False


def replica_lag_ms(rdb):
    """Atraso de replay medido na replica (ms), ou None se nao for standby."""
    row = rdb.execute(text(
        "SELECT CASE WHEN pg_is_in_recovery() THEN "
        "COALESCE(EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp())) * 1000, 0) END"
    )).scalar()
    return None if row is None else round(float(row), 1)


def consistency_status():
    """Estado da replicacao visto do primario e da replica (nunca levanta excecao)."""
    st = {"replica_configured": replica_engine is not None, "replica_reachable": False,
          "replication_lag_ms": None, "replay_lag_bytes": None, "sync_mode": None,
          "primary_in_recovery": False}
    try:
        with engine.connect() as c:
            st["primary_in_recovery"] = bool(c.execute(text("SELECT pg_is_in_recovery()")).scalar())
            row = c.execute(text(
                "SELECT sync_state, pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn), "
                "EXTRACT(EPOCH FROM replay_lag) * 1000 FROM pg_stat_replication LIMIT 1")).first()
            if row:
                st["sync_mode"] = row[0]
                st["replay_lag_bytes"] = int(row[1]) if row[1] is not None else None
                st["_primary_lag_ms"] = round(float(row[2]), 1) if row[2] is not None else None
    except Exception:
        pass
    if replica_engine is not None:
        try:
            with replica_engine.connect() as c:
                st["replica_reachable"] = True
                lag = c.execute(text(
                    "SELECT CASE WHEN pg_is_in_recovery() THEN COALESCE(EXTRACT(EPOCH FROM "
                    "(now() - pg_last_xact_replay_timestamp())) * 1000, 0) END")).scalar()
                if lag is not None:
                    st["replication_lag_ms"] = round(float(lag), 1)
        except Exception:
            pass
    if st["replication_lag_ms"] is None:
        st["replication_lag_ms"] = st.get("_primary_lag_ms")
    st.pop("_primary_lag_ms", None)
    return st


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