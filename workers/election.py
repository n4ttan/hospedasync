import os
import re
import time
import asyncio
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from lamport import EventLog
import servicetoken
from security import require_peer

router = APIRouter()

SELF_URL = os.getenv("SELF_URL", f"http://127.0.0.1:{os.getenv('PORT', '8001')}")
_RAW_PEERS = [p.strip() for p in os.getenv("PEER_WORKERS", "").split(",") if p.strip()]

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


def parse_peer(entry: str):
    """Aceita 'id=url' (ex.: 1=http://10.0.1.5:8001) ou só 'url' (ID pelo hostname). Devolve (id, url)."""
    m = re.match(r"^(\d+)=(.+)$", entry)
    if m:
        return int(m.group(1)), m.group(2).strip()
    return extract_id_from_url(entry), entry


def _sig(method: str, path: str) -> dict:
    """Headers HMAC para chamadas worker->worker (id = WORKER_ID)."""
    secret = os.getenv("SERVICE_SECRET", "")
    # sem secret nao assina: o peer rejeita com 401 (falha fechada, sem derrubar a eleicao)
    return servicetoken.sign_request(secret, WORKER_ID, method, path) if secret else {}


def derive_my_id(worker_id, self_url: str) -> int:
    """ID do Bully: sufixo numérico de WORKER_ID (worker-02 -> 2); senão, hostname de SELF_URL."""
    m = re.search(r"(\d+)$", worker_id or "")
    if m:
        return int(m.group(1))
    return extract_id_from_url(self_url)


MY_ID = derive_my_id(os.getenv("WORKER_ID"), SELF_URL)
WORKER_ID = os.getenv("WORKER_ID") or f"worker-{MY_ID:02d}"
PEER_WORKERS = [parse_peer(p) for p in _RAW_PEERS]
PEERS = {MY_ID: SELF_URL}
for _pid, _purl in PEER_WORKERS:
    PEERS[_pid] = _purl

# Relógio de Lamport e log de eventos deste nó (compartilhado com consumer/scheduler/main)
EVENTS = EventLog(WORKER_ID)
CLOCK = EVENTS.clock

# Callbacks async chamados (sem argumentos) quando este nó vira líder
on_became_leader = []

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
    lamport: int = 0


class CoordinatorRequest(BaseModel):
    leader_id: int
    term: float
    lamport: int = 0


@router.post("/election", dependencies=[Depends(require_peer)])
async def receive_election(payload: ElectionRequest):
    EVENTS.log("election_received", received=payload.lamport, candidate_id=payload.candidate_id)
    if MY_ID > payload.candidate_id:
        print(f"[{WORKER_ID}] Recebida eleição de candidato {payload.candidate_id}, respondendo OK e iniciando própria eleição")
        asyncio.create_task(start_election())
        return {"status": "OK", "responder_id": MY_ID, "lamport": CLOCK.tick()}
    return {"status": "IGNORED", "reason": "responder_id menor ou igual ao candidate_id", "lamport": CLOCK.tick()}


@router.post("/coordinator", dependencies=[Depends(require_peer)])
async def receive_coordinator(payload: CoordinatorRequest):
    global CURRENT_LEADER_ID
    last_term = _last_coordinator_term_by_leader.get(payload.leader_id, -1)
    if payload.term < last_term:
        print(f"[{WORKER_ID}] Coordinator obsoleto de worker {payload.leader_id} (term={payload.term} < {last_term}), ignorando")
        return {"status": "STALE", "worker_id": MY_ID, "lamport": CLOCK.update(payload.lamport)}
    _last_coordinator_term_by_leader[payload.leader_id] = payload.term
    CURRENT_LEADER_ID = payload.leader_id
    EVENTS.log("coordinator_received", received=payload.lamport, leader_id=payload.leader_id, term=payload.term)
    print(f"[{WORKER_ID}] Novo líder reconhecido via coordinator: worker com ID {payload.leader_id} (term={payload.term})")
    return {"status": "ACK", "worker_id": MY_ID, "lamport": CLOCK.tick()}


@router.get("/leader", dependencies=[Depends(require_peer)])
async def get_leader():
    return {"leader_id": CURRENT_LEADER_ID, "am_i_leader": CURRENT_LEADER_ID == MY_ID}


@router.get("/events", dependencies=[Depends(require_peer)])
async def get_events():
    return {"node": WORKER_ID, "lamport": CLOCK.time, "events": EVENTS.snapshot()}


async def become_leader(term: float):
    global CURRENT_LEADER_ID
    CURRENT_LEADER_ID = MY_ID
    print(f"[{WORKER_ID}] Autoproclamado líder (term={term})")
    ev = EVENTS.log("became_leader", term=term, leader_id=MY_ID)
    async with httpx.AsyncClient(timeout=ELECTION_TIMEOUT) as client:
        for peer_id, url in PEERS.items():
            if peer_id == MY_ID:
                continue
            try:
                resp = await client.post(
                    f"{url}/coordinator", headers=_sig("POST", "/coordinator"), json={"leader_id": MY_ID, "term": term, "lamport": CLOCK.tick()}
                )
                try:
                    CLOCK.update(resp.json().get("lamport", 0))
                except Exception:
                    pass
                print(f"[{WORKER_ID}] Enviando coordinator para {url} (novo líder={MY_ID})")
            except Exception as e:
                print(f"[{WORKER_ID}] Falha ao avisar {url} sobre nova liderança ({e})")
    for hook in list(on_became_leader):
        try:
            await hook()
        except Exception as e:
            print(f"[{WORKER_ID}] Hook on_became_leader falhou ({e})")


async def start_election():
    global election_in_progress, election_term

    async with election_lock:
        if election_in_progress:
            return
        election_in_progress = True
        election_term = time.time()
        my_term = election_term

    print(f"[{WORKER_ID}] Iniciando eleição (term={my_term})")
    EVENTS.log("election_started", term=my_term)

    higher_peers = {peer_id: url for peer_id, url in PEERS.items() if peer_id > MY_ID}

    try:
        if not higher_peers:
            await become_leader(my_term)
            return

        got_ok = False
        async with httpx.AsyncClient(timeout=ELECTION_TIMEOUT) as client:
            tasks = [
                client.post(f"{url}/election", headers=_sig("POST", "/election"), json={"candidate_id": MY_ID, "term": my_term, "lamport": CLOCK.tick()})
                for url in higher_peers.values()
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                continue
            if result.status_code == 200 and result.json().get("status") == "OK":
                got_ok = True
                EVENTS.log("election_ok_received", received=result.json().get("lamport", 0))

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


async def _higher_peer_alive() -> bool:
    for pid, url in PEERS.items():
        if pid <= MY_ID:
            continue
        try:
            async with httpx.AsyncClient(timeout=ELECTION_TIMEOUT) as client:
                if (await client.get(f"{url}/health")).status_code == 200:
                    return True
        except Exception:
            continue
    return False


async def monitor_leader():
    consecutive_failures = 0
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)

        if CURRENT_LEADER_ID is None:
            # sem lider (ex.: /coordinator perdido): nao fica para sempre sem coordenacao
            consecutive_failures = 0
            EVENTS.log("no_leader_known")
            asyncio.create_task(start_election())
            continue

        if CURRENT_LEADER_ID == MY_ID:
            consecutive_failures = 0
            # Bully: se um ID MAIOR esta vivo (ex.: cura de particao), ele deve assumir; re-elege.
            if await _higher_peer_alive():
                EVENTS.log("higher_peer_alive_reelect")
                asyncio.create_task(start_election())
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
            EVENTS.log("leader_suspected_down", leader_id=CURRENT_LEADER_ID)
            consecutive_failures = 0
            asyncio.create_task(start_election())
