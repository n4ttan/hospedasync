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
* **Comunicação Inter-serviços:** REST com cliente HTTP assíncrono (`httpx`)[cite: 1]
* **Virtualização:** Docker e Docker Compose[cite: 1]
* **Nuvem:** AWS (EC2 / ECS)[cite: 1]

---

## 🚀 Como Executar

### Opção 1: Via Docker Compose (Recomendado)

Suba toda a malha de serviços em contêineres interligados por rede virtual interna[cite: 1]:

```bash
docker compose up --build -d

Gateway: http://localhost:8000

Worker: http://localhost:8001
