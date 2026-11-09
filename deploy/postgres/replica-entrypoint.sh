#!/bin/sh
# Replica hot standby: na primeira subida faz pg_basebackup -R do primario (com slot).
set -e
PGDATA="${PGDATA:-/var/lib/postgresql/data}"
PRIMARY_HOST="${PRIMARY_HOST:-postgres-db}"
SLOT="${REPLICA_SLOT:-replica_slot}"
export PGPASSWORD="${REPLICATOR_PASSWORD:-replicatorpassword}"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "[replica] PGDATA vazio: pg_basebackup de $PRIMARY_HOST"
  mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA" && chmod 700 "$PGDATA"
  # slot criado de forma idempotente (o replicator tem REPLICATION; CREATE_REPLICATION_SLOT via protocolo)
  su-exec postgres pg_basebackup -h "$PRIMARY_HOST" -U replicator -D "$PGDATA" \
    -R -X stream -S "$SLOT" -C --no-password -P || {
      echo "[replica] slot ja existe, tentando sem -C"
      rm -rf "$PGDATA"/* "$PGDATA"/.[!.]* 2>/dev/null || true
      su-exec postgres pg_basebackup -h "$PRIMARY_HOST" -U replicator -D "$PGDATA" \
        -R -X stream -S "$SLOT" --no-password -P
    }
fi
exec docker-entrypoint.sh postgres -c hot_standby=on
