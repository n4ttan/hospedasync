#!/usr/bin/env bash
# Demonstracao: varredura via RabbitMQ sobrevive a queda do lider e de outro worker (reentrega da fila).
# Uso: bash scripts/demo_fila_falha.sh   (cluster ja no ar: docker compose up --build -d)
set -u
GW=${GATEWAY_URL:-http://localhost:8000}
source "$(dirname "$0")/_auth.sh"
lider() { curl -s "$GW/coordination/status" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("lider=%s consenso=%s visoes=%s" % (d["leader_id"], d["consensus"], d["worker_views"]))'; }

echo "== 1. Estado inicial"; lider
echo "== 2. Disparando varredura de 15 jobs (5 hoteis x 3 datas) pela fila"
SW=$(curl -s -X POST "$GW/sweeps" -H 'Content-Type: application/json' -d '{"top_n":5,"date_offsets":[21,28,35]}')
echo "$SW"; ID=$(echo "$SW" | python3 -c 'import sys,json;print(json.load(sys.stdin)["sweep_id"])')
sleep 5
echo "== 3. Derrubando o LIDER (worker-03) e o worker-02 com jobs em voo"
docker kill hospeda_worker_03 hospeda_worker_02 >/dev/null
for i in $(seq 1 40); do
  sleep 5; R=$(curl -s "$GW/sweeps/$ID"); echo "$(date +%T) $R"
  echo "$R" | grep -q '"finished":true' && break
done
echo "== 4. Nova eleicao"; lider
echo "== 5. Garantia de nao duplicacao (job_id e chave primaria)"
curl -s "$GW/history" | python3 -c 'import sys,json; h=[r for r in json.load(sys.stdin) if r.get("sweep_id")=="'"$ID"'"]; print("registros:",len(h),"unicos:",len({r["job_id"] for r in h}))'
echo "== 6. Religando os workers (o maior ID retoma a lideranca)"
docker start hospeda_worker_02 hospeda_worker_03 >/dev/null; sleep 25; lider
