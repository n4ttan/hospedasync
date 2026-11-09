#!/usr/bin/env bash
# Gera o .env local com segredos aleatorios (NUNCA versionado). Nao sobrescreve um .env existente.
# Uso: bash scripts/gen_env.sh
set -euo pipefail
cd "$(dirname "$0")/.."
[ -e .env ] && { echo ".env ja existe; nada a fazer (apague-o se quiser regenerar)."; exit 0; }
r() { openssl rand -hex 24; }
cat > .env <<ENV
POSTGRES_PASSWORD=$(r)
REPLICATOR_PASSWORD=$(r)
REDIS_PASSWORD=$(r)
RABBITMQ_USER=hospeda
RABBITMQ_PASS=$(r)
JWT_SECRET=$(openssl rand -hex 32)
JWT_TTL_MINUTES=30
SERVICE_SECRET=$(openssl rand -hex 32)
ADMIN_USER=admin
ADMIN_PASSWORD=$(r)
SWEEP_INTERVAL_SECONDS=120
ENV
chmod 600 .env
echo ".env criado (chmod 600). Usuario admin: admin. A senha esta em .env (ADMIN_PASSWORD)."
