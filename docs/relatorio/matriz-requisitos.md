# Matriz de requisitos do enunciado

Rastreabilidade entre cada requisito de Sistemas Distribuídos, a implementação, o ponto exato do código e a forma de comprovar. Os números de linha foram conferidos no código da branch `feature/messaging`.

Legenda de status: **Atendido** (implementado e verificável), **Parcial** (implementado com limitação declarada), **Pendente** (não feito).

Convenção dos comandos: `GW=http://localhost:8000`; os `curl` autenticados usam `-H "Authorization: Bearer $TOKEN"`. Os scripts em `scripts/` fazem o login sozinhos (`scripts/_auth.sh`) usando `ADMIN_USER` e `ADMIN_PASSWORD` do `.env`.

## 1. Arquitetura

| Requisito | Como foi atendido | Código | Como comprovar | Status |
|---|---|---|---|---|
| Estilo arquitetural justificado | Microsserviços com API Gateway e 3 workers, com fluxo orientado a eventos pela fila (RabbitMQ). Justificativa em `docs/relatorio/arquitetura.md`, seção 1 | `docker-compose.yml:91` (gateway), `:126`, `:160`, `:194` (workers) | `docker compose ps` mostra 9 contêineres independentes; `docs/relatorio/arquitetura.md` | Atendido |
| Middleware de comunicação | RabbitMQ (AMQP) para a via assíncrona e REST/HTTP (FastAPI + httpx) para a síncrona; Nginx como borda | `shared/mq.py:41-53`, `api-gateway/main.py:412-414`, `deploy/nginx/nginx.conf` | `curl -sk https://localhost/health`; painel do RabbitMQ em `http://localhost:15672` (loopback) | Atendido |

## 2. Processos e virtualização

| Requisito | Como foi atendido | Código | Como comprovar | Status |
|---|---|---|---|---|
| Contêineres | Dockerfiles do gateway e dos workers; `docker-compose.yml` com 9 serviços, rede `hospeda-net`, volumes nomeados e `restart: unless-stopped` | `api-gateway/Dockerfile`, `workers/Dockerfile`, `docker-compose.yml:2-249` | `docker compose up --build -d && docker compose ps` | Atendido |
| Concorrência async | FastAPI/Uvicorn com corrotinas; `httpx.AsyncClient`, `aio_pika`, `asyncio.gather` nas consultas aos pares; `asyncio.create_task` para heartbeat, eleição, consumidor e agendador | `workers/main.py:42-49`, `api-gateway/main.py:149-157`, `workers/election.py:175-180` | Logs `docker compose logs worker-01`; `GET /coordination/status` consulta os 3 workers em paralelo | Atendido |
| Concorrência com threads | Chamadas bloqueantes do Redis são enviadas para a thread pool com `asyncio.to_thread`; `LamportClock` e `CircuitBreaker` usam `threading.Lock`; semáforo limita 3 chamadas simultâneas à Xotelo | `workers/consumer.py:65`, `workers/scraper.py:48-52`, `:148`, `shared/lamport.py:21`, `shared/resilience.py:21` | `pytest tests/test_scraper.py tests/test_resilience.py` | Atendido |
| Um processo Uvicorn por contêiner | Cada serviço roda um único processo Uvicorn (sem múltiplos workers de processo) | `api-gateway/Dockerfile`, `workers/Dockerfile` | Leitura dos Dockerfiles | Parcial: o paralelismo vem dos 3 workers e do event loop, não de vários processos por contêiner |

## 3. Comunicação

| Requisito | Como foi atendido | Código | Como comprovar | Status |
|---|---|---|---|---|
| RPC via REST | Gateway chama `POST /scrape` do worker (JSON); workers chamam `POST /registry/register`, `/election`, `/coordinator` | `api-gateway/main.py:412-414`, `workers/main.py:86`, `:65-79`, `workers/election.py:93-114` | `curl -X POST $GW/jobs/dispatch ...` (ver `docs/relatorio/roteiro-apresentacao.md`) | Atendido |
| gRPC | Não utilizado; REST foi a escolha (justificada em `arquitetura.md`). O enunciado aceita REST ou gRPC | n/a | n/a | Atendido por REST (gRPC não implementado) |
| Mensageria por fila | Filas duráveis `hospeda.scrape.jobs` e `hospeda.scrape.results`, mensagens persistentes, `prefetch=1`, ack tardio, reentrega se o worker morrer | `shared/mq.py:41-65`, `workers/consumer.py:53-107`, `:128` | `bash scripts/demo_fila_falha.sh`; `POST /sweeps` seguido de `GET /sweeps/<id>` | Atendido |
| Tópico/broadcast | Exchange `fanout` `hospeda.cluster.events` com eventos `leader_elected` e `sweep_scheduled` recebidos por todos os workers | `shared/mq.py:52`, `workers/scheduler.py:33-37`, `:80-82`, `:106-133` | `docker compose logs worker-01 \| grep cluster_event` | Atendido (fanout; não há roteamento por tópico com wildcard) |
| Dead-letter | Fila `hospeda.scrape.dead` ligada à fila de jobs por `x-dead-letter-exchange`; `nack(requeue=False)` envia para lá | `shared/mq.py:43-50`, `workers/consumer.py:108-113` | Painel do RabbitMQ, fila `hospeda.scrape.dead` | Parcial: **a DLQ não tem consumidor**; mensagens só são inspecionadas |
| Resultado idempotente | `job_id` é chave primária; `SADD sweep:<id>:seen` evita recontar | `api-gateway/database.py:32`, `api-gateway/sweeps.py:194-236` | `pytest tests/test_sweeps.py`; experimento 1 de `chaos_demo.sh` (job_ids únicos) | Atendido |

## 4. Coordenação

| Requisito | Como foi atendido | Código | Como comprovar | Status |
|---|---|---|---|---|
| Relógio lógico | Lamport (`tick` em evento local/envio, `max(L, recebido)+1` no recebimento). Propaga em `X-Lamport` (HTTP) e no campo `lamport` (eleição e mensagens). Log fundido em ordem de Lamport | `shared/lamport.py:18-73`, `api-gateway/main.py:411-422`, `workers/main.py:88-96`, `api-gateway/main.py:172-195` | `curl -H "Authorization: Bearer $TOKEN" $GW/coordination/events` (perfil admin) | Atendido. Limite: L(A)<L(B) não prova causalidade (exigiria relógio vetorial) |
| Relógio físico | `time.time()` usado como `term` da eleição e em `wall_time` dos eventos (desempate). Sem sincronização NTP própria | `workers/election.py:161`, `shared/lamport.py:58`, `:73` | `curl $GW/coordination/events` mostra `wall_time` | Parcial: relógio físico só é usado para term/desempate; não há algoritmo de sincronização (Cristian/Berkeley) |
| Eleição de líder | Bully: ID pelo sufixo de `WORKER_ID`, `POST /election`, `POST /coordinator`, `term` por timestamp, detecção por health a cada 3 s com 2 falhas | `workers/election.py:18-21`, `:93-114`, `:127`, `:154`, `:205-232` | `bash scripts/demo_coordenacao.sh`; `pytest tests/test_election.py` | Atendido. Limite: **Bully sem quórum** (partição gera dois líderes) |
| Papel do líder | Só o líder publica varreduras periódicas e anuncia no fanout | `workers/scheduler.py:91-103` | Experimento 2 de `chaos_demo.sh` | Atendido. O líder **não** influencia o roteamento do `/jobs/dispatch` (sorteio entre workers, `api-gateway/main.py:373-377`) |
| Exclusão mútua | Lock Redis `SET NX EX` por `hotel_key`, dono = `job_id`, liberação atômica por Lua; usado no dispatch síncrono (TTL 30 s) e no consumidor (TTL 60 s) | `shared/distlock.py:14`, `:37-50`, `:53-65`, `api-gateway/main.py:392`, `workers/consumer.py:11`, `:65` | `bash scripts/demo_coordenacao.sh` (200 e 409 simultâneos); `pytest tests/test_distlock.py tests/test_dispatch_mutex.py` | Atendido. Limite: **Redis único**, sem Redlock |

## 5. Nomeação

| Requisito | Como foi atendido | Código | Como comprovar | Status |
|---|---|---|---|---|
| Esquema de nomes e identificadores | `worker-NN` (nó e ID do Bully), `job-<uuid8>`, `sweep-<uuid8>`, `lock:hospedasync:<hotel_key>`, filas `hospeda.*`, `hotel_key` do TripAdvisor `gNNNNNN-dNNNN`. Detalhe e motivos em `arquitetura.md`, seção 7 | `workers/election.py:24-52`, `api-gateway/main.py:381`, `api-gateway/sweeps.py:104`, `:112`, `shared/distlock.py:14`, `shared/mq.py:20-24`, `api-gateway/data/hotels_rio.json` | `curl -H "Authorization: Bearer $TOKEN" $GW/locks` durante um dispatch; `GET /hotels?q=copacabana` | Atendido |
| Descoberta de serviços | Service registry em memória no gateway, alimentado por heartbeat assinado a cada 5 s, expira em 15 s; DNS do Docker resolve os nomes dos serviços | `api-gateway/main.py:197-218`, `workers/main.py:65-79` | `curl -H "Authorization: Bearer $TOKEN" $GW/registry/nodes` | Parcial: registry não persiste nem é replicado (reinício do gateway zera até os heartbeats chegarem) |

## 6. Consistência e replicação

| Requisito | Como foi atendido | Código | Como comprovar | Status |
|---|---|---|---|---|
| Replicação | PostgreSQL 15 com streaming replication assíncrona: primário com `wal_level=replica`, slot, usuário `replicator`; réplica hot standby via `pg_basebackup -R` | `docker-compose.yml:2-52`, `deploy/postgres/01-replicator.sh`, `deploy/postgres/replica-entrypoint.sh` | `docker exec hospeda_db_replica psql -U postgres -tAc "select pg_is_in_recovery()"` retorna `t`; `GET /consistency/status` | Atendido. Limite: **sem failover automático** (promoção manual de réplica) |
| Modelos de consistência | Leitura forte (padrão, no primário) e eventual (`?consistency=eventual`, na réplica, com fallback e headers `X-Served-By`, `X-Replication-Lag-Ms`) | `api-gateway/main.py:278-306`, `api-gateway/database.py:96-120` | `curl -i -H "Authorization: Bearer $TOKEN" "$GW/history?consistency=eventual"`; `pytest tests/test_consistency_routing.py` | Atendido |
| Modelos testados | Experimento assíncrono x forte (`synchronous_commit=remote_apply`) x réplica parada, N=50 | `scripts/consistency_experiment.py`, `scripts/consistency_experiment.sh`, resultados em `docs/felipe/resultados_consistencia.md`, análise em `docs/notas/2026-11-09-replicacao-consistencia.md` | `PYTHON=<python com psycopg2> bash scripts/consistency_experiment.sh` | Atendido (98% de leituras obsoletas no assíncrono, 0% no forte; escrita 0,80 ms contra 1,47 ms em média) |
| Idempotência na escrita | Escritor único (gateway) no primário; chave primária `job_id` | `api-gateway/sweeps.py:168-192`, `api-gateway/main.py:458-485` | `pytest tests/test_sweeps.py` | Atendido |
| Cache coerente | Cache de cotação em duas camadas (memória + Redis, TTL 300 s) | `workers/scraper.py:63-93`, `:144-172` | `pytest tests/test_scraper.py` | Atendido (consistência eventual por TTL) |

## 7. Tolerância a falhas

| Requisito | Como foi atendido | Código | Como comprovar | Status |
|---|---|---|---|---|
| Detecção | Heartbeat do registry (5 s / expira em 15 s); health do líder (3 s, 2 falhas); healthchecks do compose; `pool_pre_ping`; `/health` do gateway informa Redis e RabbitMQ | `workers/main.py:65-79`, `workers/election.py:205-232`, `docker-compose.yml:23`, `:48`, `:61`, `:83`, `:117`, `api-gateway/main.py:116-125`, `api-gateway/database.py:14` | `curl -s $GW/health` | Atendido |
| Recuperação automática | Reeleição Bully; reentrega da fila (ack tardio); `restart: unless-stopped`; tarefas de background reiniciam após 5 s; retry de infra com backoff quando o Redis cai; consumidor do gateway reconecta | `workers/election.py:154`, `workers/consumer.py:64-78`, `workers/main.py:52-62`, `docker-compose.yml:3`, `:32`, `:57`, `:74`, `:116`, `:150`, `:184`, `:218`, `:240`, `api-gateway/sweeps.py:269-281` | `bash scripts/chaos_demo.sh all` | Atendido |
| Retry e circuit breaker | Dispatch tenta até 3 workers com breaker por worker (3 falhas, recuperação em 20 s); scraper com 3 tentativas, backoff exponencial e breaker (5 falhas, 30 s) | `api-gateway/main.py:83-93`, `:397-420`, `workers/scraper.py:96-141`, `shared/resilience.py:14-86` | `pytest tests/test_dispatch_failover.py tests/test_resilience.py`; `GET /metrics` mostra `hospeda_worker_breaker_state` | Atendido |
| Degradação controlada | Sem worker ou Xotelo fora, o gateway devolve a última cotação gravada (`DEGRADED`, `stale`, `source=cache`) | `api-gateway/main.py:339-362`, `:437-449` | Experimento 5 de `chaos_demo.sh` | Atendido |
| Experimentos de caos | 6 experimentos (matar worker, matar líder, parar RabbitMQ, parar Redis, bloquear Xotelo, parar Postgres) | `scripts/chaos_demo.sh`, nota `docs/notas/2026-11-09-tolerancia-a-falhas.md` | `SAVE=1 bash scripts/chaos_demo.sh all` | Parcial: resultados em `docs/relatorio/evidencias/chaos_<data>.md`, gerado por `scripts/chaos_demo.sh`; o arquivo só existe depois da execução |
| Redis | Instância única com senha | `docker-compose.yml:56-71` | n/a | Parcial: ponto único de falha (locks, contadores de varredura, cache). Sem Sentinel nem réplica |
| RabbitMQ | Instância única | `docker-compose.yml:73-89` | n/a | Parcial: ponto único de falha; sem cluster nem filas quorum |
| PostgreSQL | Primário + réplica | `docker-compose.yml:2-52` | n/a | Parcial: **sem failover automático** (sem Patroni/repmgr); primário fora impede escrita |
| Gateway | Instância única | `docker-compose.yml:91` | n/a | Parcial: sem segunda instância nem balanceador |

## 8. Segurança

| Requisito | Como foi atendido | Código | Como comprovar | Status |
|---|---|---|---|---|
| Criptografia em trânsito | TLS 1.2/1.3 no Nginx com redirecionamento 80 para 443, HSTS e cabeçalhos de segurança; certificado autoassinado por `scripts/gen_selfsigned_cert.sh` (Let's Encrypt documentado em `docs/seguranca/tls-letsencrypt.md`) | `deploy/nginx/nginx.conf:25`, `:35`, `:42-46`, `docker-compose.yml:228-243` | `curl -sk -I https://localhost/health`; `curl -sI http://localhost/health` retorna 301 | Atendido na borda. Tráfego interno (gateway, workers, Redis, RabbitMQ, Postgres) segue em claro na rede Docker |
| Criptografia em repouso/segredos | Senhas de usuários com Argon2; segredos em `.env` gerado por `scripts/gen_env.sh`, fora do git; Redis com senha | `api-gateway/auth.py:35`, `:64-65`, `scripts/gen_env.sh`, `docker-compose.yml:59` | `git check-ignore .env`; `git ls-files .env` vazio | Atendido. Sem criptografia de disco do banco |
| Autenticação | Login com JWT HS256 (30 min, `jti`), limite de 5 falhas por minuto por IP e usuário, Nginx com 5 req/min no login | `api-gateway/auth.py:24-27`, `:75-79`, `:91-99`, `:183-194`, `deploy/nginx/nginx.conf:15` | `curl -sk -X POST https://localhost/auth/login ...`; `pytest tests/test_auth.py` | Atendido. Sem revogação de token individual |
| Autorização | Papéis `analyst` e `admin` aplicados por `require_role`; papel lido do banco a cada requisição. Matriz em `docs/seguranca/matriz-autorizacao.md` | `api-gateway/auth.py:119-145`, `api-gateway/main.py:36-37`, `:39-40` | Chamar `/locks` com usuário `analyst` retorna 403; `pytest tests/test_gateway_authz.py` | Atendido |
| Autenticação entre nós | Heartbeat de registro e consulta do catálogo pelo agendador (`GET /internal/hotels`, `api-gateway/main.py:263-269`) assinados com HMAC-SHA256 (`worker_id`, recurso, timestamp), janela de 60 s, comparação em tempo constante | `shared/servicetoken.py:9-32`, `workers/main.py:71`, `api-gateway/main.py:198-200`, `api-gateway/auth.py:148-152` | `curl -X POST $GW/registry/register -d '{"worker_id":"x","endpoint":"http://x"}'` retorna 401 | Parcial: cobre só o registro e o catálogo interno. `/scrape`, `/election` e `/coordinator` dos workers não têm autenticação (portas publicadas apenas em `127.0.0.1`, mas expostas na rede interna) |
| Exposição de portas | Portas de dados e do gateway ligadas a `127.0.0.1`; só Nginx publica 80 e 443 | `docker-compose.yml:19`, `:41`, `:69`, `:81-82`, `:97`, `:132`, `:166`, `:200`, `:232-233` | `ss -ltn` no host | Atendido |

## 9. Entregáveis

| Entregável | Arquivo | Status |
|---|---|---|
| README | `README.md` | Atendido |
| Relatório técnico (arquitetura, diagramas, fluxos) | `docs/relatorio/arquitetura.md` | Atendido |
| Matriz de requisitos | este arquivo | Atendido |
| Roteiro de demonstração | `docs/relatorio/roteiro-apresentacao.md` | Atendido |
| Testes e resultados | `tests/` (cerca de 111 funções de teste), `docs/felipe/resultados_consistencia.md`, `docs/relatorio/evidencias/chaos_<data>.md` (gerado por `scripts/chaos_demo.sh`) | Parcial: evidências do caos dependem da execução do script |
| Implantação AWS multi-instância | `deploy/aws/` (compose por instância, `provision.sh`, `user-data.sh`) | **Atendido e verificado em 09/11/2026**: 3 EC2 (edge, workers-a, workers-b); evidências em `docs/relatorio/evidencias/aws_demo_2026-11-09.md` e `chaos_aws_2026-11-09.md` (caos: 5 de 6 PASS, o 6 pendente de repetição) |

## 10. Quadro de seleção de tecnologias

| Bloco | Opção agnóstica | Opção AWS equivalente | Escolhida | Por quê |
|---|---|---|---|---|
| Borda e balanceamento | Proxy reverso | ALB / API Gateway | **Nginx** (TLS, rate limit, redirecionamento) | Reproduzível em qualquer máquina com Docker; mesma função de um ALB com terminação TLS. Limite de requisição por IP já na borda |
| RPC síncrono | REST ou gRPC | API Gateway + Lambda/ECS | **REST com FastAPI e httpx** | JSON legível e testável por `curl`; OpenAPI automático; async nativo; gRPC traria geração de código sem ganho para chamadas de baixa frequência (uma cotação leva cerca de 4 s) |
| Mensageria | Fila e tópico | SQS + SNS / Amazon MQ | **RabbitMQ 3.13** | Fila durável, ack, prefetch, DLX e fanout no mesmo broker; roda local. Kafka seria exagero (log distribuído para milhões de eventos); SQS prenderia a demo à nuvem |
| Virtualização | Contêineres | ECS / EC2 com Docker | **Docker e Compose** | Um comando sobe os 9 serviços; mesma imagem local e na EC2 |
| Concorrência | Threads, async | n/a | **asyncio** (I/O) + **threads** (Redis síncrono, locks) | Carga é I/O lento (Xotelo); async evita uma thread por requisição |
| Exclusão mútua | Lock distribuído | ElastiCache Redis | **Redis `SET NX EX` + Lua** | Uma operação atômica com TTL; sem janela entre criar e expirar. Redlock exigiria 5 instâncias; aqui há uma só e isso é declarado como limite |
| Eleição | Bully, Raft, ZooKeeper | n/a | **Bully** | 3 nós que se conhecem; simples de demonstrar derrubando o worker-03. Raft/etcd daria quórum, mas é muito maior que o necessário |
| Relógio lógico | Lamport, vetorial | n/a | **Lamport** | Ordena eventos entre nós a baixo custo; limite conhecido e declarado |
| Nomes e IDs | UUID, DNS, registry | Route 53, Cloud Map | **UUID curto, `worker-NN`, `hotel_key` e registry próprio** | Identificadores legíveis em log; `hotel_key` reaproveita o ID do TripAdvisor, estável e único |
| Replicação | Primário-réplica, multi-primário | RDS Multi-AZ / read replica | **PostgreSQL 15 streaming replication** | Replicação nativa, permite comparar assíncrona e síncrona (`remote_apply`) com medição |
| Tolerância a falhas | Retry, breaker, failover | Auto Scaling, health checks | **Retry com backoff + circuit breaker + restart policy** | Cobre falha de worker, de fonte externa e de infra sem orquestrador externo |
| Segurança | TLS, tokens, MAC | ACM, Cognito, Secrets Manager | **TLS no Nginx + JWT + Argon2 + HMAC entre nós** | Cobre cifra em trânsito, autenticação e autorização de usuários e autenticação de nós, sem serviço gerenciado |
| Fonte de dados | API de terceiros | n/a | **Xotelo** | Preço real por OTA, sem chave, JSON estável; não exige raspagem de site (ver `docs/notas/2026-11-09-fonte-de-dados-real.md`) |

## 11. Lacunas conhecidas (resumo honesto)

1. AWS multi-instância não implantada nesta versão.
2. DLQ (`hospeda.scrape.dead`) sem consumidor.
3. Redis e RabbitMQ em instância única.
4. PostgreSQL sem failover automático.
5. Bully sem quórum; a eleição não influencia o roteamento do dispatch.
6. O HMAC cobre toda chamada interna (registro, `/internal/hotels`, `/scrape`, eleição), mas o MAC não inclui o corpo e o tráfego interno é HTTP sem TLS.
7. Gateway em instância única; registry em memória.
8. Relógio físico sem sincronização própria; Lamport não detecta concorrência.
9. gRPC não implementado (REST atende o requisito).
10. Evidências de caos dependem de executar `scripts/chaos_demo.sh`.
11. A nota `docs/notas/2026-11-09-tolerancia-a-falhas.md` afirma que não há `restart:` nem healthchecks nos serviços da aplicação; isso está desatualizado, pois o compose atual define ambos (`docker-compose.yml:116-122`, `:150-156`).
