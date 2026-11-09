# Resultados do experimento de consistencia

Gerado por `scripts/consistency_experiment.sh` (primario hospeda_db x replica hospeda_db_replica).

## Execucao 2026-11-09 02:21:58 (N=50)

| Modo                 | Leituras obsoletas | Lag medio (ms) | Lag max (ms) | Escrita media (ms) | Escrita p95 (ms) | Escrita max (ms) |
|----------------------|--------------------|----------------|--------------|--------------------|------------------|------------------|
| Assincrono           | 49/50 (98%)        | 1.8            | 4.2          | 0.80               | 0.91             | 2.51             |
| Forte (remote_apply) | 0/50 (0%)          | 1.4            | 2.9          | 1.47               | 2.02             | 3.68             |

Replica parada (timeout 5s por escrita):
  - modo forte    : BLOQUEOU 5s ate o cancelamento do cliente (commit ficou so no primario, sem confirmacao da replica) em 5.00s
  - modo assincrono: concluiu em 0.002s
Custo da consistencia forte na escrita: 1.8x a latencia media assincrona.
