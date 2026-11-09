# Roteiro da apresentação (14 minutos)

Cada bloco do enunciado é mostrado ao vivo. A tabela da seção 2 traz comando e o que o professor verá. A seção 3 traz as perguntas prováveis.

## 1. Preparação (antes de entrar na sala)

```bash
bash scripts/gen_env.sh && bash scripts/gen_selfsigned_cert.sh
docker compose up --build -d && docker compose ps     # todos healthy
set -a; . ./.env; set +a
GW=http://localhost:8000
TOKEN=$(curl -s -X POST $GW/auth/login -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
H="Authorization: Bearer $TOKEN"
```

Deixar abertos: navegador em `https://localhost` já logado (painel); painel do RabbitMQ em `http://localhost:15672` (usuário e senha do `.env`); terminal com `docker compose logs -f worker-01 worker-02 worker-03`; este arquivo e `docs/relatorio/arquitetura.md` (diagramas). Aquecer o cache com uma cotação antes de começar (a primeira leva cerca de 4 s). O token expira em 30 min: gerar de novo perto da hora.

Ter pronto o arquivo `docs/relatorio/evidencias/chaos_<data>.md`, gerado antes por `SAVE=1 bash scripts/chaos_demo.sh all` (o experimento derruba contêineres; rodar fora da apresentação).

## 2. Minuto a minuto

| Min | Bloco | Comando | O que o professor verá |
|---|---|---|---|
| 0:00 a 1:30 | Arquitetura | Mostrar o diagrama de componentes (`arquitetura.md`, seção 2) e `docker compose ps` | Nove contêineres: Nginx, gateway, 3 workers, RabbitMQ, Redis, Postgres primário e réplica. Dizer: microsserviços com gateway e workers, event-driven pela fila; REST na via síncrona, AMQP na assíncrona. Dado real da Xotelo, não simulado |
| 1:30 a 3:00 | Processos e virtualização | `docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'` e `docker compose logs --tail 5 worker-01` | Contêineres com `healthy`, portas internas só em `127.0.0.1`, 80/443 públicas. Citar `workers/main.py:42-49` (tarefas async) e `workers/consumer.py:65` (`to_thread` para o Redis bloqueante), `workers/scraper.py:48` (semáforo de 3) |
| 3:00 a 4:30 | Comunicação síncrona (REST) | `curl -s -X POST $GW/jobs/dispatch -H "$H" -H 'Content-Type: application/json' -d '{"hotel_name":"Copacabana Palace","checkin_date":"2026-12-18","checkout_date":"2026-12-20"}'` | JSON com `selected_worker_id`, tarifas por OTA, `source=live` e `processing_time_ms` de alguns segundos |
| 4:30 a 6:00 | Comunicação assíncrona (fila e fanout) | `curl -s -X POST $GW/sweeps -H "$H" -H 'Content-Type: application/json' -d '{"top_n":5,"date_offsets":[21,28,35]}'` e depois `curl -s -H "$H" $GW/sweeps/<sweep_id>` | `202` com `sweep_id` e 15 jobs. No painel do RabbitMQ, fila `hospeda.scrape.jobs` esvaziando; nos logs, os 3 workers pegando jobs; o progresso sobe até `done=15`. Mostrar a exchange `hospeda.cluster.events` (fanout) e a fila `hospeda.scrape.dead` |
| 6:00 a 7:30 | Coordenação: eleição e Lamport | `curl -s -H "$H" $GW/coordination/status` e `curl -s -H "$H" "$GW/coordination/events?limit=15"` | `leader_id: 3`, `consensus: true`; eventos de vários nós na ordem de Lamport (`election_started`, `coordinator_received`, `dispatch_sent`, `scrape_http`). Explicar a regra `max(L, recebido)+1` |
| 7:30 a 9:00 | Coordenação: queda do líder | `docker stop hospeda_worker_03`; esperar 10 a 15 s; repetir `coordination/status` | `leader_id: 2`, consenso mantido, `became_leader` nos logs do worker-02. Depois `docker start hospeda_worker_03`: volta a ser o líder |
| 9:00 a 10:00 | Coordenação: exclusão mútua | `BODY='{"hotel_name":"Copacabana Palace","checkin_date":"2026-12-20","checkout_date":"2026-12-23"}'; for i in 1 2; do curl -s -o /dev/null -w "%{http_code}\n" -X POST $GW/jobs/dispatch -H "$H" -H 'Content-Type: application/json' -d "$BODY" & done; wait` | Um `200` e um `409`. Em outro terminal: `curl -s -H "$H" $GW/locks` mostra `lock:hospedasync:g303506-d...` durante o processamento |
| 10:00 a 11:00 | Tolerância a falhas | `bash scripts/demo_fila_falha.sh` (ou mostrar o resumo de `docs/relatorio/evidencias/chaos_<data>.md`, gerado por `scripts/chaos_demo.sh`) | Varredura que termina 15/15 com job_ids únicos mesmo com a queda do líder e de outro worker. Mostrar também `curl -s -H "$H" $GW/metrics` com `hospeda_worker_breaker_state` |
| 11:00 a 12:00 | Consistência e replicação | `curl -s -H "$H" $GW/consistency/status` e `curl -s -i -H "$H" "$GW/history?consistency=eventual" -o /dev/null -D -` | Réplica alcançável, `replay_lag_bytes` perto de 0, headers `X-Served-By: replica` e `X-Replication-Lag-Ms`. Mostrar a tabela de `docs/felipe/resultados_consistencia.md`: 98% de leituras obsoletas no assíncrono contra 0% no forte; escrita de 0,80 ms contra 1,47 ms |
| 12:00 a 13:00 | Segurança | `curl -sk -I https://localhost/health` e `curl -sI http://localhost/health`; `curl -s -o /dev/null -w "%{http_code}\n" $GW/hotels`; `curl -s -X POST $GW/registry/register -H 'Content-Type: application/json' -d '{"worker_id":"x","endpoint":"http://x"}'` | HTTPS com HSTS; HTTP responde 301; sem token `401`; registro de nó falso sem HMAC `401`. Usuário `analyst` em `/locks` recebe `403` (`docs/seguranca/matriz-autorizacao.md`) |
| 13:00 a 14:00 | Painel, nomes e fechamento | Navegador em `https://localhost` | Painel com líder, saúde do cluster, histórico (leitura forte) e botão de varredura. Citar nomes: `worker-NN`, `job-<uuid8>`, `sweep-<uuid8>`, `lock:hospedasync:<hotel_key>`. Encerrar com as limitações reais (seção 4) |

Plano B se a rede da sala bloquear a Xotelo: o dispatch devolve `DEGRADED` com `source=cache`, o que também demonstra a degradação controlada (`api-gateway/main.py:444-456`). Se um comando de queda demorar, voltar ao diagrama de eleição (`arquitetura.md`, seção 5).

## 3. Perguntas prováveis do professor e respostas

**1. Por que a Xotelo e não raspar o Booking?**
Raspar o Booking viola os termos e é bloqueado por captcha; a Xotelo devolve a diária por OTA em JSON, sem chave. Também descartamos Amadeus (desligado em julho de 2026), SerpApi (250 buscas por mês) e Inside Airbnb (histórico, não é cotação ao vivo). Evidência: `docs/notas/2026-11-09-fonte-de-dados-real.md`. Risco assumido: API de terceiro sem termos publicados.

**2. Por que Bully e não Raft?**
São 3 nós que se conhecem; o Bully é simples e demonstrável derrubando o worker-03 (`workers/election.py:93-232`). Raft daria quórum e log replicado, mas o líder aqui só agenda varreduras e o dado é idempotente, então a eleição errada não corrompe nada. Limite que reconheço: o Bully não tem quórum, e numa partição surgem dois líderes.

**3. Por que `SET NX EX` e não Redlock?**
Uma única operação atômica cria a chave e define o TTL (`shared/distlock.py:49`), com liberação por Lua para só o dono apagar (`:53-65`). Redlock exige várias instâncias independentes de Redis; temos uma. A consequência está declarada: com um Redis só, falha dele é falha do lock. Como mitigação, a gravação é idempotente por `job_id`, então um lock violado gera trabalho repetido, não dado errado.

**4. Por que RabbitMQ e não Kafka?**
Precisamos de fila de trabalho com ack, prefetch, reentrega, dead-letter e um fanout, tudo no mesmo broker (`shared/mq.py:41-53`). Kafka é um log distribuído para alto volume e reprocessamento; seria exagero para dezenas de jobs por varredura. O SQS prenderia a demonstração à nuvem.

**5. O que o relógio de Lamport garante e o que não garante?**
Garante que, se A aconteceu antes de B (causalidade), então L(A) < L(B). Não garante o inverso: L(A) < L(B) não prova que A causou B, e eventos concorrentes recebem valores arbitrários em ordem. Para detectar concorrência seria preciso relógio vetorial. Usamos Lamport para ordenar o log fundido dos nós (`shared/lamport.py:70-73`, `/coordination/events`).

**6. Por que consistência forte no painel?**
O painel mostra o histórico que o analista acabou de disparar; ler de uma réplica assíncrona poderia não exibir o resultado recém-gravado (49/50 leituras obsoletas no experimento, `docs/felipe/resultados_consistencia.md`). Por isso o padrão é `strong`, que lê o primário (`api-gateway/main.py:278-306`). A leitura `eventual` fica opcional para análises que aceitam atraso e aliviam o primário.

**7. Qual é o CAP/PACELC do sistema?**
Por padrão PA/EL: replicação assíncrona prioriza disponibilidade e latência de escrita (0,80 ms contra 1,47 ms). Nas leituras do painel, PC/EC, porque vão ao primário. Com a réplica síncrona (`remote_apply`) e a réplica parada, a escrita bloqueou até o cancelamento do cliente aos 5 s: comportamento CP medido. Análise: `docs/notas/2026-11-09-replicacao-consistencia.md`.

**8. O que acontece se o Redis cair?**
O dispatch síncrono falha (sem lock não despachamos) e `POST /sweeps` responde 503. Na fila, o consumidor republica o job com backoff de 2 a 30 s, até 10 vezes, sem mandar para a DLQ (`workers/consumer.py:64-78`). O Redis é ponto único de falha; é uma limitação declarada. O experimento 4 do caos mede isso (resultado em `docs/relatorio/evidencias/chaos_<data>.md`, gerado por `scripts/chaos_demo.sh`).

**9. Como os nós se protegem uns dos outros?**
Só o registro de worker e a consulta de catálogo do agendador (`/internal/hotels`) são autenticados no gateway, com HMAC-SHA256 e janela de 60 s (`shared/servicetoken.py`), o que impede um nó falso de entrar no registry. Honestamente: `/scrape`, `/election` e `/coordinator` dos workers não têm autenticação e o tráfego interno não usa TLS; a proteção aqui é a rede privada do Docker e as portas ligadas a `127.0.0.1`. Próximo passo seria mTLS e assinar também as rotas de eleição.

**10. Qual seria o custo na AWS?**
Não implantamos a versão atual na AWS, então não tenho custo medido. A versão inicial rodou numa EC2 t3.micro (free tier). Para a versão atual, a estimativa seria uma instância maior com os 9 contêineres ou 2 a 3 instâncias para separar workers; o custo real depende do tipo escolhido e não foi medido. Alternativa gerenciada (Amazon MQ, ElastiCache, RDS Multi-AZ) resolveria os pontos únicos de falha, a um custo bem maior.

**11. Quais são as limitações reais?**
Redis, RabbitMQ e gateway em instância única; Postgres sem failover automático (promoção manual com `pg_promote()`); Bully sem quórum; DLQ sem consumidor; registry em memória; sem TLS interno; implantação AWS multi-instância não feita. A lista completa com status está em `docs/relatorio/matriz-requisitos.md`, seção 11.

**12. O que acontece se o Postgres primário cair?**
Escritas e leituras `strong` falham. O gateway continua de pé e responde com cache nas cotações; resultados de varredura não são perdidos porque o consumidor só dá ack após o commit e devolve a mensagem à fila (`api-gateway/sweeps.py:247-260`). A réplica atende leitura `eventual`, mas ninguém a promove sozinha. Experimento 6 do caos.

**13. Como evitam que um resultado seja contado duas vezes?**
A entrega é at-least-once (ack tardio), então pode haver duplicata. O `job_id` é chave primária (`api-gateway/database.py:32`) e o contador usa `SADD` em `sweep:<id>:seen`; se o `SADD` devolve 0, não recontamos (`api-gateway/sweeps.py:224`). O scrape pode rodar duas vezes; o efeito no banco, não.

**14. Por que o líder não roteia o dispatch?**
Foi uma decisão de escopo: o líder agenda as varreduras periódicas (`workers/scheduler.py:91-103`), e o gateway sorteia entre workers vivos com failover. Fazer o gateway sempre mandar para o líder criaria gargalo e ponto único de falha sem ganho. É uma limitação que declaramos: a eleição não influencia o roteamento.

**15. Por que REST e não gRPC?**
Chamadas de baixa frequência (uma cotação leva cerca de 4 s), JSON legível por `curl` e OpenAPI automático do FastAPI. O ganho de desempenho do gRPC não aparece neste perfil. O enunciado aceita REST ou gRPC.

**16. Como o sistema detecta uma falha e quanto demora?**
Heartbeat do worker a cada 5 s, considerado morto após 15 s sem sinal (`api-gateway/main.py:371`); health do líder a cada 3 s com 2 falhas consecutivas, ou seja, 6 a 9 s (`workers/election.py:18-21`, `:205-232`). No dispatch, a falha de conexão é detectada na hora e o gateway tenta outro worker; 3 falhas abrem o circuit breaker por 20 s.

**17. O que impede que um worker lento prenda um hotel para sempre?**
O TTL do lock: 30 s no dispatch e 60 s no consumidor. Se o dono morrer, a chave expira. O risco inverso (job mais lento que o TTL) existe e é declarado; a liberação por Lua impede que o dono antigo apague o lock do novo.

**18. Qual a diferença entre o relógio físico e o lógico no sistema?**
O físico (`time.time()`) vira o `term` da eleição e desempata eventos; não há sincronização própria (NTP/Cristian), e no Compose todos usam o relógio do mesmo host. O lógico (Lamport) ordena a causalidade entre mensagens. A limitação do `term` por timestamp com hosts reais está registrada em `docs/notas/2026-11-09-tolerancia-a-falhas.md`.

**19. Como testam isso tudo?**
Cerca de 111 funções de teste em `tests/` (pytest com Redis de teste na porta 6380) cobrindo lock, eleição, failover, idempotência, autenticação e roteamento de consistência; experimento de consistência (N=50) em `scripts/consistency_experiment.sh`; seis experimentos de caos em `scripts/chaos_demo.sh`. As evidências do caos ficam em `docs/relatorio/evidencias/chaos_<data>.md`, gerado por `scripts/chaos_demo.sh`.

## 4. Fechamento (30 segundos)

Dizer o que está comprovado ao vivo: REST e fila, eleição com Lamport, exclusão mútua, replicação com dois níveis de leitura, recuperação automática e camadas de segurança. Dizer o que falta: AWS multi-instância, failover do Postgres, quórum no Bully, consumidor da DLQ e autenticação nas rotas internas dos workers.
