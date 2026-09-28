import os
import re
import time
import asyncio
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

WORKER_ID = os.getenv("WORKER_ID", "worker-00")
SELF_URL = os.getenv("SELF_URL", f"http://127.0.0.1:{os.getenv('PORT', '8001')}")
PEER_WORKERS = [url.strip() for url in os.getenv("PEER_WORKERS", "").split(",") if url.strip()]

ELECTION_TIMEOUT = 2.0
HEARTBEAT_INTERVAL = 3.0
INITIAL_ELECTION_DELAY = 3.0
MAX_LEADER_FAILURES = 2


def extract_id_from_url(url: str) -> int:
    """Extrai o ID numérico do Bully a partir do hostname (ex.: worker-02 -> 2)."""
    host = urlparse(url).hostname or url
    match = re.search(r"(\d+)$", host)
    if not match:
        raise ValueError(
            f"Não foi possível derivar um ID numérico do Bully a partir de '{url}' "
            f"(hostname '{host}' precisa terminar em dígitos, ex.: worker-01)"
        )
    return int(match.group(1))


MY_ID = extract_id_from_url(SELF_URL)
PEERS = {MY_ID: SELF_URL}
for _peer_url in PEER_WORKERS:
    PEERS[extract_id_from_url(_peer_url)] = _peer_url

CURRENT_LEADER_ID = None
# O "term" é um timestamp (time.time()), não um contador incremental: um contador
# reseta a cada restart do processo, o que faria um líder que acabou de voltar
# (ex.: worker-03 reassumindo após reiniciar) ser descartado como "obsoleto" por
# quem já tinha visto um term maior antes dele cair. Timestamp de parede é
# monotônico mesmo entre restarts, então segue servindo para descartar mensagens
# de rede fora de ordem sem esse efeito colateral.
election_term = 0.0
election_in_progress = False
election_lock = asyncio.Lock()
# Último term de coordinator aceito por leader_id, para descartar mensagens
# atrasadas do MESMO líder que chegam fora de ordem pela rede.
_last_coordinator_term_by_leader = {}


class ElectionRequest(BaseModel):
    candidate_id: int
    term: float


class CoordinatorRequest(BaseModel):
    leader_id: int
    term: float


@router.post("/election")
async def receive_election(payload: ElectionRequest):
    if MY_ID > payload.candidate_id:
        print(f"[{WORKER_ID}] Recebida eleição de candidato {payload.candidate_id}, respondendo OK e iniciando própria eleição")
        asyncio.create_task(start_election())
        return {"status": "OK", "responder_id": MY_ID}
    return {"status": "IGNORED", "reason": "responder_id menor ou igual ao candidate_id"}


@router.post("/coordinator")
async def receive_coordinator(payload: CoordinatorRequest):
    global CURRENT_LEADER_ID
    last_term = _last_coordinator_term_by_leader.get(payload.leader_id, -1)
    if payload.term < last_term:
        print(f"[{WORKER_ID}] Coordinator obsoleto de worker {payload.leader_id} (term={payload.term} < {last_term}), ignorando")
        return {"status": "STALE", "worker_id": MY_ID}
    _last_coordinator_term_by_leader[payload.leader_id] = payload.term
    CURRENT_LEADER_ID = payload.leader_id
    print(f"[{WORKER_ID}] Novo líder reconhecido via coordinator: worker com ID {payload.leader_id} (term={payload.term})")
    return {"status": "ACK", "worker_id": MY_ID}


@router.get("/leader")
async def get_leader():
    return {"leader_id": CURRENT_LEADER_ID, "am_i_leader": CURRENT_LEADER_ID == MY_ID}


async def become_leader(term: float):
    global CURRENT_LEADER_ID
    CURRENT_LEADER_ID = MY_ID
    print(f"[{WORKER_ID}] Autoproclamado líder (term={term})")
    async with httpx.AsyncClient(timeout=ELECTION_TIMEOUT) as client:
        for peer_id, url in PEERS.items():
            if peer_id == MY_ID:
                continue
            try:
                await client.post(f"{url}/coordinator", json={"leader_id": MY_ID, "term": term})
                print(f"[{WORKER_ID}] Enviando coordinator para {url} (novo líder={MY_ID})")
            except Exception as e:
                print(f"[{WORKER_ID}] Falha ao avisar {url} sobre nova liderança ({e})")


async def start_election():
    global election_in_progress, election_term

    async with election_lock:
        if election_in_progress:
            return
        election_in_progress = True
        election_term = time.time()
        my_term = election_term

    print(f"[{WORKER_ID}] Iniciando eleição (term={my_term})")

    higher_peers = {peer_id: url for peer_id, url in PEERS.items() if peer_id > MY_ID}

    try:
        if not higher_peers:
            await become_leader(my_term)
            return

        got_ok = False
        async with httpx.AsyncClient(timeout=ELECTION_TIMEOUT) as client:
            tasks = [
                client.post(f"{url}/election", json={"candidate_id": MY_ID, "term": my_term})
                for url in higher_peers.values()
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                continue
            if result.status_code == 200 and result.json().get("status") == "OK":
                got_ok = True

        if my_term != election_term:
            print(f"[{WORKER_ID}] Eleição (term={my_term}) obsoleta, descartando resultado")
            return

        if not got_ok:
            print(f"[{WORKER_ID}] Nenhum peer de ID maior respondeu à eleição (term={my_term}), autoproclamando líder")
            await become_leader(my_term)
    finally:
        election_in_progress = False


async def initial_election_delay():
    await asyncio.sleep(INITIAL_ELECTION_DELAY)
    await start_election()


async def monitor_leader():
    consecutive_failures = 0
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)

        if CURRENT_LEADER_ID is None or CURRENT_LEADER_ID == MY_ID:
            consecutive_failures = 0
            continue

        leader_url = PEERS.get(CURRENT_LEADER_ID)
        if not leader_url:
            continue

        try:
            async with httpx.AsyncClient(timeout=ELECTION_TIMEOUT) as client:
                resp = await client.get(f"{leader_url}/health")
                if resp.status_code == 200:
                    consecutive_failures = 0
                    continue
                consecutive_failures += 1
        except Exception:
            consecutive_failures += 1

        if consecutive_failures >= MAX_LEADER_FAILURES:
            print(f"[{WORKER_ID}] Líder {CURRENT_LEADER_ID} não respondeu, iniciando nova eleição")
            consecutive_failures = 0
            asyncio.create_task(start_election())
