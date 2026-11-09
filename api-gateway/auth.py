"""Autenticacao (JWT), autorizacao (papeis) e assinatura de servico (HMAC) do gateway."""
import datetime
import logging
import os
import secrets
import time
import uuid
from collections import defaultdict, deque

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.orm import Session

from database import Base, SessionLocal
import servicetoken

log = logging.getLogger("auth")
ROLES = ("analyst", "admin")
ALGO = "HS256"
JWT_TTL_MINUTES = int(os.getenv("JWT_TTL_MINUTES", "30"))
MIN_PASSWORD = 10
MAX_FAILS, WINDOW = 5, 60

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    JWT_SECRET = secrets.token_hex(32)
    log.warning("JWT_SECRET ausente: segredo aleatorio em memoria; tokens NAO sobrevivem a restart "
                "(e divergem entre replicas). Defina JWT_SECRET no ambiente.")

_ph = PasswordHasher()
_DUMMY_HASH = _ph.hash(secrets.token_hex(16))  # equaliza o tempo quando o usuario nao existe
_bearer = HTTPBearer(auto_error=False)
router = APIRouter()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False, default="analyst")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    disabled = Column(Boolean, nullable=False, default=False)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_table(db: Session):
    User.__table__.create(bind=db.get_bind(), checkfirst=True)


# ---------- senha / token ----------
def hash_password(p: str) -> str:
    return _ph.hash(p)


def verify_password(p: str, h: str) -> bool:
    try:
        return _ph.verify(h, p)
    except (VerifyMismatchError, InvalidHashError):
        return False


def create_token(username: str, role: str):
    now = int(time.time())
    ttl = JWT_TTL_MINUTES * 60
    claims = {"sub": username, "role": role, "iat": now, "exp": now + ttl, "jti": uuid.uuid4().hex}
    return jwt.encode(claims, JWT_SECRET, algorithm=ALGO), ttl


# ---------- limite de tentativas ----------
_FAILS = defaultdict(deque)


def _key(request: Request, username: str):
    ip = request.client.host if request.client else "?"
    return (ip, username.lower())


def _check_rate(key):
    q = _FAILS[key]
    now = time.monotonic()
    while q and now - q[0] > WINDOW:
        q.popleft()
    if len(q) >= MAX_FAILS:
        retry = max(1, int(WINDOW - (now - q[0])) + 1)
        raise HTTPException(429, "Muitas tentativas. Tente novamente em instantes.",
                            headers={"Retry-After": str(retry)})


def reset_rate_limit():
    _FAILS.clear()


# ---------- schemas ----------
class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "analyst"


# ---------- dependencias ----------
def current_user(creds: HTTPAuthorizationCredentials = Depends(_bearer),
                 db: Session = Depends(get_session)) -> User:
    bad = HTTPException(401, "Nao autenticado", headers={"WWW-Authenticate": "Bearer"})
    if creds is None or creds.scheme.lower() != "bearer":
        raise bad
    try:
        claims = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[ALGO],
                            options={"require": ["exp", "sub", "iat", "jti"]})
    except jwt.PyJWTError:
        raise bad
    user = db.query(User).filter(User.username == claims["sub"]).first()
    if user is None or user.disabled:
        raise bad
    return user  # papel vem do banco, nao do token (revogacao de papel vale na hora)


def require_role(role: str):
    if role not in ROLES:
        raise ValueError(role)

    def dep(user: User = Depends(current_user)) -> User:
        if role == "analyst" and user.role in ("analyst", "admin"):
            return user
        if user.role == role:
            return user
        raise HTTPException(403, "Permissao insuficiente")
    return dep


def check_service_headers(request_headers, worker_id: str, endpoint: str) -> None:
    """Chamavel de dentro do handler; levanta 401 se a assinatura HMAC nao confere."""
    secret = os.getenv("SERVICE_SECRET", "")
    if not servicetoken.verify(secret, request_headers, worker_id, endpoint):
        raise HTTPException(401, "Assinatura de servico invalida")


async def verify_service_request(request: Request) -> None:
    """Dependencia: le o corpo JSON (worker_id, endpoint) e valida o HMAC."""
    try:
        body = await request.json()
        wid, ep = str(body["worker_id"]), str(body["endpoint"])
    except Exception:
        raise HTTPException(401, "Assinatura de servico invalida")
    check_service_headers(request.headers, wid, ep)


# ---------- seed ----------
def seed_admin(db: Session) -> bool:
    user, pwd = os.getenv("ADMIN_USER"), os.getenv("ADMIN_PASSWORD")
    if not user or not pwd:
        log.warning("ADMIN_USER/ADMIN_PASSWORD ausentes: nenhum admin criado.")
        return False
    ensure_table(db)
    if db.query(User).filter(User.username == user).first():
        return False
    if len(pwd) < MIN_PASSWORD:
        log.warning("ADMIN_PASSWORD menor que %d caracteres: admin nao criado.", MIN_PASSWORD)
        return False
    db.add(User(username=user, password_hash=hash_password(pwd), role="admin"))
    db.commit()
    return True


# ---------- rotas ----------
@router.post("/auth/login")
def login(req: LoginRequest, request: Request, db: Session = Depends(get_session)):
    key = _key(request, req.username)
    _check_rate(key)
    user = db.query(User).filter(User.username == req.username).first()
    ok = verify_password(req.password, user.password_hash if user else _DUMMY_HASH)
    if not (user and ok) or user.disabled:
        _FAILS[key].append(time.monotonic())
        raise HTTPException(401, "Credenciais invalidas")
    _FAILS.pop(key, None)
    token, ttl = create_token(user.username, user.role)
    return {"access_token": token, "token_type": "bearer", "expires_in": ttl, "role": user.role}


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return {"username": user.username, "role": user.role}


@router.post("/auth/users", status_code=201)
def create_user(req: CreateUserRequest, _: User = Depends(require_role("admin")),
                db: Session = Depends(get_session)):
    if req.role not in ROLES:
        raise HTTPException(422, f"role deve ser um de {ROLES}")
    if len(req.password) < MIN_PASSWORD:
        raise HTTPException(422, f"Senha deve ter ao menos {MIN_PASSWORD} caracteres")
    if not req.username.strip():
        raise HTTPException(422, "username vazio")
    if db.query(User).filter(User.username == req.username).first():
        raise HTTPException(409, "Usuario ja existe")
    db.add(User(username=req.username, password_hash=hash_password(req.password), role=req.role))
    db.commit()
    return {"username": req.username, "role": req.role}
