from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette import status
from sqlalchemy import text

from app.api.auth import router as auth_router
from app.api.blunder import router as blunder_router
from app.api.health import router as health_router
from app.api.game import router as game_router
from app.api.history import router as history_router
from app.api.session import router as session_router
from app.db import engine
from app.security import AuthMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    yield
    engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Ghost Replay API", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        AuthMiddleware,
        exempt_prefixes=("/api/auth", "/health", "/docs", "/openapi.json", "/redoc"),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router)
    app.include_router(blunder_router)
    app.include_router(health_router)
    app.include_router(game_router)
    app.include_router(history_router)
    app.include_router(session_router)

    def _build_error_response(
        status_code: int,
        message: str,
        *,
        code: str,
        details: object | None = None,
    ) -> JSONResponse:
        # Keep `detail` for backwards compatibility while adding a standard envelope.
        payload = {
            "detail": message,
            "error": {
                "code": code,
                "message": message,
                "retryable": status_code == status.HTTP_429_TOO_MANY_REQUESTS or status_code >= 500,
            },
        }
        if details is not None:
            payload["error"]["details"] = details
        return JSONResponse(status_code=status_code, content=payload)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, str):
            message = exc.detail
            details = None
        else:
            message = "Request failed"
            details = exc.detail
        return _build_error_response(
            exc.status_code,
            message,
            code=f"http_{exc.status_code}",
            details=details,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _build_error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Validation error",
            code="validation_error",
            details=exc.errors(),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, __: Exception) -> JSONResponse:
        return _build_error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Internal server error",
            code="internal_error",
        )

    @app.get("/")
    def root() -> dict:
        return {"name": "ghostreplay-api", "status": "ok"}

    return app


app = create_app()
