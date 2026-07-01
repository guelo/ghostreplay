import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette import status
from sqlalchemy import text

from app.api.analysis import router as analysis_router
from app.api.auth import router as auth_router
from app.api.blunder import router as blunder_router
from app.api.drills import router as drills_router
from app.api.health import router as health_router
from app.api.game import router as game_router
from app.api.history import router as history_router
from app.api.openings import router as openings_router
from app.api.stats import router as stats_router
from app.api.session import router as session_router
from app.api.srs import router as srs_router
from app.db import engine
from app.opening_baseline_scheduler import get_baseline_scheduler
from app.opening_score_scheduler import get_scheduler
from app.session_evidence_scheduler import get_evidence_scheduler
from app.posthog_client import shutdown as posthog_shutdown
from app.security import AuthMiddleware
from app.http_logging import HTTPLoggingMiddleware
from app.logging_config import configure_logging

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Database health check
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    # Start the in-process opening-score recompute scheduler. A start failure
    # must never block the API from coming up — enqueues then degrade to
    # best-effort no-ops.
    try:
        get_scheduler().start()
    except Exception:
        logging.getLogger(__name__).exception("opening score scheduler failed to start")

    # Start the in-process /moves evidence scheduler (deferred graph/opportunity/
    # analysis-cache/recompute side effects). Start failure must not block boot —
    # enqueues then degrade to best-effort no-ops. Start order is irrelevant.
    try:
        get_evidence_scheduler().start()
    except Exception:
        logging.getLogger(__name__).exception("session evidence scheduler failed to start")

    # Start the in-process opening-baseline scheduler (g-mxeo): async capture of
    # GameSession.opening_score_baseline off the /start hot path. Start failure must
    # not block boot — enqueues then degrade to best-effort no-ops, leaving the
    # baseline NULL (no delta). Start order is irrelevant.
    try:
        get_baseline_scheduler().start()
    except Exception:
        logging.getLogger(__name__).exception("opening baseline scheduler failed to start")

    try:
        yield
    finally:
        # Teardown must not be wedged by a hung run: each drain is wrapped so a
        # hang/failure can't wedge teardown and engine.dispose() always runs.
        #
        # Drain the opening-baseline scheduler FIRST. It is a LEAF worker: it reads
        # cache state and writes the session row but never enqueues a recompute, so
        # it has no ordering dependency on the other two schedulers and draining it
        # up front lets its jobs finish while the others are still live.
        try:
            get_baseline_scheduler().shutdown(drain=True)
        except Exception:
            logging.getLogger(__name__).exception("opening baseline scheduler shutdown failed")
        # SHUTDOWN ORDER IS LOAD-BEARING. Drain the evidence scheduler before the
        # opening scheduler: its drain runs _run_session_move_evidence_side_effects,
        # whose last step calls request_recompute(...) on the OPENING scheduler. That
        # enqueue early-returns (silently drops) once the opening scheduler's
        # _shutdown is set, so the opening scheduler must still be live while the
        # evidence scheduler drains. The opening recompute (best-effort,
        # self-healing) then drains in the next step.
        try:
            get_evidence_scheduler().shutdown(drain=True)
        except Exception:
            logging.getLogger(__name__).exception("session evidence scheduler shutdown failed")
        try:
            get_scheduler().shutdown(drain=True)
        except Exception:
            logging.getLogger(__name__).exception("opening score scheduler shutdown failed")
        # Flush queued analytics before disposing the engine so events aren't
        # dropped on deploy/restart. shutdown() is already defensive; wrap it
        # anyway so teardown never wedges on engine.dispose().
        try:
            posthog_shutdown()
        except Exception:
            logging.getLogger(__name__).exception("posthog shutdown failed")
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
        # Let browser fetch read the request id during cross-origin dev so the
        # client can attach it to its api_request_client analytics event.
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(HTTPLoggingMiddleware)

    app.include_router(analysis_router)
    app.include_router(auth_router)
    app.include_router(blunder_router)
    app.include_router(drills_router)
    app.include_router(health_router)
    app.include_router(game_router)
    app.include_router(history_router)
    app.include_router(openings_router)
    app.include_router(stats_router)
    app.include_router(session_router)
    app.include_router(srs_router)

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
