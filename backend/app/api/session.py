from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

import chess
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func, tuple_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.analysis_cache_policy import Reason, browser_live_descriptor
from app.analysis_cache_repo import write_analysis_cache_rows
from app.browser_provenance_metrics import session_provenance_verdict
from app.analysis_profiles import (
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    BROWSER_GAME_V2_PROFILE_ID,
    BROWSER_PROFILE_ID,
    stamp_dynamic_profile,
    stamp_profile_full,
)
from app.evidence_policy import validate_browser_provenance
from app.move_classification import EngineScore, classify_root_alternative
from app.move_upgrade import MoveUpgrade, move_upgrade_for_row
from app.centipawn_loss import (
    centipawn_loss,
    centipawn_loss_expr,
    clamp_delta_nonneg,
    round_half_up_cpl,
)
from app.evidence_contracts import (
    RESOLVER_COMPLETE_V2,
    contract_satisfied,
    select_browser_contract,
)
from app.db import get_db
from app.accuracy import (
    expected_total_moves_from_pgn,
    game_accuracy_for_rows,
    recompute_session_accuracy,
)
from app.fen import active_color, fen_hash, normalize_fen
from app.graph_write_lock import acquire_graph_write_lock
from app.opening_cache import bump_evidence_seq, load_cached_rows_nonblocking
from app.opening_evidence import session_is_evidence_eligible
from app.opening_score_scheduler import request_recompute
from app.opening_roots import get_opening_roots, played_opening_chain_indexed
from app.position_analysis_repo import resolve_trusted_positions
from app.posthog_client import capture
from app.row_locks import for_no_key_update
from app.session_evidence_scheduler import enqueue_session_evidence
from app.models import (
    AnalysisCache,
    Blunder,
    BlunderOpportunityEvent,
    GameSession,
    Move,
    Position,
    SessionMove,
    SessionUploadReceipt,
    decode_uci_line,
    encode_uci_line,
)
from app.security import TokenPayload, get_current_user
from app.session_contracts import (
    DRILL_SESSION_MODE,
    NORMAL_MOVE_SEGMENT,
    VISIBLE_DRILL_STATE,
    is_visible_game_session,
    normal_play_started_at,
    segment_for_move,
)
from app.srs_math import OPPORTUNITY_ANCESTOR_RADIUS_PLY

router = APIRouter(prefix="/api/session", tags=["session"])
logger = logging.getLogger(__name__)

# Per-user graph-write serialization guardrails (g-q0aw / g-graph-lock, Postgres
# only). The advisory-lock acquisition + txn-local lock_timeout/statement_timeout now
# live in the shared ``acquire_graph_write_lock`` helper so the deferred evidence
# worker (below) and both blunder-recording paths serialize on the SAME per-user
# lock instead of racing the (user_id, fen_hash) unique index. See
# app/graph_write_lock.py for the guardrail rationale and the tunable timeouts.
#
# Postgres SQLSTATEs we treat as recoverable graph-write timeouts: lock_not_available
# (55P03, from lock_timeout) and query_canceled (57014, from statement_timeout). Only
# the worker retries on these (below); the blunder paths let the timeout propagate to
# a 500 with a clean rollback (no partial graph/cursor), so it is a worker-only policy.
_GRAPH_TIMEOUT_SQLSTATES = frozenset({"55P03", "57014"})


def _graph_timeout_sqlstate(err: OperationalError) -> str | None:
    """Return the SQLSTATE of ``err`` if it is a recoverable graph-write timeout.

    psycopg3 exposes the SQLSTATE on the wrapped DBAPI error as ``.sqlstate``.
    Returns the matched code (truthy) for lock/statement timeouts, else ``None`` so
    callers re-raise connection failures and every other OperationalError unchanged.
    """
    sqlstate = getattr(getattr(err, "orig", None), "sqlstate", None)
    return sqlstate if sqlstate in _GRAPH_TIMEOUT_SQLSTATES else None


class MoveColor(str, Enum):
    WHITE = "white"
    BLACK = "black"


class MoveClassification(str, Enum):
    BEST = "best"
    EXCELLENT = "excellent"
    GOOD = "good"
    INACCURACY = "inaccuracy"
    MISTAKE = "mistake"
    BLUNDER = "blunder"


class SessionDecisionSource(str, Enum):
    GHOST_PATH = "ghost_path"
    BACKEND_ENGINE = "backend_engine"
    LOCAL_FALLBACK = "local_fallback"


class SessionMoveInput(BaseModel):
    move_number: int = Field(..., ge=1)
    color: MoveColor
    move_san: str = Field(..., min_length=1, max_length=10)
    fen_after: str = Field(..., min_length=1)
    eval_cp: int | None = None
    eval_mate: int | None = None
    best_move_san: str | None = Field(None, max_length=10)
    best_move_eval_cp: int | None = None
    eval_delta: int | None = None
    classification: MoveClassification | None = None
    fen_before: str | None = Field(None, min_length=1)
    move_uci: str | None = Field(None, min_length=2, max_length=5)
    best_move_uci: str | None = Field(None, max_length=5)
    best_line_uci: list[str] | None = None
    decision_source: SessionDecisionSource | None = None
    target_blunder_id: int | None = Field(None, ge=1)
    # Transient provenance for deterministic game-end terminal evals. The eval
    # fields still persist to SessionMove, but this flag intentionally does not:
    # synthetic rows are too sparse (and draws can be history-dependent) to be
    # safe global analysis-cache evidence.
    synthetic_terminal_eval: bool = False
    # Per-row browser-game-v2 DYNAMIC provenance (g-mk1d): the seven engine/search
    # values this device actually used for THIS move's own local search. Absent or
    # null ⇒ the row is stamped browser-game-v1 exactly as before (legacy clients,
    # cache-sourced results, canonically reconciled tuples, time-truncated searches).
    #
    # Typed ``Any`` DELIBERATELY. A constrained shape (even ``dict[str, object]``)
    # makes Pydantic reject every non-object JSON value during request parsing,
    # which 422s the ENTIRE SessionMovesRequest before the endpoint body runs —
    # exactly the batch-wide failure the /moves per-row-degradation contract must
    # prevent. ``Any`` accepts any JSON shape unconditionally, so nothing about
    # provenance can fail schema validation; ALL checking (starting with "is this
    # even a mapping?") happens per row in validate_browser_provenance, whose
    # failure drops only that row's cache evidence.
    provenance: Any | None = None


class SessionMovesRequest(BaseModel):
    moves: list[SessionMoveInput] = Field(default_factory=list)
    # When False, this upload SKIPS the expensive blunder-opportunity recompute
    # (forward BFS + load-all-blunders + bulk DELETE/UPSERT). The frontend's
    # mid-game incremental uploader opts out so only the final, complete upload
    # pays for it. Default True keeps any other caller's behavior unchanged
    # (compute — safe, just slower). The graph upsert + analysis-cache write +
    # opening-score recompute enqueue still run on EVERY upload regardless.
    recompute_opportunity: bool = True
    # Client-sent ONLY for the end-of-session final_full upload (g-upload-observe);
    # the mid-game incremental uploader and the revert upload never send it. Its
    # PRESENCE is how the server identifies a final_full upload and gates the
    # durable receipt write — recompute_opportunity can't (the revert path also
    # sends true). Pydantic allowlists the four terminal actions; anything else is
    # rejected 422 before the endpoint runs.
    terminal_action: (
        Literal["game_end", "resign", "drill_natural_end", "accuracy_fail"] | None
    ) = None


class SessionMovesResponse(BaseModel):
    moves_inserted: int
    drill_state: str | None = None
    drill_terminal_reason: str | None = None


class SessionAnalysisMove(BaseModel):
    move_number: int
    color: MoveColor
    move_san: str
    fen_after: str
    # Exact evidence keys (g-cache-stronger-evals): the stored ``SessionMove``
    # fen_before plus the python-chess SAN->UCI derivation. Null only for legacy
    # moves whose ``SessionMove.fen_before`` is null or whose SAN fails to parse;
    # those moves are not evidence-eligible. Consumers (analysis board, evidence
    # driver, exact-best projection) use these directly and NEVER reconstruct
    # fen_before from the previous move's fen_after nor derive UCI from SAN.
    fen_before: str | None = None
    move_uci: str | None = None
    eval_cp: int | None = None
    eval_mate: int | None = None
    best_move_san: str | None = None
    best_move_eval_cp: int | None = None
    eval_delta: int | None = None
    classification: MoveClassification | None = None
    segment: str = NORMAL_MOVE_SEGMENT
    # Read-time re-annotation overlay (g-xox0 Part C): a stronger label for this exact
    # played move, joined from analysis_cache. Attached ALONGSIDE the base fields —
    # the base classification/eval_* stay on the ORIGINAL game-time evidence so the
    # SUMMARY below and accuracy keep aggregates on original. The FE does NOT display
    # these base fields verbatim: both review pages run them through projectExactBest
    # before computing the displayed counts/Avg CPL (g-22t8.2), so a played move equal
    # to the trusted position best displays as best/0. Mirror of the note on
    # AnalysisMove.upgraded in src/utils/api.ts — keep the two in step. Null when no
    # display-upgrade-eligible cache row exists for (fen_before, move_uci).
    upgraded: MoveUpgrade | None = None


class SessionAnalysisSummary(BaseModel):
    blunders: int
    mistakes: int
    inaccuracies: int
    # None IFF no player move has an eval_delta. 0 means perfect play, not missing
    # data. A partially analyzed game reports the average over the plies that
    # resolved (no completeness gate — unlike accuracy).
    average_centipawn_loss: int | None
    accuracy: int | None = None


class PositionAnalysis(BaseModel):
    best_move_uci: str
    best_move_san: str | None = None
    best_move_eval_cp: int | None = None  # side-to-move-relative
    best_move_eval_mate: int | None = None  # side-to-move-relative
    best_line_uci: list[str] | None = None
    # Backend trust decision for the POSITION evidence in this entry: True when it
    # came from a trusted position_analysis winner / legacy v2 projection; False when
    # it is an untrusted SessionMove seed. Set explicitly per entry — never defaulted
    # so an untrusted seed can never read as trusted.
    position_trusted: bool


class SessionAnalysisResponse(BaseModel):
    session_id: uuid.UUID
    pgn: str | None
    result: str | None
    player_color: str
    moves: list[SessionAnalysisMove]
    summary: SessionAnalysisSummary
    position_analysis: dict[str, PositionAnalysis] = {}
    expected_total_moves: int | None = None
    analyzed_moves: int = 0
    is_complete: bool = False
    rated_start_ply: int | None = None


class AnalysisEvidenceRow(BaseModel):
    """One approved analysis-board evidence row submitted for cache persistence.

    White-relative evals; ``eval_delta`` is recomputed client-side from those
    white-relative evals. Deliberately carries NO SAN, profile, authority, source,
    or evidence-contract fields — the backend derives SAN, stamps the profile, and
    validates the contract. ``played_eval`` / ``best_eval`` / ``eval_delta`` /
    ``classification`` are optional at the wire level so a sparse row degrades to a
    per-row ``contract_unsatisfied`` rejection rather than a 422 for the whole batch.
    """

    fen: str = Field(..., min_length=1)
    move_uci: str = Field(..., min_length=2, max_length=5)
    best_move_uci: str = Field(..., min_length=2, max_length=5)
    # Real PVs are ≲ 40 plies; the cap defends against a long legal shuffle line
    # forcing per-ply python-chess legality generation across up to 60 rows. An
    # over-cap line 422s the whole batch (a defensive bound well above real PVs).
    best_line_uci: list[str] = Field(default_factory=list, max_length=64)
    played_eval: int | None = None
    played_eval_mate: int | None = None
    best_eval: int | None = None
    best_eval_mate: int | None = None
    eval_delta: int | None = None
    classification: str | None = None


class AnalysisEvidenceRequest(BaseModel):
    # Cap defensive against a runaway client; normal operation submits one dwelled
    # move at a time. An over-cap request 422s the whole batch by design.
    rows: list[AnalysisEvidenceRow] = Field(default_factory=list, max_length=60)
    # Endpoint-controlled producer discriminator (g-reuse-d21-search §6.3). Optional
    # at schema validation so an under-specified/stale client degrades to per-row
    # rejection (stale_producer / unknown_producer), never a batch 422. The only
    # allowed value is "visible-multipv-v1", which maps server-side to the
    # browser-analysis-multipv-v2 profile. A stale client running the retired hidden
    # worker sends no producer and fails closed.
    producer: str | None = None


class AnalysisEvidenceResult(BaseModel):
    """Per-submitted-row outcome, one per request row, in request order."""

    fen: str
    move_uci: str
    reason: str
    # Immediate MoveList patch (g-xox0 Part B): the stronger re-annotation built from
    # the STORED row on an accepted write, else None. Built from the stored (post-
    # merge) row — NOT the submitted row — so it is exact even when the writer merged.
    upgrade: MoveUpgrade | None = None


class AnalysisEvidenceResponse(BaseModel):
    results: list[AnalysisEvidenceResult]


class OpeningLineageItem(BaseModel):
    opening_key: str
    opening_name: str
    opening_family: str
    eco: str | None
    depth: int
    score: float | None
    confidence: float | None
    coverage: float | None
    sample_size: int | None
    game_count: int | None
    path: list[str]
    # The player's actual SAN moves up to and including the move that crossed
    # into this opening (e.g. ["e4", "c6", "Bc4"] for a Hillbilly Attack item).
    # Numbering is anchored by SessionOpeningsResponse.start_ply.
    moves: list[str]


class SessionOpeningsResponse(BaseModel):
    player_color: str
    lineage: list[OpeningLineageItem]
    # Ply of moves[0] across all lineage items (1 = White's move 1). Constant
    # because every item's move prefix starts at the game's first stored move;
    # computed authoritatively from move_number/color so drill lineages whose
    # stored moves don't start at ply 1 still number correctly.
    start_ply: int = 1
    # "pending" when no score batch exists yet and a background recompute is
    # in flight — the lineage is complete but every score field is null and the
    # client should show a loading affordance rather than "unscored". A warm
    # batch is always "ready", even with a background refresh running: it is
    # displayable, and calling stale-warm "pending" would pin a permanent
    # spinner on the common path.
    score_status: Literal["ready", "pending"] = "ready"


def _get_session_or_404(db: Session, session_id: uuid.UUID) -> GameSession:
    game_session = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not game_session:
        raise HTTPException(status_code=404, detail="Game session not found")
    return game_session


def _ensure_session_owned_by_user(game_session: GameSession, user: TokenPayload) -> None:
    if game_session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this game")


def _derive_move_uci(fen_before: str | None, move_san: str | None) -> str | None:
    """Derive a played move's UCI from a stored ``(fen_before, move_san)`` pair.

    ``SessionMove`` persists SAN but not UCI, so the exact-key model derives the UCI
    half server-side via python-chess. Returns ``None`` for a null/unparseable FEN
    or SAN so a legacy move is simply not evidence-eligible. This is the SAN->UCI
    derivation the endpoint's membership check and the analysis wire fields both use;
    it is expected to agree with the browser-game upload's chess.js-derived UCI at
    castling / promotion / en-passant edge cases (covered by targeted tests).
    """
    if not fen_before or not move_san:
        return None
    try:
        board = chess.Board(fen_before)
        return board.parse_san(move_san).uci()
    except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError):
        return None


def _validate_unique_move_keys(moves: list[SessionMoveInput]) -> None:
    seen: set[tuple[int, str]] = set()
    for move in moves:
        key = (move.move_number, move.color.value)
        if key in seen:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Duplicate move entry in payload for "
                    f"move_number={move.move_number}, color={move.color.value}"
                ),
            )
        seen.add(key)


def _reverse_ancestor_position_ids(
    db: Session,
    *,
    start_position_id: int,
    player_color: str,
    user_id: int,
    radius_ply: int = OPPORTUNITY_ANCESTOR_RADIUS_PLY,
) -> set[int]:
    """Opponent-color positions that can reach ``start_position_id`` within
    ``radius_ply`` reverse plies.

    No longer used by the request hot path — ``_compute_blunder_opportunity_events``
    now classifies every blunder with one forward BFS (see
    ``_forward_reachable_position_ids``). This per-blunder reverse walk is retained
    for the offline ``scripts/recompute_srs_opportunities.py --blunder-id /
    --all-blunders`` maintenance path, where ancestors of a single blunder are
    computed once and reused across all of that blunder's sessions; it produces
    results identical to the forward path by the same ancestor⇄reachable duality.

    Like the forward path, each reverse expansion is constrained to ``user_id``'s
    positions (``moves`` carries no ``user_id``) so a cross-user edge path can never
    leak an ancestor — keeping the offline result identical to the scoped live one.
    """
    opponent_color = "black" if player_color == "white" else "white"
    frontier = {start_position_id}
    visited = {start_position_id}
    ancestors: set[int] = set()

    for _ in range(radius_ply):
        if not frontier:
            break
        parent_ids = {
            row[0]
            for row in db.query(Move.from_position_id)
            .join(Position, Position.id == Move.from_position_id)
            .filter(Move.to_position_id.in_(frontier), Position.user_id == user_id)
            .all()
        }
        parent_ids -= visited
        if not parent_ids:
            break
        visited.update(parent_ids)

        matching_ids = {
            row[0]
            for row in db.query(Position.id)
            .filter(Position.id.in_(parent_ids), Position.active_color == opponent_color)
            .all()
        }
        ancestors.update(matching_ids)
        frontier = parent_ids

    ancestors.discard(start_position_id)
    return ancestors


def _forward_reachable_position_ids(
    db: Session,
    *,
    user_id: int,
    start_ids: set[int],
    radius_ply: int = OPPORTUNITY_ANCESTOR_RADIUS_PLY,
) -> set[int]:
    """Positions reachable from ``start_ids`` by ≤``radius_ply`` forward move-edges.

    Forward dual of the old per-blunder reverse walk: a session position ``S``
    makes blunder position ``B`` an opportunity iff ``B`` is forward-reachable
    from an opponent-color session position ``S`` within ``radius_ply`` plies
    (``S != B``). Seeding the BFS from the full opponent-color session set lets us
    classify every blunder with a single 8-level traversal instead of one walk per
    blunder.

    ``moves`` carries no ``user_id`` (``models.py``), so each level explicitly
    constrains expansion to this user's positions — preventing cross-user graph
    leakage by construction even if two users share position ids in the edge set.
    """
    if not start_ids:
        return set()

    frontier = set(start_ids)
    visited = set(start_ids)
    reachable: set[int] = set()

    for _ in range(radius_ply):
        if not frontier:
            break
        child_ids = {
            row[0]
            for row in db.query(Move.to_position_id)
            .join(Position, Position.id == Move.to_position_id)
            .filter(Move.from_position_id.in_(frontier), Position.user_id == user_id)
            .all()
        }
        child_ids -= visited
        if not child_ids:
            break
        visited.update(child_ids)
        reachable.update(child_ids)
        frontier = child_ids

    return reachable


def _bulk_upsert_opportunity_events(db: Session, rows: list[dict]) -> None:
    """Upsert many opportunity-event rows in a single statement.

    ``rows`` is a list of dicts with keys ``session_id``, ``blunder_id``,
    ``occurred_at``, ``opportunity``, ``reached``. On sqlite/postgresql this emits
    one multi-row ``INSERT ... ON CONFLICT ... VALUES`` (the dialect ``.values()``
    accepts a list of dicts), collapsing the former one-round-trip-per-blunder loop
    (g-b809). The generic-dialect fallback keeps the per-row query-then-merge path.
    """
    if not rows:
        return

    dialect_name = db.bind.dialect.name if db.bind else ""
    if dialect_name == "sqlite":
        stmt = sqlite_insert(BlunderOpportunityEvent).values(rows)
    elif dialect_name == "postgresql":
        stmt = postgresql_insert(BlunderOpportunityEvent).values(rows)
    else:
        for values in rows:
            existing = db.query(BlunderOpportunityEvent).filter(
                BlunderOpportunityEvent.session_id == values["session_id"],
                BlunderOpportunityEvent.blunder_id == values["blunder_id"],
            ).first()
            if existing:
                existing.occurred_at = values["occurred_at"]
                existing.opportunity = values["opportunity"]
                existing.reached = values["reached"]
            else:
                db.add(BlunderOpportunityEvent(**values))
        return

    stmt = stmt.on_conflict_do_update(
        index_elements=[BlunderOpportunityEvent.session_id, BlunderOpportunityEvent.blunder_id],
        set_={
            "occurred_at": stmt.excluded.occurred_at,
            "opportunity": stmt.excluded.opportunity,
            "reached": stmt.excluded.reached,
        },
    )
    db.execute(stmt)


def _upsert_opportunity_event(
    db: Session,
    *,
    session_id: uuid.UUID,
    blunder_id: int,
    occurred_at,
    opportunity: bool,
    reached: bool,
) -> None:
    _bulk_upsert_opportunity_events(
        db,
        [
            {
                "session_id": session_id,
                "blunder_id": blunder_id,
                "occurred_at": occurred_at,
                "opportunity": opportunity,
                "reached": reached,
            }
        ],
    )


@dataclass
class GhostGraphUpsertStats:
    """Counts from a ghost-graph upsert; single source of truth for backfill reporting."""

    valid_moves: int = 0
    invalid_moves: int = 0
    positions_created: int = 0
    edges_created: int = 0
    edges_existing: int = 0


def _upsert_session_position_graph(
    db: Session,
    *,
    user_id: int,
    moves: list[SessionMoveInput],
) -> GhostGraphUpsertStats:
    """Teach the ghost graph from ordinary uploaded game moves.

    Bulk-query design (g-wlzj): resolve every position and edge with a fixed
    number of round-trips — one positions SELECT, one positions flush, one moves
    SELECT, one final edge flush — instead of the former per-move Position lookup
    + per-move Move lookup (~3 round-trips/move). The six phases below replace the
    old per-row loop while preserving its counters and dedup semantics exactly.
    """
    stats = GhostGraphUpsertStats()

    def move_matches_fens(move: SessionMoveInput) -> bool:
        if not move.fen_before or not move.fen_after:
            return False

        try:
            board = chess.Board(move.fen_before)
            parsed_move = board.parse_san(move.move_san)
            board.push(parsed_move)
            return normalize_fen(board.fen()) == normalize_fen(move.fen_after)
        except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError):
            return False

    # ``hash_meta`` maps fen_hash -> (first-seen raw FEN, active color) in
    # insertion order. It is the single source of truth for the lookup IN-set
    # and the creation order of new positions; first-seen wins (transposed FENs
    # share a hash but differ in raw FEN / move clocks). ``valid_specs`` holds
    # (h_before, h_after, san) for every move whose BOTH endpoint FENs hashed.
    hash_meta: dict[str, tuple[str, str]] = {}
    valid_specs: list[tuple[str, str, str]] = []

    def record_hash(fen: str) -> str | None:
        try:
            hash_val = fen_hash(fen)
            color = active_color(fen)
        except (IndexError, ValueError):
            return None
        if hash_val not in hash_meta:
            hash_meta[hash_val] = (fen, color)
        return hash_val

    # Phase 1 — validate + hash (CPU only, no DB). Record before then after,
    # independently — mirrors the old unconditional ensure_position(before) then
    # ensure_position(after): each position is created/counted even when the
    # other FEN fails to hash and the edge is dropped.
    for move in moves:
        if not move_matches_fens(move):
            stats.invalid_moves += 1
            continue
        stats.valid_moves += 1

        h_before = record_hash(move.fen_before)
        h_after = record_hash(move.fen_after)
        if h_before is not None and h_after is not None:
            valid_specs.append((h_before, h_after, move.move_san))

    hash_to_position_id: dict[str, int] = {}

    # Phase 2 — bulk-fetch existing positions (1 SELECT).
    if hash_meta:
        for pid, fh in (
            db.query(Position.id, Position.fen_hash)
            .filter(Position.user_id == user_id, Position.fen_hash.in_(hash_meta.keys()))
            .all()
        ):
            hash_to_position_id[fh] = pid

    # Phase 3 — bulk-create new positions (1 flush). Iterate hash_meta in
    # insertion order so ids are assigned in first-seen order; one flush assigns
    # all ids (SQLAlchemy 2.x insertmanyvalues -> batched RETURNING on Postgres).
    new_positions: list[tuple[str, Position]] = []
    for hash_val, (fen_raw, color) in hash_meta.items():
        if hash_val in hash_to_position_id:
            continue
        position = Position(
            user_id=user_id,
            fen_hash=hash_val,
            fen_raw=fen_raw,
            active_color=color,
        )
        db.add(position)
        new_positions.append((hash_val, position))
    if new_positions:
        db.flush()
        for hash_val, position in new_positions:
            hash_to_position_id[hash_val] = position.id
        stats.positions_created += len(new_positions)

    # Phase 4 — bulk-fetch existing moves (1 SELECT). Resolve endpoints, then
    # fetch every (from_id, san) edge for the candidate from-ids in one query.
    # Filtering by from_position_id only over-fetches sibling SANs at a from-id;
    # newly-created from-ids return nothing (their edges aren't flushed yet).
    resolved_specs: list[tuple[int, int, str]] = []
    candidate_from_ids: set[int] = set()
    for h_before, h_after, san in valid_specs:
        from_id = hash_to_position_id.get(h_before)
        to_id = hash_to_position_id.get(h_after)
        if from_id is None or to_id is None:
            continue
        resolved_specs.append((from_id, to_id, san))
        candidate_from_ids.add(from_id)

    existing_edges: set[tuple[int, str]] = set()
    if candidate_from_ids:
        existing_edges = {
            (fid, san)
            for fid, san in db.query(Move.from_position_id, Move.move_san)
            .filter(Move.from_position_id.in_(candidate_from_ids))
            .all()
        }

    # Phase 5 — resolve + stage edges in-memory (no round-trips). pending_edges
    # dedups within this batch (a game can transpose back to the same edge); the
    # check order matches the old loop exactly.
    pending_edges: set[tuple[int, str]] = set()
    for from_id, to_id, san in resolved_specs:
        edge_key = (from_id, san)
        if edge_key in pending_edges:
            stats.edges_existing += 1
            continue
        if edge_key in existing_edges:
            stats.edges_existing += 1
            continue
        db.add(
            Move(
                from_position_id=from_id,
                move_san=san,
                to_position_id=to_id,
            )
        )
        pending_edges.add(edge_key)
        stats.edges_created += 1

    # Phase 6 — flush all staged edges (g-wlzj). The immediately-following
    # opportunity BFS (_forward_reachable_position_ids) runs in this same txn with
    # autoflush off, so it only sees flushed edges; flushing here exposes this
    # upload's fresh edges to it. Strict superset of the old behavior, where the
    # incidental per-position flush already leaked most — but not the tail — of
    # these edges. No-op when no edges were staged.
    db.flush()

    return stats


def _compute_blunder_opportunity_events(
    db: Session,
    *,
    session_id: uuid.UUID,
    user_id: int,
    player_color: str,
) -> None:
    game_session = db.query(GameSession).filter(GameSession.id == session_id).first()
    if game_session is None:
        return

    session_moves = (
        db.query(SessionMove)
        .filter(SessionMove.session_id == session_id)
        .all()
    )
    session_hashes: set[str] = set()
    for move in session_moves:
        if move.fen_before:
            try:
                session_hashes.add(fen_hash(move.fen_before))
            except ValueError:
                pass
        if move.fen_after:
            try:
                session_hashes.add(fen_hash(move.fen_after))
            except ValueError:
                pass
    opponent_color = "black" if player_color == "white" else "white"
    session_position_ids: set[int] = set()
    # Opponent-color session positions seed the forward BFS: only an opponent-move
    # ancestor can "steer" the player into a blunder, so player-color positions are
    # never opportunity sources (they were filtered out by the old reverse walk too).
    opp_source: set[int] = set()
    if session_hashes:
        for position_id, active in (
            db.query(Position.id, Position.active_color)
            .filter(Position.user_id == user_id, Position.fen_hash.in_(session_hashes))
            .all()
        ):
            session_position_ids.add(position_id)
            if active == opponent_color:
                opp_source.add(position_id)

    matched: dict[int, tuple[bool, bool]] = {}
    if session_position_ids:
        forward_reachable = _forward_reachable_position_ids(
            db, user_id=user_id, start_ids=opp_source
        )
        blunders = db.query(Blunder).filter(Blunder.user_id == user_id).all()
        for blunder in blunders:
            reached = blunder.position_id in session_position_ids
            opportunity_only = blunder.position_id in forward_reachable
            opportunity = opportunity_only or reached
            if opportunity:
                matched[blunder.id] = (opportunity, reached)

    existing_events = (
        db.query(BlunderOpportunityEvent)
        .join(Blunder, Blunder.id == BlunderOpportunityEvent.blunder_id)
        .filter(BlunderOpportunityEvent.session_id == session_id, Blunder.user_id == user_id)
        .all()
    )
    # One DELETE for all stale rows, keyed by primary id. Deriving stale_ids from
    # the already-loaded, Blunder.user_id-scoped existing_events preserves that
    # scoping and bounds the IN-list by stale-row count (g-b809).
    stale_ids = [e.id for e in existing_events if e.blunder_id not in matched]
    if stale_ids:
        db.query(BlunderOpportunityEvent).filter(
            BlunderOpportunityEvent.id.in_(stale_ids)
        ).delete(synchronize_session=False)

    occurred_at = normal_play_started_at(game_session)
    # One INSERT ... ON CONFLICT over all matched blunders (g-b809). Matched and
    # stale id sets are disjoint by construction, so DELETE-then-INSERT is
    # behavior-preserving.
    _bulk_upsert_opportunity_events(
        db,
        [
            {
                "session_id": session_id,
                "blunder_id": blunder_id,
                "occurred_at": occurred_at,
                "opportunity": opportunity,
                "reached": reached,
            }
            for blunder_id, (opportunity, reached) in matched.items()
        ],
    )
    # NB: no commit here. The caller owns the commit. The ghost-graph edges are
    # already flushed (by _upsert_session_position_graph's own Phase 6 flush, so
    # the forward-reachable BFS above could see this upload's fresh edges); the
    # bulk DELETE and bulk INSERT here also emit SQL within this stage (autoflush
    # is off; ``.delete()`` and ``db.execute()`` issue immediately). Only their
    # durability finalizes at the caller's commit. Direct callers (tests/scripts)
    # must commit.


def _encoded_browser_provenance(move: SessionMoveInput) -> str | None:
    """JSON for ``session_moves.browser_provenance``, or ``None`` (g-mk1d §5.2).

    Runs the SAME :func:`validate_browser_provenance` gate as the cache write, so
    the persisted operand can only ever be a well-formed dynamic tuple: absent,
    malformed, or reconciled-null provenance all persist NULL. A malformed claim
    must never survive as a comparison operand — a NULL simply withholds the
    overlay, which is the safe direction.

    Synthetic-terminal rows persist NULL for the same reason ``_upsert_analysis_cache``
    refuses to cache them: their eval is FABRICATED by the client for a game-ending
    state, so no search produced it and no search-limit claim can describe it. Without
    this guard a request carrying both fields would store provenance that the cache
    correctly declined — and that stored tuple is exactly what
    ``browser_live_descriptor`` later hands the overlay as its "live search" operand,
    letting a fabricated eval suppress or license a genuine stored row.
    """
    if move.synthetic_terminal_eval:
        return None
    if move.provenance is None:
        return None
    fields = validate_browser_provenance(move.provenance)
    if fields is None:
        return None
    return json.dumps(fields.values, sort_keys=True, separators=(",", ":"))


# Writer verdicts that actually TOUCHED the stored row — every Reason returned with
# a ``Decision`` of INSERT / REPLACE / MERGE. Used for the ``cache_rows_written``
# log field, which answers "how many rows did this upload write", NOT the endpoint's
# "is the stored row now this evidence" (``_EVIDENCE_ACCEPTED_REASONS``).
#
# `same_profile_idempotent` is deliberately ABSENT: it is a ``Decision.KEEP`` and the
# writer's replace/merge branch ends at "KEEP: nothing to write"
# (analysis_cache_repo.py), so counting it would report a write for a re-upload that
# changed nothing — exactly the overcount this field exists to remove.
# `merge_conflict_keep` is likewise absent: it is reached INSIDE the MERGE branch
# when the merge is refused, and stores nothing.
_ROW_MUTATING_REASONS = frozenset(
    {
        Reason.NEW_KEY.value,  # INSERT
        Reason.DOMINATES_REPLACE.value,  # REPLACE
        Reason.PROTOCOL_CORRECTED_REPLACE.value,  # REPLACE
        Reason.STRENGTH_REPLACE.value,  # REPLACE
        # REPLACE, unreachable from a non-authoritative producer like this one, but
        # included because the set is defined by DECISION, not by today's callers.
        Reason.LEGACY_REPLACED_BY_AUTH.value,
        Reason.SAME_PROFILE_SUPERSET_MERGE.value,  # MERGE
        Reason.SAME_PROFILE_CONTRACT_UPGRADE.value,  # MERGE
    }
)


def _upsert_analysis_cache(
    db: Session,
    moves: list[SessionMoveInput],
    *,
    timing_fields: dict | None = None,
) -> int:
    """Upsert browser-game analysis evidence into the global cache.

    Evals are converted from player-relative (as uploaded) to white-relative for
    storage. A row carrying valid client provenance is stamped ``browser-game-v2``;
    one without is stamped the RETIRED ``browser-game-v1`` (g-bgv1-cutover) and is
    therefore refused by the writer with ``INACTIVE_PROFILE_KEEP`` — the upload
    still succeeds, it simply stores nothing. Rows are routed through the shared
    quality-aware writer: game uploads INSERT evidence for keys that have none and
    never replace existing canonical or legacy rows. Each row is classified
    per-shape into the most specific browser-allowed contract; rows matching no
    allowed contract are rejected (not stored).

    The return value is the count SUBMITTED to the writer (g-dckw's latency cohort
    key), which since the retirement is no longer the count written — see
    ``cache_rows_written`` on the ``analysis_cache_write`` log line for that.

    Returns the number of rows actually handed to the writer (``len(cache_values)``
    after per-shape filtering) — the cohort key for the ``analysis_cache_write``
    timing line (g-dckw). This is strictly ≤ the uploaded ``move_count``: moves
    with no ``fen_before``/``move_uci``, no eval, or matching no allowed contract
    are dropped here, so ``move_count`` overcounts the rows the write path touches.

    ``timing_fields`` (the mutable dict from :func:`_timed_side_effect`) has its
    ``cache_row_count`` stamped BEFORE the write is issued. The write is what can
    raise (an exhausted PG retry, a driver error) and the timing line is logged in
    ``finally``, so stamping only after a successful write would record a
    non-empty, possibly-slow failed batch as ``cache_row_count=0`` — falsely in the
    zero-row cohort. Stamped up front, a failed write logs its true row count (and,
    via the caller's ``status=error``, is excludable from the latency scrape).

    Per-row provenance (g-mk1d) selects the profile stamped on each row:
      * VALID   -> ``browser-game-v2`` + the seven dynamic identity columns;
      * ABSENT  -> ``browser-game-v1``, all-``None`` identity, exactly as before;
      * MALFORMED -> the row is DROPPED from the cache write. A malformed claim is
        never laundered into a silent v1 downgrade — it must stay visible in the
        counters. The move's ``session_moves`` row is still written (with NULL
        provenance), so the player's own eval/classification display is unaffected
        and the batch stays HTTP 200.
    """
    cache_values = []
    # Provenance observability (§2.4.1). NOTE the grain: this function runs inside
    # ``run_side_effects``, which the deferred evidence scheduler invokes ONCE PER
    # COALESCED SESSION RUN over the last-write-wins-merged move set — NOT once per
    # /moves request. So these counts are per coalesced run and ROW-WEIGHTED (a long
    # game contributes many rows). They are the operational health signal ("are
    # malformed rows appearing at all?"), NOT the fleet-adoption metric; adoption is
    # the length-independent per-session ``session_provenance`` bit stamped below.
    provenance_valid = 0
    provenance_absent = 0
    provenance_malformed = 0
    for move in moves:
        if move.synthetic_terminal_eval:
            continue
        if not move.fen_before or not move.move_uci:
            continue
        if move.eval_cp is None and move.best_move_eval_cp is None:
            continue

        # Classify this row's provenance BEFORE building it: a malformed claim
        # drops the row rather than downgrading it.
        if move.provenance is None:
            provenance_fields = None
            provenance_absent += 1
        else:
            provenance_fields = validate_browser_provenance(move.provenance)
            if provenance_fields is None:
                provenance_malformed += 1
                continue
            provenance_valid += 1

        is_black = move.color == MoveColor.BLACK
        sign = -1 if is_black else 1
        played_eval = move.eval_cp * sign if move.eval_cp is not None else None
        # Mate count flips perspective by sign-negation, same as cp.
        played_eval_mate = move.eval_mate * sign if move.eval_mate is not None else None
        best_eval = move.best_move_eval_cp * sign if move.best_move_eval_cp is not None else None
        # RAW-evidence clamp (floor at 0, NO upper cap): analysis_cache.eval_delta
        # must remain the exact contract value (may be mate pseudo-cp ~10000). It
        # deliberately does NOT inherit the centipawn_loss decisive-mistake cap —
        # /lookup returns this raw, and capping here would silently violate the
        # raw-evidence invariant (g-no51). session_moves gets the capped write below.
        eval_delta = clamp_delta_nonneg(move.eval_delta)

        row = {
            "fen_before": move.fen_before,
            "move_uci": move.move_uci,
            "move_san": move.move_san,
            "best_move_uci": move.best_move_uci,
            "best_move_san": move.best_move_san,
            "best_line_uci": encode_uci_line(move.best_line_uci),
            "played_eval": played_eval,
            "played_eval_mate": played_eval_mate,
            "best_eval": best_eval,
            "eval_delta": eval_delta,
            "classification": move.classification.value if move.classification else None,
            "source": "game",
            "analysis_profile_id": BROWSER_PROFILE_ID,
        }
        if provenance_fields is not None:
            # The client supplies ONLY the dynamic half; the fixed identity half
            # and the manifest digest are stamped server-side from the registry,
            # so no client can claim a fixed identity it did not earn.
            row["analysis_profile_id"] = BROWSER_GAME_V2_PROFILE_ID
            row.update(
                stamp_dynamic_profile(
                    BROWSER_GAME_V2_PROFILE_ID, provenance_fields.values
                )
            )
        contract_id = select_browser_contract(row)
        if contract_id is None:
            # Row satisfies no allowed contract; skip rather than store a row it
            # does not satisfy (logged as INVALID_INCOMING_KEEP by the helper if
            # it were passed through; here we drop it deterministically).
            continue
        row["evidence_contract_id"] = contract_id
        cache_values.append(row)

    # Stamp the cohort key before the write, which is the step that can raise.
    if timing_fields is not None:
        timing_fields["cache_row_count"] = len(cache_values)
        timing_fields["provenance_valid"] = provenance_valid
        timing_fields["provenance_absent"] = provenance_absent
        timing_fields["provenance_malformed"] = provenance_malformed
        # The fleet-adoption bit: ONE verdict per coalesced run, independent of how
        # long the game is (a 200-move legacy game and a 20-move v2 game each
        # contribute exactly one verdict). The g-bgv1-cutover criterion rolls these
        # up per DISTINCT session via ``session_v2_adoption`` — it must never read
        # the row-weighted counts above.
        timing_fields["session_provenance"] = session_provenance_verdict(
            valid=provenance_valid,
            absent=provenance_absent,
            malformed=provenance_malformed,
        )

    if not cache_values:
        # Nothing was submitted, so nothing was written. Stamped explicitly rather
        # than left absent so the field's ABSENCE means exactly one thing: the
        # writer raised and the written count is unknown.
        if timing_fields is not None:
            timing_fields["cache_rows_written"] = 0
        return 0

    # The shared helper owns its own transaction; ensure the caller session is
    # clean (no pending state on this connection) before delegating.
    db.commit()
    results = write_analysis_cache_rows(db, cache_values)
    # ``cache_row_count`` above is what we ASKED the writer to store; this is what
    # it actually wrote. The two diverged the moment browser-game-v1 retired
    # (g-bgv1-cutover): a legacy client's rows are refused with
    # INACTIVE_PROFILE_KEEP, so the upload succeeds having written nothing, and a
    # count of the batch would report writes that never happened. Stamped only on
    # the success path — a raising writer wrote an UNKNOWN number of rows, and
    # ``cache_row_count`` (stamped pre-write, g-dckw) remains the cohort key.
    if timing_fields is not None:
        timing_fields["cache_rows_written"] = sum(
            1 for _, reason in results if reason.value in _ROW_MUTATING_REASONS
        )
    # The RETURN stays the pre-writer filtered count (g-dckw's cohort key, which
    # must keep the same meaning across this change so latency buckets stay
    # comparable). The honest post-write number is the log field above.
    return len(cache_values)


def _should_run_session_move_evidence(game_session: GameSession) -> bool:
    # Ended, unconverted drills are hidden, unrated, and no longer recoverable
    # as normal games. Late client uploads may still arrive after a restart or
    # natural-end cleanup; keep raw rows idempotent but skip expensive evidence.
    return not (
        game_session.session_mode == DRILL_SESSION_MODE
        and (
            game_session.drill_state == "abandoned"
            or (
                game_session.drill_state != VISIBLE_DRILL_STATE
                and game_session.status == "ended"
            )
        )
    )


@contextmanager
def _timed_side_effect(
    stage: str, *, session_id: uuid.UUID, user_id: int, move_count: int, **extra
):
    """Bracket one synchronous ``upsert_session_moves`` side effect with timing.

    Emits a single ``stage`` log line with ``elapsed_ms`` plus ``session_id``,
    ``user_id`` and ``move_count`` so prod logs can attribute /moves latency to a
    specific side effect (see g-zuym). Logged in ``finally`` so a raising side
    effect still records how long it ran before failing.

    Any ``**extra`` fields render as ``key=value`` between ``move_count`` and
    ``elapsed_ms``. The context manager yields that mutable field dict so a body
    can stamp a value only known after it runs — e.g. the analysis-cache writer
    stamps ``cache_row_count`` once it has filtered the upload down to real rows.

    ``cache_row_count`` is the count SUBMITTED to the writer, not the count it
    stored, and it is the cohort key for the g-dckw latency scrape: it is stamped
    BEFORE the write so a raising writer still buckets by the batch size it was
    given rather than collapsing into the zero-row cohort. ``cache_rows_written``
    is the honest post-write count: present whenever the cache side effect
    completes successfully — including the zero-row path, which stamps ``0/0``
    without calling the writer at all — and ABSENT only when a non-empty writer
    call raises, because then the number of rows written is genuinely unknown. The
    two differ by the rows the writer refused — since g-bgv1-cutover retired
    browser-game-v1, a legacy client's whole batch is refused, so the pair is what
    distinguishes "wrote nothing" from "was asked for nothing".
    """
    fields = dict(extra)
    start = time.perf_counter()
    try:
        yield fields
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        extra_rendered = "".join(f" {k}={v}" for k, v in fields.items())
        logger.info(
            "upsert_session_moves side_effect=%s session_id=%s user_id=%s "
            "move_count=%d%s elapsed_ms=%.1f",
            stage,
            session_id,
            user_id,
            move_count,
            extra_rendered,
            elapsed_ms,
        )


def _run_graph_evidence_txn(
    db: Session,
    *,
    session_id: uuid.UUID,
    user_id: int,
    player_color: str,
    evidence_moves: list,
    move_count: int,
    dialect_name: str,
    run_opportunity: bool = True,
) -> None:
    """Acquire the per-user advisory lock, upsert the graph + opportunity events,
    and commit — the graph-dependent failure boundary (g-q0aw).

    ``run_opportunity`` gates ONLY the blunder-opportunity recompute (g-y90g):
    mid-game incremental uploads pass False to skip it (the current session's own
    mid-game opportunity events are never consumed during its own play), while the
    final, complete upload passes True so opportunity is computed exactly once at
    finalize. The advisory lock + ghost-graph upsert + commit run regardless; the
    opportunity recompute, when enabled, stays in THIS txn AFTER the graph upsert
    so its forward BFS sees this upload's fresh edges.

    This is the FIRST statement of a fresh autobegun txn (the prior
    ``session_moves_upsert`` stage committed). On Postgres it takes
    ``pg_advisory_xact_lock(user_id)`` so concurrent same-user uploads serialize
    deterministically instead of racing the (user_id, fen_hash) unique index, and
    sets txn-local ``lock_timeout``/``statement_timeout`` so a stuck queue or
    pathological query aborts (SQLSTATE 55P03 / 57014) rather than hanging. The
    advisory lock is xact-scoped and the SET LOCALs reset — both released by the
    ``evidence_commit`` below, which is exactly the contention window
    (graph_upsert + opportunity_events).

    Raises ``OperationalError`` on timeout; the caller owns rollback + retry/degrade.
    """
    with _timed_side_effect(
        "graph_lock",
        session_id=session_id,
        user_id=user_id,
        move_count=move_count,
    ):
        # First statement of this fresh graph txn: take the per-user advisory lock
        # (+ txn-local timeouts) shared with the blunder-recording paths. Released,
        # with the SET LOCALs, at the evidence_commit below. No-op off Postgres.
        acquire_graph_write_lock(db, user_id=user_id, dialect_name=dialect_name)
    with _timed_side_effect(
        "ghost_graph_upsert",
        session_id=session_id,
        user_id=user_id,
        move_count=move_count,
    ):
        _upsert_session_position_graph(db, user_id=user_id, moves=evidence_moves)
    if run_opportunity:
        with _timed_side_effect(
            "opportunity_events",
            session_id=session_id,
            user_id=user_id,
            move_count=move_count,
        ):
            _compute_blunder_opportunity_events(
                db,
                session_id=session_id,
                user_id=user_id,
                player_color=player_color,
            )
    # Single commit that finalizes durability. NB: the ghost-graph edges were
    # already flushed inside the ghost_graph_upsert stage (Phase 6 of
    # _upsert_session_position_graph), so duplicate-edge IntegrityErrors now
    # surface there, not here. For sqlite/postgres the opportunity-event
    # INSERT/UPDATEs and the bulk stale-event DELETE also already executed inside
    # the opportunity_events stage (they issue immediately; the DELETE uses
    # synchronize_session=False so it emits SQL there, not at commit). This stage
    # owns the COMMIT (durability) only. Timed separately so commit cost is not
    # misattributed to compute.
    with _timed_side_effect(
        "evidence_commit",
        session_id=session_id,
        user_id=user_id,
        move_count=move_count,
    ):
        db.commit()


def _run_session_move_evidence_side_effects(
    db: Session,
    *,
    session_id: uuid.UUID,
    user_id: int,
    player_color: str,
    evidence_moves: list,
    move_count: int,
    dialect_name: str,
    run_opportunity: bool = True,
    is_final: bool = False,
) -> None:
    """Run the evidence side effects after the session_moves upsert, each timed.

    The graph-dependent stages (advisory lock + ghost-graph upsert + opportunity
    events + commit) run inside :func:`_run_graph_evidence_txn`. On a recoverable
    Postgres timeout (SQLSTATE 55P03 / 57014) we rollback and RETRY ONCE: the
    SessionMove rows are already committed and ``_compute_blunder_opportunity_events``
    rebuilds events from ALL session moves, so the retry is a clean full recompute,
    not a partial patch. If the retry also times out we rollback and accept the gap
    with an explicit WARNING — opportunity events do NOT self-heal (SRS counters read
    the persisted rows with no lazy recompute), so the dropped accounting regenerates
    only on the next OPPORTUNITY-ENABLED upload (``run_opportunity=True``) or the
    offline ``scripts/recompute_srs_opportunities.py`` recompute. After g-y90g
    mid-game incremental uploads skip opportunity, and the final enabled upload may
    be the last one for the session, so regeneration is not guaranteed without the
    offline script.

    The analysis-cache write (own txn) and background-recompute enqueue (cheap,
    coalesced, opening-score self-heal) run REGARDLESS — they sit outside the
    graph-dependent failure boundary, and the session is clean after rollback.

    Shared by the SQLite/Postgres and generic-dialect paths so both emit the same
    per-stage timing and stay in sync. Non-postgres dialects skip the advisory lock
    + SET LOCALs and never raise the timeout SQLSTATEs, so the degrade path is a
    Postgres-only concern.
    """
    graph_txn_kwargs = dict(
        session_id=session_id,
        user_id=user_id,
        player_color=player_color,
        evidence_moves=evidence_moves,
        move_count=move_count,
        dialect_name=dialect_name,
        run_opportunity=run_opportunity,
    )
    try:
        _run_graph_evidence_txn(db, **graph_txn_kwargs)
    except OperationalError as err:
        if _graph_timeout_sqlstate(err) is None:
            # Connection failures / non-timeout OperationalErrors (and any other
            # error type) propagate unchanged — the narrow catch only owns timeouts.
            raise
        db.rollback()
        try:
            _run_graph_evidence_txn(db, **graph_txn_kwargs)
        except OperationalError as retry_err:
            if _graph_timeout_sqlstate(retry_err) is None:
                raise
            db.rollback()
            logger.warning(
                "upsert_session_moves graph evidence timed out twice; opportunity "
                "events skipped for session_id=%s user_id=%s; not self-healing — "
                "regenerates only on the next opportunity-enabled upload "
                "(run_opportunity=True, not guaranteed post g-y90g) or the offline "
                "recompute_srs_opportunities.py script",
                session_id,
                user_id,
            )
    # ``final``/``kind`` are g-dckw's LATENCY cohort and stay keyed on
    # ``run_opportunity`` — they separate the expensive opportunity-recomputing run
    # from the cheap incremental ones, which is exactly what latency cohorting wants.
    #
    # ``session_final`` is a DIFFERENT question — "is this session over?" — and the
    # browser-game-v2 adoption rollup (g-mk1d §2.4.1) needs that one, not this one.
    # run_opportunity cannot answer it: the revert upload also sends True, and a
    # client predating g-y90g sends True on every mid-game upload, so an abandoned
    # or reverted session would be scored as if complete. Only terminal_action's
    # presence marks the end-of-session final_full upload. Deliberately additive:
    # redefining ``final`` here would silently re-cohort the existing latency metric.
    with _timed_side_effect(
        "analysis_cache_write",
        session_id=session_id,
        user_id=user_id,
        move_count=move_count,
        cache_row_count=0,  # stamped with the real filtered count before the write
        final=run_opportunity,
        session_final=is_final,
        kind="final" if run_opportunity else "live",
        status="error",  # flipped to ok only once the write returns; a raising
        # writer leaves status=error so the latency scrape excludes the failure
    ) as cache_fields:
        _upsert_analysis_cache(db, evidence_moves, timing_fields=cache_fields)
        cache_fields["status"] = "ok"
    with _timed_side_effect(
        "recompute_enqueue",
        session_id=session_id,
        user_id=user_id,
        move_count=move_count,
    ):
        request_recompute(user_id, player_color)


def _validated_content_length(http_request: Request) -> int | None:
    """Parse the ``Content-Length`` header into a non-negative int, else None.

    CLIENT-DECLARED metadata for the receipt (named ``content_length_bytes`` so it
    is never conflated with the client event's ``payload_bytes`` — the actual
    serialized byte count — nor with a true received-byte count). Best-effort: a
    missing/malformed header records None rather than failing the upload.
    """
    raw = http_request.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return None
    return value if value >= 0 else None


def _emit_session_moves_uploaded(
    user_id: int,
    *,
    move_count: int,
    recompute_queued: bool,
    client_request_id: str | None,
    recompute_opportunity: bool,
    session_mode: str | None,
) -> None:
    """Emit ``session_moves_uploaded`` once an upload is durably committed.

    Shared by both the SQLite/Postgres and generic-dialect return paths so the
    event reflects a persisted upload, not merely a received request.

    This remains a CONVENIENCE/timing signal, NOT the measurement: ``capture()``
    is fire-and-forget (no-ops without the SDK, swallows delivery failures), so a
    missing event proves nothing. The exact final-upload commit classification
    reads the durable ``session_upload_receipt`` row keyed by ``client_request_id``.
    ``client_request_id`` is carried here so a delivered event can still be joined.
    """
    capture(
        str(user_id),
        "session_moves_uploaded",
        {
            "move_count": move_count,
            "recompute_queued": recompute_queued,
            "client_request_id": client_request_id,
            "recompute_opportunity": recompute_opportunity,
            "session_mode": session_mode,
        },
    )


@router.post(
    "/{session_id}/moves",
    response_model=SessionMovesResponse,
    response_model_exclude_none=True,
)
def upsert_session_moves(
    session_id: uuid.UUID,
    request: SessionMovesRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> SessionMovesResponse:
    game_session = for_no_key_update(
        db.query(GameSession).filter(GameSession.id == session_id)
    ).first()
    if game_session is None:
        raise HTTPException(status_code=404, detail="Game session not found")
    _ensure_session_owned_by_user(game_session, user)
    _validate_unique_move_keys(request.moves)

    # g-upload-observe. A client-sent terminal_action marks the end-of-session
    # final_full upload — the one bounded by the 4s client deadline and the sole
    # writer of a durable receipt. The middleware already validated + normalized
    # the client-generated correlation id and published both ids to request state.
    is_final_full = request.terminal_action is not None
    client_request_id = getattr(http_request.state, "client_request_id", None)
    server_request_id = getattr(http_request.state, "request_id", None)

    # Reject a final_full upload lacking a valid client id BEFORE any writes. A
    # header stripped by a proxy or malformed by a client would otherwise commit a
    # null-id receipt that the join reads as loss against the client's id. Surfacing
    # it as a clean 400 (error_kind='http', no commit) instead both keeps the
    # receipt's client_request_id column non-null and never manufactures false loss.
    if is_final_full and client_request_id is None:
        raise HTTPException(
            status_code=400,
            detail="final_full upload requires a valid X-Client-Request-ID",
        )

    def _add_upload_receipt() -> None:
        """Stage the durable final_full receipt into the CURRENT transaction.

        Called in each dialect path immediately before that path's pre-cursor-bump
        ``db.flush()``, so the receipt INSERT is flushed AHEAD of the
        ``evidence_seq`` cursor bump (the transaction's final blocking statement)
        and never lands after it. Written ONLY for final_full; a commit/insert
        failure creates no receipt because it shares the transaction.

        Also called on the empty-move-list path, which commits the receipt alone —
        the endpoint must never return 200 for a final_full upload without one.
        """
        if not is_final_full:
            return
        db.add(
            SessionUploadReceipt(
                session_id=session_id,
                user_id=user.user_id,
                # Re-wrap the middleware-normalized canonical string; guaranteed
                # non-null here by the reject-before-write guard above.
                client_request_id=uuid.UUID(client_request_id),
                server_request_id=server_request_id,
                recompute_opportunity=request.recompute_opportunity,
                session_mode=game_session.session_mode,
                terminal_action=request.terminal_action,
                content_length_bytes=_validated_content_length(http_request),
            )
        )

    if not request.moves:
        # An empty upload writes no moves, but a final_full one still MUST leave a
        # receipt: the join classifies "200 with no receipt" as a noncommit, so
        # short-circuiting here would manufacture false loss for any final upload
        # the client sends with an empty tail (the contract permits it even though
        # the current lifecycle does not). No moves, no recompute, no cursor bump —
        # the receipt INSERT is the only statement in this transaction.
        if is_final_full:
            _add_upload_receipt()
            db.commit()
        return SessionMovesResponse(moves_inserted=0)

    values = [
        {
            "session_id": session_id,
            "move_number": move.move_number,
            "color": move.color.value,
            "move_san": move.move_san,
            "fen_after": move.fen_after,
            "eval_cp": move.eval_cp,
            "eval_mate": move.eval_mate,
            "best_move_san": move.best_move_san,
            "best_move_eval_cp": move.best_move_eval_cp,
            "eval_delta": centipawn_loss(move.eval_delta),
            "classification": move.classification.value if move.classification else None,
            "fen_before": move.fen_before,
            "best_move_uci": move.best_move_uci,
            "best_line_uci": encode_uci_line(move.best_line_uci),
            "decision_source": move.decision_source.value if move.decision_source else None,
            "target_blunder_id": move.target_blunder_id,
            "segment": segment_for_move(game_session, move.move_number, move.color.value),
            "browser_provenance": _encoded_browser_provenance(move),
        }
        for move in request.moves
    ]
    # Amended drill policy (2026-06-01): pre-continue drill-prefix moves feed the
    # same regular evidence side effects as normal moves, so every uploaded move
    # drives blunder opportunity, analysis-cache, and opening-score refresh.
    evidence_moves = (
        list(request.moves)
        if _should_run_session_move_evidence(game_session)
        else []
    )
    # Opening-evidence counter gate (g-jact): a session_moves write is digest-
    # visible ONLY for an evidence-eligible session (ended, or accuracy-failed
    # drill — NOT the _should_run_session_move_evidence gate above, which permits
    # live sessions). A live upload does not bump (the digest excludes active
    # sessions; the eligibility transition folds all its moves in with one bump);
    # a post-end eval-backfill upload does. One bump per eligible upload — the
    # ON-CONFLICT path can't distinguish insert from update, and over-bumping one
    # eligible upload is a harmless rebuild, never a false accept.
    bump_for_evidence = session_is_evidence_eligible(game_session)
    dialect_name = db.bind.dialect.name if db.bind else ""
    if dialect_name == "sqlite":
        statement = sqlite_insert(SessionMove).values(values)
    elif dialect_name == "postgresql":
        statement = postgresql_insert(SessionMove).values(values)
    else:
        with _timed_side_effect(
            "session_moves_upsert",
            session_id=session_id,
            user_id=user.user_id,
            move_count=len(values),
        ):
            for value in values:
                existing_row = db.query(SessionMove).filter(
                    SessionMove.session_id == value["session_id"],
                    SessionMove.move_number == value["move_number"],
                    SessionMove.color == value["color"],
                ).first()
                if existing_row:
                    existing_row.move_san = value["move_san"]
                    existing_row.fen_after = value["fen_after"]
                    existing_row.eval_cp = value["eval_cp"]
                    existing_row.eval_mate = value["eval_mate"]
                    existing_row.best_move_san = value["best_move_san"]
                    existing_row.best_move_eval_cp = value["best_move_eval_cp"]
                    existing_row.eval_delta = value["eval_delta"]
                    existing_row.classification = value["classification"]
                    existing_row.fen_before = value["fen_before"]
                    existing_row.best_move_uci = value["best_move_uci"]
                    existing_row.best_line_uci = value["best_line_uci"]
                    existing_row.decision_source = value["decision_source"]
                    existing_row.target_blunder_id = value["target_blunder_id"]
                    existing_row.segment = value["segment"]
                    existing_row.browser_provenance = value["browser_provenance"]
                else:
                    db.add(SessionMove(**value))

            # Flush the move add/mutates so recompute's scoped SELECT sees them
            # (backend sessions disable autoflush, so this flush is load-bearing).
            db.flush()
            recompute_session_accuracy(db, game_session)
            # Stage the final_full receipt (no-op otherwise) so the drain flush
            # below emits its INSERT ahead of the cursor bump (g-upload-observe).
            _add_upload_receipt()
            # Drain the dirty accuracy assignment before the cursor bump so the
            # cache write lands ahead of the transaction's final statement.
            db.flush()
            if bump_for_evidence:
                bump_evidence_seq(db, user.user_id, game_session.player_color)
            db.commit()
        if evidence_moves:
            # Deferred off the request path: the expensive graph/opportunity/
            # analysis-cache/recompute pipeline runs on the evidence scheduler's
            # worker thread (best-effort; an enqueue failure never regresses
            # /moves to 500). See app/session_evidence_scheduler.py.
            enqueue_session_evidence(
                db,
                session_id=session_id,
                user_id=user.user_id,
                player_color=game_session.player_color,
                evidence_moves=evidence_moves,
                move_count=len(values),
                recompute_opportunity=request.recompute_opportunity,
                is_final=is_final_full,
            )
        # Emitted only after the upload is durable (post-commit) so a failed
        # insert/commit never produces a successful-looking analytics event.
        with _timed_side_effect(
            "analytics",
            session_id=session_id,
            user_id=user.user_id,
            move_count=len(values),
        ):
            _emit_session_moves_uploaded(
                user.user_id,
                move_count=len(values),
                recompute_queued=bool(evidence_moves),
                client_request_id=client_request_id,
                recompute_opportunity=request.recompute_opportunity,
                session_mode=game_session.session_mode,
            )
        return SessionMovesResponse(
            moves_inserted=len(values),
            drill_state=game_session.drill_state,
            drill_terminal_reason=game_session.drill_terminal_reason,
        )

    statement = statement.on_conflict_do_update(
        index_elements=[
            SessionMove.session_id,
            SessionMove.move_number,
            SessionMove.color,
        ],
        set_={
            "move_san": statement.excluded.move_san,
            "fen_after": statement.excluded.fen_after,
            "eval_cp": statement.excluded.eval_cp,
            "eval_mate": statement.excluded.eval_mate,
            "best_move_san": statement.excluded.best_move_san,
            "best_move_eval_cp": statement.excluded.best_move_eval_cp,
            "eval_delta": statement.excluded.eval_delta,
            "classification": statement.excluded.classification,
            "fen_before": statement.excluded.fen_before,
            "best_move_uci": statement.excluded.best_move_uci,
            "best_line_uci": statement.excluded.best_line_uci,
            "decision_source": statement.excluded.decision_source,
            "target_blunder_id": statement.excluded.target_blunder_id,
            "segment": statement.excluded.segment,
            "browser_provenance": statement.excluded.browser_provenance,
        },
    )
    with _timed_side_effect(
        "session_moves_upsert",
        session_id=session_id,
        user_id=user.user_id,
        move_count=len(values),
    ):
        # The core INSERT ... ON CONFLICT is emitted to the transaction here, so
        # recompute's scoped SELECT already sees the upserted moves without a
        # prior flush. recompute then dirties the accuracy columns; the flush
        # below drains them ahead of the conditional cursor bump.
        db.execute(statement)
        recompute_session_accuracy(db, game_session)
        # Stage the final_full receipt (no-op otherwise) BEFORE this flush so its
        # INSERT is emitted ahead of the cursor bump (g-upload-observe).
        _add_upload_receipt()
        db.flush()
        if bump_for_evidence:
            bump_evidence_seq(db, user.user_id, game_session.player_color)
        db.commit()

    if evidence_moves:
        # Deferred off the request path: the expensive graph/opportunity/
        # analysis-cache/recompute pipeline runs on the evidence scheduler's
        # worker thread (best-effort; an enqueue failure never regresses /moves
        # to 500). See app/session_evidence_scheduler.py.
        enqueue_session_evidence(
            db,
            session_id=session_id,
            user_id=user.user_id,
            player_color=game_session.player_color,
            evidence_moves=evidence_moves,
            move_count=len(values),
            recompute_opportunity=request.recompute_opportunity,
            is_final=is_final_full,
        )

    # Emitted only after the upload is durable (post-commit) so a failed
    # insert/commit never produces a successful-looking analytics event.
    with _timed_side_effect(
        "analytics",
        session_id=session_id,
        user_id=user.user_id,
        move_count=len(values),
    ):
        _emit_session_moves_uploaded(
            user.user_id,
            move_count=len(values),
            recompute_queued=bool(evidence_moves),
            client_request_id=client_request_id,
            recompute_opportunity=request.recompute_opportunity,
            session_mode=game_session.session_mode,
        )
    return SessionMovesResponse(
        moves_inserted=len(values),
        drill_state=game_session.drill_state,
        drill_terminal_reason=game_session.drill_terminal_reason,
    )


@router.get("/{session_id}/analysis", response_model=SessionAnalysisResponse)
def get_session_analysis(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> SessionAnalysisResponse:
    game_session = _get_session_or_404(db, session_id)
    _ensure_session_owned_by_user(game_session, user)
    if not is_visible_game_session(game_session):
        raise HTTPException(status_code=404, detail="Game session not found")

    color_order = case((SessionMove.color == MoveColor.WHITE.value, 0), else_=1)
    session_moves = (
        db.query(SessionMove)
        .filter(SessionMove.session_id == session_id)
        .order_by(SessionMove.move_number.asc(), color_order.asc())
        .all()
    )
    # Amended drill policy (2026-06-01): summary spans the full line, including
    # pre-continue drill-prefix moves.
    summary_filter = [SessionMove.session_id == session_id]

    player_loss_expr = case(
        (
            SessionMove.color == game_session.player_color,
            centipawn_loss_expr(SessionMove.eval_delta),
        ),
        else_=None,
    )
    player_move_expr = SessionMove.color == game_session.player_color
    summary_row = (
        db.query(
            func.sum(
                case(
                    (
                        player_move_expr
                        & (SessionMove.classification == MoveClassification.BLUNDER.value),
                        1,
                    ),
                    else_=0,
                )
            ).label("blunders"),
            func.sum(
                case(
                    (
                        player_move_expr
                        & (SessionMove.classification == MoveClassification.MISTAKE.value),
                        1,
                    ),
                    else_=0,
                )
            ).label("mistakes"),
            func.sum(
                case(
                    (
                        player_move_expr
                        & (SessionMove.classification == MoveClassification.INACCURACY.value),
                        1,
                    ),
                    else_=0,
                )
            ).label("inaccuracies"),
            func.avg(player_loss_expr).label("average_centipawn_loss"),
        )
        .filter(*summary_filter)
        .one()
    )

    average_centipawn_loss = (
        round_half_up_cpl(summary_row.average_centipawn_loss)
        if summary_row.average_centipawn_loss is not None
        else None
    )

    # Position-analysis export bridges two grains. Storage rows are keyed by
    # normalized FEN, but the wire response keys by the ORIGINAL full
    # ``move.fen_before`` (one entry per played position). For each distinct
    # fen_before we resolve trusted position evidence (storage winner, else trusted
    # legacy v2 projection) by its normalized FEN; only when none is trusted do we
    # fall back to the (untrusted) SessionMove seed. The resolver lookups are batched
    # over the distinct normalized FENs to avoid an N+1.
    norm_by_fen: dict[str, str | None] = {}
    for move in session_moves:
        if move.fen_before and move.fen_before not in norm_by_fen:
            try:
                norm_by_fen[move.fen_before] = normalize_fen(move.fen_before)
            except Exception:
                norm_by_fen[move.fen_before] = None
    resolved = resolve_trusted_positions(db, [n for n in norm_by_fen.values() if n])

    position_analysis: dict[str, PositionAnalysis] = {}
    for move in session_moves:
        if not move.fen_before or move.fen_before in position_analysis:
            continue
        norm = norm_by_fen.get(move.fen_before)
        tp = resolved.get(norm) if norm else None
        if tp is not None:
            # ``tp.best_eval`` is white-relative; the wire field is
            # side-to-move-relative, so sign-convert by the active color.
            sign = 1 if active_color(move.fen_before) == "white" else -1
            best_move_eval_cp = tp.best_eval * sign if tp.best_eval is not None else None
            best_move_eval_mate = (
                tp.best_eval_mate * sign if tp.best_eval_mate is not None else None
            )
            position_analysis[move.fen_before] = PositionAnalysis(
                best_move_uci=tp.best_move_uci,
                best_move_san=tp.best_move_san,
                best_move_eval_cp=best_move_eval_cp,
                best_move_eval_mate=best_move_eval_mate,
                best_line_uci=tp.best_line_uci,
                position_trusted=True,
            )
        elif move.best_move_uci:
            # Untrusted legacy seed: SessionMove eval is already side-to-move-relative.
            position_analysis[move.fen_before] = PositionAnalysis(
                best_move_uci=move.best_move_uci,
                best_move_san=move.best_move_san,
                best_move_eval_cp=move.best_move_eval_cp,
                best_line_uci=decode_uci_line(move.best_line_uci),
                position_trusted=False,
            )
        # else: no usable best move at any trust level -> no entry.

    # Completion metadata: derive expected_total_moves from stored PGN
    expected_total_moves = expected_total_moves_from_pgn(game_session.pgn)

    analyzed_moves = len(session_moves)
    is_complete = (
        expected_total_moves is not None
        and analyzed_moves >= expected_total_moves
    )

    accuracy = game_accuracy_for_rows(
        session_moves,
        player_color=game_session.player_color,
        expected_total_moves=expected_total_moves,
        session_id=session_id,
    )

    # Read-time re-annotation overlay (g-xox0 Part C): join the PERSISTED (base)
    # session moves against analysis_cache by exact (fen_before, move_uci) and attach a
    # MoveUpgrade for any display-upgrade-eligible stored row (browser-analysis d21
    # or canonical). Exact-key only: byte-identical fen_before, so it covers this
    # session on refetch and other sessions whose stored fen_before is identical, but
    # NOT chess transpositions reached via different move orders / clocks. `move_uci`
    # is derived once per move here and reused when building the wire moves. The base
    # classification/eval_* fields are not rewritten by the overlay, so the summary
    # aggregates above read ORIGINAL game-time evidence. (Base, not immutable: a
    # post-end POST /moves upload can still add, change, or clear evaluations.)
    derived_moves = [
        (move, _derive_move_uci(move.fen_before, move.move_san))
        for move in session_moves
    ]
    overlay_keys = [
        (move.fen_before, uci)
        for move, uci in derived_moves
        if move.fen_before and uci
    ]
    # LIVE comparison operand per key (g-mk1d §5.2). A stored REQUIRES_COMPARISON
    # row (browser-game-v2) may only re-label a move when it is provably STRONGER
    # than what THIS session itself searched, and its own per-move provenance is
    # the only operand available at GET time for a saved game. Legacy/NULL/tampered
    # provenance yields None -> no overlay, and the player keeps their own label.
    # (Two session moves can share one key only via an exact position repetition
    # within the same session; their provenance is identical because the device
    # depth is fixed for the whole page session, so last-wins is immaterial.)
    live_by_key: dict[tuple[str, str], object] = {}
    for move, uci in derived_moves:
        if move.fen_before and uci:
            live_by_key[(move.fen_before, uci)] = browser_live_descriptor(
                move.browser_provenance
            )
    upgrade_by_key: dict[tuple[str, str], MoveUpgrade] = {}
    if overlay_keys:
        stored_rows = (
            db.query(AnalysisCache)
            .filter(
                tuple_(AnalysisCache.fen_before, AnalysisCache.move_uci).in_(overlay_keys)
            )
            .all()
        )
        for row in stored_rows:
            key = (row.fen_before, row.move_uci)
            upgrade = move_upgrade_for_row(row, live=live_by_key.get(key))
            if upgrade is not None:
                upgrade_by_key[key] = upgrade

    return SessionAnalysisResponse(
        session_id=game_session.id,
        pgn=game_session.pgn,
        result=game_session.result,
        player_color=game_session.player_color,
        moves=[
            SessionAnalysisMove(
                move_number=move.move_number,
                color=move.color,
                move_san=move.move_san,
                fen_after=move.fen_after,
                fen_before=move.fen_before,
                move_uci=uci,
                eval_cp=move.eval_cp,
                eval_mate=move.eval_mate,
                best_move_san=move.best_move_san,
                best_move_eval_cp=move.best_move_eval_cp,
                eval_delta=centipawn_loss(move.eval_delta),
                classification=move.classification,
                segment=move.segment,
                upgraded=(
                    upgrade_by_key.get((move.fen_before, uci))
                    if move.fen_before and uci
                    else None
                ),
            )
            for move, uci in derived_moves
        ],
        summary=SessionAnalysisSummary(
            blunders=int(summary_row.blunders or 0),
            mistakes=int(summary_row.mistakes or 0),
            inaccuracies=int(summary_row.inaccuracies or 0),
            average_centipawn_loss=average_centipawn_loss,
            accuracy=accuracy,
        ),
        position_analysis=position_analysis,
        expected_total_moves=expected_total_moves,
        analyzed_moves=analyzed_moves,
        is_complete=is_complete,
        rated_start_ply=game_session.rated_start_ply if game_session.session_mode == DRILL_SESSION_MODE else None,
    )


# Per-row rejection reasons the endpoint emits BEFORE the shared writer (the
# writer only returns Reasons for rows it actually receives). Kept as module
# constants so tests assert against one definition.
EVIDENCE_SESSION_NOT_ELIGIBLE = "session_not_evidence_eligible"
EVIDENCE_NOT_IN_SESSION = "not_in_session"
EVIDENCE_DUPLICATE_KEY = "duplicate_request_key"
EVIDENCE_INVALID_LEGALITY = "invalid_legality"
EVIDENCE_CONTRACT_UNSATISFIED = "contract_unsatisfied"
# The submitted classification does not follow from the best/played root-relative
# scores (g-reuse-d21-search §6.1). Every row's label is independently rederived
# with the root-alternative classifier and any disagreement is rejected pre-writer.
EVIDENCE_CLASSIFICATION_MISMATCH = "classification_mismatch"
# Producer discriminator rejections (g-reuse-d21-search §6.3), both pre-writer.
# A stale client running the retired hidden-worker producer sends no producer field.
EVIDENCE_STALE_PRODUCER = "stale_producer"
EVIDENCE_UNKNOWN_PRODUCER = "unknown_producer"
# Post-writer anomaly, not a success: the shared writer returned but omitted a
# survivor key, violating its one-Reason-per-row contract. Surfaced as a distinct
# reason so a writer regression shows up instead of being masked as new_key.
EVIDENCE_WRITER_NO_RESULT = "writer_no_result"

# The single allowed producer token and its server-side profile mapping. The
# client selects no profile id; the endpoint maps a recognized producer to the
# stamped profile. Any other (or absent) producer is rejected per-row.
_EVIDENCE_PRODUCER_PROFILE = {
    "visible-multipv-v1": BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
}

# Writer verdicts under which the STORED row is now THIS browser-analysis evidence,
# so the endpoint emits a MoveUpgrade for the open MoveList (g-xox0 Part B). Every
# other verdict — the `*_keep` families, `duplicate_conflict`, `recovery_aborted_keep`,
# and the endpoint pre-writer reasons — leaves the stored row unchanged / unwritten,
# so it yields no upgrade. `legacy_replaced_by_auth` cannot occur for a non-
# authoritative profile and is therefore not accepted here.
#
# NOT the same question as ``_ROW_MUTATING_REASONS`` (which counts DB writes), and
# the difference is deliberate: `same_profile_idempotent` belongs HERE, because the
# stored row already IS this evidence and the endpoint can return it, but it is a
# ``Decision.KEEP`` that writes nothing.
_EVIDENCE_ACCEPTED_REASONS = frozenset(
    {
        Reason.NEW_KEY.value,
        Reason.DOMINATES_REPLACE.value,
        # The corrective replacement of a defective retired browser-analysis-v1 row
        # by the visible-MultiPV successor is an accepted write (g-reuse-d21-search).
        Reason.PROTOCOL_CORRECTED_REPLACE.value,
        # A MEASURED replacement (D4 steps 4-5) is a replacement like any other: the
        # stored row IS now this evidence, so it must emit its MoveUpgrade. Today's
        # producer cannot generate it — this endpoint stamps the FIXED multipv-v2
        # profile, which Rule 2a skips and which meets every other profile across an
        # explicit edge or the authority barrier before the measured steps run — but
        # omitting it would fail SILENTLY the moment that changes: the write would
        # succeed and the open MoveList would simply never be told (g-mk1d review).
        Reason.STRENGTH_REPLACE.value,
        Reason.SAME_PROFILE_SUPERSET_MERGE.value,
        Reason.SAME_PROFILE_CONTRACT_UPGRADE.value,
        Reason.SAME_PROFILE_IDEMPOTENT.value,
    }
)


@dataclass
class _PreparedEvidenceRow:
    """One request row after endpoint preparation: either a pre-writer rejection
    ``reason`` OR a ``cache_row`` dict ready for the shared writer (never both)."""

    fen: str
    move_uci: str
    reason: str | None
    cache_row: dict | None


def _parse_legal_uci(board: chess.Board, uci: str) -> chess.Move | None:
    """Return the move iff ``uci`` is a legal move in ``board``; else ``None``."""
    try:
        move = chess.Move.from_uci(uci)
    except (ValueError, chess.InvalidMoveError):
        return None
    return move if move in board.legal_moves else None


def _is_legal_line(fen: str, line: list[str]) -> bool:
    """True when ``line`` is a non-empty legal UCI sequence played from ``fen``.

    Stricter than the browser-game upload path, which does not validate full-PV
    legality. A malformed FEN or any illegal ply fails closed.
    """
    try:
        board = chess.Board(fen)
    except (ValueError, chess.InvalidMoveError):
        return False
    if not line:
        return False
    for uci in line:
        move = _parse_legal_uci(board, uci)
        if move is None:
            return False
        board.push(move)
    return True


def _session_membership_keys(session_moves: list[SessionMove]) -> set[tuple[str, str]]:
    """Exact ``(fen_before, played_uci)`` keys for a session's mainline moves.

    The FEN half is the same byte string browser-game rows used; the UCI half is
    the server-side SAN->UCI re-derivation (``SessionMove`` has no stored UCI). Legacy
    moves with a null/unparseable ``fen_before`` or SAN are omitted (not eligible).
    """
    keys: set[tuple[str, str]] = set()
    for sm in session_moves:
        uci = _derive_move_uci(sm.fen_before, sm.move_san)
        if uci is not None:
            keys.add((sm.fen_before, uci))
    return keys


def _white_relative_score(cp: int, mate: int | None, mover: str) -> EngineScore:
    """Convert a white-relative wire eval to a ROOT mover-relative EngineScore.

    Evidence rows store white-relative evals (see ``AnalysisEvidenceRow``); the
    root-alternative classifier needs mover-relative (root side-to-move) scores. A
    present mate count uses the raw ``mate`` (a mate-to-CP ``eval`` is only for the
    delta arithmetic), else the finite CP.
    """
    if mate is not None:
        value = mate if mover == "white" else -mate
        return EngineScore("mate", value)
    value = cp if mover == "white" else -cp
    return EngineScore("cp", value)


def _build_evidence_cache_row(
    row: AnalysisEvidenceRow, profile_id: str
) -> tuple[dict | None, str | None]:
    """Validate legality + contract + classification for one primary row and build
    the writer dict.

    Returns ``(cache_row, None)`` on success or ``(None, reason)`` on rejection. SAN
    is derived server-side from the validated UCI and is never trusted from the
    client; the row is stamped with the endpoint-selected ``profile_id`` identity,
    ``source="analysis"``, and the ``resolver-complete-v2`` contract, which is then
    revalidated. Beyond the contract's enum/arithmetic checks, the classification is
    independently REDERIVED from the best/played root-relative scores with the
    root-alternative classifier and any disagreement is rejected (g-reuse-d21-search
    §6.1) — lower lines and mate transitions are as client-supplied as line 1.
    """
    try:
        board = chess.Board(row.fen)
    except (ValueError, chess.InvalidMoveError):
        return None, EVIDENCE_INVALID_LEGALITY

    move = _parse_legal_uci(board, row.move_uci)
    if move is None:
        return None, EVIDENCE_INVALID_LEGALITY
    move_san = board.san(move)

    best_move = _parse_legal_uci(board, row.best_move_uci)
    if best_move is None:
        return None, EVIDENCE_INVALID_LEGALITY
    best_move_san = board.san(best_move)

    if not _is_legal_line(row.fen, row.best_line_uci):
        return None, EVIDENCE_INVALID_LEGALITY

    cache_row = {
        "fen_before": row.fen,
        "move_uci": row.move_uci,
        "move_san": move_san,
        "best_move_uci": row.best_move_uci,
        "best_move_san": best_move_san,
        "best_line_uci": encode_uci_line(row.best_line_uci),
        "played_eval": row.played_eval,
        "played_eval_mate": row.played_eval_mate,
        "best_eval": row.best_eval,
        "best_eval_mate": row.best_eval_mate,
        "eval_delta": row.eval_delta,
        "classification": row.classification,
        "source": "analysis",
        "analysis_profile_id": profile_id,
        "evidence_contract_id": RESOLVER_COMPLETE_V2,
        **stamp_profile_full(profile_id),
    }

    if not contract_satisfied(RESOLVER_COMPLETE_V2, cache_row):
        return None, EVIDENCE_CONTRACT_UNSATISFIED

    # Independent classification rederivation (§6.1). The contract guarantees
    # played_eval / best_eval are finite ints and classification is a valid enum;
    # now enforce that the LABEL follows from the scores.
    is_best = row.move_uci == row.best_move_uci
    # ``best`` requires the played move to BE the best move with equal scores and
    # zero delta — a client cannot label a zero-loss best while carrying a drop.
    if row.classification == "best" and not (
        is_best
        and row.played_eval == row.best_eval
        and row.played_eval_mate == row.best_eval_mate
        and (row.eval_delta or 0) == 0
    ):
        return None, EVIDENCE_CLASSIFICATION_MISMATCH

    mover = "white" if board.turn == chess.WHITE else "black"
    best_score = _white_relative_score(row.best_eval, row.best_eval_mate, mover)
    played_score = _white_relative_score(row.played_eval, row.played_eval_mate, mover)
    expected = classify_root_alternative(best_score, played_score, mover, is_best)
    if expected != row.classification:
        return None, EVIDENCE_CLASSIFICATION_MISMATCH

    return cache_row, None


def _prepare_analysis_evidence_rows(
    rows: list[AnalysisEvidenceRow],
    membership: set[tuple[str, str]],
    producer: str | None,
) -> list[_PreparedEvidenceRow]:
    """Membership → dedupe → producer → legality → contract → classification, one
    result per request row in order. The first occurrence of each
    ``(fen, move_uci)`` that passed membership is primary; later occurrences get
    ``duplicate_request_key`` and never reach the writer, so the writer receives
    only unique keys.

    The producer discriminator (§6.3) gates every primary row before the cache-row
    build: an absent producer is a stale client (``stale_producer``), an
    unrecognized one is ``unknown_producer``, and the single allowed
    ``visible-multipv-v1`` maps to the browser-analysis-multipv-v2 profile."""
    if producer is None:
        producer_reason: str | None = EVIDENCE_STALE_PRODUCER
        profile_id: str | None = None
    elif producer not in _EVIDENCE_PRODUCER_PROFILE:
        producer_reason = EVIDENCE_UNKNOWN_PRODUCER
        profile_id = None
    else:
        producer_reason = None
        profile_id = _EVIDENCE_PRODUCER_PROFILE[producer]

    prepared: list[_PreparedEvidenceRow] = []
    seen_keys: set[tuple[str, str]] = set()
    for row in rows:
        key = (row.fen, row.move_uci)
        if key not in membership:
            prepared.append(
                _PreparedEvidenceRow(row.fen, row.move_uci, EVIDENCE_NOT_IN_SESSION, None)
            )
            continue
        if key in seen_keys:
            prepared.append(
                _PreparedEvidenceRow(row.fen, row.move_uci, EVIDENCE_DUPLICATE_KEY, None)
            )
            continue
        seen_keys.add(key)
        if producer_reason is not None:
            prepared.append(
                _PreparedEvidenceRow(row.fen, row.move_uci, producer_reason, None)
            )
            continue
        assert profile_id is not None
        cache_row, reason = _build_evidence_cache_row(row, profile_id)
        prepared.append(_PreparedEvidenceRow(row.fen, row.move_uci, reason, cache_row))
    return prepared


@router.post(
    "/{session_id}/analysis-evidence",
    response_model=AnalysisEvidenceResponse,
)
def submit_analysis_evidence(
    session_id: uuid.UUID,
    request: AnalysisEvidenceRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> AnalysisEvidenceResponse:
    """Persist approved visible-MultiPV analysis-board evidence through the shared
    cache writer (g-reuse-d21-search).

    Owner-only and scoped to exact mainline moves of the session. The endpoint-
    controlled producer discriminator maps ``producer="visible-multipv-v1"`` to the
    non-authoritative but replacement-eligible ``browser-analysis-multipv-v2``
    profile; a stale client sending no/unknown producer fails closed per-row. Each
    row's classification is independently rederived from its root-relative scores.
    Complete evidence can correctively replace a defective retired
    ``browser-analysis-v1`` row and a weaker ``browser-game-v1`` row for the same
    exact ``(fen_before, move_uci)``, but never becomes a trusted /lookup hit,
    reclaims legacy rows, or overwrites canonical depth-24 evidence.
    """
    game_session = _get_session_or_404(db, session_id)
    _ensure_session_owned_by_user(game_session, user)

    rows = request.rows

    # Session-eligibility backstop: hidden/abandoned drills never write evidence.
    # Reuses the exact predicate that gates browser-game evidence in
    # upsert_session_moves — normal sessions and visible/converted drills pass.
    if not _should_run_session_move_evidence(game_session):
        return AnalysisEvidenceResponse(
            results=[
                AnalysisEvidenceResult(
                    fen=r.fen,
                    move_uci=r.move_uci,
                    reason=EVIDENCE_SESSION_NOT_ELIGIBLE,
                )
                for r in rows
            ]
        )

    session_moves = (
        db.query(SessionMove).filter(SessionMove.session_id == session_id).all()
    )
    membership = _session_membership_keys(session_moves)

    prepared = _prepare_analysis_evidence_rows(rows, membership, request.producer)

    survivors = [p.cache_row for p in prepared if p.cache_row is not None]
    writer_reasons: dict[tuple[str, str], str] = {}
    if survivors:
        # Commit immediately before the writer so it receives a clean session with
        # no open transaction (the membership query above opened one).
        db.commit()
        for (fen, uci), reason in write_analysis_cache_rows(db, survivors):
            writer_reasons[(fen, uci)] = reason.value

    # Immediate MoveList patch (g-xox0 Part B): for every ACCEPTED key, read the
    # STORED (post-merge) row back and emit its MoveUpgrade — NOT the submitted row,
    # which a same-profile merge may differ from (`_build_merged` keeps existing
    # non-null fields). Gate through the same `display_upgrade_eligible` seam Part C
    # uses so B and C never diverge; the client never re-runs the strength comparator.
    accepted_keys = [
        (p.fen, p.move_uci)
        for p in prepared
        if p.cache_row is not None
        and writer_reasons.get((p.fen, p.move_uci)) in _EVIDENCE_ACCEPTED_REASONS
    ]
    upgrade_by_key: dict[tuple[str, str], MoveUpgrade] = {}
    if accepted_keys:
        stored_rows = (
            db.query(AnalysisCache)
            .filter(
                tuple_(AnalysisCache.fen_before, AnalysisCache.move_uci).in_(accepted_keys)
            )
            .all()
        )
        for row in stored_rows:
            upgrade = move_upgrade_for_row(row)
            if upgrade is not None:
                upgrade_by_key[(row.fen_before, row.move_uci)] = upgrade

    results = [
        AnalysisEvidenceResult(
            fen=p.fen,
            move_uci=p.move_uci,
            reason=(
                p.reason
                if p.reason is not None
                else writer_reasons.get((p.fen, p.move_uci), EVIDENCE_WRITER_NO_RESULT)
            ),
            # Attach the upgrade ONLY to the row that actually reached the writer and
            # was accepted — not to a later duplicate_request_key row that merely
            # shares the accepted primary's (fen, move_uci) key (its cache_row is
            # None). This preserves the contract that every pre-writer rejection
            # (duplicate_request_key, not_in_session, ...) returns upgrade = None.
            upgrade=(
                upgrade_by_key.get((p.fen, p.move_uci))
                if p.cache_row is not None
                and writer_reasons.get((p.fen, p.move_uci)) in _EVIDENCE_ACCEPTED_REASONS
                else None
            ),
        )
        for p in prepared
    ]
    return AnalysisEvidenceResponse(results=results)


@router.get("/{session_id}/openings", response_model=SessionOpeningsResponse)
def get_session_openings(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> SessionOpeningsResponse:
    game_session = _get_session_or_404(db, session_id)
    _ensure_session_owned_by_user(game_session, user)
    # Serve the owner's own session — including an in-progress (not-yet-converted)
    # drill — so the live opening lineage shows during drill play (g-8nke). The
    # visibility gate only hides drills from history lists; access is already
    # enforced by ownership above. The guard stays defensive against any future
    # mode that is neither normal nor a drill.
    if (
        not is_visible_game_session(game_session)
        and game_session.session_mode != DRILL_SESSION_MODE
    ):
        raise HTTPException(status_code=404, detail="Game session not found")

    player_color = game_session.player_color

    color_order = case((SessionMove.color == MoveColor.WHITE.value, 0), else_=1)
    session_moves = (
        db.query(SessionMove)
        .filter(SessionMove.session_id == session_id)
        .order_by(SessionMove.move_number.asc(), color_order.asc())
        .all()
    )

    roots_registry = get_opening_roots()

    # Walk played positions in move order; whenever a position is a boundary
    # opening root, append it to the chain (dedup consecutive) together with the
    # move index that crossed it. The order roots are crossed in is broadest ->
    # deepest along this game's DAG path.
    chain = played_opening_chain_indexed(
        [move.fen_after for move in session_moves], roots_registry
    )

    # Direct-row lineage scores (matching the /openings card). NON-BLOCKING
    # reader: warm reads serve the cached batch and schedule a background
    # recompute; a cold cache returns immediately with no batch rather than
    # blocking on the initial compute, so the lineage renders during live play
    # and the client re-polls for scores (g-a5v3).
    #
    # Resolved BEFORE the empty-chain return, and independently of it. The client
    # derives its own lineage from local move history, so it can be showing a
    # card while this (upload-lagged) chain is still empty. Returning a bare
    # "ready" here would leave that card stuck on "—": no recompute enqueued and
    # no pending status to start reconciliation from.
    _, cached_rows, scores_pending = load_cached_rows_nonblocking(
        db, user.user_id, player_color
    )
    score_status: Literal["ready", "pending"] = "pending" if scores_pending else "ready"

    if not chain:
        return SessionOpeningsResponse(
            player_color=player_color,
            lineage=[],
            start_ply=1,
            score_status=score_status,
        )

    # Ply of the game's first stored move, computed authoritatively from
    # move_number/color (not assumed to be 1) so a drill whose stored moves start
    # mid-game still numbers correctly. All lineage prefixes share moves[0], so
    # this ply is constant across items.
    first_move = session_moves[0]
    start_ply = (first_move.move_number - 1) * 2 + (
        1 if first_move.color == MoveColor.WHITE.value else 2
    )

    rows_by_key = {row.opening_key: row for row in cached_rows}  # already snapshotted

    lineage: list[OpeningLineageItem] = []
    for index, (root, move_index) in enumerate(chain):
        direct = rows_by_key.get(root.opening_key)
        scored = direct is not None
        lineage.append(
            OpeningLineageItem(
                opening_key=root.opening_key,
                opening_name=root.opening_name,
                opening_family=root.opening_family,
                eco=root.eco,
                depth=root.depth,
                score=direct.opening_score if scored else None,
                confidence=direct.confidence if scored else None,
                coverage=direct.coverage if scored else None,
                sample_size=direct.sample_size if scored else None,
                game_count=direct.game_count if scored else None,
                path=[prev.opening_key for prev, _ in chain[:index]],
                # The player's SAN prefix up to and including the crossing move.
                moves=[m.move_san for m in session_moves[: move_index + 1]],
            )
        )

    return SessionOpeningsResponse(
        player_color=player_color,
        lineage=lineage,
        start_ply=start_ply,
        score_status=score_status,
    )
