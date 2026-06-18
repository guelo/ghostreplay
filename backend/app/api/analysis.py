from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.analysis_profiles import IDENTITY_FIELDS, get_profile
from app.analysis_trust import cache_row_as_move_dict, move_trust_flags
from app.db import get_db
from app.evidence_contracts import RESOLVER_COMPLETE_V2, contract_satisfied
from app.fen import normalize_fen
from app.models import AnalysisCache
from app.position_analysis_repo import resolve_trusted_positions
from app.security import TokenPayload, get_current_user

router = APIRouter(prefix="/api/analysis", tags=["analysis"])
logger = logging.getLogger(__name__)

MAX_LOOKUP_POSITIONS = 60
SLOW_ANALYSIS_LOOKUP_LOG_MS = 500


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


class AnalysisLookupPosition(BaseModel):
    fen: str = Field(..., min_length=1)
    move_uci: str = Field(..., min_length=2, max_length=5)


class AnalysisLookupRequest(BaseModel):
    positions: list[AnalysisLookupPosition] = Field(
        ..., min_length=1, max_length=MAX_LOOKUP_POSITIONS
    )


class CachedAnalysisResult(BaseModel):
    move_san: str
    # POSITION-grain evidence (properties of the position, not the played move).
    # Derived from the trusted-position resolver (storage winner / legacy v2
    # projection) keyed by NORMALIZED FEN — null when no trusted position exists,
    # NEVER copied from this move row. ``best_eval`` here is white-relative.
    best_move_uci: str | None = None
    best_move_san: str | None = None
    best_line_uci: list[str] | None = None
    best_eval: int | None = None
    best_eval_mate: int | None = None
    # MOVE-grain evidence (the played move), kept from the exact (fen, move) row.
    played_eval: int | None = None
    played_eval_mate: int | None = None
    eval_delta: int | None = None
    classification: str | None = None
    source: str | None = None
    analysis_profile_id: str | None = None
    engine_version: str | None = None
    engine_build: str | None = None
    # The move row's declared contract (move-grain diagnostic).
    evidence_contract_id: str | None = None
    # True when the move row's stored identity metadata matches its claimed,
    # authoritative profile (same validation the write comparator uses).
    authoritative: bool = False
    # True when the move row's evidence passes its DECLARED contract's semantic
    # validation. Diagnostics only — trust additionally requires authoritative
    # identity and the resolver-complete-v2 contract (see trusted_for_resolution).
    contract_satisfied: bool = False
    # Legacy backend-owned trust decision: the move row is authoritative AND
    # declares resolver-complete-v2 AND that contract's semantic validation
    # passes. As of Phase 5 the frontend no longer reads this — it keys off the
    # grain-specific position_trusted/move_trusted pair below. Still emitted
    # transitionally; removal is a later cleanup once no consumer reads it.
    trusted_for_resolution: bool = False
    # Grain-specific trust (g-position-analysis Phase 4), independent of one another:
    #   position_trusted — a trusted position was resolved (drives the best_* fields).
    #   move_trusted      — this move row's played evidence passes the move-grain gate.
    position_trusted: bool = False
    move_trusted: bool = False


def _is_authoritative(row: AnalysisCache) -> bool:
    profile = get_profile(row.analysis_profile_id)
    # A retired (inactive) profile's rows stay identity-verifiable but stop
    # counting as trusted/authoritative cache hits.
    if profile is None or not profile.authoritative or not profile.active:
        return False
    return all(getattr(row, f) == getattr(profile, f) for f in IDENTITY_FIELDS)


def _row_contract_data(row: AnalysisCache) -> dict:
    """Project a cache row into the dict shape the contract validators read.

    Includes the mate fields so the declared-contract ``contract_satisfied``
    diagnostic is accurate for grain contracts that accept a mate in lieu of a CP
    eval — ``move-complete-v1`` (``played_eval_mate``) and ``position-complete-v1``
    (``best_eval_mate``).
    """
    return {
        "fen_before": row.fen_before,
        "best_move_uci": row.best_move_uci,
        "best_line_uci": row.best_line_uci,
        "classification": row.classification,
        "played_eval": row.played_eval,
        "played_eval_mate": row.played_eval_mate,
        "best_eval": row.best_eval,
        "best_eval_mate": row.best_eval_mate,
        "eval_delta": row.eval_delta,
    }


def _trust_flags(row: AnalysisCache) -> tuple[bool, bool, bool]:
    """Return (authoritative, contract_satisfied, trusted_for_resolution)."""
    authoritative = _is_authoritative(row)
    satisfied = contract_satisfied(row.evidence_contract_id, _row_contract_data(row))
    trusted = (
        authoritative
        and row.evidence_contract_id == RESOLVER_COMPLETE_V2
        and satisfied
    )
    return authoritative, satisfied, trusted


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
    started = time.perf_counter()
    fens = [p.fen for p in request.positions]
    query_started = time.perf_counter()
    rows = (
        db.query(AnalysisCache)
        .filter(AnalysisCache.fen_before.in_(fens))
        .all()
    )
    query_ms = _elapsed_ms(query_started)

    # Index rows by (fen, move_uci) for O(1) lookup
    build_started = time.perf_counter()
    row_map: dict[tuple[str, str], AnalysisCache] = {}
    for row in rows:
        row_map[(row.fen_before, row.move_uci)] = row

    # Resolve POSITION evidence separately by NORMALIZED FEN (transposition). Only
    # FENs that will emit a result (an exact move row exists) need resolving, and
    # they are resolved in a single batched call so the whole endpoint adds at most
    # two extra queries regardless of hit count (no per-row N+1). FEN normalization
    # is the only failure narrowed here — resolver/DB errors propagate.
    norm_by_fen: dict[str, str | None] = {}
    for fen in {p.fen for p in request.positions if (p.fen, p.move_uci) in row_map}:
        try:
            norm_by_fen[fen] = normalize_fen(fen)
        except Exception:
            norm_by_fen[fen] = None
    resolved = resolve_trusted_positions(db, [n for n in norm_by_fen.values() if n])

    results: dict[str, CachedAnalysisResult] = {}
    for position in request.positions:
        # A result is still emitted only when an exact (fen, move_uci) MOVE row
        # exists; a position-only hit (storage row, no move row) is intentionally
        # suppressed. Un-suppressing it is Phase 6, where strictness-0 exact-best
        # from a trusted position with no exact move row needs it.
        row = row_map.get((position.fen, position.move_uci))
        if row is not None:
            key = _make_cache_key(position.fen, position.move_uci)
            authoritative, satisfied, trusted = _trust_flags(row)
            _, _, move_trusted = move_trust_flags(cache_row_as_move_dict(row))
            # The flattened best-move fields are derived from the trusted position
            # payload (null when untrusted), never from this move row. The
            # white-relative eval is returned as-is here.
            norm = norm_by_fen.get(position.fen)
            tp = resolved.get(norm) if norm else None
            results[key] = CachedAnalysisResult(
                move_san=row.move_san,
                best_move_uci=tp.best_move_uci if tp else None,
                best_move_san=tp.best_move_san if tp else None,
                best_line_uci=tp.best_line_uci if tp else None,
                best_eval=tp.best_eval if tp else None,
                best_eval_mate=tp.best_eval_mate if tp else None,
                played_eval=row.played_eval,
                played_eval_mate=row.played_eval_mate,
                eval_delta=row.eval_delta,
                classification=row.classification,
                source=row.source,
                analysis_profile_id=row.analysis_profile_id,
                engine_version=row.engine_version,
                engine_build=row.engine_build,
                evidence_contract_id=row.evidence_contract_id,
                authoritative=authoritative,
                contract_satisfied=satisfied,
                trusted_for_resolution=trusted,
                position_trusted=tp is not None,
                move_trusted=move_trusted,
            )

    build_ms = _elapsed_ms(build_started)
    total_ms = _elapsed_ms(started)
    if total_ms >= SLOW_ANALYSIS_LOOKUP_LOG_MS:
        logger.info(
            "analysis_lookup slow user_id=%s total_ms=%.3f query_ms=%.3f "
            "build_ms=%.3f positions=%d unique_fens=%d rows=%d results=%d",
            user.user_id,
            total_ms,
            query_ms,
            build_ms,
            len(request.positions),
            len(set(fens)),
            len(rows),
            len(results),
        )

    return AnalysisLookupResponse(results=results)
