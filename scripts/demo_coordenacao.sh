#!/usr/bin/env bash
# Demonstracao da camada de Coordenacao (eleicao Bully + exclusao mutua via Redis).
# Uso: bash scripts/demo_coordenacao.sh   (precisa de docker compose e curl)
set -u
GW=${GATEWAY_URL:-http://localhost:8000}
source "$(dirname "$0")/_auth.sh"
DEMO_HOTEL="Hotel Copacabana Palace"
body() { printf '{"hotel_name":"%s","checkin_date":"2026-12-20","checkout_date":"2026-12-23"}' "$1"; }
leader() { curl -s "$GW/coordination/status" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("lider=%s consenso=%s visoes=%s" % (d["leader_id"], d["consensus"], d["worker_views"]))'; }
step() { printf '\n=== %s ===\n' "$1"; }

step "0. Subindo o cluster"
docker compose up --build -d >/dev/null 2>&1 || { echo "falha ao subir docker compose"; exit 1; }
for i in $(seq 1 40); do curl -sf "$GW/health" >/dev/null && break; sleep 2; done

step "1. Aguardando a eleicao inicial (maior ID vence: worker-03)"
for i in $(seq 1 15); do
  out=$(leader); echo "$out"; echo "$out" | grep -q "consenso=True" && break; sleep 2
done

step "2. Exclusao mutua: 2 jobs simultaneos no MESMO hotel (esperado: um 200 e um 409)"
for n in 1 2; do
  ( curl -s -o /dev/null -w "requisicao $n -> HTTP %{http_code}\n" -X POST "$GW/jobs/dispatch" \
      -H 'Content-Type: application/json' -d "$(body "$DEMO_HOTEL")" ) &
done; wait

step "3. Hoteis DIFERENTES em paralelo (esperado: dois 200)"
for h in "Hotel A" "Hotel B"; do
  ( curl -s -o /dev/null -w "$h -> HTTP %{http_code}\n" -X POST "$GW/jobs/dispatch" \
      -H 'Content-Type: application/json' -d "$(body "$h")" ) &
done; wait

step "4. Derrubando o lider (worker-03) e esperando a reeleicao (esperado: worker-02)"
docker stop hospeda_worker_03 >/dev/null
for i in $(seq 1 12); do sleep 3; out=$(leader); echo "$out"; echo "$out" | grep -q "lider=2 " && break; done

step "5. Religando worker-03 (Bully: o maior ID retoma a lideranca)"
docker start hospeda_worker_03 >/dev/null
for i in $(seq 1 12); do sleep 3; out=$(leader); echo "$out"; echo "$out" | grep -q "lider=3 " && break; done

step "Locks ativos agora"
curl -s "$GW/locks"; echo
