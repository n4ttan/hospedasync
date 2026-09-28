"""
Exclusao mutua distribuida via Redis (padrao SET NX EX).

Cada recurso protegido (ex.: um hotel sendo processado) e representado por
uma chave Redis no formato `lock:hospedasync:<resource_name>`. O valor
armazenado e o `owner_id` de quem detem o lock, permitindo que a liberacao
seja feita de forma segura (so libera quem realmente e o dono atual).
"""

import os
import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://redis-lock:6379/0")
LOCK_PREFIX = "lock:hospedasync:"
DEFAULT_TTL = 15

_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# Script Lua para liberacao atomica: so remove a chave se o valor
# armazenado ainda for igual ao owner_id informado. Isso evita que um
# processo libere um lock que ja expirou e foi readquirido por outro
# owner nesse meio tempo (classico problema de "release" nao atomico).
_RELEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""
_release_script = _client.register_script(_RELEASE_LUA)


def _lock_key(resource_name: str) -> str:
    return f"{LOCK_PREFIX}{resource_name}"


def acquire_lock(resource_name: str, owner_id: str, ttl: int = DEFAULT_TTL) -> bool:
    """
    Tenta adquirir o lock para `resource_name`.

    Usa SET chave valor NX EX ttl: a chave so e criada se ainda nao existir
    (NX) e expira automaticamente apos `ttl` segundos (EX), evitando locks
    orfaos caso o processo dono trave ou caia.

    Retorna True se o lock foi adquirido, False caso o recurso ja esteja
    travado por outro owner.
    """
    key = _lock_key(resource_name)
    acquired = _client.set(key, owner_id, nx=True, ex=ttl)
    return bool(acquired)


def release_lock(resource_name: str, owner_id: str) -> bool:
    """
    Libera o lock de `resource_name` de forma atomica: so remove a chave
    se o valor armazenado ainda for igual a `owner_id`. Feito via script
    Lua (EVAL) para que a leitura + comparacao + delecao ocorram em uma
    unica operacao atomica no Redis, sem race condition entre processos.

    Retorna True se o lock foi efetivamente liberado por este owner,
    False se o lock nao existia mais ou pertencia a outro owner.
    """
    key = _lock_key(resource_name)
    result = _release_script(keys=[key], args=[owner_id])
    return bool(result)


def is_locked(resource_name: str) -> bool:
    """Uso de diagnostico: informa se o recurso esta atualmente travado."""
    key = _lock_key(resource_name)
    return bool(_client.exists(key))


def check_connection() -> bool:
    """Usado no health check do Gateway para reportar status do Redis."""
    try:
        return bool(_client.ping())
    except redis.RedisError:
        return False
