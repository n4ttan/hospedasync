# Fonte de dados real: API Xotelo

Decisão de 09/11/2026. O worker deixou de simular preço (antes: `280 + random`) e consulta a API Xotelo
(`https://data.xotelo.com/api`), que devolve a diária por OTA (Booking.com, Trip.com, Vio, Accor, Agoda...) com base em dados do TripAdvisor.

## Por que essa fonte
- Preço real e comparável entre OTAs, JSON estável, sem chave, gratuita.
- Não raspamos site de terceiro (Booking/Expedia proíbem nos termos e bloqueiam com captcha).
- Alternativas descartadas: Amadeus Self-Service (desligado em 17/07/2026), SerpApi (250 buscas/mês no plano grátis), Inside Airbnb (histórico, não é cotação ao vivo).

## Por que justifica um sistema distribuído
Cada cotação leva cerca de 4 s e o trabalho cobre hotel x data x OTA: é I/O lento e paralelizável. Workers em paralelo, lock por hotel
(evita duas consultas simultâneas ao mesmo hotel) e líder como agendador fazem sentido para esse dado.

## Tolerância a falhas do dado
Retry com backoff (3 tentativas) e cache de 5 min no worker; se a Xotelo cair, o Gateway devolve a última cotação real gravada, marcada
`source=cache`, `stale=true` e `gateway_status=DEGRADED`.

## Riscos assumidos (declarar no relatório)
API de terceiro, não oficial, sem termos publicados. O endpoint `/search` só existe via RapidAPI, por isso o catálogo de 100 hotéis do Rio
(`api-gateway/data/hotels_rio.json`) vem de `/list`. Cache do worker é em memória (por processo); cache compartilhado em Redis fica para a fase de mensageria.

## Prova de que é real
Teste em 09/11/2026: Copacabana Palace, 18 a 20/12/2026, Booking.com R$ 7.456, `source=live`, 3,9 s.
Cada registro guarda OTA, valor, `fetched_at` e link do TripAdvisor.
