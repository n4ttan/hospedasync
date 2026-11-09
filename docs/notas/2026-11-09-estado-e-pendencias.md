# Estado e pendências

Branch `feature/messaging`, tudo commitado localmente. Nada enviado ao GitHub. Nada implantado na AWS.

## Feito e verificado
- Dados reais (Xotelo), fila RabbitMQ, lock por hotel, Bully com Lamport, líder agendador, réplica Postgres com consistência forte/eventual medida,
  breaker e failover, JWT/RBAC, HMAC em toda chamada interna, Nginx com TLS, segredos no `.env` local.
- Caos (`scripts/chaos_demo.sh all`, 09/11/2026): 6 de 6 PASS em uma execução completa. Evidência em `docs/relatorio/evidencias/chaos_2026-11-09.md`.
- Dois defeitos achados pelo próprio caos e corrigidos: agendador levava 401 em `/hotels` depois do JWT (agora usa `/internal/hotels` com HMAC);
  502 do worker por queda da fonte abria o breaker do gateway (agora só conta falha de conexão); o script de caos usava a mesma data a cada chamada (cache).
- Suíte: 164 testes verdes (Redis de teste na porta 6380).

## Falta
- AWS: subir 3 EC2 com `deploy/aws/` (NUNCA testado na AWS; depende da conta do usuário). Rodar demo e caos lá.
- Revisões: `security-review` e `architect-reviewer` antes da entrega 4 (07/12).
- Relatório final: base em `docs/relatorio/` (matriz, arquitetura, roteiro); incluir prints e logs reais.
- Rafael (nomeação) e Felipe (consistência): combinar merge no `develop`; roteiros já em `docs/felipe/`.
- Push e PR só com autorização do usuário.

## Limites conhecidos (declarar no relatório)
Redis, RabbitMQ e gateway únicos; DLQ sem consumidor; Postgres sem failover automático (promoção manual documentada);
Bully sem quórum; líder não influencia o roteamento do dispatch; canal interno em HTTP com assinatura HMAC, sem TLS interno;
fonte de preços de terceiro, não oficial; sem gRPC (REST atende o enunciado).
