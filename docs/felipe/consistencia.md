# Consistência dos dados de scraping (PostgreSQL)

Responsável: Felipe. As respostas marcadas "(do código)" foram preenchidas a partir do código. Confira cada uma e complete os `[PREENCHER]`.

## Definições rápidas

* **Consistência forte:** depois que uma escrita é confirmada, qualquer leitura seguinte já enxerga o valor novo. O sistema se comporta como se existisse uma única cópia do dado.
* **Consistência eventual:** se não houver novas escritas, todas as cópias acabam convergindo para o mesmo valor, mas uma leitura logo após a escrita pode ainda mostrar o valor antigo.

## 1. Onde o dado é gravado?

(do código) Em uma única instância do PostgreSQL (contêiner `hospeda_db`), tabela `job_history`, com `job_id` como chave primária. Quem grava é o API Gateway (`api-gateway/main.py`, função `dispatch_job`), não o Worker. O volume `postgres_data` mantém os dados se o contêiner reiniciar.

Sua confirmação: [PREENCHER: confirma? o que você viu no `psql`?]

## 2. Quando a resposta volta ao usuário?

(do código) Depois do `commit`. O Gateway chama o Worker, espera o resultado, grava no PostgreSQL com `db.commit()` e só então devolve a resposta ao cliente. Ponto de atenção: se o commit falhar, o código faz `rollback`, imprime o erro e mesmo assim responde SUCCESS ao cliente, sem o dado no banco.

Sua observação: [PREENCHER: isso é um problema para a consistência? por quê?]

## 3. De onde o dashboard lê?

(do código) De `GET /history`, que consulta o mesmo PostgreSQL e traz os 50 registros mais recentes. Não há cache nem outra fonte de leitura.

Dica: o dashboard e a escrita usam o mesmo banco, então o que muda para a leitura?
Sua resposta: [PREENCHER]

## 4. Existe réplica?

(do código) Não hoje. Há só um PostgreSQL (`docker-compose.yml` tem um único serviço `postgres-db`), sem standby, sem réplica de leitura e sem replicação configurada.

Sua resposta: [PREENCHER: o que uma réplica traria? o que ela complicaria?]

## 5. Qual modelo de consistência é esse e por quê?

Pré-resposta para você revisar: como há uma única cópia do dado, e a escrita é confirmada (commit) antes da resposta, uma leitura feita depois da resposta enxerga o dado. Isso é comportamento de consistência forte.

Dica: o que mudaria se existisse uma réplica de leitura atrasada em relação ao banco principal?
Sua resposta: [PREENCHER: forte ou eventual, e o porquê em 2 a 3 frases]

## 6. O que acontece se o Postgres cair?

(do código) O Gateway sobe mesmo sem o banco (o `init_db` tenta 10 vezes, a cada 3 s). Com o banco fora, o job é executado no Worker, mas a gravação falha, o erro é só impresso no log e `/history` falha. Teste no passo 5 do `LEIA-ME.md` e registre aqui o que você viu.

Sua resposta: [PREENCHER: o que o usuário vê? o dado do job se perde? o que seria uma melhoria?]
