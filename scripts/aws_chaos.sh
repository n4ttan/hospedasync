#!/usr/bin/env bash
# Roda os 6 experimentos de caos (scripts/chaos_demo.sh) contra o cluster na AWS (3 EC2).
# Como: define uma funcao `docker` que encaminha cada comando por SSH para a EC2 dona do container,
# e abre tuneis SSH para o painel do RabbitMQ (edge) e para a porta /health de cada worker.
# Uso: EDGE=<ip-pub> WA=<ip-pub> WB=<ip-pub> WA_PRIV=<ip-priv> WB_PRIV=<ip-priv> ADMIN_PASSWORD=... \
#      RABBITMQ_PASS=... bash scripts/aws_chaos.sh [1 2 3 4 5 6]
set -uo pipefail
: "${EDGE:?}" "${WA:?}" "${WB:?}" "${WA_PRIV:?}" "${WB_PRIV:?}" "${ADMIN_PASSWORD:?}" "${RABBITMQ_PASS:?}"
KEY="${KEY_FILE:-$HOME/.ssh/hospeda-key.pem}"
SSHO=(-i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o ServerAliveInterval=15)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

host_of() {
  case "$1" in
    hospeda_worker_01|hospeda_worker_02|hospeda_db) echo "$WA" ;;
    hospeda_worker_03|hospeda_db_replica) echo "$WB" ;;
    hospeda_rabbitmq|hospeda_redis_lock|hospeda_gateway|hospeda_nginx) echo "$EDGE" ;;
    *) echo "" ;;
  esac
}
docker() {
  local a h=""
  for a in "$@"; do h=$(host_of "$a"); [ -n "$h" ] && break; done
  [ -z "$h" ] && { echo "docker: container desconhecido em: $*" >&2; return 1; }
  local q; q=$(printf '%q ' "$@")
  # shellcheck disable=SC2086
  ssh $SSHO_STR "ec2-user@$h" "docker $q"
}
export -f host_of docker
export KEY WA WB EDGE
export SSHO_STR="${SSHO[*]}"
# a funcao exportada precisa do array no subshell: reconstroi a partir da string
SSHO=($SSHO_STR)

TUN=()
cleanup() { for p in "${TUN[@]:-}"; do kill "$p" 2>/dev/null; done; }
trap cleanup EXIT
tunnel() {  # tunnel <porta-local> <host-ssh> <destino:porta>
  ssh "${SSHO[@]}" -N -L "$1:$3" "ec2-user@$2" & TUN+=($!)
}
tunnel 25672 "$EDGE" localhost:15672
tunnel 18001 "$WA" "$WA_PRIV:8001"
tunnel 18002 "$WA" "$WA_PRIV:8002"
tunnel 18003 "$WB" "$WB_PRIV:8003"
for _ in $(seq 1 30); do
  (exec 3<>/dev/tcp/127.0.0.1/25672) 2>/dev/null && break; sleep 1
done
(exec 3<>/dev/tcp/127.0.0.1/25672) 2>/dev/null || { echo "tunel do RabbitMQ (25672) nao abriu"; exit 1; }
# cotacoes em cache fariam a varredura terminar instantaneamente (sem jobs em voo): zera o cache do experimento
docker exec hospeda_redis_lock sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning --scan --pattern "cache:rates:*" | xargs -r redis-cli -a "$REDIS_PASSWORD" --no-auth-warning del' >/dev/null 2>&1 && echo "cache de cotacoes zerado" || echo "aviso: nao consegui zerar o cache"

mkdir -p "$HOME/.curl-insecure-aws" && echo insecure > "$HOME/.curl-insecure-aws/.curlrc"
export CURL_HOME="$HOME/.curl-insecure-aws"
export GATEWAY_URL="https://$EDGE" ADMIN_USER="${ADMIN_USER:-admin}" ADMIN_PASSWORD RABBITMQ_PASS
export RABBITMQ_USER="${RABBITMQ_USER:-hospeda}" RABBITMQ_API="http://localhost:25672" WORKER_PORT_OFFSET=10000
export EVIDENCE_FILE="${EVIDENCE_FILE:-$ROOT/docs/relatorio/evidencias/chaos_aws_$(date +%F).md}"
export GW_PROBE_RETRIES=3 GW_OK_TIMEOUT=10 SAVE="${SAVE:-1}"
bash "$ROOT/scripts/chaos_demo.sh" "$@"
