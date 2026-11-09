# Coordenação: decisões (2026-11-09)

Responsável: Nattan. Código: `workers/election.py`, `api-gateway/distlock.py`, `api-gateway/main.py`.

## Por que Bully
* O cluster é pequeno (3 Workers) e todos se conhecem via `PEER_WORKERS`, então o custo de mensagens do Bully é irrelevante.
* É simples de explicar e de demonstrar: derrubar o worker-03 e ver o worker-02 assumir.
* O ID vem do hostname (`worker-NN`), sem configuração extra e sem coordenação para atribuir IDs.

## Por que SET NX EX
* Uma única operação no Redis cria a chave só se ela não existir e já define a expiração. Não há janela entre "criar" e "definir TTL".
* O TTL de 15 s evita lock órfão se o Gateway travar no meio do despacho.
* A liberação usa script Lua (compara o dono e apaga numa operação atômica), para que um job lento não apague um lock já readquirido por outro.
* O dono é o `job_id`, que é único por requisição.

## Por que term por timestamp
* Um contador incremental volta a zero quando o processo reinicia. O worker-03 religado seria tratado como obsoleto pelos pares que já tinham visto um term maior.
* `time.time()` cresce entre restarts e continua servindo para ignorar `/coordinator` atrasado do mesmo líder.
* Custo: depende de relógios razoavelmente alinhados. No Docker Compose todos os contêineres usam o relógio do mesmo host, então isso não aparece.

## Limitações
* O líder é eleito e exposto (`/leader`, `/coordination/status`), mas o Gateway ainda sorteia o Worker no `/jobs/dispatch`. A eleição ainda não influencia o roteamento.
* O lock usa um único Redis. Não há Redlock nem réplicas; o Redis é ponto único de falha.
* O Bully não tem quórum: numa partição de rede podem existir dois líderes.
* O lock protege o hotel durante o despacho. Ele não cobre a janela entre a resposta do Worker e o commit no PostgreSQL além do que já está dentro do `try/finally` do Gateway.
