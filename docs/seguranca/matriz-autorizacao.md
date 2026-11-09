# Matriz de autorizacao

| Endpoint | Acesso |
|---|---|
| GET `/`, GET `/health`, `/static/*`, POST `/auth/login` | publico |
| GET `/auth/me` | qualquer usuario autenticado |
| GET `/hotels`, GET `/history`, POST `/jobs/dispatch`, POST `/sweeps`, GET `/sweeps/{id}`, GET `/registry/nodes` | analyst (admin tambem) |
| GET `/locks`, GET `/locks/{hotel}`, GET `/coordination/*`, POST `/hotels/refresh`, POST `/auth/users` | admin |
| POST `/registry/register` | servico (HMAC `X-Service-*` com `SERVICE_SECRET`) |

## Modelo de ameacas (resumo)
- **TLS (nginx)**: protege confidencialidade/integridade entre cliente e borda; nao cobre o trafego interno gateway-workers-Redis-RabbitMQ-Postgres (HTTP/AMQP em claro na rede Docker).
- **JWT**: autentica e autoriza usuarios (HS256, 30 min, papel lido do banco); nao ha revogacao por token (apenas expiracao/desativacao do usuario) e o segredo unico compromete todos os tokens.
- **HMAC de servico**: impede registro falso de workers e adulteracao de endpoint/worker_id; janela de 60 s permite replay dentro dela e nao cobre `/scrape` nem as rotas de eleicao dos workers.
- **Rede privada / Security Groups**: so 80/443 publicos; protegem portas internas, mas nao contra um container ou host interno comprometido.
- **Rate limit** (nginx + 5 falhas/min no app): mitiga forca bruta; contadores em memoria sao por processo e nao protegem contra ataque distribuido.
- Fora de escopo: WAF, auditoria centralizada, rotacao automatica de segredos, TLS mutuo interno.
