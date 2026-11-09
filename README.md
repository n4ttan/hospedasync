# HospedaSync

Plataforma distribuída de monitoramento de tarifas hoteleiras. Coleta cotações reais de hotéis do Rio de Janeiro, distribui o trabalho entre três workers coordenados, grava o histórico em PostgreSQL com réplica e mostra tudo em um painel web autenticado.

Trabalho da disciplina de Sistemas Distribuídos. Documentação de apoio:

| Documento | Conteúdo |
|---|---|
| `docs/relatorio/arquitetura.md` | Estilo arquitetural, diagramas (componentes, fluxos, eleição, replicação), tabela de comunicação e esquema de nomes |
| `docs/relatorio/matriz-requisitos.md` | Cada requisito do enunciado, o código que o atende, como comprovar e o status (com as lacunas) |
| `docs/relatorio/roteiro-apresentacao.md` | Roteiro da defesa oral e perguntas prováveis |
| `docs/notas/` | Decisões e evidências por tema (coordenação, mensageria, replicação, falhas, fonte de dados) |
| `docs/seguranca/` | Matriz de autorização, modelo de ameaças e TLS |
| `docs/felipe/` | Experimento de consistência e resultados |

---

## Visão geral

O sistema segue o estilo de **microsserviços com API Gateway e pool de workers**, com **fluxo orientado a eventos** pela fila RabbitMQ.

* **Nginx (`deploy/nginx/nginx.conf`):** única porta pública (80 redireciona para 443). Termina TLS, aplica limite de requisições e cabeçalhos de segurança.
* **API Gateway (`api-gateway/`):** FastAPI. Autentica (JWT), autoriza por papel, resolve o hotel no catálogo, despacha cotações síncronas com lock e failover entre workers, publica varreduras na fila, consome os resultados e grava no PostgreSQL. Serve o painel (`api-gateway/static/index.html`).
* **Workers (`workers/`, `worker-01` a `worker-03`):** consultam a Xotelo, consomem jobs da fila, disputam locks por hotel, elegem um líder (Bully) e o líder agenda varreduras periódicas.
* **RabbitMQ:** filas de jobs, resultados e dead-letter; exchange fanout para eventos do cluster.
* **Redis:** locks por hotel (`SET NX EX`), contadores de varredura e cache compartilhado de cotações.
* **PostgreSQL 15:** primário e réplica por streaming replication. Leitura forte no primário e eventual na réplica.

### Fonte de dados real

Os preços vêm da API **Xotelo** (`https://data.xotelo.com/api`), que devolve a diária por agência (Booking.com, Trip.com, Agoda e outras) com base nos dados do TripAdvisor. Não há simulação de preço. O catálogo de 100 hotéis do Rio fica em `api-gateway/data/hotels_rio.json` e é carregado no banco na primeira subida (`api-gateway/hotels.py`). Cada hotel é identificado pelo `hotel_key` do TripAdvisor (por exemplo `g303506-d1200117`). Motivos da escolha, alternativas descartadas e riscos: `docs/notas/2026-11-09-fonte-de-dados-real.md`.

Se a Xotelo ou todos os workers ficarem indisponíveis, o gateway devolve a última cotação gravada, marcada `source=cache`, `stale=true` e `gateway_status=DEGRADED` (`api-gateway/main.py:444-456`).

### Dois caminhos de coleta

1. **Síncrono:** `POST /jobs/dispatch`. O gateway pega o lock do hotel, escolhe um worker, tenta até 3 em caso de falha e responde na mesma chamada.
2. **Assíncrono:** `POST /sweeps` (ou o líder, a cada `SWEEP_INTERVAL_SECONDS`). Publica um job por hotel e data na fila; os workers processam em paralelo e o gateway grava os resultados de forma idempotente.

---

## Stack tecnológica

| Bloco | Tecnologia |
|---|---|
| Linguagem e framework | Python 3.11, FastAPI, Uvicorn, SQLAlchemy |
| Comunicação síncrona | REST/JSON com `httpx` assíncrono |
| Mensageria | RabbitMQ 3.13 com `aio-pika` (fila durável, DLX, fanout) |
| Lock e cache | Redis 7 (`SET NX EX` e script Lua) |
| Banco | PostgreSQL 15 com streaming replication |
| Coordenação | Eleição Bully, relógio de Lamport (`shared/lamport.py`) |
| Segurança | Nginx com TLS, JWT (PyJWT), Argon2, HMAC-SHA256 entre nós |
| Virtualização | Docker e Docker Compose |
| Nuvem | AWS EC2 (planejada; ver "Implantação em nuvem") |

---

## Como executar

Requisitos: Docker com o plugin Compose, `openssl`, `curl` e `python3`.

```bash
bash scripts/gen_env.sh              # cria .env com segredos aleatórios (não sobrescreve)
bash scripts/gen_selfsigned_cert.sh  # certificado autoassinado em deploy/nginx/certs/
docker compose up --build -d
docker compose ps                    # aguarde todos "healthy" (cerca de 30 s)
```

Acesso:

* **https://localhost** (porta 443): painel e API pelo Nginx. O navegador avisa sobre o certificado autoassinado; aceite o aviso. `http://localhost` redireciona para HTTPS.
* **http://localhost:8000**: gateway direto, publicado **somente em loopback** (`127.0.0.1`), para scripts e diagnóstico. Os workers (8001 a 8003), Postgres (5432, 5433), Redis (6379) e RabbitMQ (5672, 15672) também só escutam em `127.0.0.1`.
* **Login:** usuário e senha do `.env` (`ADMIN_USER`, padrão `admin`, e `ADMIN_PASSWORD`). Para ver a senha: `grep ADMIN_ .env`.

Obter um token para usar com `curl`:

```bash
set -a; . ./.env; set +a
TOKEN=$(curl -sk -X POST https://localhost/auth/login -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -sk https://localhost/health
curl -sk -H "Authorization: Bearer $TOKEN" "https://localhost/hotels?q=copacabana&limit=3"
```

Cotação síncrona:

```bash
curl -sk -X POST https://localhost/jobs/dispatch -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"hotel_name":"Copacabana Palace","checkin_date":"2026-12-18","checkout_date":"2026-12-20"}'
```

Varredura assíncrona e progresso:

```bash
curl -sk -X POST https://localhost/sweeps -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"top_n":5,"date_offsets":[21,28]}'
curl -sk -H "Authorization: Bearer $TOKEN" https://localhost/sweeps/<sweep_id>
```

Para parar: `docker compose down` (mantém os volumes). `docker compose down -v` apaga os dados, inclusive a réplica.

### Variáveis do `.env`

`POSTGRES_PASSWORD`, `REPLICATOR_PASSWORD`, `REDIS_PASSWORD`, `RABBITMQ_USER`, `RABBITMQ_PASS`, `JWT_SECRET`, `JWT_TTL_MINUTES`, `SERVICE_SECRET`, `ADMIN_USER`, `ADMIN_PASSWORD`, `SWEEP_INTERVAL_SECONDS`. O compose recusa subir sem os obrigatórios. O `.env` não é versionado (`.gitignore`); `.env.example` mostra o formato com valores falsos.

---

## Mapa de pastas

```
api-gateway/          Gateway FastAPI
  main.py               rotas, dispatch com lock e failover, histórico, coordenação, métricas
  auth.py               login JWT, papéis, limite de tentativas, HMAC de serviço
  sweeps.py             POST /sweeps e consumidor idempotente de resultados
  hotels.py             catálogo (seed e refresh pela Xotelo)
  database.py           modelos, primário e réplica, roteamento de consistência
  data/hotels_rio.json  catálogo de 100 hotéis
  static/index.html     painel web
workers/              Worker FastAPI
  main.py               /scrape, heartbeat assinado, tarefas de background
  election.py           Bully, Lamport, /election /coordinator /leader /events
  consumer.py           consumo da fila com lock por hotel
  scheduler.py          varredura periódica (só o líder) e fanout de eventos
  scraper.py            cliente Xotelo: retry, circuit breaker, semáforo, cache
shared/               código comum aos dois serviços
  distlock.py  lamport.py  mq.py  resilience.py  servicetoken.py
deploy/
  nginx/nginx.conf      TLS, rate limit, cabeçalhos
  postgres/             criação do usuário replicador e entrypoint da réplica
scripts/              demonstrações, experimentos e utilitários (ver abaixo)
tests/                testes automatizados (pytest)
docs/                 relatório, notas, segurança
docker-compose.yml    9 serviços, rede hospeda-net, volumes, healthchecks
```

---

## Testes automatizados

Os testes usam um Redis próprio na porta **6380** (separado do Redis da aplicação) e SQLite em memória. Os testes que dependem de Redis são ignorados (skip) se ele não responder.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt -r api-gateway/requirements.txt -r workers/requirements.txt
docker run -d --name redis-teste -p 6380:6379 redis:7-alpine   # ou: redis-server --port 6380
pytest -q
```

A configuração está em `pytest.ini` e `tests/conftest.py` (`REDIS_URL` padrão `redis://localhost:6380/0`). A suíte cobre lock, eleição, dispatch com exclusão mútua e failover, varreduras idempotentes, consumidor da fila, scraper, circuit breaker, autenticação, autorização e roteamento de consistência.

---

## Scripts de demonstração

Todos fazem login sozinhos (`scripts/_auth.sh`, com `ADMIN_USER` e `ADMIN_PASSWORD` do `.env`). Usam `GATEWAY_URL` (padrão `http://localhost:8000`). Os que derrubam contêineres devem ser rodados apenas em ambiente de demonstração.

| Script | O que mostra | Derruba contêineres |
|---|---|---|
| `scripts/demo_coordenacao.sh` | Líder atual, queda do líder e reeleição, dois jobs simultâneos no mesmo hotel (200 e 409), retorno do worker de maior ID | Sim (worker-03) |
| `scripts/demo_fila_falha.sh` | Varredura de 15 jobs pela fila que termina mesmo com a queda do líder e de outro worker, sem duplicar resultados | Sim |
| `scripts/consistency_experiment.sh` | Leitura obsoleta, latência de escrita e comportamento com a réplica parada, nos modos assíncrono e forte. Precisa de `PYTHON` com `psycopg2`. Gera `docs/felipe/resultados_consistencia.md` | Sim (para a réplica temporariamente) |
| `scripts/chaos_demo.sh` | 6 experimentos de falha (worker, líder, RabbitMQ, Redis, Xotelo, Postgres). `SAVE=1` grava `docs/relatorio/evidencias/chaos_<data>.md` | Sim |

Outros utilitários: `scripts/gen_env.sh`, `scripts/gen_selfsigned_cert.sh [IP] [DNS]`.

---

## Comunicação entre serviços

Resumo; o detalhe (origem, destino, protocolo, formato, autenticação) está em `docs/relatorio/arquitetura.md`.

* Cliente para Nginx: HTTPS. Nginx para gateway: HTTP.
* Gateway para worker: REST (`POST /scrape`, `httpx`, timeout de 15 s) com o cabeçalho `X-Lamport`.
* Worker para gateway: heartbeat a cada 5 s em `POST /registry/register`, assinado com HMAC. O worker some do registry após 15 s sem heartbeat.
* Worker para worker: `POST /election`, `POST /coordinator`, `GET /health`.
* Gateway e workers para RabbitMQ: AMQP. Jobs em `hospeda.scrape.jobs`, resultados em `hospeda.scrape.results`, rejeitados em `hospeda.scrape.dead`, eventos em fanout `hospeda.cluster.events`.

### Endpoints principais do gateway

| Método e caminho | Acesso | Função |
|---|---|---|
| `GET /health` | público | Estado do gateway, Redis e RabbitMQ |
| `POST /auth/login` | público | Emite o JWT |
| `GET /hotels?q=` | analyst | Busca no catálogo |
| `POST /jobs/dispatch` | analyst | Cotação síncrona (`409` se o hotel está em processamento; `404` sem tarifas; `503` sem worker e sem cache) |
| `POST /sweeps`, `GET /sweeps/{id}` | analyst | Varredura assíncrona e progresso |
| `GET /history?consistency=strong\|eventual` | analyst | Histórico; headers `X-Consistency`, `X-Served-By`, `X-Replication-Lag-Ms` |
| `GET /consistency/status` | analyst | Lag e estado da réplica |
| `GET /registry/nodes` | analyst | Workers vivos |
| `GET /locks`, `GET /coordination/status`, `GET /coordination/events`, `GET /metrics`, `POST /hotels/refresh`, `POST /auth/users` | admin | Diagnóstico, eventos em ordem de Lamport, métricas, administração |
| `POST /registry/register`, `GET /internal/hotels` | serviço (HMAC) | Heartbeat dos workers; catálogo para o agendador |

Matriz completa: `docs/seguranca/matriz-autorizacao.md`.

### Falhas na comunicação síncrona

O dispatch tenta até `MAX_DISPATCH_ATTEMPTS` (3) workers; cada worker tem um circuit breaker no gateway (3 falhas abrem, recuperação em 20 s). Se nenhum responde, usa o cache do banco; sem cache, responde `503` (`api-gateway/main.py:404-456`).

---

## Segurança

* **Cifra em trânsito:** TLS 1.2/1.3 no Nginx, redirecionamento de 80 para 443, HSTS, CSP e demais cabeçalhos (`deploy/nginx/nginx.conf`). O tráfego interno entre contêineres não é cifrado.
* **Autenticação de usuários:** login em `POST /auth/login`; senha guardada com Argon2; JWT HS256 de 30 min (`JWT_TTL_MINUTES`); no máximo 5 falhas por minuto por IP e usuário, e 5 requisições por minuto no login no Nginx.
* **Autorização:** papéis `analyst` e `admin` por rota; o papel é lido do banco a cada requisição (`api-gateway/auth.py:119-145`).
* **Autenticação entre nós:** o registro de worker e a consulta do catálogo pelo agendador (`GET /internal/hotels`) exigem assinatura HMAC-SHA256 de identidade, recurso e horário, com janela de 60 s (`shared/servicetoken.py`).
* **Segredos:** gerados por `scripts/gen_env.sh`, ficam no `.env` (fora do git). Redis, RabbitMQ e Postgres exigem senha.
* **Exposição de rede:** apenas as portas 80 e 443 são públicas.
* **Limites conhecidos:** as rotas `/scrape`, `/election` e `/coordinator` dos workers não têm autenticação; não há TLS interno; o segredo do JWT é único e não há revogação de token individual. Detalhe em `docs/seguranca/matriz-autorizacao.md`.

Certificado válido (Let's Encrypt) para a AWS: `docs/seguranca/tls-letsencrypt.md`.

---

## Tolerância a falhas

Detecção: heartbeat (5 s / 15 s), health do líder (3 s, 2 falhas), healthchecks do compose. Recuperação: reeleição Bully, reentrega da fila com ack tardio, `restart: unless-stopped`, retry com backoff, circuit breaker, fallback para cache e gravação idempotente. Detalhe e lacunas: `docs/notas/2026-11-09-tolerancia-a-falhas.md` e `docs/relatorio/matriz-requisitos.md`.

Lacunas assumidas: Redis, RabbitMQ e gateway em instância única; PostgreSQL sem failover automático; DLQ sem consumidor; Bully sem quórum.

---

## Replicação e consistência

O gateway é o escritor único do primário. A réplica assíncrona atende leituras `eventual`; o painel usa `strong` (primário). Experimento com N=50: 49/50 leituras obsoletas no modo assíncrono, 0/50 no forte (`remote_apply`), com escrita média de 0,80 ms contra 1,47 ms (`docs/felipe/resultados_consistencia.md`). Análise CAP e PACELC: `docs/notas/2026-11-09-replicacao-consistencia.md`. A promoção da réplica é manual.

---

## Coordenação (eleição de líder e exclusão mútua)

Implementada em `workers/election.py` (eleição), `shared/distlock.py` (lock distribuído) e `shared/lamport.py` (relógio lógico).

### O que cada mecanismo resolve

* **Eleição de líder (Bully):** os três workers concordam sobre qual deles é o coordenador. Se o líder cai, os demais detectam a queda e elegem outro sem intervenção manual. O líder é o agendador das varreduras periódicas.
* **Exclusão mútua (lock no Redis):** impede que duas requisições, síncronas ou vindas da fila, processem o mesmo hotel ao mesmo tempo, mesmo em workers diferentes.
* **Relógio de Lamport:** ordena os eventos de todos os nós. `GET /coordination/events` junta os logs ordenados por Lamport.

### Como funciona o Bully

* **IDs:** o ID numérico é o número no fim de `WORKER_ID` (`worker-01` vira 1, `worker-03` vira 3). Sem esse sufixo, usa o hostname de `SELF_URL`. Quanto maior o ID, maior a prioridade.
* **Pares conhecidos:** cada worker recebe os demais em `PEER_WORKERS` (formato `url` ou `id=url`).
* **Início da eleição:** 3 s após subir, cada worker inicia uma eleição. Envia `POST /election` apenas aos pares de ID maior. Se algum responde `OK`, esse par assume a eleição. Se nenhum responde, o worker se proclama líder.
* **`POST /coordinator`:** o novo líder avisa todos os pares com `leader_id`, `term` e `lamport`.
* **`GET /leader`:** cada worker informa em quem ele acredita (`leader_id`) e se ele próprio é o líder (`am_i_leader`).
* **Detecção de queda:** a cada 3 s, cada worker que não é líder consulta `GET /health` do líder. Após 2 falhas consecutivas (timeout de 2 s por tentativa), inicia nova eleição.
* **Term por timestamp:** o `term` é `time.time()` no momento da eleição. Um contador reiniciaria a cada restart do processo, e um líder que acabou de voltar seria descartado como obsoleto.

### Como funciona o lock

* **Aquisição:** `SET lock:hospedasync:<hotel_key> <dono> NX EX <ttl>`. A chave só é criada se não existir (NX) e expira sozinha (EX): 30 s no dispatch síncrono (`api-gateway/main.py:392`) e 60 s no consumidor da fila (`workers/consumer.py:11`).
* **Conflito:** no dispatch, `409` sem chamar nenhum worker. No consumidor, o job é republicado com o cabeçalho `x-lock-conflicts`, até 30 vezes.
* **Liberação atômica:** script Lua compara o valor guardado com o dono e só então faz `DEL`. Um job lento não apaga um lock que já expirou e foi readquirido por outro.
* **Dono do lock:** `job_id` no dispatch; `worker-NN:job_id` no consumidor.

### Limitações

* **O líder não despacha trabalho síncrono.** Ele agenda as varreduras, mas o gateway escolhe o worker do `/jobs/dispatch` por sorteio entre os registrados. A eleição não influencia esse roteamento.
* **Redis de instância única.** Sem Redlock nem réplicas: se o Redis cair, o dispatch deixa de funcionar e os jobs da fila são republicados com backoff até 10 vezes, sem ir para a DLQ.
* **TTL fixo.** Um job que ultrapasse o TTL perde a exclusão antes de terminar.
* **Sem quórum no Bully.** Numa partição de rede, cada lado pode eleger seu próprio líder. A gravação idempotente evita dano nos dados, mas a varredura periódica pode rodar em duplicidade.

### Endpoints de coordenação

| Serviço | Método e caminho | Função |
|---|---|---|
| Worker | `POST /election` | Recebe pedido de eleição; responde `OK` se o ID for maior que o do candidato |
| Worker | `POST /coordinator` | Recebe o anúncio do novo líder (`leader_id`, `term`) |
| Worker | `GET /leader`, `GET /events` | Líder em que o nó acredita; eventos locais com Lamport |
| Gateway | `GET /locks`, `GET /locks/{hotel}` | Locks ativos (admin) |
| Gateway | `GET /coordination/status` | Visão de cada worker sobre o líder, `consensus` e locks ativos (admin) |
| Gateway | `GET /coordination/events` | Eventos de todos os nós em ordem de Lamport (admin) |

### Roteiro de demonstração

Com o sistema no ar, aguarde cerca de 10 s para a primeira eleição. O script `scripts/demo_coordenacao.sh` automatiza os passos abaixo (os `curl` precisam de `Authorization: Bearer $TOKEN` com usuário admin).

1. Ver o líder. Esperado: `leader_id` 3 e `consensus` true.
   ```bash
   curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/coordination/status
   ```
2. Derrubar o líder: `docker stop hospeda_worker_03`.
3. Em 10 a 15 s, o worker-02 assume (`leader_id` 2). Consultar de novo o passo 1 e `curl -s http://localhost:8002/leader`.
4. Disparar dois jobs simultâneos no mesmo hotel. Um retorna `200` e o outro `409`. Repita se os dois passarem (a cotação leva alguns segundos, então o conflito é provável).
   ```bash
   BODY='{"hotel_name":"Copacabana Palace","checkin_date":"2026-12-20","checkout_date":"2026-12-23"}'
   for i in 1 2; do
     curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/jobs/dispatch \
       -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "$BODY" &
   done; wait
   ```
5. Religar: `docker start hospeda_worker_03`. Em cerca de 5 s ele volta a ser o líder.

---

## Implantação em nuvem (AWS)

Estado real: o repositório contém apenas o ambiente Docker Compose; não há templates de infraestrutura (Terraform ou CloudFormation). Uma versão inicial do projeto (gateway e um worker) foi publicada em uma EC2 t3.micro em sa-east-1. **A versão atual, com três workers, filas, réplica e Nginx, ainda não foi implantada na AWS em múltiplas instâncias.**

Plano para a implantação: instância EC2 com Docker e Compose; Security Group liberando 80 e 443 de qualquer origem e 22 apenas do IP do administrador; nenhuma outra porta aberta (os serviços internos já escutam só em loopback); certificado conforme `docs/seguranca/tls-letsencrypt.md`. Para distribuir os workers em instâncias distintas, o Bully aceita `PEER_WORKERS` no formato `id=url` com IPs (`workers/election.py:36-41`), o que ainda precisa ser exercitado.
