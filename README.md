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
