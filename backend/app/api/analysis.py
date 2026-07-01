from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.analysis_profiles import (
    IDENTITY_FIELDS,
    StrengthComparison,
    compare_search_strength,
    get_profile,
)
from app.analysis_trust import cache_row_as_move_dict, move_trust_flags
from app.db import get_db
from app.evidence_contracts import contract_satisfied
from app.fen import normalize_fen
from app.models import AnalysisCache
from app.position_analysis_repo import TrustedPosition, resolve_trusted_positions
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
    # Nullable as of Phase 6: a POSITION-ONLY hit (trusted position resolved, no
    # exact (fen, move_uci) move row) emits the position grain with no move row,
    # so there is no played move SAN.
    move_san: str | None = None
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
    # CROSS-GRAIN: the drill threshold loss (mover-relative CP, clamped >= 0),
    # derived on the BACKEND from the trusted position best_eval and the trusted
    # move row played_eval. Non-null ONLY when both grains are trusted, both are
    # pure CP (no mate field on either), and their profiles are search-strength
    # EQUAL — see ``_position_eval_loss_cp``. Distinct from ``eval_delta``, which
    # is a canonical-run snapshot for blunder/SRS/display; THIS is the trusted
    # threshold loss the drill grader reads. The frontend does no eval arithmetic.
    position_eval_loss_cp: int | None = None
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
    # validation. Diagnostics only — trust is decided per grain by
    # position_trusted / move_trusted below.
    contract_satisfied: bool = False
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


def _trust_flags(row: AnalysisCache) -> tuple[bool, bool]:
    """Return (authoritative, contract_satisfied)."""
    authoritative = _is_authoritative(row)
    satisfied = contract_satisfied(row.evidence_contract_id, _row_contract_data(row))
    return authoritative, satisfied


def _position_eval_loss_cp(
    fen: str,
    tp: TrustedPosition,
    row: AnalysisCache,
    move_trusted: bool,
) -> int | None:
    """Backend-derived drill threshold loss (CP), or None when un-derivable.

    The drill threshold reads THIS, not ``eval_delta``: it is the only loss known
    to come from the same canonical search on both grains. Emitted ONLY when every
    guard holds:

    * the move row is trusted (``move_trusted``) AND a trusted position resolved;
    * BOTH grains are pure CP — no mate field set on EITHER, even when a CP value
      is also present. Producers store mate AND a mate->CP conversion in the same
      row, so "finite CP" alone is insufficient (g-position-analysis.6 #8);
    * the position winner's profile and the move row's profile are search-strength
      ``EQUAL``. There are multiple active authoritative profiles, and subtracting
      evals across different-strength runs is invalid (#9). ``engine_build`` is
      intentionally not strength-invariant, so the two canonical (x86-64 vs bmi2)
      profiles stay comparable.

    The loss is mover-relative and clamped >= 0, mirroring the producer
    (``precompute_openings`` ``eval_delta``): white-to-move ``best - played``,
    black-to-move ``played - best``. Inputs are white-relative as stored.
    """
    if not move_trusted:
        return None
    # Pure CP both grains (#8): a mate field on EITHER grain disqualifies.
    if (
        tp.best_eval is None
        or tp.best_eval_mate is not None
        or row.played_eval is None
        or row.played_eval_mate is not None
    ):
        return None
    # Same search strength (#9): cross-profile subtraction is invalid.
    pp = get_profile(tp.analysis_profile_id)
    mp = get_profile(row.analysis_profile_id)
    if pp is None or mp is None:
        return None
    if compare_search_strength(pp, mp) is not StrengthComparison.EQUAL:
        return None
    parts = fen.split()
    if len(parts) < 2:
        return None
    mover_is_white = parts[1] == "w"
    loss = (
        tp.best_eval - row.played_eval
        if mover_is_white
        else row.played_eval - tp.best_eval
    )
    return max(loss, 0)


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

    # Resolve POSITION evidence separately by NORMALIZED FEN (transposition).
    # Phase 6: normalize ALL requested FENs (not just the move-row subset) so a
    # POSITION-ONLY hit (trusted position, no exact move row) can still emit its
    # position grain. Resolved in a single batched call so the whole endpoint adds
    # at most two extra queries regardless of hit count (no per-row N+1). FEN
    # normalization is the only failure narrowed here — resolver/DB errors propagate.
    norm_by_fen: dict[str, str | None] = {}
    for fen in {p.fen for p in request.positions}:
        try:
            norm_by_fen[fen] = normalize_fen(fen)
        except Exception:
            norm_by_fen[fen] = None
    resolved = resolve_trusted_positions(db, [n for n in norm_by_fen.values() if n])

    results: dict[str, CachedAnalysisResult] = {}
    for position in request.positions:
        key = _make_cache_key(position.fen, position.move_uci)
        row = row_map.get((position.fen, position.move_uci))
        norm = norm_by_fen.get(position.fen)
        tp = resolved.get(norm) if norm else None
        if row is not None:
            authoritative, satisfied = _trust_flags(row)
            _, _, move_trusted = move_trust_flags(cache_row_as_move_dict(row))
            # The flattened best-move fields are derived from the trusted position
            # payload (null when untrusted), never from this move row. The
            # white-relative eval is returned as-is here.
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
                position_trusted=tp is not None,
                move_trusted=move_trusted,
                # Backend-derived trusted CP loss; null unless every guard passes
                # (and necessarily null when no trusted position resolved).
                position_eval_loss_cp=(
                    _position_eval_loss_cp(position.fen, tp, row, move_trusted)
                    if tp is not None
                    else None
                ),
            )
        elif tp is not None:
            # POSITION-ONLY hit (Phase 6): a trusted position resolved but no
            # exact (fen, move_uci) move row exists. Pre-Phase-6 this was
            # suppressed; now we emit the POSITION grain so strictness-0 drill
            # exact-best can grade against ``best_move_uci`` alone. The MOVE grain
            # is null/untrusted, and no threshold loss is derivable without a
            # played eval (``position_eval_loss_cp`` is None). All move-row
            # metadata (source/profile/engine/contract) is absent.
            results[key] = CachedAnalysisResult(
                move_san=None,
                best_move_uci=tp.best_move_uci,
                best_move_san=tp.best_move_san,
                best_line_uci=tp.best_line_uci,
                best_eval=tp.best_eval,
                best_eval_mate=tp.best_eval_mate,
                played_eval=None,
                played_eval_mate=None,
                eval_delta=None,
                classification=None,
                source=None,
                analysis_profile_id=None,
                engine_version=None,
                engine_build=None,
                evidence_contract_id=None,
                authoritative=False,
                contract_satisfied=False,
                position_trusted=True,
                move_trusted=False,
                position_eval_loss_cp=None,
            )
        # both None -> skip (no result emitted for this key).

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
