#!/usr/bin/env bash
# Experimento: assincrono x forte x falha da replica. Requer postgres-db e postgres-replica no ar.
# Uso: N=50 scripts/consistency_experiment.sh   (PYTHON=... para escolher o interpretador com psycopg2)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
"$PY" -c "import psycopg2" 2>/dev/null || { echo "psycopg2 ausente em $PY (defina PYTHON=...)" >&2; exit 1; }
docker exec hospeda_db_replica psql -U postgres -tAc "select pg_is_in_recovery()" | grep -q t \
  || { echo "replica nao esta em recovery/no ar" >&2; exit 1; }
exec "$PY" "$ROOT/scripts/consistency_experiment.py"
