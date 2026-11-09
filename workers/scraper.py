"""Cliente da API Xotelo: retries com backoff, semaforo de concorrencia e cache TTL."""
import asyncio
import json
import os
import time
from datetime import datetime, timezone

import httpx

from resilience import CircuitBreaker

BASE_URL = os.getenv("XOTELO_BASE_URL", "https://data.xotelo.com/api").rstrip("/")
TIMEOUT = float(os.getenv("XOTELO_TIMEOUT", "15"))
MAX_ATTEMPTS = int(os.getenv("XOTELO_MAX_ATTEMPTS", "3"))
BACKOFF_BASE = float(os.getenv("XOTELO_BACKOFF_BASE", "0.5"))  # 0.5 s, 1 s
CACHE_TTL = float(os.getenv("XOTELO_CACHE_TTL", "300"))
MAX_CONCURRENCY = int(os.getenv("XOTELO_MAX_CONCURRENCY", "3"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis-lock:6379/0")
BREAKER_THRESHOLD = int(os.getenv("XOTELO_BREAKER_THRESHOLD", "5"))
BREAKER_RECOVERY = float(os.getenv("XOTELO_BREAKER_RECOVERY", "30"))
USER_AGENT = "HospedaSync/1.0 (trabalho academico de Sistemas Distribuidos)"

breaker = CircuitBreaker("xotelo", BREAKER_THRESHOLD, BREAKER_RECOVERY)
_transport = None  # injetavel em testes (httpx.MockTransport)
_cache: dict = {}  # chave -> (expira_em, payload)
_sem = None
_redis = None  # cliente redis-py sincrono (lazy); injetavel em testes
_redis_down_until = 0.0  # apos falha, nao tenta o Redis ate este instante (monotonic)
REDIS_RETRY_AFTER = 10.0


class UpstreamError(Exception):
    """Xotelo indisponivel apos retries (timeout, 5xx, error != null)."""


class UpstreamCircuitOpen(UpstreamError):
    """Breaker da Xotelo aberto: falha imediata, sem chamar o upstream."""


class NoRates(Exception):
    """Hotel sem nenhuma oferta."""


def breaker_state():
    return breaker.snapshot()


def _semaphore():
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(MAX_CONCURRENCY)
    return _sem


def clear_cache():
    _cache.clear()


def _cache_key(hotel_key, chk_in, chk_out, currency):
    return f"cache:rates:{hotel_key}:{chk_in}:{chk_out}:{currency}"


def _redis_client():
    global _redis
    if _redis is None:
        import redis
        _redis = redis.Redis.from_url(
            REDIS_URL, decode_responses=True, socket_connect_timeout=1, socket_timeout=1
        )
    return _redis


def _redis_get(key):
    """Sincrona. Devolve dict ou None; qualquer falha vira None (e pausa o Redis por alguns segundos)."""
    global _redis_down_until
    if time.monotonic() < _redis_down_until:
        return None
    try:
        raw = _redis_client().get(key)
        return json.loads(raw) if raw else None
    except Exception:
        _redis_down_until = time.monotonic() + REDIS_RETRY_AFTER
        return None


def _redis_set(key, data):
    global _redis_down_until
    if time.monotonic() < _redis_down_until:
        return
    try:
        _redis_client().set(key, json.dumps(data), ex=int(CACHE_TTL))
    except Exception:
        _redis_down_until = time.monotonic() + REDIS_RETRY_AFTER


async def _request(hotel_key, chk_in, chk_out, currency):
    params = {"hotel_key": hotel_key, "chk_in": chk_in, "chk_out": chk_out, "currency": currency}
    last = "erro desconhecido"
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            await asyncio.sleep(BACKOFF_BASE * 2 ** (attempt - 1))
        try:
            async with _semaphore():
                async with httpx.AsyncClient(
                    timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, transport=_transport
                ) as client:
                    resp = await client.get(f"{BASE_URL}/rates", params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            continue
        if resp.status_code >= 500:
            last = f"HTTP {resp.status_code}"
            continue
        try:
            body = resp.json()
        except ValueError:
            raise UpstreamError(f"resposta invalida da Xotelo (HTTP {resp.status_code})")
        if resp.status_code >= 400 or body.get("error"):
            # erro nao transitorio: sem retry
            raise UpstreamError(f"Xotelo retornou erro: {body.get('error') or resp.status_code}")
        return body.get("result") or {}
    raise _Transient(f"Xotelo indisponivel apos {MAX_ATTEMPTS} tentativas ({last})")


class _Transient(UpstreamError):
    """Falha transitoria (conta para o breaker)."""


async def _call(hotel_key, chk_in, chk_out, currency):
    if not breaker.allow():
        raise UpstreamCircuitOpen("circuit_open")
    try:
        result = await _request(hotel_key, chk_in, chk_out, currency)
    except _Transient:
        breaker.record_failure()
        raise
    except BaseException:
        breaker.record_success()  # upstream respondeu (erro nao transitorio): libera a sonda
        raise
    breaker.record_success()
    return result


async def fetch_rates(hotel_key, chk_in, chk_out, currency="BRL"):
    """Retorna dict {rates, min_rate, source, fetched_at, tripadvisor_url}."""
    key = (hotel_key, chk_in, chk_out, currency)
    rkey = _cache_key(hotel_key, chk_in, chk_out, currency)
    shared = await asyncio.to_thread(_redis_get, rkey)
    if shared:
        return {**shared, "source": "cache"}
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return {**hit[1], "source": "cache"}

    result = await _call(hotel_key, chk_in, chk_out, currency)
    rates = []
    for r in result.get("rates") or []:
        try:
            rates.append({"code": r["code"], "name": r.get("name") or r["code"], "rate": float(r["rate"])})
        except (KeyError, TypeError, ValueError):
            continue
    if not rates:
        raise NoRates()
    data = {
        "rates": rates,
        "min_rate": min(r["rate"] for r in rates),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "tripadvisor_url": result.get("tripadvisor_url"),
    }
    _cache[key] = (time.monotonic() + CACHE_TTL, data)
    await asyncio.to_thread(_redis_set, rkey, data)
    return {**data, "source": "live"}
