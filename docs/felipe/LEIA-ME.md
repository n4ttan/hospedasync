# Felipe: Replicação e Consistência

Sua parte do trabalho é explicar como os dados de scraping são guardados no PostgreSQL e qual modelo de consistência o sistema tem hoje. Você não precisa programar. É rodar alguns comandos, olhar o resultado e responder perguntas no arquivo `docs/felipe/consistencia.md`. Já deixei preenchido o que o código mostra. Seu trabalho é conferir e escrever o "por quê" com suas palavras, que é a parte que o professor mais valoriza.

Rode os comandos na raiz do projeto (`hospedasync`), em um terminal com Docker instalado.

## Passo 1: subir o sistema

```bash
docker compose up --build -d
```

Espere uns 15 segundos e confira:

```bash
curl -s http://localhost:8000/health
```

Deve aparecer `"status":"UP"`.

## Passo 2: disparar um job

```bash
curl -s -X POST http://localhost:8000/jobs/dispatch \
  -H "Content-Type: application/json" \
  -d '{"hotel_name":"Hotel Felipe","checkin_date":"2026-12-01","checkout_date":"2026-12-03"}'
```

Anote o `job_id` e o `selected_worker_id` da resposta.

## Passo 3: conferir que o dado foi gravado

Pela API (é o que o dashboard usa):

```bash
curl -s http://localhost:8000/history
```

Direto no banco:

```bash
docker exec hospeda_db psql -U postgres -d hospedasync_db -c "select job_id, hotel_name, processed_by from job_history order by created_at desc limit 5;"
```

O `job_id` do passo 2 deve aparecer nos dois. Se aparecer, o dado foi gravado.

## Passo 4: preencher `docs/felipe/consistencia.md`

1. Abra o arquivo (já existe, com as respostas pré-preenchidas).
2. Releia cada resposta e confira com o que você viu nos passos 2 e 3. Se algo não bater, corrija.
3. Troque cada `[PREENCHER]` por uma ou duas frases suas. Há dicas dentro do arquivo.
4. As definições de consistência forte e eventual estão no arquivo, para você consultar.

## Passo 5 (opcional): ver o que acontece se o Postgres cair

```bash
docker stop hospeda_db
curl -s -X POST http://localhost:8000/jobs/dispatch -H "Content-Type: application/json" -d '{"hotel_name":"Hotel Teste","checkin_date":"2026-12-01","checkout_date":"2026-12-03"}'
curl -s http://localhost:8000/history
docker start hospeda_db
```

Observe a resposta de cada comando e use isso na última pergunta do arquivo. Depois do `docker start`, espere alguns segundos antes de testar de novo.

## Se algo não funcionar

* `docker compose ps` mostra o estado dos contêineres.
* `docker compose logs api-gateway --tail 30` mostra os últimos logs do Gateway.
* Qualquer dúvida, chame o Nattan ou o Rafael. Perguntar cedo poupa tempo.
