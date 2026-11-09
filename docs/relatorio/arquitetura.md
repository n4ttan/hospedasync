# Arquitetura do HospedaSync

Plataforma distribuída de monitoramento de tarifas hoteleiras. Coleta cotações reais de hotéis do Rio de Janeiro pela API Xotelo, distribui o trabalho entre três workers, guarda o histórico em PostgreSQL replicado e expõe um painel web autenticado.

## 1. Estilo arquitetural e justificativa

**Estilo:** microsserviços com API Gateway e pool de workers, combinado com arquitetura orientada a eventos (event-driven) pela fila RabbitMQ.

| Decisão | Justificativa | Evidência |
|---|---|---|
| Gateway como ponto único de entrada (REST) | Concentra autenticação, autorização, persistência e métricas; esconde os workers do cliente | `api-gateway/main.py:36-40` |
| Workers sem estado de negócio, com escala horizontal | Qualquer worker executa qualquer job; adicionar um `worker-NN` não muda código | `docker-compose.yml:126`, `:160`, `:194` |
| Fila para varreduras (event-driven) | A cotação demora cerca de 4 s e é I/O. A fila desacopla quem pede de quem executa, absorve picos e reentrega o job se o worker morrer | `shared/mq.py:41-53`, `workers/consumer.py:53-107` |
| REST síncrono para consulta pontual | Quem pede uma cotação de um hotel quer a resposta na mesma chamada | `api-gateway/main.py:364` |
| Banco como escritor único (gateway) | Evita conflito de escrita; workers apenas publicam resultados | `api-gateway/sweeps.py:239-260` |
| Coordenação entre pares (Bully + Redis) | Eleição do agendador e exclusão mútua por hotel sem componente central de coordenação além do Redis | `workers/election.py`, `shared/distlock.py` |

Por que não um monólito: a coleta é lenta e paralelizável, as falhas precisam ficar isoladas (um worker caindo não derruba o painel) e o enunciado exige coordenação entre nós. Por que não mais serviços: o domínio é pequeno; separar além de gateway e worker aumentaria a operação sem ganho didático.

Middleware de comunicação: **RabbitMQ (AMQP)** para a via assíncrona, **HTTP/REST (FastAPI, httpx)** para a síncrona, **Nginx** na borda.

## 2. Visão de componentes e rede

```mermaid
flowchart LR
    U[Navegador / curl] -->|HTTPS 443| N[Nginx<br/>TLS, rate limit]
    U -.->|HTTP 80: 301| N
    N -->|HTTP| G[api-gateway :8000<br/>FastAPI]

    subgraph Rede Docker hospeda-net
      G -->|POST /scrape + X-Lamport| W1[worker-01 :8001]
      G --> W2[worker-02 :8002]
      G --> W3[worker-03 :8003]
      W1 <-->|/election /coordinator /health| W2
      W2 <--> W3
      W1 <--> W3
      W1 -.->|heartbeat HMAC /registry/register| G
      W2 -.-> G
      W3 -.-> G

      G -->|SET NX EX lock| R[(Redis<br/>locks, contadores, cache)]
      W1 --> R
      W2 --> R
      W3 --> R

      G -->|publica jobs| MQ{{RabbitMQ}}
      MQ -->|hospeda.scrape.jobs| W1
      MQ --> W2
      MQ --> W3
      W1 -->|hospeda.scrape.results| MQ
      MQ -->|results| G
      MQ -.->|fanout hospeda.cluster.events| W1
      MQ -.-> W2
      MQ -.-> W3

      G -->|escrita e leitura forte| PG[(PostgreSQL primário)]
      PG ==>|streaming replication| PR[(PostgreSQL réplica)]
      G -.->|leitura eventual| PR
    end

    W1 -->|HTTPS| X[API Xotelo]
    W2 --> X
    W3 --> X
```

Portas publicadas no host: 80 e 443 (Nginx) para qualquer origem; 8000, 8001 a 8003, 5432, 5433, 6379, 5672 e 15672 apenas em `127.0.0.1` (`docker-compose.yml:19`, `:41`, `:69`, `:81-82`, `:97`, `:132`, `:166`, `:200`, `:232-233`).

## 3. Fluxo síncrono: `POST /jobs/dispatch` com lock e failover

Código: `api-gateway/main.py:364-496`.

```mermaid
sequenceDiagram
    autonumber
    participant C as Cliente
    participant N as Nginx
    participant G as Gateway
    participant R as Redis
    participant W as Worker (sorteado)
    participant X as Xotelo
    participant P as PostgreSQL

    C->>N: POST /jobs/dispatch (Bearer JWT)
    N->>G: proxy HTTP
    G->>G: valida JWT e papel analyst (auth.py:119-145)
    G->>G: resolve hotel_key, gera job-uuid8, embaralha workers vivos
    G->>R: SET lock:hospedasync:hotel_key job_id NX EX 30
    alt lock ocupado
        R-->>G: nil
        G-->>C: 409 hotel já em processamento
    else lock adquirido
        R-->>G: OK
        loop até 3 workers (circuit breaker por worker)
            G->>W: POST /scrape + X-Lamport
            alt worker não responde (RequestError)
                G->>G: breaker.record_failure, evento dispatch_failover
                Note over G,W: tenta o próximo candidato
            else worker responde
                W->>X: GET cotação (retry, breaker, cache)
                X-->>W: tarifas por OTA
                W-->>G: 200 + X-Lamport
            end
        end
        alt algum worker devolveu dados
            G->>P: INSERT job_history (job_id PK)
            G-->>C: 200 SUCCESS (ou DEGRADED se o banco falhou)
        else nenhum devolveu
            G->>P: busca última cotação
            G-->>C: 200 DEGRADED, stale=true, source=cache (ou 503 sem cache)
        end
        G->>R: libera lock via Lua (só o dono)
    end
```

Pontos de atenção: 502 ou 503 do worker (fonte fora) interrompe o laço sem tentar outro worker, mas conta para o breaker (`main.py:423-427`). O lock é sempre liberado no `finally` (`main.py:495-496`).

## 4. Fluxo assíncrono: varredura via RabbitMQ com resultado idempotente

Código: `api-gateway/sweeps.py:78-142` (publicação), `workers/consumer.py:53-107` (execução), `api-gateway/sweeps.py:194-260` (resultado).

```mermaid
sequenceDiagram
    autonumber
    participant C as Cliente ou líder (agendador)
    participant G as Gateway
    participant Q as RabbitMQ
    participant W as Worker (um dos 3)
    participant R as Redis
    participant X as Xotelo
    participant P as PostgreSQL

    C->>G: POST /sweeps {top_n, date_offsets}
    G->>R: HSET sweep:sweep-uuid8 total, done, errors
    G->>Q: N jobs em hospeda.scrape.jobs (persistentes)
    G-->>C: 202 sweep_id, jobs_enqueued
    Q->>W: entrega job (prefetch=1)
    W->>R: SET lock:hospedasync:hotel_key NX EX 60
    alt lock ocupado
        W->>Q: republica o job com x-lock-conflicts+1 (até 30)
        W->>Q: ack da cópia antiga
    else Redis fora
        W->>Q: republica com x-infra-retries+1 (até 10, backoff)
    else lock adquirido
        W->>X: cotação (cache, retry, breaker)
        W->>Q: publica resultado em hospeda.scrape.results
        W->>Q: ack (só depois de publicar)
        W->>R: libera lock (Lua)
    end
    Q->>G: resultado
    G->>P: INSERT job_history com job_id como chave primária
    G->>R: SADD sweep:id:seen job_id
    alt SADD devolve 0 (duplicata)
        G->>Q: ack sem recontar
    else novo
        G->>R: HINCRBY sweep:id done
        G->>Q: ack
    end
    C->>G: GET /sweeps/sweep-uuid8 (progresso)
    Note over W,Q: Se o worker morrer antes do ack, o broker reentrega a outro worker
    Note over W,Q: Exceção inesperada: nack sem requeue envia à DLQ hospeda.scrape.dead (sem consumidor)
```

Garantia: entrega **at-least-once** com gravação **idempotente**, que produz efeito exatamente-uma-vez no banco e nos contadores. O custo é que o scrape pode rodar duas vezes (consulta extra à Xotelo).

## 5. Eleição Bully com relógio de Lamport

Código: `workers/election.py:93-114` (recepção), `:127-151` (`become_leader`), `:154-197` (`start_election`), `:205-232` (`monitor_leader`). Relógio: `shared/lamport.py`.

```mermaid
sequenceDiagram
    autonumber
    participant W1 as worker-01 (ID 1)
    participant W2 as worker-02 (ID 2)
    participant W3 as worker-03 (ID 3, líder)

    Note over W1,W3: Estado normal: líder = 3
    W2->>W3: GET /health (a cada 3 s)
    W3--xW2: sem resposta (falha 1)
    W2->>W3: GET /health
    W3--xW2: sem resposta (falha 2)
    W2->>W2: leader_suspected_down, L=7
    W2->>W3: POST /election {candidate_id=2, term=t1, lamport=8}
    W3--xW2: timeout 2 s (caiu)
    W2->>W2: nenhum ID maior respondeu, became_leader, L=9
    W2->>W1: POST /coordinator {leader_id=2, term=t1, lamport=10}
    W1->>W1: L = max(local, 10) + 1, CURRENT_LEADER_ID = 2
    W1-->>W2: ACK {lamport=12}
    W2->>W2: L = max(10, 12) + 1
    Note over W1,W2: Novo líder: 2. Só ele publica as varreduras periódicas
    W3->>W3: volta a subir e inicia eleição (atraso inicial 3 s)
    W3->>W1: POST /election {candidate_id=3}
    W1-->>W3: IGNORED (ID 1 menor)
    W3->>W2: POST /election {candidate_id=3}
    W2-->>W3: IGNORED (ID 2 menor)
    W3->>W3: sem ID maior: became_leader
    W3->>W1: POST /coordinator {leader_id=3, term=t2}
    W3->>W2: POST /coordinator {leader_id=3, term=t2}
```

Valores de L do diagrama são ilustrativos; as regras reais são: evento local ou envio faz `L = L + 1`; recebimento faz `L = max(L, recebido) + 1`. `GET /coordination/events` junta os logs de todos os nós ordenados por `(lamport, nó, horário físico)` (`shared/lamport.py:70-73`).

Parâmetros: eleição inicial 3 s após subir, timeout de 2 s por requisição, heartbeat do líder a cada 3 s, 2 falhas consecutivas disparam nova eleição (`election.py:18-21`). O `term` é `time.time()` (`election.py:161`) para que um líder que reinicia não seja descartado como obsoleto (`election.py:106-109`).

Limites: sem quórum (numa partição surgem dois líderes); o líder não participa do roteamento do dispatch.

## 6. Topologia de replicação PostgreSQL

```mermaid
flowchart LR
    GW[api-gateway<br/>escritor único] -->|DATABASE_URL<br/>escrita e leitura strong| P[(postgres-db<br/>primário :5432<br/>wal_level=replica)]
    P -->|WAL streaming<br/>slot replica_slot<br/>usuário replicator| R[(postgres-replica<br/>hot standby :5433)]
    GW -.->|DATABASE_REPLICA_URL<br/>GET /history?consistency=eventual| R
    R -.->|fallback se fora: primário<br/>header X-Replica-Fallback| P
    VP[(postgres_data)] --- P
    VR[(postgres_replica_data)] --- R
```

| Item | Valor | Fonte |
|---|---|---|
| Replicação | streaming assíncrona, hot standby | `docker-compose.yml:12-21`, `deploy/postgres/replica-entrypoint.sh` |
| Usuário e regra | `replicator`; `host replication replicator all scram-sha-256` | `deploy/postgres/01-replicator.sh` |
| Carga inicial da réplica | `pg_basebackup -R -X stream -C -S replica_slot` | `deploy/postgres/replica-entrypoint.sh` |
| Leitura forte (padrão) | primário | `api-gateway/database.py:96-120` |
| Leitura eventual | réplica, com fallback e headers `X-Served-By`, `X-Replication-Lag-Ms` | `api-gateway/main.py:278-306` |
| Estado | `GET /consistency/status` (lag em ms e bytes, `sync_state`) | `api-gateway/database.py:132-163` |
| Failover | **manual** (`SELECT pg_promote()`); não há Patroni | `docs/notas/2026-11-09-replicacao-consistencia.md` |

Resultados medidos (N=50, `docs/felipe/resultados_consistencia.md`): leituras obsoletas 49/50 no modo assíncrono e 0/50 no modo forte (`remote_apply`); escrita média 0,80 ms contra 1,47 ms (custo de 1,8 vez); com a réplica parada, o commit forte bloqueou até o cancelamento do cliente aos 5 s e o assíncrono concluiu em 0,002 s.

CAP e PACELC: padrão PA/EL (replica assíncrona, escrita rápida), com PC/EC nas leituras do painel, que vão ao primário.

## 7. Fluxos de comunicação

| Origem | Destino | Protocolo | Formato | Autenticação |
|---|---|---|---|---|
| Navegador / curl | Nginx | HTTPS (TLS 1.2/1.3) na 443; HTTP na 80 redireciona (301) | JSON, HTML | Nenhuma na borda; limite de requisições por IP |
| Nginx | Gateway | HTTP | JSON | Repassa o cabeçalho `Authorization` |
| Cliente | Gateway (rotas protegidas) | HTTP/REST | JSON | JWT HS256 no `Authorization: Bearer`; papel `analyst` ou `admin` |
| Cliente | `POST /auth/login` | HTTP/REST | JSON | Usuário e senha (Argon2); 5 falhas por minuto bloqueiam |
| Gateway | Worker `POST /scrape` | HTTP/REST (httpx, timeout 50 s) | JSON + cabeçalho `X-Lamport` | HMAC-SHA256 por requisição (id, método, caminho, timestamp; janela de 60 s) |
| Worker (líder) | Gateway `GET /internal/hotels` | HTTP/REST a cada varredura | JSON | HMAC-SHA256 (mesmos cabeçalhos), recurso `internal:hotels` |
| Worker | Gateway `POST /registry/register` | HTTP/REST a cada 5 s | JSON | HMAC-SHA256 em `X-Service-Id`, `X-Timestamp`, `X-Service-Signature` com `SERVICE_SECRET`, janela de 60 s |
| Worker | Worker `/election`, `/coordinator`, `/health`, `/leader`, `/events` | HTTP/REST | JSON com campo `lamport` | HMAC-SHA256 por requisição (`/health` é público) |
| Gateway | Worker `/leader`, `/events` (diagnóstico) | HTTP/REST (timeout 1,5 s) | JSON | HMAC-SHA256 por requisição |
| Gateway / Worker | RabbitMQ | AMQP 0-9-1 (aio-pika) | JSON, mensagens persistentes | Usuário e senha do broker (`RABBITMQ_URL`) |
| Worker (líder) | Todos os workers | AMQP fanout `hospeda.cluster.events` | JSON (`type`, `node`, `lamport`) | Idem broker |
| Gateway / Worker | Redis | RESP | chave `lock:hospedasync:<hotel_key>`, `sweep:<id>` | `requirepass` (`REDIS_PASSWORD`) |
| Gateway | PostgreSQL primário | TCP (psycopg2) | SQL | Senha do usuário `postgres` |
| Réplica | Primário | Protocolo de replicação | WAL | Usuário `replicator` (SCRAM-SHA-256) |
| Worker | Xotelo | HTTPS | JSON | Nenhuma (API pública, sem chave) |

Observação: TLS cobre apenas a borda. O tráfego interno (HTTP, AMQP, RESP, SQL) trafega em claro dentro da rede Docker `hospeda-net`.

## 8. Esquema de nomes e identificadores

| Identificador | Formato | Quem gera | Por quê |
|---|---|---|---|
| Worker | `worker-NN` (`worker-01`) | Configuração (`WORKER_ID` no compose) | O sufixo numérico vira o ID do Bully (`election.py:44-49`): maior número, maior prioridade. Nenhuma tabela de IDs para manter e legível em log. `PEER_WORKERS` também aceita `id=url`, para hosts com IP em vez de nome |
| Job | `job-<uuid8>` (8 primeiros hex do UUID4) | Gateway (`main.py:381`, `sweeps.py:112`) | Único sem coordenação central, curto para digitar na demonstração. É a chave primária do histórico, base da idempotência. Com 32 bits a colisão é improvável nesta escala, mas não impossível em escala maior |
| Varredura | `sweep-<uuid8>` | Gateway (`sweeps.py:104`) ou líder (`scheduler.py:68`) | Agrupa os jobs; chave dos contadores no Redis |
| Lock | `lock:hospedasync:<hotel_key>` | `shared/distlock.py:14`, `:33` | Prefixo evita colisão com outras chaves do Redis; chave por `hotel_key` (e não por nome) evita que dois nomes diferentes do mesmo hotel contornem o lock |
| Contadores de varredura | `sweep:<sweep_id>` e `sweep:<sweep_id>:seen` | `sweeps.py:224-228` | Progresso e deduplicação |
| Hotel | `hotel_key` do TripAdvisor, `g303506-dNNNN` (`g303506` é o Rio de Janeiro, `dNNNN` o hotel) | Origem externa (Xotelo), carregado de `api-gateway/data/hotels_rio.json` | Identificador estável e único; a Xotelo e o link do TripAdvisor usam o mesmo código; dispensa mapeamento próprio |
| Filas e exchanges | `hospeda.scrape.jobs`, `hospeda.scrape.results`, `hospeda.scrape.dead`, `hospeda.dlx`, `hospeda.cluster.events` | `shared/mq.py:20-24` | Prefixo `hospeda.` isola o domínio no broker; o terceiro segmento diz a função |
| Serviços na rede | `api-gateway`, `worker-01`, `redis-lock`, `rabbitmq`, `postgres-db`, `postgres-replica` | DNS interno do Docker | Descoberta de endereço sem configuração de IP |
| Registry de nós | Dicionário `worker_id -> {endpoint, last_seen}` em memória | Gateway (`main.py:197-205`) | Descoberta dinâmica de workers vivos; expira em 15 s sem heartbeat |

## 9. Segurança em camadas

| Camada | Mecanismo | Referência |
|---|---|---|
| Borda | TLS 1.2/1.3, HSTS, CSP, `X-Frame-Options`, rate limit (20 req/s geral, 5 req/min no login) | `deploy/nginx/nginx.conf:14-15`, `:35`, `:42-46` |
| Usuário | JWT HS256 com expiração de 30 min, Argon2 nas senhas, limite de tentativas | `api-gateway/auth.py:24-35`, `:75-79`, `:91-99` |
| Autorização | Papéis `analyst` e `admin`; papel lido do banco | `api-gateway/auth.py:119-145`, `docs/seguranca/matriz-autorizacao.md` |
| Nó a nó | HMAC-SHA256 no registro de worker e na consulta do catálogo pelo agendador | `shared/servicetoken.py` |
| Segredos | `.env` gerado, fora do git; Redis, RabbitMQ e Postgres com senha | `scripts/gen_env.sh`, `docker-compose.yml` |
| Rede | Portas internas ligadas a `127.0.0.1` | `docker-compose.yml` |

Lacunas declaradas: sem TLS interno nem mTLS (o canal interno é HTTP com assinatura HMAC; o MAC não cobre o corpo); segredo JWT único; sem revogação individual de token.

## 10. Limitações da arquitetura

Redis, RabbitMQ e gateway em instância única; PostgreSQL sem failover automático; Bully sem quórum; DLQ sem consumidor; registry em memória; implantação AWS multi-instância não realizada. Detalhe e status em `docs/relatorio/matriz-requisitos.md`.
