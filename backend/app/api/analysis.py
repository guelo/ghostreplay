from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.analysis_profiles import IDENTITY_FIELDS, get_profile
from app.db import get_db
from app.models import AnalysisCache, decode_uci_line
from app.security import TokenPayload, get_current_user

router = APIRouter(prefix="/api/analysis", tags=["analysis"])

MAX_LOOKUP_POSITIONS = 60


class AnalysisLookupPosition(BaseModel):
    fen: str = Field(..., min_length=1)
    move_uci: str = Field(..., min_length=2, max_length=5)


class AnalysisLookupRequest(BaseModel):
    positions: list[AnalysisLookupPosition] = Field(
        ..., min_length=1, max_length=MAX_LOOKUP_POSITIONS
    )


class CachedAnalysisResult(BaseModel):
    move_san: str
    best_move_uci: str | None = None
    best_move_san: str | None = None
    best_line_uci: list[str] | None = None
    played_eval: int | None = None
    played_eval_mate: int | None = None
    best_eval: int | None = None
    eval_delta: int | None = None
    classification: str | None = None
    source: str | None = None
    analysis_profile_id: str | None = None
    engine_version: str | None = None
    engine_build: str | None = None
    evidence_contract_id: str | None = None
    # True when the row's stored identity metadata matches its claimed,
    # authoritative profile (same validation the write comparator uses).
    authoritative: bool = False


def _is_authoritative(row: AnalysisCache) -> bool:
    profile = get_profile(row.analysis_profile_id)
    if profile is None or not profile.authoritative:
        return False
    return all(getattr(row, f) == getattr(profile, f) for f in IDENTITY_FIELDS)


class AnalysisLookupResponse(BaseModel):
    results: dict[str, CachedAnalysisResult]


def _make_cache_key(fen: str, move_uci: str) -> str:
    return f"{fen}::{move_uci}"


@router.post("/lookup", response_model=AnalysisLookupResponse)
def lookup_analysis(
    request: AnalysisLookupRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> AnalysisLookupResponse:
    fens = [p.fen for p in request.positions]
    rows = (
        db.query(AnalysisCache)
        .filter(AnalysisCache.fen_before.in_(fens))
        .all()
    )

    # Index rows by (fen, move_uci) for O(1) lookup
    row_map: dict[tuple[str, str], AnalysisCache] = {}
    for row in rows:
        row_map[(row.fen_before, row.move_uci)] = row

    results: dict[str, CachedAnalysisResult] = {}
    for position in request.positions:
        row = row_map.get((position.fen, position.move_uci))
        if row is not None:
            key = _make_cache_key(position.fen, position.move_uci)
            results[key] = CachedAnalysisResult(
                move_san=row.move_san,
                best_move_uci=row.best_move_uci,
                best_move_san=row.best_move_san,
                best_line_uci=decode_uci_line(row.best_line_uci),
                played_eval=row.played_eval,
                played_eval_mate=row.played_eval_mate,
                best_eval=row.best_eval,
                eval_delta=row.eval_delta,
                classification=row.classification,
                source=row.source,
                analysis_profile_id=row.analysis_profile_id,
                engine_version=row.engine_version,
                engine_build=row.engine_build,
                evidence_contract_id=row.evidence_contract_id,
                authoritative=_is_authoritative(row),
            )

    return AnalysisLookupResponse(results=results)
