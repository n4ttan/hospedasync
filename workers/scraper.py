"""Cliente da API Xotelo: retries com backoff, semaforo de concorrencia e cache TTL."""
import asyncio
import os
import time
from datetime import datetime, timezone

import httpx

BASE_URL = os.getenv("XOTELO_BASE_URL", "https://data.xotelo.com/api").rstrip("/")
TIMEOUT = float(os.getenv("XOTELO_TIMEOUT", "15"))
MAX_ATTEMPTS = int(os.getenv("XOTELO_MAX_ATTEMPTS", "3"))
BACKOFF_BASE = float(os.getenv("XOTELO_BACKOFF_BASE", "0.5"))  # 0.5 s, 1 s
CACHE_TTL = float(os.getenv("XOTELO_CACHE_TTL", "300"))
MAX_CONCURRENCY = int(os.getenv("XOTELO_MAX_CONCURRENCY", "3"))
USER_AGENT = "HospedaSync/1.0 (trabalho academico de Sistemas Distribuidos)"

_transport = None  # injetavel em testes (httpx.MockTransport)
_cache: dict = {}  # chave -> (expira_em, payload)
_sem = None


class UpstreamError(Exception):
    """Xotelo indisponivel apos retries (timeout, 5xx, error != null)."""


class NoRates(Exception):
    """Hotel sem nenhuma oferta."""


def _semaphore():
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(MAX_CONCURRENCY)
    return _sem


def clear_cache():
    _cache.clear()


async def _call(hotel_key, chk_in, chk_out, currency):
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
    raise UpstreamError(f"Xotelo indisponivel apos {MAX_ATTEMPTS} tentativas ({last})")


async def fetch_rates(hotel_key, chk_in, chk_out, currency="BRL"):
    """Retorna dict {rates, min_rate, source, fetched_at, tripadvisor_url}."""
    key = (hotel_key, chk_in, chk_out, currency)
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
    return {**data, "source": "live"}
