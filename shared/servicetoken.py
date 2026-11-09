"""Assinatura HMAC-SHA256 para chamadas servico-a-servico (worker -> gateway)."""
import hashlib
import hmac
import time

H_ID, H_TS, H_SIG = "X-Service-Id", "X-Timestamp", "X-Service-Signature"


def _mac(secret: str, worker_id: str, endpoint: str, ts: str) -> str:
    msg = f"{worker_id}|{endpoint}|{ts}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def sign(secret: str, worker_id: str, endpoint: str, ts=None) -> dict:
    ts = str(int(time.time() if ts is None else ts))
    return {H_ID: worker_id, H_TS: ts, H_SIG: _mac(secret, worker_id, endpoint, ts)}


def verify(secret: str, headers, worker_id: str, endpoint: str, max_skew: int = 60) -> bool:
    if not secret:
        return False
    # headers pode ser dict ou starlette Headers (case-insensitive); normaliza
    h = {str(k).lower(): v for k, v in dict(headers).items()}
    sid, ts, sig = h.get(H_ID.lower()), h.get(H_TS.lower()), h.get(H_SIG.lower())
    if not (sid and ts and sig) or sid != worker_id:
        return False
    try:
        if abs(time.time() - int(ts)) > max_skew:
            return False
    except ValueError:
        return False
    return hmac.compare_digest(sig, _mac(secret, worker_id, endpoint, ts))
