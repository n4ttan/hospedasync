# HospedaSync 🏨🔄

Plataforma distribuída para monitoramento inteligente e análise concorrencial de tarifas hoteleiras em nuvem (AWS).

---

## Visão Geral do Sistema

O sistema é construído sobre uma arquitetura de microsserviços desacoplados para automação de coletas tarifárias:
* **API Gateway (`api-gateway`):** Ponto de entrada REST; recebe as ordens de monitoramento, gera identificadores únicos de tarefa (`job_id`) e despacha chamadas assíncronas para os nós coletores.
* **Worker Node (`workers`):** Serviço distribuído responsável por simular/executar a extração tarifária e retornar métricas de processamento e preços estruturados.

---

## Stack Tecnológica

* **Linguagem:** Python 3.11+
* **Framework:** FastAPI + Uvicorn (I/O assíncrono não bloqueante)
* **Comunicação Inter-serviços:** REST com cliente HTTP assíncrono (`httpx`)
* **Virtualização:** Docker e Docker Compose
* **Nuvem:** AWS (EC2)

---

## 🚀 Como Executar

### Opção 1: Via Docker Compose (Recomendado)

Suba toda a malha de serviços em contêineres interligados por rede virtual interna:

```bash
docker compose up --build -d
```

* Gateway: http://localhost:8000
* Worker: http://localhost:8001

### Opção 2: Localmente (sem Docker)

```bash
# Worker (em um terminal)
cd workers
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8001

# API Gateway (em outro terminal)
cd api-gateway
pip install -r requirements.txt
WORKER_SERVICE_URL=http://127.0.0.1:8001 uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## 🔗 Comunicação entre Serviços (REST)

O **API Gateway** e o **Worker Node** são dois processos independentes que se comunicam de forma assíncrona via HTTP/REST, usando `httpx.AsyncClient`. Não compartilham memória nem banco de dados entre si — toda a interação passa pela rede (via Docker network interna, `hospeda-net`).

### Fluxo de uma requisição

```
Cliente → POST /jobs/dispatch → API Gateway
                                     │
                                     │ gera job_id (uuid4)
                                     │ POST /scrape (via httpx, timeout 10s)
                                     ▼
                                Worker Node
                                     │
                                     │ simula coleta de tarifa (latência 0.2–0.5s)
                                     ▼
                                API Gateway ← resposta JSON
                                     │
                                     ▼
                                Cliente ← resposta agregada
```

### Endpoints

#### `GET /health` (Gateway e Worker)
Verificação de disponibilidade do serviço.

**Resposta 200:**
```json
{"status": "UP", "service": "api-gateway"}
```

#### `POST /jobs/dispatch` (API Gateway)
Recebe uma ordem de monitoramento e despacha para o Worker.

**Request:**
```json
{
  "hotel_name": "Hotel Termas",
  "checkin_date": "2026-10-01",
  "checkout_date": "2026-10-03"
}
```

**Response 200:**
```json
{
  "gateway_status": "SUCCESS",
  "dispatched_to": "http://worker-node:8001",
  "result": {
    "job_id": "job-d7b2c9e1",
    "hotel_name": "Hotel Termas",
    "daily_rate": 253.34,
    "currency": "BRL",
    "collected_at": "2026-09-21T18:41:45.846997",
    "processing_time_ms": 375.01
  }
}
```

**Erros:**
* `502` — Worker respondeu com erro
* `503` — Falha de comunicação distribuída (worker indisponível, timeout, DNS)

#### `POST /scrape` (Worker Node)
Executa a coleta (simulada) de tarifa para um hotel/período. Chamado internamente pelo Gateway — não é exposto ao cliente final.

**Request:**
```json
{
  "job_id": "job-d7b2c9e1",
  "hotel_name": "Hotel Termas",
  "checkin_date": "2026-10-01",
  "checkout_date": "2026-10-03"
}
```

**Response 200:**
```json
{
  "job_id": "job-d7b2c9e1",
  "hotel_name": "Hotel Termas",
  "daily_rate": 253.34,
  "currency": "BRL",
  "collected_at": "2026-09-21T18:41:45.846997",
  "processing_time_ms": 375.01
}
```

### Tratamento de falhas de comunicação distribuída

O Gateway usa `httpx.AsyncClient(timeout=10.0)` e captura `httpx.RequestError` (timeout, conexão recusada, DNS não resolvido) para não propagar uma exceção não tratada ao cliente — em vez disso, retorna `503` com uma mensagem indicando falha de comunicação distribuída. Isso evita que uma falha no Worker derrube o Gateway.

---

## ☁️ Implantação em Nuvem (AWS EC2)

O sistema foi implantado em uma instância **EC2 t3.micro** (free tier), região **sa-east-1 (São Paulo)**, rodando Amazon Linux 2023.

### Passos realizados

1. Criação da instância EC2 (t3.micro, Amazon Linux 2023)
2. Configuração do Security Group liberando as portas:
   - `22` (SSH, acesso administrativo)
   - `8000` (API Gateway)
   - `8001` (Worker Node)
3. Conexão via SSH e instalação do Docker + Docker Compose plugin:
   ```bash
   sudo dnf install -y docker git
   sudo systemctl enable --now docker
   ```
4. Clone do repositório e build/subida dos containers:
   ```bash
   git clone https://github.com/n4ttan/hospedasync
   cd hospedasync
   sudo docker compose up --build -d
   ```
5. Verificação de funcionamento (endpoints acessíveis publicamente pela internet):
   ```bash
   curl http://<IP_PUBLICO>:8000/health
   curl http://<IP_PUBLICO>:8001/health
   ```

### Observação de segurança

As portas 8000/8001 estão liberadas para `0.0.0.0/0` (qualquer origem) propositalmente, para permitir demonstração e avaliação externa. Em um ambiente de produção real, isso seria restringido por autenticação na API e/ou por um Security Group mais restritivo, e o SSH (porta 22) seria limitado ao IP dos administradores.

---

## Coordenação (Eleição de Líder e Exclusão Mútua)

Esta seção descreve os dois mecanismos de coordenação do sistema, implementados em `workers/election.py` (eleição) e `api-gateway/distlock.py` (lock distribuído).

### O que cada mecanismo resolve

* **Eleição de líder (Bully):** os três Workers precisam concordar sobre qual deles é o coordenador do grupo. Se o líder cai, os demais detectam a queda e elegem outro sem intervenção manual.
* **Exclusão mútua (lock no Redis):** impede que duas requisições processem o mesmo hotel ao mesmo tempo, mesmo que caiam em Workers diferentes.

### Como funciona o Bully

* **IDs pelo hostname:** o ID numérico de cada Worker é o número no fim do hostname de `SELF_URL` (`worker-01` vira 1, `worker-03` vira 3). Quanto maior o ID, maior a prioridade. Se o hostname não terminar em dígitos, o Worker falha na inicialização.
* **Pares conhecidos:** cada Worker recebe a lista dos demais em `PEER_WORKERS` (ver `docker-compose.yml`).
* **Início da eleição:** 3 s após subir, cada Worker inicia uma eleição. Ele envia `POST /election` apenas aos pares de ID maior. Se algum responde `OK`, esse par assume a eleição. Se nenhum responde, o Worker se proclama líder.
* **`POST /coordinator`:** o novo líder avisa todos os pares com `leader_id` e `term`. Cada Worker guarda quem é o líder atual.
* **`GET /leader`:** cada Worker informa em quem ele acredita (`leader_id`) e se ele próprio é o líder (`am_i_leader`).
* **Detecção de queda:** a cada 3 s, cada Worker que não é líder consulta `GET /health` do líder. Após 2 falhas consecutivas (timeout de 2 s por tentativa), inicia nova eleição.
* **Term por timestamp:** o `term` é `time.time()` no momento da eleição, e não um contador. Um contador reinicia a cada restart do processo, e um líder que acabou de voltar (ex.: worker-03) seria descartado como obsoleto. O timestamp serve para ignorar `/coordinator` atrasado do mesmo líder que chegue fora de ordem.

### Como funciona o lock

* **Aquisição:** `POST /jobs/dispatch` executa `SET lock:hospedasync:<hotel_name> <job_id> NX EX 15`. A chave só é criada se não existir (NX) e expira sozinha em 15 s (EX), então um Gateway que trave não deixa o hotel preso para sempre.
* **Conflito:** se o lock já existe, o Gateway responde `409` sem chamar nenhum Worker.
* **Liberação atômica:** ao terminar (sucesso ou erro), o Gateway libera o lock com um script Lua que compara o valor guardado com o `job_id` e só então faz `DEL`. Assim, um job lento não apaga o lock que já expirou e foi readquirido por outro job.
* **Dono do lock:** o valor guardado é o `job_id` da requisição.

### Limitações

* **O líder não despacha trabalho.** O líder é eleito e exposto (`/leader`, `/coordination/status`), mas o Gateway ainda escolhe o Worker por sorteio entre os registrados. Hoje a eleição não influencia o roteamento.
* **Redis de instância única.** O lock depende de um único Redis (`redis-lock`). Não há Redlock nem réplicas: se o Redis cair, o dispatch deixa de funcionar com lock e o ponto único de falha fica nele.
* **TTL fixo de 15 s.** Se um job passar disso, o lock expira antes do fim. O timeout do Gateway para o Worker é 10 s, então no fluxo atual isso não ocorre.
* **Sem quórum no Bully.** Numa partição de rede, cada lado pode eleger seu próprio líder.

### Endpoints de coordenação

| Serviço | Método e caminho | Função |
|---|---|---|
| Worker (8001 a 8003) | `POST /election` | Recebe pedido de eleição; responde `OK` se o ID for maior que o do candidato |
| Worker | `POST /coordinator` | Recebe o anúncio do novo líder (`leader_id`, `term`) |
| Worker | `GET /leader` | Retorna `leader_id` e `am_i_leader` |
| Gateway (8000) | `POST /jobs/dispatch` | Despacha o job; `409` se o hotel já está em processamento |
| Gateway | `GET /locks` | Lista locks ativos (hotel, dono, TTL restante); `503` se o Redis estiver fora |
| Gateway | `GET /locks/{hotel_name}` | Informa se um hotel específico está travado |
| Gateway | `GET /coordination/status` | Pergunta a cada Worker registrado quem é o líder; retorna `leader_id`, `consensus`, `worker_views` e `active_locks` |

### Roteiro de demonstração

Com o sistema no ar (`docker compose up --build -d`), aguarde cerca de 10 s para a primeira eleição.

1. Ver o líder. Esperado: `leader_id` 3 e `consensus` true.
   ```bash
   curl -s http://localhost:8000/coordination/status
   ```
2. Derrubar o líder.
   ```bash
   docker stop hospeda_worker_03
   ```
3. Ver a reeleição (leva cerca de 10 a 15 s, e o registro do worker-03 sai da lista após 15 s). Esperado: `leader_id` 2.
   ```bash
   curl -s http://localhost:8000/coordination/status
   curl -s http://localhost:8002/leader
   ```
4. Disparar dois jobs simultâneos no mesmo hotel. Um retorna `200` e o outro `409`. Como o scraping simulado dura de 0,1 a 0,4 s, repita o comando se os dois passarem.
   ```bash
   BODY='{"hotel_name":"Hotel Termas","checkin_date":"2026-12-01","checkout_date":"2026-12-03"}'
   for i in 1 2; do
     curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/jobs/dispatch \
       -H "Content-Type: application/json" -d "$BODY" &
   done; wait
   ```
5. Religar o worker-03. Ele volta a ser o líder (maior ID) após cerca de 5 s.
   ```bash
   docker start hospeda_worker_03
   sleep 8
   curl -s http://localhost:8000/coordination/status
   ```

O script `scripts/demo_coordenacao.sh` automatiza esse roteiro.

### Testes automatizados

A pasta `tests/` contém os testes de coordenação. Para executar:

```bash
pip install -r requirements-dev.txt && pytest -q
```
