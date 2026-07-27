from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.analysis_profiles import StrengthComparison, get_profile
from app.analysis_submissions import viewer_associated_ids
from app.analysis_trust import (
    ResolvedEvidence,
    cache_row_as_move_dict,
    describe_move_row,
    move_trust_flags,
)
from app.evidence_coherence import (
    CoherentEvidence,
    MoveGrain,
    PositionGrain,
    resolve_coherent_evidence_tuple,
)
from app.evidence_policy import Capability, compare_row_strength, verify_identity
from app.db import get_db
from app.evidence_contracts import contract_satisfied
from app.fen import normalize_fen
from app.models import AnalysisCache, decode_uci_line
from app.position_analysis_repo import (
    TrustedPosition,
    load_position_candidates,
    resolve_positions_from_candidates,
)
from app.security import TokenPayload, get_current_user

router = APIRouter(prefix="/api/analysis", tags=["analysis"])
logger = logging.getLogger(__name__)

MAX_LOOKUP_POSITIONS = 60
SLOW_ANALYSIS_LOOKUP_LOG_MS = 500

# The capabilities this endpoint resolves the position grain for. All four are
# answered from ONE loaded candidate set and ONE viewer-scoped association fetch —
# never a second resolver round-trip and never a per-position query (g-v21l §3).
_REUSE_CAPABILITIES = (
    Capability.INTERACTIVE_ANALYSIS_REUSE,
    Capability.GAME_ANALYSIS_REUSE,
)
_LOOKUP_CAPABILITIES = (
    Capability.POSITION_READ,
    Capability.DRILL_GRADE,
    *_REUSE_CAPABILITIES,
)


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


class AnalysisLookupPosition(BaseModel):
    fen: str = Field(..., min_length=1)
    move_uci: str = Field(..., min_length=2, max_length=5)


class AnalysisLookupRequest(BaseModel):
    positions: list[AnalysisLookupPosition] = Field(
        ..., min_length=1, max_length=MAX_LOOKUP_POSITIONS
    )


class ReusableAnalysis(BaseModel):
    """One ATOMIC, coherent analysis tuple a consumer may republish (g-v21l §5).

    Deliberately STRICTER than the generic best/move fields below: a canonical
    position row and a browser move row with merely comparable settings cannot be
    published here unless their immutable provenance AND their facts form one
    coherent tuple (``evidence_coherence.resolve_coherent_evidence_tuple``).

    The two flags are resolved INDEPENDENTLY, one per reuse capability. When the
    two capabilities would approve different facts, the payload carries exactly one
    capability's facts and only that flag is true — slices are never combined
    across capability winners.
    """

    best_move_uci: str
    best_line_uci: list[str] | None = None
    best_eval: int | None = None
    best_eval_mate: int | None = None
    played_eval: int | None = None
    played_eval_mate: int | None = None
    classification: str | None = None
    eval_delta: int
    interactive_analysis_reuse: bool
    game_analysis_reuse: bool


class PublicationBest(BaseModel):
    """The exact best move a consumer may RECONCILE a worker result against (§7).

    ``reconcileTrustedBest`` is not a display helper: it rewrites classification,
    delta, blunder, recordability and provenance, and those rewritten values reach
    the store, the incremental upload, and the SRS/decision paths. Reconciliation is
    therefore a durable PUBLICATION effect and must require the capability that
    authorizes durable publication for that consumer — never a read grant.

    POSITION-GRAIN ONLY by design: "which move is best" is a position-grain
    question, and requiring the full coherent tuple here would regress canonical
    position-only and mate hits that reconcile correctly today. Independent of
    ``reusable_analysis`` in BOTH directions.
    """

    best_move_uci: str
    interactive_analysis_reuse: bool
    game_analysis_reuse: bool


class CachedAnalysisResult(BaseModel):
    # Nullable as of Phase 6: a POSITION-ONLY hit (trusted position resolved, no
    # exact (fen, move_uci) move row) emits the position grain with no move row,
    # so there is no played move SAN.
    move_san: str | None = None
    # POSITION-grain evidence (properties of the position, not the played move).
    # Derived from the trusted-position resolver (storage winner / legacy v2
    # projection) keyed by NORMALIZED FEN under POSITION_READ — null when no
    # position holds that capability for this viewer, NEVER copied from this move
    # row. ``best_eval`` here is white-relative.
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
    # CROSS-GRAIN drill fields, gated on DRILL_GRADE for BOTH grains (g-v21l §7).
    # Resolved from a DRILL_GRADE-specific position winner, independent of the
    # generic POSITION_READ winner above, so no read or reuse grant can leak into
    # a drill grade.
    #
    #   drill_best_move_uci — the DRILL_GRADE position winner's best move. Emitted
    #     even when that eval is mate-valued or there is no exact move row.
    #   position_eval_loss_cp — the drill threshold loss (mover-relative CP,
    #     clamped >= 0). Non-null ONLY when both drill grains exist, both hold
    #     DRILL_GRADE, both are pure CP (no mate field on either), and their
    #     captured settings compare EQUAL. Distinct from ``eval_delta``, which is a
    #     canonical-run snapshot for blunder/SRS/display; THIS is the trusted
    #     threshold loss the drill grader reads. The frontend does no eval arithmetic.
    drill_best_move_uci: str | None = None
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
    # Grain-specific trust (g-position-analysis Phase 4), independent of one
    # another, now capability- and viewer-scoped (g-v21l):
    #   position_trusted — a POSITION_READ position resolved (drives best_*).
    #   move_trusted      — this move row holds MOVE_READ for this viewer.
    # Their effect is confined to the generic fields above and the session
    # position-analysis export. They never feed drill grading, never feed
    # publication reconciliation, and never rewrite classification, delta,
    # blunder, recordability, or provenance.
    position_trusted: bool = False
    move_trusted: bool = False
    # Capability-gated publication surfaces (g-v21l §5 / §7).
    reusable_analysis: ReusableAnalysis | None = None
    publication_best: PublicationBest | None = None


def _is_authoritative(row: AnalysisCache) -> bool:
    profile = get_profile(row.analysis_profile_id)
    # A retired (inactive) profile's rows stay identity-verifiable but stop
    # counting as trusted/authoritative cache hits.
    if profile is None or not profile.authoritative or not profile.active:
        return False
    # verify_identity reads getattr(row, f) off the ORM row (no dict projection),
    # preserving the historical access shape.
    return verify_identity(row)


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
    move_evidence: ResolvedEvidence,
    viewer_user_id: int | None,
) -> int | None:
    """Backend-derived drill threshold loss (CP), or None when un-derivable.

    The drill threshold reads THIS, not ``eval_delta``: it is the only loss known to
    come from the same search on both grains. Emitted ONLY when every guard holds:

    * ``tp`` is the DRILL_GRADE position winner AND the move row holds DRILL_GRADE
      for this viewer (g-v21l — a POSITION_READ or reuse grant cannot substitute);
    * BOTH grains are pure CP — no mate field set on EITHER, even when a CP value is
      also present. Producers store mate AND a mate->CP conversion in the same row,
      so "finite CP" alone is insufficient (g-position-analysis.6 #8);
    * the two grains' CAPTURED identity snapshots compare ``EQUAL`` under
      :func:`compare_row_strength`. Subtracting evals across different-strength runs
      is invalid (#9); ``engine_build`` is intentionally not strength-invariant, so
      the two canonical (x86-64 vs bmi2) profiles stay comparable. Comparing the
      captured ROW snapshots rather than registry profiles is required for a
      declared-dynamic profile, whose registry values are all ``None``.

    The loss is mover-relative and clamped >= 0, mirroring the producer
    (``precompute_openings`` ``eval_delta``): white-to-move ``best - played``,
    black-to-move ``played - best``. Inputs are white-relative as stored.
    """
    if not move_evidence.holds(Capability.DRILL_GRADE, viewer_user_id):
        return None
    # Pure CP both grains (#8): a mate field on EITHER grain disqualifies.
    if (
        tp.best_eval is None
        or tp.best_eval_mate is not None
        or row.played_eval is None
        or row.played_eval_mate is not None
    ):
        return None
    # Same search strength (#9): cross-strength subtraction is invalid.
    if compare_row_strength(tp.evidence, move_evidence) is not StrengthComparison.EQUAL:
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


def _position_grain(tp: TrustedPosition) -> PositionGrain:
    return PositionGrain(
        evidence=tp.evidence,
        best_move_uci=tp.best_move_uci,
        best_move_san=tp.best_move_san,
        best_line_uci=tp.best_line_uci,
        best_eval=tp.best_eval,
        best_eval_mate=tp.best_eval_mate,
    )


def _move_grain(row: AnalysisCache, evidence: ResolvedEvidence) -> MoveGrain:
    """Build the move grain, carrying any best-* facts the SAME row embeds.

    A legacy ``resolver-complete-v2`` row is COMBINED (both grains in one row); the
    coherence resolver uses the embedded facts to prove the pair agrees. A future
    native ``move-complete-v1`` row leaves them null and takes the move-only branch.
    """
    return MoveGrain(
        evidence=evidence,
        move_uci=row.move_uci,
        played_eval=row.played_eval,
        played_eval_mate=row.played_eval_mate,
        eval_delta=row.eval_delta,
        classification=row.classification,
        best_move_uci=row.best_move_uci,
        best_line_uci=decode_uci_line(row.best_line_uci),
        best_eval=row.best_eval,
        best_eval_mate=row.best_eval_mate,
    )


def _build_reusable_analysis(
    tuples: dict[Capability, CoherentEvidence | None],
) -> ReusableAnalysis | None:
    """Publish ONE coherent tuple, flagging only the capabilities it actually carries.

    A flag may be true only when that capability's own
    ``resolve_coherent_evidence_tuple`` call returned a tuple AND that tuple is the
    one being published. When the two capabilities approve DIFFERENT facts, the
    payload carries one capability's facts and the other flag is false — never a
    flag describing facts it did not approve.
    """
    interactive = tuples.get(Capability.INTERACTIVE_ANALYSIS_REUSE)
    game = tuples.get(Capability.GAME_ANALYSIS_REUSE)
    payload = interactive or game
    if payload is None:
        return None
    return ReusableAnalysis(
        best_move_uci=payload.best_move_uci,
        best_line_uci=payload.best_line_uci,
        best_eval=payload.best_eval,
        best_eval_mate=payload.best_eval_mate,
        played_eval=payload.played_eval,
        played_eval_mate=payload.played_eval_mate,
        classification=payload.classification,
        eval_delta=payload.eval_delta,
        interactive_analysis_reuse=interactive == payload,
        game_analysis_reuse=game == payload,
    )


def _build_publication_best(
    interactive: TrustedPosition | None, game: TrustedPosition | None
) -> PublicationBest | None:
    """Emit the reconcilable best move, or nothing when the two capabilities disagree.

    Canonical parity: canonical rows hold every capability and carry no
    associations, so both winners are the same row and both flags are true exactly
    where ``position_trusted`` is true today.
    """
    if interactive is None and game is None:
        return None
    if (
        interactive is not None
        and game is not None
        and interactive.best_move_uci != game.best_move_uci
    ):
        # Never let one flag describe the other capability's facts.
        return None
    winner = interactive or game
    assert winner is not None
    return PublicationBest(
        best_move_uci=winner.best_move_uci,
        interactive_analysis_reuse=interactive is not None,
        game_analysis_reuse=game is not None,
    )


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
    """Batch cache lookup, resolved per CONSUMER capability for ONE viewer.

    Query ceiling (g-v21l §3): at most FOUR evidence SELECTs for any non-empty
    request of up to :data:`MAX_LOOKUP_POSITIONS`, independent of hit count and
    capability count —

      1. exact/full-FEN ``analysis_cache`` rows;
      2. normalized-FEN ``position_analysis`` rows;
      3. normalized-FEN fallback ``analysis_cache`` rows;
      4. ONE viewer-scoped ``analysis_cache_submission`` fetch over the already-
         loaded candidate ids.

    Query 4 stamps each descriptor's ``viewer_associated``; all four capabilities
    then resolve against that same in-memory membership set. No capability may
    re-enter the two-query resolver and no loop may issue a query per position.
    """
    started = time.perf_counter()
    viewer_user_id = user.user_id
    fens = [p.fen for p in request.positions]
    query_started = time.perf_counter()
    # (1) exact/full-FEN move rows.
    rows = (
        db.query(AnalysisCache)
        .filter(AnalysisCache.fen_before.in_(fens))
        .all()
    )

    # Index rows by (fen, move_uci) for O(1) lookup
    build_started = time.perf_counter()
    row_map: dict[tuple[str, str], AnalysisCache] = {}
    for row in rows:
        row_map[(row.fen_before, row.move_uci)] = row

    # Resolve POSITION evidence separately by NORMALIZED FEN (transposition).
    # Phase 6: normalize ALL requested FENs (not just the move-row subset) so a
    # POSITION-ONLY hit (trusted position, no exact move row) can still emit its
    # position grain. FEN normalization is the only failure narrowed here —
    # resolver/DB errors propagate.
    norm_by_fen: dict[str, str | None] = {}
    for fen in {p.fen for p in request.positions}:
        try:
            norm_by_fen[fen] = normalize_fen(fen)
        except Exception:
            norm_by_fen[fen] = None
    # (2) + (3): the whole candidate set, loaded once, capability-agnostic.
    candidates = load_position_candidates(
        db, [n for n in norm_by_fen.values() if n]
    )
    # (4): the ONE viewer-scoped association fetch, over the union of every
    # already-loaded candidate id (fallback position candidates AND exact move
    # rows). Issued after 1 and 3, once per request, never per capability.
    associated_ids = viewer_associated_ids(
        db, viewer_user_id, set(candidates.cache_ids) | {r.id for r in rows}
    )
    query_ms = _elapsed_ms(query_started)

    # Every capability resolves in memory over that single candidate set.
    resolved_by_capability = {
        capability: resolve_positions_from_candidates(
            candidates, capability, viewer_user_id, associated_ids
        )
        for capability in _LOOKUP_CAPABILITIES
    }

    def _position_for(capability: Capability, norm: str | None) -> TrustedPosition | None:
        if norm is None:
            return None
        return resolved_by_capability[capability].get(norm)

    results: dict[str, CachedAnalysisResult] = {}
    for position in request.positions:
        key = _make_cache_key(position.fen, position.move_uci)
        row = row_map.get((position.fen, position.move_uci))
        norm = norm_by_fen.get(position.fen)

        tp_read = _position_for(Capability.POSITION_READ, norm)
        tp_drill = _position_for(Capability.DRILL_GRADE, norm)
        tp_interactive = _position_for(Capability.INTERACTIVE_ANALYSIS_REUSE, norm)
        tp_game = _position_for(Capability.GAME_ANALYSIS_REUSE, norm)
        publication_best = _build_publication_best(tp_interactive, tp_game)

        if row is None and tp_read is None and publication_best is None and tp_drill is None:
            # No evidence at any capability -> no result emitted for this key.
            continue

        if row is not None:
            authoritative, satisfied = _trust_flags(row)
            move_associated = row.id in associated_ids
            move_evidence = describe_move_row(row, viewer_associated=move_associated)
            _, _, move_trusted = move_trust_flags(
                cache_row_as_move_dict(row, viewer_associated=move_associated),
                Capability.MOVE_READ,
                viewer_user_id,
            )
            move_grain = _move_grain(row, move_evidence)
            reuse_tuples: dict[Capability, CoherentEvidence | None] = {}
            for capability in _REUSE_CAPABILITIES:
                tp_cap = _position_for(capability, norm)
                reuse_tuples[capability] = (
                    resolve_coherent_evidence_tuple(
                        position.fen,
                        _position_grain(tp_cap),
                        move_grain,
                        capability,
                        viewer_user_id,
                    )
                    if tp_cap is not None
                    else None
                )
            results[key] = CachedAnalysisResult(
                move_san=row.move_san,
                best_move_uci=tp_read.best_move_uci if tp_read else None,
                best_move_san=tp_read.best_move_san if tp_read else None,
                best_line_uci=tp_read.best_line_uci if tp_read else None,
                best_eval=tp_read.best_eval if tp_read else None,
                best_eval_mate=tp_read.best_eval_mate if tp_read else None,
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
                position_trusted=tp_read is not None,
                move_trusted=move_trusted,
                drill_best_move_uci=tp_drill.best_move_uci if tp_drill else None,
                position_eval_loss_cp=(
                    _position_eval_loss_cp(
                        position.fen, tp_drill, row, move_evidence, viewer_user_id
                    )
                    if tp_drill is not None
                    else None
                ),
                reusable_analysis=_build_reusable_analysis(reuse_tuples),
                publication_best=publication_best,
            )
        else:
            # POSITION-ONLY hit (Phase 6): a position resolved for at least one
            # capability but no exact (fen, move_uci) move row exists. The MOVE
            # grain is null/untrusted, no threshold loss is derivable without a
            # played eval, and no coherent tuple can be assembled — but the drill
            # best move and the publication best still stand on the position grain
            # alone. All move-row metadata (source/profile/engine/contract) is absent.
            results[key] = CachedAnalysisResult(
                move_san=None,
                best_move_uci=tp_read.best_move_uci if tp_read else None,
                best_move_san=tp_read.best_move_san if tp_read else None,
                best_line_uci=tp_read.best_line_uci if tp_read else None,
                best_eval=tp_read.best_eval if tp_read else None,
                best_eval_mate=tp_read.best_eval_mate if tp_read else None,
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
                position_trusted=tp_read is not None,
                move_trusted=False,
                drill_best_move_uci=tp_drill.best_move_uci if tp_drill else None,
                position_eval_loss_cp=None,
                reusable_analysis=None,
                publication_best=publication_best,
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
