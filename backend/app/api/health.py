from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database_url import is_deploy_platform
from app.db import get_db
from app.maia3_client import boot_weights_error
from app.opening_roots import is_opening_roots_warm

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check(response: Response) -> dict:
    # Railway calls this only while a deployment starts, so a 503 holds back an
    # image without valid Maia3 weights and the previous deployment keeps serving.
    # It never restarts a live deployment. This reads a module value and does no
    # I/O, so it can't use up the 30 s healthcheck window.
    if is_deploy_platform() and boot_weights_error() is not None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error", "reason": "maia3 weights missing"}
    return {"status": "ok"}


@router.get("/health/openings")
def health_openings_check() -> dict:
    # Observability only — never wire this into the deploy healthcheck: the
    # opening build takes 30-60s and gating readiness on it would fail
    # Railway's 30s /health timeout. Reports warm state without triggering
    # a build.
    return {
        "status": "ok",
        "openings": "warm" if is_opening_roots_warm() else "warming",
    }


@router.get("/health/db")
def health_db_check(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok", "db": "ok"}
