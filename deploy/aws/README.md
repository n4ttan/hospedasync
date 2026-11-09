# Implantação distribuída na AWS (3 instâncias EC2)

> **Estado:** implantação executada e verificada na AWS em 09/11/2026 (3 EC2 `t3.small`, us-east-1): `/health`, 3 nós, consenso de líder,
> portas internas fechadas, demo de coordenação e caos 6 de 6 (`docs/relatorio/evidencias/`). Ajustes aprendidos no uso: `provision.sh` é repetível
> (reusa SGs, regras e par de chaves; imagem via `describe-images`, sem permissão no SSM). A rede instável do operador exige `AWS_MAX_ATTEMPTS`/retries.

## Topologia

| Instância (tag Name) | Papel | Containers |
|---|---|---|
| EC2-1 `hospeda-edge` | edge | nginx (TLS 80/443), api-gateway, rabbitmq, redis-lock |
| EC2-2 `hospeda-workers-a` | workers A | worker-01, worker-02, postgres-db (primário) |
| EC2-3 `hospeda-workers-b` | workers B | worker-03, postgres-replica |

Tudo na mesma VPC (padrão) e na mesma subnet, falando por **IP privado**. Só o edge expõe 80/443 ao mundo;
SSH (22) só do seu IP; 8000, 8001-8003, 5432, 5672 e 6379 apenas entre os Security Groups (`provision.sh`).
O painel do RabbitMQ (15672) fica só no loopback do edge (use túnel SSH). Arquivos:
`compose.edge.yml`, `compose.workers-a.yml`, `compose.workers-b.yml`, `env.aws.example`, `user-data.sh`,
`provision.sh`, `start-all.sh`, `stop-all.sh`.

## 1. Conta, créditos e custos

- Crédito disponível: **US$ 72,98 até 21/03/2027**. Confira em Billing > Credits.
- **Alertas de orçamento** (Billing > Budgets): crie dois, em **US$ 30** e **US$ 50** (e-mail), antes de criar recursos.
- Estimativa (us-east-1, sob demanda, valores aproximados; confirme na calculadora da AWS):

| Item | Por hora (3 instâncias) | Por semana ligado 24x7 |
|---|---|---|
| 3x t3.small (US$ 0,0208/h cada) | ~US$ 0,062 | ~US$ 10,5 |
| 3x IPv4 público (US$ 0,005/h cada) | ~US$ 0,015 | ~US$ 2,5 |
| 3x EBS gp3 16 GB (~US$ 0,08/GB-mês) | ~US$ 0,005 | ~US$ 0,9 (cobra mesmo desligado) |
| **Total** | **~US$ 0,08** | **~US$ 14** |

  Com t3.micro o total cai para ~US$ 0,05/h (~US$ 9/semana). Ligado 24x7 o crédito dura ~5 semanas; **desligue fora
  dos testes** (`bash deploy/aws/stop-all.sh`): parado, só o EBS (e IPv4, se houver Elastic IP) é cobrado.
- **Ao terminar tudo**, termine (terminate) as instâncias, apague os volumes e os Security Groups.

## 2. Provisionar

`provision.sh` é um **template não testado** (cobra da sua conta). Leia, depois rode primeiro em simulação:

```bash
export MY_IP="$(curl -s https://checkip.amazonaws.com)/32"   # seu IP para o SSH
DRY_RUN=1 bash deploy/aws/provision.sh                        # só imprime
bash deploy/aws/provision.sh                                  # cria de verdade
```

Parâmetros por variável: `AWS_REGION`, `KEY_NAME`, `EDGE_TYPE`, `WA_TYPE`, `WB_TYPE`, `VOLUME_GB`, `AMI_PARAM`.
Alternativa manual: crie as instâncias no console (Amazon Linux 2023, `t3.small`, mesma subnet) colando
`user-data.sh` em "User data". Ao final anote os **IPs privados e o IP público do edge**.

## 3. Preparar cada instância

O `user-data.sh` instala Docker, plugin Compose e git (aguarde ~2 min; log em `/var/log/hospeda-bootstrap.log`).

```bash
ssh -i ~/.ssh/hospeda-key.pem ec2-user@<ip-publico>
docker compose version                         # confirma o bootstrap
git clone <URL-DO-REPOSITORIO> hospedasync && cd hospedasync   # repositório privado: use deploy key ou token
```

No seu computador, gere `.env.aws` a partir de `env.aws.example` (IPs **privados** + segredos com
`openssl rand -hex 24`; `JWT_SECRET` e `SERVICE_SECRET` com `-hex 32`) e copie o **mesmo arquivo** às 3 máquinas:

```bash
cp deploy/aws/env.aws.example .env.aws && chmod 600 .env.aws && $EDITOR .env.aws
for h in <ip-edge> <ip-wa> <ip-wb>; do scp -i ~/.ssh/hospeda-key.pem .env.aws ec2-user@$h:hospedasync/.env.aws; done
```

`.env.aws` está no `.gitignore`; nunca o versione.

## 4. Ordem de subida

Em cada máquina, dentro de `~/hospedasync` (valide antes com `config -q`):

1. **workers-a** (Postgres primário primeiro):
   `docker compose -f deploy/aws/compose.workers-a.yml --env-file .env.aws up --build -d`
   Os workers reiniciam até o edge existir; é normal nesta fase.
2. **workers-b** (a réplica faz `pg_basebackup` do primário em `WA_IP`):
   `docker compose -f deploy/aws/compose.workers-b.yml --env-file .env.aws up --build -d`
3. **edge** (precisa do certificado antes):
   ```bash
   bash scripts/gen_selfsigned_cert.sh <IP_PUBLICO_DO_EDGE>
   docker compose -f deploy/aws/compose.edge.yml --env-file .env.aws up --build -d
   ```

### Certificado

- **Autoassinado** (demo): `scripts/gen_selfsigned_cert.sh <IP_PUBLICO>` cria `deploy/nginx/certs/{fullchain,privkey}.pem`
  com o IP no SAN. O IP público **muda** se a instância for parada/iniciada (sem Elastic IP): regenere e rode
  `docker compose -f deploy/aws/compose.edge.yml --env-file .env.aws restart nginx`. Clientes usam `curl -k`.
- **Let's Encrypt**: precisa de domínio (ex.: DuckDNS) apontando ao edge; siga `docs/seguranca/tls-letsencrypt.md`
  (os caminhos `deploy/nginx/certs` são os mesmos; reinicie o nginx do `compose.edge.yml` na renovação).

## 5. Verificação pós-deploy

Do seu computador (`E=<ip-publico-edge>`):

```bash
curl -ksI https://$E/health | head -1                         # 200 via nginx/TLS
curl -sI  http://$E/health | head -1                          # 301 para https
curl -ks https://$E/health                                    # gateway: db, redis, rabbitmq ok
TOKEN=$(curl -ks -X POST https://$E/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<ADMIN_PASSWORD>"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -ks -H "Authorization: Bearer $TOKEN" https://$E/registry/nodes            # count = 3
curl -ks -H "Authorization: Bearer $TOKEN" https://$E/coordination/status       # consensus = true, 1 líder
nc -zv -w3 $E 8000; nc -zv -w3 $E 5432; nc -zv -w3 $E 6379                      # devem FALHAR (portas internas fechadas)
```

Nas instâncias:

```bash
# workers-a:  docker ps (3 containers healthy);  docker exec hospeda_db psql -U postgres -d hospedasync_db \
#               -c "select client_addr,state,sync_state from pg_stat_replication;"      # 1 linha, state=streaming
# workers-b:  docker exec hospeda_db_replica psql -U postgres -d hospedasync_db -c "select pg_is_in_recovery();"  # t
# edge:       docker logs hospeda_gateway --tail 20;  docker ps
```

## 6. Demos e caos

```bash
export GATEWAY_URL=https://<ip-edge> CURL_INSECURE=1 ADMIN_PASSWORD=<senha>
bash scripts/demo_coordenacao.sh        # usa scripts/_auth.sh: CURL_INSECURE=1 vira curl -k
bash scripts/demo_fila_falha.sh
```

`CURL_INSECURE=1` só afeta `scripts/_auth.sh` (sem a variável o comportamento não muda). `scripts/chaos_demo.sh`
tem seu próprio `curl` e **não foi alterado**; para aceitar o certificado autoassinado, sem editar o script:

```bash
mkdir -p ~/.curl-insecure && echo insecure > ~/.curl-insecure/.curlrc
CURL_HOME=~/.curl-insecure GATEWAY_URL=https://<ip-edge> ADMIN_USER=admin ADMIN_PASSWORD=<senha> bash scripts/chaos_demo.sh 6
```

O `chaos_demo.sh` age com `docker stop/kill/exec hospeda_*` **no host local**; na AWS os containers estão em
máquinas diferentes. Onde rodar cada experimento (por SSH, com `docker` acessível ao usuário):

| Exp. | Alvo (container) | Onde funciona |
|---|---|---|
| 1 worker morto em varredura | `hospeda_worker_02` | rodar **no workers-a** (o script também usa `curl` ao gateway via `GATEWAY_URL`, ok remoto) |
| 2 líder morto | worker de maior ID (`hospeda_worker_03`) | rodar no **workers-b** (ou matar o container à mão onde estiver o líder) |
| 3 RabbitMQ fora 20 s | `hospeda_rabbitmq` | **só no edge** (API de gerenciamento em `localhost:15672` também é do edge) |
| 4 Redis fora 20 s | `hospeda_redis_lock` | **só no edge** |
| 5 Xotelo bloqueada | `docker exec` em todos os workers | **parcial**: afeta só os workers locais da máquina; execute em cada host |
| 6 Postgres fora 15 s | `hospeda_db` | **só no workers-a** |

Na prática, o script só completa os 6 experimentos com todos os containers na mesma máquina (cluster local).
Na AWS, rode o experimento correspondente no host dono do container, ou adapte as chamadas `docker` para
`ssh ec2-user@<ip> docker ...`. As verificações via HTTP (`GATEWAY_URL`) funcionam de qualquer lugar; as de
`docker inspect/exec` não. Não foi feito esse porte.

## 7. Operação e desligamento

```bash
bash deploy/aws/stop-all.sh     # para as 3 (containers voltam sozinhos: restart unless-stopped)
bash deploy/aws/start-all.sh    # inicia; mostra os novos IPs públicos (regenere o certificado)
```

Reinicie na ordem do item 4 se algo não reconectar. Dados do Postgres ficam em volume Docker no EBS (persistem
ao parar). Para recomeçar do zero: `docker compose -f ... down -v` na máquina correspondente.

## Limitações conhecidas

- Não testado na AWS; `provision.sh`, `start-all.sh` e `stop-all.sh` nunca foram executados contra a conta.
- Gateway e workers não têm `depends_on` entre máquinas: dependem de tentativas/reinícios até o outro lado subir.
- Edge é ponto único de falha (gateway, RabbitMQ, Redis); a replica é apenas leitura (sem promoção automática).
- Sem Elastic IP, o IP público muda a cada stop/start (com Elastic IP, cobra-se IPv4 mesmo desligado).
