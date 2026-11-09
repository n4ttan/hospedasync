import asyncio

import pytest

from resilience import CircuitBreaker, CircuitOpenError, retry_async


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def mk(**k):
    c = Clock()
    return CircuitBreaker("x", failure_threshold=3, recovery_timeout=30, clock=c, **k), c


def test_closed_open_half_open_closed():
    b, c = mk()
    assert b.state == "closed"
    for _ in range(2):
        b.record_failure()
    assert b.state == "closed" and b.allow()
    b.record_failure()
    assert b.state == "open" and not b.allow()
    c.t += 29
    assert b.state == "open"
    c.t += 1
    assert b.state == "half_open"
    assert b.allow() and not b.allow()  # half_open_max=1
    b.record_success()
    assert b.state == "closed" and b.snapshot()["failures"] == 0


def test_half_open_failure_reopens():
    b, c = mk()
    for _ in range(3):
        b.record_failure()
    c.t += 30
    assert b.allow()
    b.record_failure()
    snap = b.snapshot()
    assert snap["state"] == "open" and snap["opened_at"] == c.t and snap["name"] == "x"
    c.t += 30
    assert b.state == "half_open"


def test_success_resets_consecutive_failures():
    b, _ = mk()
    b.record_failure(); b.record_failure(); b.record_success(); b.record_failure()
    assert b.state == "closed"


def test_call_raises_circuit_open():
    b, c = mk()

    async def boom():
        raise ValueError("x")

    async def ok():
        return 1

    async def go():
        for _ in range(3):
            with pytest.raises(ValueError):
                await b.call(boom)
        with pytest.raises(CircuitOpenError):
            await b.call(ok)
        c.t += 30
        assert await b.call(ok) == 1
    asyncio.run(go())
    assert b.state == "closed"


def test_retry_async_backoff_no_jitter():
    sleeps, n = [], {"n": 0}

    async def sleep(d):
        sleeps.append(d)

    async def fn():
        n["n"] += 1
        if n["n"] < 4:
            raise RuntimeError
        return "ok"

    r = asyncio.run(retry_async(fn, attempts=4, base_delay=1, max_delay=3, jitter=0, sleep=sleep))
    assert r == "ok" and sleeps == [1, 2, 3]


def test_retry_async_exhausts_and_filters():
    async def sleep(d):
        pass

    async def fn():
        raise KeyError

    with pytest.raises(KeyError):
        asyncio.run(retry_async(fn, attempts=2, retry_on=(KeyError,), sleep=sleep))
    calls = []

    async def other():
        calls.append(1)
        raise ValueError
    with pytest.raises(ValueError):
        asyncio.run(retry_async(other, attempts=3, retry_on=(KeyError,), sleep=sleep))
    assert len(calls) == 1
