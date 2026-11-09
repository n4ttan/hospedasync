# Mensageria (RabbitMQ), relógio de Lamport e líder agendador

## Fluxo
Gateway `POST /sweeps` (ou o worker líder, a cada `SWEEP_INTERVAL_SECONDS`) publica um JOB por hotel x data em `hospeda.scrape.jobs`.
Cada worker consome com `prefetch=1`, pega o lock Redis do `hotel_key`, consulta a Xotelo e publica o RESULT em `hospeda.scrape.results`; só então dá ack.
O Gateway consome os resultados e grava no Postgres (escritor único), de forma idempotente por `job_id`.

## Decisões
- **At-least-once + gravação idempotente:** se o worker morre com a mensagem em voo, o broker reentrega; `job_id` é chave primária, então a repetição não duplica.
- **Lock no worker:** exclusão mútua por hotel vale também para a via assíncrona. Lock ocupado -> republica a mensagem com contador `x-lock-conflicts` (máx. 30) em vez de descartar.
- **DLQ:** exceção inesperada -> `nack(requeue=False)` -> `hospeda.scrape.dead`.
- **Líder = agendador:** só o líder publica varreduras periódicas; ao cair, o novo líder continua. Anúncios (`leader_elected`, `sweep_scheduled`) vão no exchange fanout `hospeda.cluster.events` (broadcast para todos os workers).
- **Lamport:** `shared/lamport.py`. Tick em evento local/envio; `max(L, recebido)+1` no recebimento. Vai no header `X-Lamport` (HTTP), no campo `lamport` (eleição e mensagens). `GET /coordination/events` junta os eventos de todos os nós ordenados por Lamport. Limite honesto: L(A)<L(B) não prova causalidade (isso exigiria relógio vetorial).
- **Bully com várias máquinas:** o ID vem de `WORKER_ID`, e `PEER_WORKERS` aceita `id=url`, então IPs de hosts diferentes não colidem.

## Evidências (09/11/2026, cluster local)
- Varredura de 6 jobs: 6/6 concluídos, 3 workers participaram, `job_lock_conflict` real registrado.
- Líder agendou varredura sozinho (10 jobs).
- Queda do líder (worker-03) e do worker-02 com 12 jobs pendentes: varredura terminou 15/15, 15 job_ids únicos, worker-01 virou líder.
- Bug achado e corrigido no teste real: listener do fanout quebrava (kwarg `event_type` colidia com `EventLog.log`); teste de regressão adicionado.

## Limites conhecidos
`aio_pika.connect` no Gateway (sem reconexão automática do cliente; o loop reconecta). Contadores de varredura no Redis usam SADD + HINCRBY sem transação.
Redis de lock fora do ar leva o job à DLQ (a tratar na fase de tolerância a falhas).
