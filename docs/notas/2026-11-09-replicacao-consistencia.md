# Replicacao do PostgreSQL e modelos de consistencia (Fase C)

## Topologia
- `postgres-db` (hospeda_db, :5432): primario. `wal_level=replica`, `max_wal_senders=10`, `max_replication_slots=10`, `hot_standby=on`, `wal_keep_size=256MB`. Usuario `replicator` criado por `deploy/postgres/01-replicator.sh` (initdb, so na primeira subida do volume) e regra `host replication replicator all scram-sha-256` no pg_hba.
- `postgres-replica` (hospeda_db_replica, host :5433): hot standby. `deploy/postgres/replica-entrypoint.sh` faz `pg_basebackup -R -X stream -C -S replica_slot` do primario na primeira subida (volume `postgres_replica_data`) e inicia em recovery. Depende do healthcheck do primario.
- Senhas: `POSTGRES_PASSWORD` e `REPLICATOR_PASSWORD` (com default de desenvolvimento). Em producao, defina-as no `.env`.
- Gateway: escritor unico no primario (`DATABASE_URL`). `DATABASE_REPLICA_URL` (opcional) habilita leituras na replica; sem ela tudo vai ao primario. Essa variavel deve ser adicionada ao bloco `api-gateway` do compose (ex.: `postgresql+psycopg2://postgres:...@postgres-replica:5432/hospedasync_db`).

## Modelo por operacao
| Operacao | Modelo | Como |
|---|---|---|
| Escrita (dispatch, sweeps, resultados) | Primario unico | `get_db` -> primario |
| Leitura do dashboard (`GET /history`, padrao) | Forte (le o que foi confirmado) | `consistency=strong` -> primario |
| Leitura analitica opcional | Eventual | `GET /history?consistency=eventual` -> replica; replica fora -> primario com `X-Replica-Fallback: true` |

Headers de `/history`: `X-Consistency`, `X-Served-By` (primary|replica), `X-Replication-Lag-Ms` (so da replica). `GET /consistency/status` mostra replica configurada/alcancavel, lag (ms e bytes), `sync_mode` (`pg_stat_replication.sync_state`) e `primary_in_recovery`.

Observacao: a replica e assincrona por padrao, entao `strong` aqui significa "le do primario" (le-o-que-escreveu). O modo sincrono (`synchronous_commit=remote_apply`) torna tambem a replica linearizavel apos o commit; e ativado so no experimento.

## CAP / PACELC
- CAP: sob particao, ou se mantem consistencia (CP) ou disponibilidade (AP). Com replica sincrona, se a replica cai a escrita bloqueia (CP); assincrona, a escrita continua e a replica fica defasada (AP).
- PACELC: mesmo sem particao (Else) ha troca entre latencia e consistencia: `remote_apply` paga o round-trip + replay na replica a cada commit.
- Nosso sistema: PA/EL por padrao (replica assincrona, escrita rapida) e PC/EC apenas nas leituras do dashboard, que vao ao primario.
- Leitura eventual na replica = menor carga no primario, ao custo de ler dado obsoleto.

## Resultados medidos (N=50, maquina de dev, Docker/WSL2; ver `docs/felipe/resultados_consistencia.md`)
| Modo | Leituras obsoletas | Lag medio / max | Escrita media / p95 |
|---|---|---|---|
| Assincrono | 49/50 (98%) | 1.8 / 4.2 ms | 0.80 / 0.91 ms |
| Forte (remote_apply) | 0/50 (0%) | 1.4 / 2.9 ms | 1.47 / 2.02 ms |

- Custo do modo forte: ~1.8x a latencia media de escrita.
- Replica parada: forte -> escrita bloqueou ate o cancelamento do cliente aos 5 s (o commit fica so no primario); assincrono -> 2 ms, sem impacto.
- 98% de obsoletas e esperado: a leitura imediata (<1 ms) chega antes do replay (~2 ms). Em outro ambiente o numero varia.
- Caveat: `now() - pg_last_xact_replay_timestamp()` cresce com o primario ocioso (sem novas transacoes a replica esta em dia, mas o numero sobe). Em `/consistency/status`, `replay_lag_bytes=0` indica replica em dia.
- `statement_timeout` NAO interrompe a espera por replica sincrona; use cancelamento do cliente (`psycopg2 conn.cancel()`) ou `synchronous_commit` por transacao.

## Limites
- Sem failover automatico (nao ha Patroni/repmgr). Primario unico = ponto unico de falha para escrita.
- Promocao manual da replica: `docker exec hospeda_db_replica psql -U postgres -c "SELECT pg_promote()"` (ou `pg_ctl promote -D $PGDATA`). Depois: apontar `DATABASE_URL` do gateway ao novo primario, e recriar o antigo como replica via novo `pg_basebackup`. Escritas assincronas ainda nao replicadas podem ser perdidas.
- Slot de replicacao retem WAL se a replica ficar parada por muito tempo (monitorar disco).
- Senhas de dev em default; `pg_hba` do primario aceita replicacao de qualquer origem da rede do compose.
- Recriar o volume do primario exige apagar `postgres_data` (o init so roda em volume vazio) e tambem `postgres_replica_data`.

## Reproduzir
`docker compose up -d postgres-db postgres-replica`, depois `PYTHON=<python com psycopg2> scripts/consistency_experiment.sh` (N via env).
