# Tolerância a falhas: mecanismos e lacunas (2026-11-09)

Responsável: Nattan. Verificação experimental: `scripts/chaos_demo.sh` (experimentos 1 a 6; `SAVE=1` grava `docs/relatorio/evidencias/chaos_<data>.md`).

## Mecanismos implementados

### Detecção

| Mecanismo | Parâmetros | Onde |
|---|---|---|
| Heartbeat do registry | worker registra a cada 5 s; o gateway considera morto quem passou de 15 s sem registro | `workers/main.py:65-79`, `api-gateway/main.py:113`, `:327`, `:175-178` |
| Health check do líder | a cada 3 s, 2 falhas consecutivas disparam nova eleição (detecção em 6 a 9 s) | `workers/election.py:19-21`, `:205-234` |
| Healthchecks do compose | `pg_isready` no primário e na réplica; `rabbitmq-diagnostics ping` | `docker-compose.yml:22-26`, `:46-50`, `:71-75` |
| `/health` do gateway | informa Redis e RabbitMQ sem depender do banco | `api-gateway/main.py:82-91` |
| `pool_pre_ping` | descarta conexão morta com o Postgres antes de usar | `api-gateway/database.py:14` |

### Recuperação

| Mecanismo | O que garante | Onde |
|---|---|---|
| Reeleição Bully | o maior ID vivo assume; o líder que volta retoma | `workers/election.py:127` (`become_leader`), `:154` (`start_election`) |
| Reentrega da fila + ack tardio | job só sai da fila após o resultado ser publicado; worker morto devolve o job a outro | `workers/consumer.py:53-107`, `:128` (prefetch 1); topologia durável em `shared/mq.py:41-53`; mensagens persistentes `:56-65` |
| Gravação idempotente | `job_id` é chave primária; reentrega não duplica linha nem recontagem (`SADD` em `sweep:<id>:seen`) | `api-gateway/sweeps.py:168-170`, `:221-236` |
| Ack só após commit; requeue se banco/Redis caírem | resultado não se perde com o Postgres fora | `api-gateway/sweeps.py:247-260` |
| TTL do lock | lock órfão expira sozinho (`SET NX EX`) | `shared/distlock.py:37-50`; `workers/consumer.py:11` (60 s) |
| Retry de infra com backoff | Redis do lock fora: republica o job com `x-infra-retries`, até 10 vezes (2 s a 30 s), sem ir para a DLQ | `workers/consumer.py:16-19`, `:64-78` |
| Retry por conflito de lock | até 30 tentativas de 1 s | `workers/consumer.py:80-91` |
| Retry com backoff na Xotelo | 3 tentativas, 0,5 s e 1 s | `workers/scraper.py:96-122`; helper genérico `shared/resilience.py:76-86` |
| Circuit breaker | 5 falhas abrem; meia-abertura após 30 s | `shared/resilience.py:14-73`; uso em `workers/scraper.py:23`, `:129-141` |
| Cache em duas camadas | Redis compartilhado + memória, TTL 300 s | `workers/scraper.py:63-93`, `:144-172` |
| Fallback para cache no gateway | worker/Xotelo fora: devolve a última cotação do banco (`DEGRADED`, `stale`) | `api-gateway/main.py:296-319`, `:380-391` |
| Persistência degradada | falha ao gravar no banco não derruba o dispatch (`persisted=false`) | `api-gateway/main.py:393-428` |
| Loops de background resilientes | tarefas do worker reiniciam após 5 s; consumidor do gateway reconecta com backoff até 30 s; `connect_robust` nos workers | `workers/main.py:52-62`, `api-gateway/sweeps.py:269-281`, `shared/mq.py:28-38`, `workers/consumer.py:122-141` |
| Falha rápida de publicação | `POST /sweeps` responde 503 e desfaz o contador se o broker estiver fora | `api-gateway/sweeps.py:132-139` |
| Leitura da réplica com fallback | `GET /history?consistency=eventual` cai para o primário se a réplica falhar | `api-gateway/main.py:236-264` |

## Falhas NÃO cobertas

* **Restart policy: atendida.** `docker-compose.yml` define `restart: unless-stopped` nos servicos e healthchecks no gateway, nos workers, no Postgres (primario e replica), no Redis e no RabbitMQ; o Nginx so sobe depois que o gateway fica saudavel. O script de caos ainda faz `docker start` na recuperacao para garantir o estado inicial entre experimentos.
* **Redis único.** Locks, contadores de varredura e cache compartilhado dependem de uma instância sem réplica nem Sentinel. Com ele fora: `POST /sweeps` e `GET /sweeps/<id>` dão 503, `/jobs/dispatch` falha, e o progresso só é preservado porque o `docker stop` faz snapshot (sem volume, `docker rm` perde tudo).
* **RabbitMQ único.** Sem cluster nem filas quorum. Com o broker fora, nenhuma varredura é aceita; o líder não consegue agendar (a tentativa falha e espera o próximo ciclo). Mensagens em voo dependem do disco do contêiner (sem volume nomeado).
* **Primário Postgres sem failover automático.** A réplica serve leitura (`eventual`), mas ninguém a promove. Com o primário fora, escritas e consultas `strong` falham até alguém reiniciá-lo; o gateway degrada, não se recupera sozinho para a réplica.
* **Bully sem quórum.** Numa partição de rede surgem dois líderes (já registrado em `2026-11-09-coordenacao.md`); a idempotência evita dano nos dados, mas a varredura periódica pode rodar em duplicidade.
* **Gateway único.** Não há segunda instância nem balanceador. O registry (`ACTIVE_WORKERS`) vive em memória: reiniciar o gateway zera a lista até os heartbeats chegarem (até 5 s).
* **Mensagem venenosa.** Exceção inesperada vai à DLQ (`workers/consumer.py:108-113`), mas nada consome ou reprocessa a `hospeda.scrape.dead`; só é inspecionada.
* **Entrega at-least-once, não exactly-once.** A duplicação é neutralizada na gravação; o scrape pode ser executado duas vezes (custo extra na Xotelo).
* **Falha da Xotelo é global.** O breaker é por worker e em memória (cada um aprende sozinho, 5 falhas cada), e não há limitação de taxa contra a API externa além do semáforo de 3 conexões.
* **Relógio.** O term da eleição usa `time.time()`; sem sincronização entre hosts reais pode haver term inconsistente (no Compose o relógio é um só).
* **Não testado:** corrupção de dados, disco cheio, lentidão (latência sem queda), partição de rede entre workers e Redis.
