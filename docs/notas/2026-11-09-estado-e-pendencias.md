# Estado e pendências (retomada)

Branch `feature/messaging`. Fases A (dados reais Xotelo), B (fila, Lamport, líder agendador), C (réplica Postgres e consistência),
D parcial (breaker, retry, failover do dispatch) e E (JWT, HMAC no registry, Nginx TLS) estão commitadas. Nada foi enviado ao GitHub.

## Resultado do caos (scripts/chaos_demo.sh, primeira execução real)
- 1 worker morto na varredura: PASS (15/15, job_ids únicos).
- 2 líder morto: FAIL. Reeleição em 2 s, mas nenhuma varredura periódica. Causa: o agendador chamava `/hotels` (agora exige JWT) e levava 401.
  Correção já escrita e com testes (137 passando), AINDA NÃO commitada nem reconstruída no cluster: rota `GET /internal/hotels` com HMAC
  (`api-gateway/main.py`) e `workers/scheduler.py` assinando o pedido. Falta `docker compose up --build -d` e repetir `bash scripts/chaos_demo.sh 2`.
- 3 RabbitMQ fora 20 s: PASS. 4 Redis fora 20 s: PASS (DLQ 0).
- 5 Xotelo bloqueada: FAIL (breakers abertos 0/3; durante o bloqueio respondeu SUCCESS com cache; não voltou a `live`). A investigar: o método de bloqueio
  (/etc/hosts) pode não impedir a chamada por causa do cache compartilhado no Redis (TTL 300 s) ou o breaker não conta falhas; conferir `workers/scraper.py`
  e `scripts/chaos_demo.sh` (experimento 5).
- 6 Postgres fora 15 s: não chegou a ser registrado; rodar `bash scripts/chaos_demo.sh 6`.

## Em andamento quando o limite acabou
Um agente adicionava autenticação HMAC em TODA chamada interna (Gateway->Worker `/scrape`, `/leader`, `/events`; Worker->Worker `/election`, `/coordinator`)
em `shared/servicetoken.py`, `workers/security.py`, `api-gateway/main.py`, `workers/election.py` e testes. Pode haver edições parciais NÃO commitadas.
Antes de commitar: `git status`, rodar a suíte inteira (REDIS_URL=redis://localhost:6380/0, Redis de teste na 6380) e só commitar se estiver verde.

## Falta (plano em ~/.claude/plans/misty-wondering-zephyr.md)
Fase F: arquivos de implantação multi-host na AWS (`deploy/compose.*.yml`), 3 EC2 em us-east-1, ensaio; depende da conta AWS do usuário.
Fase G: relatório final (base em docs/relatorio/), evidências de caos em docs/relatorio/evidencias/, revisão com security-review e architect-reviewer.
A nota docs/notas/2026-11-09-tolerancia-a-falhas.md está desatualizada (o compose já tem restart e healthchecks).
Push e PR só com autorização do usuário. Segredos ficam no `.env` local (não versionado).
