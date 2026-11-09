"""Autenticacao de chamadas internas: dependencia FastAPI com falha fechada."""
import os

from fastapi import HTTPException, Request

import servicetoken


async def require_peer(request: Request) -> str:
    """Exige assinatura HMAC valida (metodo + path reais da requisicao). Devolve o X-Service-Id."""
    secret = os.getenv("SERVICE_SECRET", "")
    if not servicetoken.verify_request(secret, request.headers, request.method, request.url.path):
        raise HTTPException(status_code=401, detail="assinatura de servico invalida ou ausente")
    return request.headers.get(servicetoken.H_ID, "")
