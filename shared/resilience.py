"""Tolerancia a falhas por software: circuit breaker e retry com backoff exponencial + jitter."""
import asyncio
import random
import threading
import time

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitOpenError(Exception):
    """Chamada recusada porque o circuito esta aberto."""


class CircuitBreaker:
    def __init__(self, name, failure_threshold=5, recovery_timeout=30, half_open_max=1, clock=time.monotonic):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max = half_open_max
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CLOSED
        self._failures = 0
        self._opened_at = None
        self._probes = 0

    def _refresh(self):  # chamar com o lock
        if self._state == OPEN and self._clock() - self._opened_at >= self.recovery_timeout:
            self._state, self._probes = HALF_OPEN, 0

    @property
    def state(self):
        with self._lock:
            self._refresh()
            return self._state

    def allow(self):
        with self._lock:
            self._refresh()
            if self._state == CLOSED:
                return True
            if self._state == HALF_OPEN and self._probes < self.half_open_max:
                self._probes += 1
                return True
            return False

    def record_success(self):
        with self._lock:
            self._state, self._failures, self._opened_at, self._probes = CLOSED, 0, None, 0

    def record_failure(self):
        with self._lock:
            self._refresh()
            self._failures += 1
            if self._state == HALF_OPEN or self._failures >= self.failure_threshold:
                self._state, self._opened_at, self._probes = OPEN, self._clock(), 0

    async def call(self, fn, *a, **k):
        if not self.allow():
            raise CircuitOpenError(f"circuit_open:{self.name}")
        try:
            result = await fn(*a, **k)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def snapshot(self):
        with self._lock:
            self._refresh()
            return {"name": self.name, "state": self._state, "failures": self._failures,
                    "opened_at": self._opened_at}


async def retry_async(fn, attempts=3, base_delay=0.5, max_delay=4.0, jitter=0.2,
                      retry_on=(Exception,), sleep=asyncio.sleep, rand=random.random):
    """Chama `fn()` (corrotina) ate `attempts` vezes; espera base*2^n (limitado) +-jitter proporcional."""
    for attempt in range(attempts):
        try:
            return await fn()
        except retry_on:
            if attempt == attempts - 1:
                raise
            delay = min(max_delay, base_delay * 2 ** attempt)
            await sleep(max(0.0, delay * (1 + jitter * (2 * rand() - 1))))
