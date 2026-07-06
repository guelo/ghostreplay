from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.opening_roots import is_opening_roots_warm

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check() -> dict:
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
