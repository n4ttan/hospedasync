# Estado e pendências

Branch `feature/messaging`, tudo commitado e enviado ao GitHub (PR #2 para `develop`, aberto). Implantado e verificado na AWS em 09/11/2026 (3 EC2, us-east-1); as máquinas ficam desligadas fora dos ensaios (`deploy/aws/stop-all.sh`).

## Feito e verificado
- Dados reais (Xotelo), fila RabbitMQ, lock por hotel, Bully com Lamport, líder agendador, réplica Postgres com consistência forte/eventual medida,
  breaker e failover, JWT/RBAC, HMAC em toda chamada interna, Nginx com TLS, segredos no `.env` local.
- Caos (`scripts/chaos_demo.sh all`, 09/11/2026): 6 de 6 PASS em uma execução completa. Evidência em `docs/relatorio/evidencias/chaos_2026-11-09.md`.
- Dois defeitos achados pelo próprio caos e corrigidos: agendador levava 401 em `/hotels` depois do JWT (agora usa `/internal/hotels` com HMAC);
  502 do worker por queda da fonte abria o breaker do gateway (agora só conta falha de conexão); o script de caos usava a mesma data a cada chamada (cache).
- Suíte: 168 testes verdes (Redis de teste na porta 6380).
- AWS (09/11/2026): demo de coordenação (mutex 200/409, queda e retorno do líder em 5 a 8 s) em `evidencias/aws_demo_2026-11-09.md`; caos 6 de 6 PASS em `evidencias/chaos_aws_2026-11-09.md` (`scripts/aws_chaos.sh`, docker por SSH e túneis).
- Revisões feitas (segurança e arquitetura). Corrigido: senha do RabbitMQ no log, primário fora do ar (auth pela réplica, 503), TTL do lock e timeout do dispatch, reeleição do Bully com ID maior vivo, docs reconciliados.

## Falta
- Rafael (nomeação) e Felipe (consistência): revisar o PR #2 e fazer o merge no `develop`; roteiros em `docs/felipe/`.
- Ensaio da demo ao vivo na AWS (`deploy/aws/start-all.sh`; IP público muda, regenerar o certificado) e relatório final com prints.
- Não verificados na AWS: demo da fila com falha, `pg_stat_replication` e o experimento de consistência.
- Itens baratos pendentes da revisão: MAC cobrir o corpo, bloquear `/internal/` e `/docs` no Nginx, fixar versões nos `requirements.txt`.

## Limites conhecidos (declarar no relatório)
Redis, RabbitMQ e gateway únicos; DLQ sem consumidor; Postgres sem failover automático (promoção manual documentada);
Bully sem quórum; líder não influencia o roteamento do dispatch; canal interno em HTTP com assinatura HMAC, sem TLS interno;
fonte de preços de terceiro, não oficial; sem gRPC (REST atende o enunciado).
