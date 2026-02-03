from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.health import router as health_router
from app.api.game import router as game_router
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
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        AuthMiddleware,
        exempt_prefixes=("/api/auth", "/health", "/docs", "/openapi.json", "/redoc"),
    )

    app.include_router(health_router)
    app.include_router(game_router)

    @app.get("/")
    def root() -> dict:
        return {"name": "ghostreplay-api", "status": "ok"}

    return app


app = create_app()
