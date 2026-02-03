from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import HTTPException, status
from jwt import ExpiredSignatureError, InvalidTokenError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

JWT_ALGORITHM = "HS256"
DEFAULT_TOKEN_TTL = timedelta(days=7)


@dataclass(frozen=True)
class TokenPayload:
    user_id: int
    username: str
    is_anonymous: bool


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("Password must be non-empty.")
    password_bytes = password.encode("utf-8")
    password_hash = bcrypt.hashpw(password_bytes, bcrypt.gensalt())
    return password_hash.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


def get_jwt_secret() -> str:
    return os.environ.get("JWT_SECRET", "dev-secret")


def create_access_token(
    user_id: int,
    username: str,
    is_anonymous: bool,
    expires_delta: timedelta | None = None,
) -> str:
    if not username:
        raise ValueError("Username is required for JWT token creation.")
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or DEFAULT_TOKEN_TTL)
    payload = {
        "sub": str(user_id),
        "username": username,
        "is_anonymous": is_anonymous,
        "exp": expire,
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> TokenPayload:
    payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    sub = payload.get("sub")
    username = payload.get("username")
    is_anonymous = payload.get("is_anonymous")

    if sub is None or username is None or is_anonymous is None:
        raise InvalidTokenError("Missing required JWT claims.")

    try:
        user_id = int(sub)
    except (TypeError, ValueError) as exc:
        raise InvalidTokenError("Invalid subject claim.") from exc

    if not isinstance(is_anonymous, bool):
        raise InvalidTokenError("Invalid anonymous flag.")

    return TokenPayload(user_id=user_id, username=username, is_anonymous=is_anonymous)


def get_current_user(request: Request) -> TokenPayload:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing auth token")
    return user


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, exempt_prefixes: tuple[str, ...] | None = None) -> None:
        super().__init__(app)
        self.exempt_prefixes = exempt_prefixes or ()

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS":
            return await call_next(request)
        if path == "/" or path.startswith(self.exempt_prefixes):
            return await call_next(request)

        # TODO: remove this dev-only escape hatch once auth endpoints are live.
        if os.environ.get("AUTH_BYPASS", "").lower() == "true":
            request.state.user = TokenPayload(
                user_id=int(request.headers.get("X-User-Id", "1")),
                username=request.headers.get("X-Username", "dev"),
                is_anonymous=request.headers.get("X-Is-Anonymous", "true").lower() == "true",
            )
            return await call_next(request)

        header = request.headers.get("Authorization")
        if not header or not header.startswith("Bearer "):
            return self._unauthorized("Missing Bearer token.")

        token = header.split(" ", 1)[1].strip()
        if not token:
            return self._unauthorized("Missing Bearer token.")

        try:
            payload = decode_access_token(token)
        except ExpiredSignatureError:
            return self._unauthorized("Token expired.")
        except InvalidTokenError:
            return self._unauthorized("Invalid token.")

        request.state.user = payload
        return await call_next(request)

    @staticmethod
    def _unauthorized(message: str) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": message},
        )
