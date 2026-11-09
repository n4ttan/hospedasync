#!/usr/bin/env bash
# Demo da coordenacao NA AWS (3 EC2): exclusao mutua com hotel real e queda/retorno do lider por SSH.
# Uso: EDGE=<ip-publico-edge> WA=<ip-publico-workers-a> WB=<ip-publico-workers-b> ADMIN_PASSWORD=... bash scripts/aws_demo.sh
# O lider (worker-03) roda no workers-b; o segundo maior ID (worker-02) roda no workers-a.
set -uo pipefail
: "${EDGE:?}" "${WA:?}" "${WB:?}" "${ADMIN_PASSWORD:?}"
GW="https://$EDGE"; ADMIN_USER="${ADMIN_USER:-admin}"; CURL_INSECURE=1
KEY="${KEY_FILE:-$HOME/.ssh/hospeda-key.pem}"
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
C="curl -ksS --retry 5 --retry-delay 3 --retry-all-errors -m 30"
TOKEN=$($C -X POST "$GW/auth/login" -H 'Content-Type: application/json' -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
A="Authorization: Bearer $TOKEN"
status() { $C -H "$A" "$GW/coordination/status" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("lider=%s consenso=%s visoes=%s"%(d["leader_id"],d["consensus"],d["worker_views"]))'; }
wait_leader() {  # wait_leader <id> <segundos>
  local t0=$(date +%s)
  while [ $(( $(date +%s) - t0 )) -lt "$2" ]; do
    s=$(status 2>/dev/null) && echo "[$(( $(date +%s) - t0 ))s] $s" && echo "$s" | grep -q "lider=$1 consenso=True" && return 0
    sleep 2
  done; return 1
}
echo "=== 0. Estado do cluster (3 EC2) ==="; status
$C -H "$A" "$GW/registry/nodes" | python3 -c 'import sys,json;[print(" ",k,v["endpoint"]) for k,v in json.load(sys.stdin)["active_nodes"].items()]'

echo; echo "=== 1. Exclusao mutua: 2 requisicoes simultaneas ao MESMO hotel real (esperado: um 200 e um 409) ==="
HK=$($C -H "$A" "$GW/hotels?q=Copacabana&limit=1" | python3 -c 'import sys,json;d=json.load(sys.stdin);d=d if isinstance(d,list) else d.get("hotels",d);print(d[0]["key"] if "key" in d[0] else d[0]["hotel_key"])')
D1=$(date -d "+$((40 + RANDOM % 200)) days" +%F); D2=$(date -d "$D1 +2 days" +%F)
echo "hotel_key=$HK  datas=$D1..$D2"
body="{\"hotel_key\":\"$HK\",\"checkin_date\":\"$D1\",\"checkout_date\":\"$D2\"}"
for i in 1 2; do ( $C -o /dev/null -w "requisicao $i -> HTTP %{http_code}\n" -X POST -H "$A" -H 'Content-Type: application/json' -d "$body" "$GW/jobs/dispatch" ) & done; wait

echo; echo "=== 2. Hoteis DIFERENTES em paralelo (esperado: dois 200) ==="
HKS=$($C -H "$A" "$GW/hotels?q=Ipanema&limit=2" | python3 -c 'import sys,json;d=json.load(sys.stdin);d=d if isinstance(d,list) else d.get("hotels",d);print(" ".join(x.get("key") or x["hotel_key"] for x in d))')
D3=$(date -d "+$((250 + RANDOM % 60)) days" +%F); D4=$(date -d "$D3 +2 days" +%F)
for k in $HKS; do ( $C -o /dev/null -w "$k -> HTTP %{http_code}\n" -X POST -H "$A" -H 'Content-Type: application/json' -d "{\"hotel_key\":\"$k\",\"checkin_date\":\"$D3\",\"checkout_date\":\"$D4\"}" "$GW/jobs/dispatch" ) & done; wait

echo; echo "=== 3. Derrubando o LIDER (worker-03, na EC2 workers-b) ==="
$SSH ec2-user@$WB 'docker stop hospeda_worker_03 >/dev/null && echo "worker-03 parado em $(hostname)"'
wait_leader 2 90 && echo "RESULTADO: reeleicao concluida, novo lider = worker-02 (EC2 workers-a)" || echo "FALHA: nao reelegeu em 90 s"

echo; echo "=== 4. Religando worker-03 (Bully: o maior ID retoma a lideranca) ==="
$SSH ec2-user@$WB 'docker start hospeda_worker_03 >/dev/null && echo "worker-03 religado"'
wait_leader 3 90 && echo "RESULTADO: worker-03 retomou a lideranca" || echo "FALHA: worker-03 nao retomou em 90 s"

echo; echo "=== 5. Eventos com relogio de Lamport (ordem causal da eleicao) ==="
$C -H "$A" "$GW/coordination/events" | python3 -c '
import sys,json
d=json.load(sys.stdin); ev=d.get("events",d) if isinstance(d,dict) else d
for e in ev[-12:]: print(" ", {k:e[k] for k in ("lamport","node","kind") if k in e})' 2>/dev/null
echo; echo "=== Locks ativos ==="; $C -H "$A" "$GW/locks"
