#!/usr/bin/env bash
# Experimentos de caos do HospedaSync: cada um injeta UMA falha e verifica uma hipotese.
#
# Uso:  bash scripts/chaos_demo.sh [all|1|2|3|4|5|6 ...]      (cluster no ar: docker compose up --build -d)
#   1  matar um worker no meio de uma varredura de 15 jobs
#   2  matar o LIDER (eleicao Bully + varredura periodica continua)
#   3  parar o RabbitMQ por 20 s durante varredura
#   4  parar o Redis por 20 s (jobs nao podem ir para a DLQ)
#   5  bloquear a Xotelo (circuit breaker + fallback para cache)
#   6  parar o Postgres por 15 s (gateway vive, resultados nao se perdem)
#
# Variaveis de ambiente:
#   GATEWAY_URL              (http://localhost:8000)
#   TOKEN                    JWT ja emitido; se ausente e houver ADMIN_USER/ADMIN_PASSWORD, faz POST /auth/login
#   RABBITMQ_USER/PASS       (hospeda / hospeda-mq-pass) para a API de gerenciamento em :15672
#   SWEEP_INTERVAL_SECONDS   (120) intervalo da varredura periodica; o exp. 2 espera ate ele + 30 s
#   SKIP_PERIODIC=1          pula, no exp. 2, a espera pela varredura periodica do novo lider
#   SAVE=1                   grava a tabela-resumo em docs/relatorio/evidencias/chaos_<data>.md
#
# Metodo do exp. 5 (sem privilegios no host): `docker exec -u root` no worker acrescenta
# "127.0.0.1 data.xotelo.com" em /etc/hosts DENTRO do container; a conexao e recusada e a
# Xotelo "some" so para aquele worker. A recuperacao remove a linha (e reiniciar o container tambem).
#
# ATENCAO: derruba containers de verdade. Rode apenas em ambiente de demonstracao.
set -u

GW=${GATEWAY_URL:-http://localhost:8000}
MQ_USER=${RABBITMQ_USER:-hospeda}
MQ_PASS=${RABBITMQ_PASS:-hospeda-mq-pass}
MQ_API=${RABBITMQ_API:-http://localhost:15672}
SWEEP_INTERVAL=${SWEEP_INTERVAL_SECONDS:-120}
TOKEN=${TOKEN:-}
LOGIN_DONE=0
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORKERS=(hospeda_worker_01 hospeda_worker_02 hospeda_worker_03)
INFRA=(hospeda_rabbitmq hospeda_redis_lock hospeda_db)

if [ -n "${NO_COLOR:-}" ]; then G=""; R=""; Y=""; B=""; N=""; else
  G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'; fi

# ---------------------------------------------------------------- resultados
NAMES=(""  "Worker morto em varredura" "Lider morto" "RabbitMQ fora 20s" "Redis fora 20s" "Xotelo bloqueada" "Postgres fora 15s")
HIPS=(""
  "varredura termina 15/15 sem job_id duplicado"
  "nova eleicao com consenso em ate 30s e varredura periodica continua"
  "gateway responde 503 enquanto fora; reconecta e varredura completa"
  "nenhum job vai para a DLQ; processamento retoma"
  "breaker abre, dispatch devolve cache/DEGRADED e depois volta a live"
  "gateway nao cai; resultados nao se perdem e sao gravados na volta")
declare -a RES REC
for i in 1 2 3 4 5 6; do RES[$i]="NAO EXECUTADO"; REC[$i]="-"; done

# ---------------------------------------------------------------- utilitarios
step()  { printf '\n%s=== %s ===%s\n' "$B" "$1" "$N"; }
info()  { printf '  %s\n' "$*"; }
warn()  { printf '  %s! %s%s\n' "$Y" "$*" "$N"; }
hip()   { printf '%sHIPOTESE:%s %s\n' "$B" "$N" "$1"; }
inj()   { printf '%sINJECAO:%s %s\n' "$B" "$N" "$1"; }
obs()   { printf '%sOBSERVACAO:%s %s\n' "$B" "$N" "$1"; }
rec()   { printf '%sRECUPERACAO:%s %s\n' "$B" "$N" "$1"; }

veredito() {  # veredito <n> <PASS|FAIL> <detalhe>
  RES[$1]=$2
  if [ "$2" = PASS ]; then printf '%sVEREDITO: PASS%s - %s\n' "$G" "$N" "$3"
  else printf '%sVEREDITO: FAIL%s - %s\n' "$R" "$N" "$3"; fi
}

# Credenciais: ambiente ou, na falta, o .env local (nunca versionado)
_HS_ENV="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env"
_from_env_file() { grep -E "^$1=" "$_HS_ENV" 2>/dev/null | cut -d= -f2-; }
[ -z "${ADMIN_USER:-}" ] && ADMIN_USER=$(_from_env_file ADMIN_USER)
[ -z "${ADMIN_PASSWORD:-}" ] && ADMIN_PASSWORD=$(_from_env_file ADMIN_PASSWORD)
export ADMIN_USER ADMIN_PASSWORD
[ -z "${RABBITMQ_PASS:-}" ] && RABBITMQ_PASS=$(_from_env_file RABBITMQ_PASS)
[ -z "${RABBITMQ_USER:-}" ] && RABBITMQ_USER=$(_from_env_file RABBITMQ_USER)
MQ_USER=${RABBITMQ_USER:-$MQ_USER}; MQ_PASS=${RABBITMQ_PASS:-$MQ_PASS}

login() {
  [ -n "$TOKEN" ] && [ "$LOGIN_DONE" = 0 ] && return 0   # TOKEN dado pelo usuario: nao renova
  if [ -n "${ADMIN_USER:-}" ] && [ -n "${ADMIN_PASSWORD:-}" ]; then
    local out
    out=$(curl -s -m 10 -X POST "$GW/auth/login" -H 'Content-Type: application/json' \
      -d "$(python3 -c 'import json,os;print(json.dumps({"username":os.environ["ADMIN_USER"],"password":os.environ["ADMIN_PASSWORD"]}))')")
    local t
    t=$(printf '%s' "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null)
    if [ -n "$t" ]; then TOKEN=$t; LOGIN_DONE=1; return 0; fi
    warn "login em /auth/login falhou: $out"
  elif [ -z "$TOKEN" ]; then
    warn "sem TOKEN nem ADMIN_USER/ADMIN_PASSWORD: chamadas seguem sem Authorization"
  fi
  return 0
}

api() {  # curl com timeout e Authorization quando houver token
  if [ -n "$TOKEN" ]; then curl -s -m "${API_TIMEOUT:-10}" -H "Authorization: Bearer $TOKEN" "$@"
  else curl -s -m "${API_TIMEOUT:-10}" "$@"; fi
}

pyj() {  # pyj '<expressao em d>' : le JSON do stdin; vazio se invalido
  python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); print(eval(sys.argv[1]))
except Exception:
    print("")' "$1"
}

status_line() { api "$GW/coordination/status" | pyj '"lider=%s consenso=%s visoes=%s" % (d["leader_id"], d["consensus"], d["worker_views"])'; }
leader_id()   { api "$GW/coordination/status" | pyj 'd["leader_id"] if d.get("consensus") else ""'; }
consensus()   { api "$GW/coordination/status" | pyj 'd.get("consensus")'; }

wait_until() {  # wait_until <timeout> <intervalo> cmd... ; 0 se cmd teve sucesso
  local t=$1 iv=$2; shift 2
  local end=$((SECONDS + t))
  while [ $SECONDS -lt $end ]; do "$@" && return 0; sleep "$iv"; done
  "$@"
}

container_healthy() {
  local st
  st=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null)
  [ "$st" = healthy ] || [ "$st" = running ]
}
redis_ok()  { [ "$(docker exec hospeda_redis_lock redis-cli ping 2>/dev/null)" = PONG ]; }
pg_ok()     { docker exec hospeda_db pg_isready -U postgres -d hospedasync_db >/dev/null 2>&1; }
gw_ok()     { curl -sf -m "${GW_OK_TIMEOUT:-3}" "$GW/health" >/dev/null; }
nodes_ok()  { [ "$(api "$GW/registry/nodes" | pyj 'd["count"]')" = 3 ]; }
cluster_ok() { gw_ok && nodes_ok && [ "$(consensus)" = True ]; }

# varredura: define SW_CODE e SW_ID (chamar direto, sem $( ))
start_sweep() {
  local out
  out=$(API_TIMEOUT=${1:-20} api -w '\n%{http_code}' -X POST "$GW/sweeps" -H 'Content-Type: application/json' \
        -d '{"top_n":5,"date_offsets":[21,28,35]}')
  SW_CODE=$(printf '%s' "$out" | tail -n1)
  SW_ID=$(printf '%s' "$out" | sed '$d' | pyj 'd.get("sweep_id","")')
}
sweep_json()     { api "$GW/sweeps/$1"; }
sweep_finished() { [ "$(sweep_json "$1" | pyj 'd.get("finished")')" = True ]; }
sweep_resumo()   { sweep_json "$1" | pyj '"total=%s done=%s errors=%s no_rates=%s pending=%s" % (d["total"],d["done"],d["errors"],d["no_rates"],d["pending"])'; }
sweep_accounted() { sweep_json "$1" | pyj 'd["done"]+d["errors"]+d["no_rates"]'; }

# historico do sweep: "registros unicos"
hist_sweep() {
  api "$GW/history" | python3 -c '
import sys,json
try:
    h=[r for r in json.load(sys.stdin) if r.get("sweep_id")==sys.argv[1]]
    print(len(h), len({r["job_id"] for r in h}))
except Exception:
    print("? ?")' "$1"
}

dlq_count() {  # mensagens na DLQ; 0 se a fila nao existe; ? se a API nao responde
  curl -s -m 8 -u "$MQ_USER:$MQ_PASS" "$MQ_API/api/queues/%2F/hospeda.scrape.dead" | python3 -c '
import sys,json
try:
    d=json.load(sys.stdin)
    print(0 if d.get("error")=="Object Not Found" else d["messages"])
except Exception:
    print("?")'
}

# ---------------------------------------------------------------- recuperacao global (idempotente)
unblock_xotelo() {
  for c in "${WORKERS[@]}"; do
    docker exec -u root "$c" sh -c 'grep -v "data.xotelo.com" /etc/hosts > /tmp/hosts.new && cat /tmp/hosts.new > /etc/hosts' >/dev/null 2>&1
  done
}
restore_all() {
  local c
  for c in "${INFRA[@]}" "${WORKERS[@]}"; do
    docker start "$c" >/dev/null 2>&1
  done
  unblock_xotelo
}
on_exit() {
  trap - EXIT INT TERM
  printf '\n%s[trap] restaurando ambiente...%s\n' "$Y" "$N"
  restore_all
  exit "${1:-0}"
}
trap 'on_exit 130' INT TERM
trap 'on_exit $?' EXIT

estabilizar() {
  info "aguardando estado estavel (3 workers registrados, consenso de lider)..."
  if wait_until 90 3 cluster_ok; then info "estavel: $(status_line)"; else warn "cluster nao estabilizou em 90s: $(status_line)"; fi
}

# ================================================================ EXPERIMENTO 1
exp1() {
  step "EXPERIMENTO 1 - ${NAMES[1]}"
  hip "${HIPS[1]}"
  login; start_sweep
  [ "$SW_CODE" = 202 ] || { veredito 1 FAIL "POST /sweeps devolveu HTTP $SW_CODE"; return; }
  info "varredura $SW_ID enfileirada (15 jobs)"
  sleep 3
  inj "docker kill hospeda_worker_02 (com jobs em voo)"
  docker kill hospeda_worker_02 >/dev/null
  obs "polling de /sweeps/$SW_ID (timeout 180 s)"
  local t0=$SECONDS
  wait_until 180 4 sweep_finished "$SW_ID"; local fin=$?
  info "$(sweep_resumo "$SW_ID") apos $((SECONDS - t0))s"
  local acc regs uniq; acc=$(sweep_accounted "$SW_ID")
  read -r regs uniq <<<"$(hist_sweep "$SW_ID")"
  info "contabilizados=$acc/15  /history: registros=$regs unicos=$uniq"
  rec "docker start hospeda_worker_02 e espera do re-registro"
  docker start hospeda_worker_02 >/dev/null
  local r0=$SECONDS; wait_until 60 2 cluster_ok; REC[1]=$((SECONDS - r0))
  if [ $fin -eq 0 ] && [ "$acc" = 15 ] && [ "$regs" = "$uniq" ] && [ "$regs" != "?" ]; then
    veredito 1 PASS "15/15 contabilizados, $regs registros, todos com job_id unico"
  else
    veredito 1 FAIL "finished=$([ $fin -eq 0 ] && echo sim || echo nao) contabilizados=$acc/15 registros=$regs unicos=$uniq"
  fi
}

# ================================================================ EXPERIMENTO 2
sweep_ids_events() {
  api "$GW/coordination/events?limit=1000" | python3 -c '
import sys,json,re
try:
    ev=json.load(sys.stdin)["events"]
    ids={m for e in ev if "sweep_scheduled" in json.dumps(e) for m in re.findall(r"sweep-[0-9a-f]{8}", json.dumps(e))}
    print(" ".join(sorted(ids)))
except Exception:
    print("")'
}
novo_sweep_periodico() {
  local cur id
  cur=$(sweep_ids_events)
  for id in $cur; do case " $BASE_SWEEPS " in *" $id "*) ;; *) return 0;; esac; done
  return 1
}
exp2() {
  step "EXPERIMENTO 2 - ${NAMES[2]}"
  hip "${HIPS[2]}"
  login
  wait_until 30 2 cluster_ok >/dev/null
  local old; old=$(leader_id)
  [ -n "$old" ] || { veredito 2 FAIL "sem lider/consenso antes da injecao: $(status_line)"; return; }
  BASE_SWEEPS=$(sweep_ids_events)
  local victim; victim=$(printf 'hospeda_worker_%02d' "$old")
  inj "docker kill $victim (lider atual = $old)"
  docker kill "$victim" >/dev/null
  local t0=$SECONDS ok=1 new=""
  obs "polling de /coordination/status ate consenso em outro lider (timeout 30 s)"
  while [ $((SECONDS - t0)) -lt 30 ]; do
    new=$(leader_id)
    if [ -n "$new" ] && [ "$new" != "$old" ]; then ok=0; break; fi
    sleep 2
  done
  local elapsed=$((SECONDS - t0))
  if [ $ok -eq 0 ]; then info "novo lider=$new apos ${elapsed}s"; else info "sem consenso em 30s: $(status_line)"; fi
  local periodic="pulado (SKIP_PERIODIC=1)" pok=0
  if [ $ok -eq 0 ] && [ -z "${SKIP_PERIODIC:-}" ]; then
    local w=$((SWEEP_INTERVAL + 30))
    obs "esperando varredura periodica do novo lider (ate ${w}s; SKIP_PERIODIC=1 pula)"
    if wait_until "$w" 5 novo_sweep_periodico; then periodic="nova varredura periodica observada"; else periodic="NENHUMA varredura periodica em ${w}s"; pok=1; fi
    info "$periodic"
  elif [ $ok -ne 0 ]; then periodic="nao avaliada"; fi
  rec "docker start $victim (Bully: o maior ID retoma a lideranca)"
  docker start "$victim" >/dev/null
  local r0=$SECONDS; wait_until 60 2 cluster_ok; REC[2]=$((SECONDS - r0)); info "$(status_line)"
  if [ $ok -eq 0 ] && [ $pok -eq 0 ]; then
    veredito 2 PASS "reeleicao em ${elapsed}s (lider $old -> $new); $periodic"
  else
    veredito 2 FAIL "reeleicao=$([ $ok -eq 0 ] && echo "${elapsed}s" || echo nao); $periodic"
  fi
}

# ================================================================ EXPERIMENTO 3
exp3() {
  step "EXPERIMENTO 3 - ${NAMES[3]}"
  hip "${HIPS[3]}"
  login; start_sweep
  [ "$SW_CODE" = 202 ] || { veredito 3 FAIL "POST /sweeps devolveu HTTP $SW_CODE"; return; }
  local sw1=$SW_ID; info "varredura $sw1 enfileirada"
  sleep 2
  inj "docker stop hospeda_rabbitmq (20 s)"
  docker stop -t 2 hospeda_rabbitmq >/dev/null
  local t0=$SECONDS saw503=1 code h
  obs "durante a queda: POST /sweeps deve devolver 503 e /health continuar respondendo"
  local gwlive=0
  while [ $((SECONDS - t0)) -lt 20 ]; do
    h=$(curl -s -m 3 "$GW/health" | pyj 'd["rabbitmq"]')
    [ -n "$h" ] && gwlive=1
    start_sweep 15; code=$SW_CODE
    info "t=$((SECONDS - t0))s  /health.rabbitmq=${h:-sem resposta}  POST /sweeps -> HTTP $code"
    [ "$code" = 503 ] && saw503=0
    [ "$code" = 202 ] && info "(202 durante a queda: broker ainda aceitou)"
    sleep 3
  done
  rec "docker start hospeda_rabbitmq"
  docker start hospeda_rabbitmq >/dev/null
  local r0=$SECONDS
  wait_until 90 3 container_healthy hospeda_rabbitmq
  obs "polling: POST /sweeps volta a 202 (timeout 90 s)"
  local back=1; SW_CODE=0
  local end=$((SECONDS + 90))
  while [ $SECONDS -lt $end ]; do
    start_sweep 15
    [ "$SW_CODE" = 202 ] && { back=0; break; }
    sleep 3
  done
  local sw2=$SW_ID
  wait_until 180 4 sweep_finished "$sw1"; local f1=$?
  local f2=1; [ $back -eq 0 ] && { wait_until 180 4 sweep_finished "$sw2"; f2=$?; }
  REC[3]=$((SECONDS - r0))
  info "varredura pre-queda: $(sweep_resumo "$sw1")"
  [ $back -eq 0 ] && info "varredura pos-volta: $(sweep_resumo "$sw2")"
  local detail="503 durante a queda=$([ $saw503 -eq 0 ] && echo sim || echo nao); 202 apos volta=$([ $back -eq 0 ] && echo sim || echo nao); pre-queda concluida=$([ $f1 -eq 0 ] && echo sim || echo nao); pos-volta concluida=$([ $f2 -eq 0 ] && echo sim || echo nao)"
  if [ $back -eq 0 ] && [ $f1 -eq 0 ] && [ $f2 -eq 0 ] && [ $gwlive -eq 1 ]; then veredito 3 PASS "$detail"
  else veredito 3 FAIL "$detail"; fi
}

# ================================================================ EXPERIMENTO 4
exp4() {
  step "EXPERIMENTO 4 - ${NAMES[4]}"
  hip "${HIPS[4]}"
  login
  sleep 6; local dlq0; dlq0=$(dlq_count); info "DLQ antes: $dlq0 mensagem(ns)"
  start_sweep
  [ "$SW_CODE" = 202 ] || { veredito 4 FAIL "POST /sweeps devolveu HTTP $SW_CODE"; return; }
  info "varredura $SW_ID enfileirada"
  inj "docker stop hospeda_redis_lock (20 s)"
  docker stop -t 2 hospeda_redis_lock >/dev/null
  obs "workers fazem retry de infra com backoff (cabecalho x-infra-retries), sem nack para a DLQ"
  sleep 20
  rec "docker start hospeda_redis_lock"
  docker start hospeda_redis_lock >/dev/null
  local r0=$SECONDS
  wait_until 30 1 redis_ok
  wait_until 240 4 sweep_finished "$SW_ID"; local fin=$?
  REC[4]=$((SECONDS - r0))
  info "$(sweep_resumo "$SW_ID" 2>/dev/null)"
  sleep 6; local dlq1; dlq1=$(dlq_count); info "DLQ depois: $dlq1 mensagem(ns)"
  local acc; acc=$(sweep_accounted "$SW_ID")
  local detail="DLQ $dlq0 -> $dlq1; varredura concluida=$([ $fin -eq 0 ] && echo sim || echo nao) (contabilizados ${acc:-?}/15)"
  if [ "$dlq1" != "?" ] && [ "$dlq0" != "?" ] && [ "$dlq1" -le "$dlq0" ] && [ $fin -eq 0 ]; then veredito 4 PASS "$detail"
  else veredito 4 FAIL "$detail"; fi
}

# ================================================================ EXPERIMENTO 5
X_OFFSET=0
fresh_body() {  # data nova a cada chamada: nunca atinge o cache TTL (300 s) da Xotelo
  # contador em arquivo: $(...) roda em subshell e perderia um contador em variavel
  X_OFFSET=$(( $(cat "$X_CF") + 1 )); echo "$X_OFFSET" > "$X_CF"
  local ci co; ci=$(date -d "+$((X_BASE + X_OFFSET)) days" +%F); co=$(date -d "+$((X_BASE + X_OFFSET + 2)) days" +%F)
  printf '{"hotel_key":"%s","checkin_date":"%s","checkout_date":"%s"}' "$HK" "$ci" "$co"
}
dispatch_fresh() {  # imprime "gateway_status source"
  api -m 40 -X POST "$GW/jobs/dispatch" -H 'Content-Type: application/json' -d "$(fresh_body)" \
    | pyj '"%s %s" % (d.get("gateway_status",""), d["result"]["source"] if "result" in d else d.get("detail",""))'
}
dispatch_is_live() { local o; o=$(dispatch_fresh); info "dispatch -> $o"; [ "$o" = "SUCCESS live" ]; }
exp5() {
  step "EXPERIMENTO 5 - ${NAMES[5]}"
  hip "${HIPS[5]}"
  login
  HK=$(api "$GW/hotels?limit=1" | pyj 'd[0]["hotel_key"]')
  [ -n "$HK" ] || { veredito 5 FAIL "catalogo de hoteis vazio ou inacessivel"; return; }
  X_BASE=$((60 + RANDOM % 120)); X_CF=$(mktemp); echo 0 > "$X_CF"
  obs "pre-condicao: consulta ao vivo do hotel $HK (grava a cotacao no banco para o fallback)"
  local o; o=$(dispatch_fresh); info "dispatch -> $o"
  if [ "$o" != "SUCCESS live" ]; then veredito 5 FAIL "pre-condicao falhou ($o): Xotelo real inacessivel antes da injecao?"; return; fi
  inj "docker exec -u root <worker> -> /etc/hosts: 127.0.0.1 data.xotelo.com (nos 3 workers; sem privilegios no host)"
  local c
  for c in "${WORKERS[@]}"; do
    docker exec -u root "$c" sh -c 'echo "127.0.0.1 data.xotelo.com" >> /etc/hosts' >/dev/null 2>&1 \
      || warn "nao foi possivel editar /etc/hosts em $c"
  done
  obs "abrindo o breaker: dispatches pelo gateway com datas novas ate os workers reportarem breaker aberto (limiar 5 falhas por worker)"
  local p n opened=0 st
  n=0
  while [ $n -lt 30 ] && [ $opened -lt 3 ]; do
    n=$((n + 1)); dispatch_fresh >/dev/null
    opened=0
    for p in 8001 8002 8003; do
      st=$(curl -s -m 5 "${WORKER_HEALTH_BASE:-http://localhost}:$((p + ${WORKER_PORT_OFFSET:-0}))/health" | pyj 'd.get("xotelo_breaker",{}).get("state","?")')
      [ "$st" = "open" ] && opened=$((opened + 1))
    done
  done
  info "breakers abertos: $opened/3 apos $n dispatches"
  local deg; deg=$(dispatch_fresh); info "dispatch pelo gateway com Xotelo fora -> $deg"
  local t_block=$SECONDS
  rec "removendo a linha de /etc/hosts e esperando o breaker (recovery 30 s) voltar a live"
  unblock_xotelo
  local r0=$SECONDS; sleep 31
  local back=1
  wait_until 90 5 dispatch_is_live && back=0
  REC[5]=$((SECONDS - r0))
  local detail="breakers abertos=$opened/3; durante o bloqueio='$deg'; volta a live=$([ $back -eq 0 ] && echo sim || echo nao)"
  if [ $opened -ge 1 ] && [ "$deg" = "DEGRADED cache" ] && [ $back -eq 0 ]; then veredito 5 PASS "$detail"
  else veredito 5 FAIL "$detail"; fi
}

# ================================================================ EXPERIMENTO 6
exp6() {
  step "EXPERIMENTO 6 - ${NAMES[6]}"
  hip "${HIPS[6]}"
  login; start_sweep
  [ "$SW_CODE" = 202 ] || { veredito 6 FAIL "POST /sweeps devolveu HTTP $SW_CODE"; return; }
  info "varredura $SW_ID enfileirada"
  sleep 3; info "antes da queda: $(sweep_resumo "$SW_ID")"
  inj "docker stop hospeda_db (15 s)"
  docker stop -t 2 hospeda_db >/dev/null
  local t0=$SECONDS up=0 total=0
  obs "durante a queda: /health do gateway deve continuar respondendo"
  while [ $((SECONDS - t0)) -lt 15 ]; do
    total=$((total + 1))
    if curl -sf --retry "${GW_PROBE_RETRIES:-0}" --retry-all-errors -m "${GW_OK_TIMEOUT:-3}" "$GW/health" >/dev/null; then up=$((up + 1)); fi
    sleep 2
  done
  info "/health respondeu $up/$total vezes durante a queda"
  rec "docker start hospeda_db"
  docker start hospeda_db >/dev/null
  local r0=$SECONDS
  wait_until 60 2 pg_ok
  wait_until 240 4 sweep_finished "$SW_ID"; local fin=$?
  REC[6]=$((SECONDS - r0))
  info "$(sweep_resumo "$SW_ID")"
  local done_n regs uniq
  done_n=$(sweep_json "$SW_ID" | pyj 'd["done"]')
  read -r regs uniq <<<"$(hist_sweep "$SW_ID")"
  info "resultados ok=$done_n  linhas gravadas em job_history: registros=$regs unicos=$uniq"
  local detail="/health $up/$total; varredura concluida=$([ $fin -eq 0 ] && echo sim || echo nao); ok=$done_n gravados=$regs unicos=$uniq"
  if [ "$up" = "$total" ] && [ $fin -eq 0 ] && [ "$regs" = "$done_n" ] && [ "$regs" = "$uniq" ]; then veredito 6 PASS "$detail"
  else veredito 6 FAIL "$detail"; fi
}

# ================================================================ principal
for tool in docker curl python3; do
  command -v "$tool" >/dev/null || { echo "ferramenta ausente: $tool"; exit 1; }
done
gw_ok || { echo "gateway indisponivel em $GW (docker compose up --build -d)"; exit 1; }

SEL=("$@"); [ ${#SEL[@]} -eq 0 ] && SEL=(all)
RUN=()
for a in "${SEL[@]}"; do
  case "$a" in
    all) RUN=(1 2 3 4 5 6);;
    [1-6]) RUN+=("$a");;
    *) echo "argumento invalido: $a (use all ou 1..6)"; exit 1;;
  esac
done

login
step "Estado inicial"
estabilizar
for n in "${RUN[@]}"; do
  "exp$n"
  step "Restaurando ambiente apos o experimento $n"
  restore_all
  estabilizar
done

# ---------------------------------------------------------------- tabela-resumo
TABLE=$(
  printf '| # | Experimento | Hipotese | Resultado | Recuperacao (s) |\n|---|---|---|---|---|\n'
  for n in "${RUN[@]}"; do
    printf '| %s | %s | %s | %s | %s |\n' "$n" "${NAMES[$n]}" "${HIPS[$n]}" "${RES[$n]}" "${REC[$n]}"
  done
)
step "TABELA-RESUMO"
printf '%-3s %-28s %-8s %-15s %s\n' "#" "Experimento" "Resultado" "Recuperacao(s)" "Hipotese"
FAILS=0
for n in "${RUN[@]}"; do
  col=$R; [ "${RES[$n]}" = PASS ] && col=$G
  [ "${RES[$n]}" = PASS ] || FAILS=$((FAILS + 1))
  printf '%-3s %-28s %s%-9s%s %-15s %s\n' "$n" "${NAMES[$n]}" "$col" "${RES[$n]}" "$N" "${REC[$n]}" "${HIPS[$n]}"
done

if [ "${SAVE:-0}" = 1 ]; then
  OUT="${EVIDENCE_FILE:-$ROOT/docs/relatorio/evidencias/chaos_$(date +%F).md}"
  mkdir -p "$(dirname "$OUT")"
  { printf '# Experimentos de caos - %s\n\nGerado por `scripts/chaos_demo.sh` (%s).\n\n' "$(date +%F)" "$(date '+%F %T')"
    printf '%s\n' "$TABLE"; } > "$OUT"
  info "tabela gravada em $OUT"
fi
exit $((FAILS > 0 ? 1 : 0))
