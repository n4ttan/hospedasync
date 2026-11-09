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

## Autenticação entre serviços

Toda chamada interna carrega `X-Service-Id`, `X-Timestamp` e `X-Service-Signature`
(HMAC-SHA256 com `SERVICE_SECRET`, em `shared/servicetoken.py`). Nas chamadas de rota, o MAC cobre
`id|MÉTODO|path|timestamp`; assim, uma assinatura capturada de `/leader` não vale para `/coordinator`
nem para outro método. Tolerância de relógio de 60 s. Falha fechada: `SERVICE_SECRET` vazio
faz o receptor recusar tudo (401) e o emissor não assina.

| Chamador | Rota (destino) | Mecanismo |
|---|---|---|
| Worker | `POST /registry/register` (gateway) | HMAC (id + endpoint + timestamp) |
| Worker (scheduler) | `GET /internal/hotels` (gateway) | HMAC (id + `internal:hotels` + timestamp) |
| Gateway (`api-gateway`) | `POST /scrape` (worker) | HMAC de requisição (`require_peer`) |
| Gateway (`api-gateway`) | `GET /leader`, `GET /events` (worker) | HMAC de requisição (`require_peer`) |
| Worker (`WORKER_ID`) | `POST /election`, `POST /coordinator` (outro worker) | HMAC de requisição (`require_peer`) |
| Docker healthcheck / monitor de líder | `GET /health` (worker) | Público, sem assinatura (só estado básico) |

### Limite honesto

O canal interno é HTTP puro dentro da rede privada (rede Docker / VPC com Security Groups).
A assinatura garante **autenticidade e integridade** do pedido (quem tem o segredo, e o método/path
não foram alterados), **não confidencialidade**: o corpo e as respostas trafegam em claro, e dentro
da janela de 60 s um pedido capturado pode ser reenviado (replay) com o mesmo método e path.
O segredo é compartilhado entre todos os serviços, então um nó comprometido pode se passar por qualquer
outro. O próximo passo é TLS interno com mTLS (certificado por serviço) e, se necessário, nonce
anti-replay e segredos por serviço.
