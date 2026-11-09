"""
Relogio logico de Lamport + registro de eventos ordenavel causalmente.

Regras (Lamport, 1978):
  - evento local ou envio de mensagem:  L = L + 1  (e a mensagem leva L)
  - recebimento de mensagem com Lm:     L = max(L, Lm) + 1

Se o evento A causou o evento B, entao L(A) < L(B). A volta nao vale: L(A) < L(B)
nao prova causalidade (para isso seriam necessarios relogios vetoriais). Para desempatar
eventos com o mesmo valor usamos o nome do no, o que da uma ordem total consistente.
"""

import threading
import time
from collections import deque


class LamportClock:
    def __init__(self, start: int = 0):
        self._time = start
        self._lock = threading.Lock()

    @property
    def time(self) -> int:
        return self._time

    def tick(self) -> int:
        """Evento local ou envio: incrementa e devolve o novo valor."""
        with self._lock:
            self._time += 1
            return self._time

    def update(self, received) -> int:
        """Recebimento: L = max(L, recebido) + 1. Valor ausente/invalido conta como 0."""
        try:
            received = int(received)
        except (TypeError, ValueError):
            received = 0
        with self._lock:
            self._time = max(self._time, received) + 1
            return self._time


class EventLog:
    """Anel de eventos de um no, cada um carimbado com o relogio de Lamport."""

    def __init__(self, node: str, clock: LamportClock = None, maxlen: int = 300):
        self.node = node
        self.clock = clock or LamportClock()
        self._events = deque(maxlen=maxlen)

    def log(self, event_type: str, received=None, **detail) -> dict:
        """Registra um evento. Se `received` vier, trata como recebimento; senao, como evento local/envio."""
        lamport = self.clock.update(received) if received is not None else self.clock.tick()
        event = {
            "node": self.node,
            "lamport": lamport,
            "wall_time": time.time(),
            "type": event_type,
            "detail": detail,
        }
        self._events.append(event)
        print(f"[{self.node}] L={lamport} {event_type} {detail}", flush=True)
        return event

    def snapshot(self) -> list:
        return list(self._events)


def merge_events(*event_lists) -> list:
    """Junta eventos de varios nos na ordem de Lamport (empate: nome do no, depois horario fisico)."""
    merged = [e for events in event_lists for e in events]
    merged.sort(key=lambda e: (e["lamport"], e["node"], e.get("wall_time", 0)))
    return merged
