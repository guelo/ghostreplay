from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum

import chess
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import case, func, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.analysis_cache_repo import write_analysis_cache_rows
from app.analysis_profiles import BROWSER_PROFILE_ID
from app.centipawn_loss import centipawn_loss, centipawn_loss_expr
from app.evidence_contracts import select_browser_contract
from app.db import get_db
from app.accuracy import (
    AccuracyMove,
    compute_game_accuracy,
    expected_total_moves_from_pgn,
)
from app.fen import active_color, fen_hash, normalize_fen
from app.opening_cache import load_cached_rows
from app.opening_score_scheduler import request_recompute
from app.opening_roots import get_opening_roots, played_opening_chain
from app.position_analysis_repo import resolve_trusted_positions
from app.posthog_client import capture
from app.session_evidence_scheduler import enqueue_session_evidence
from app.models import (
    Blunder,
    BlunderOpportunityEvent,
    GameSession,
    Move,
    Position,
    SessionMove,
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

# Per-user graph-write serialization guardrails (g-q0aw, Postgres only).
#
# Concurrent same-opening drill replays take pg_advisory_xact_lock(user_id) so
# they queue deterministically instead of racing the (user_id, fen_hash) unique
# index. ``lock_timeout`` bounds how long acquisition waits on a stuck queue
# (advisory locks go through the PG lock manager) and ``statement_timeout``
# bounds any single pathological query, so a degenerate case fails fast (SQLSTATE
# 55P03 / 57014) instead of hanging ~166s. Tunable by patching these constants in
# tests to keep the timeout-path tests fast.
GRAPH_LOCK_TIMEOUT = "5s"
GRAPH_STATEMENT_TIMEOUT = "10s"
# Postgres SQLSTATEs we treat as recoverable graph-write timeouts: lock_not_available
# (55P03, from lock_timeout) and query_canceled (57014, from statement_timeout).
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


class SessionMovesRequest(BaseModel):
    moves: list[SessionMoveInput] = Field(default_factory=list)
    # When False, this upload SKIPS the expensive blunder-opportunity recompute
    # (forward BFS + load-all-blunders + bulk DELETE/UPSERT). The frontend's
    # mid-game incremental uploader opts out so only the final, complete upload
    # pays for it. Default True keeps any other caller's behavior unchanged
    # (compute — safe, just slower). The graph upsert + analysis-cache write +
    # opening-score recompute enqueue still run on EVERY upload regardless.
    recompute_opportunity: bool = True


class SessionMovesResponse(BaseModel):
    moves_inserted: int
    drill_state: str | None = None
    drill_terminal_reason: str | None = None


class SessionAnalysisMove(BaseModel):
    move_number: int
    color: MoveColor
    move_san: str
    fen_after: str
    eval_cp: int | None = None
    eval_mate: int | None = None
    best_move_san: str | None = None
    best_move_eval_cp: int | None = None
    eval_delta: int | None = None
    classification: MoveClassification | None = None
    segment: str = NORMAL_MOVE_SEGMENT


class SessionAnalysisSummary(BaseModel):
    blunders: int
    mistakes: int
    inaccuracies: int
    average_centipawn_loss: int
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


class SessionOpeningsResponse(BaseModel):
    player_color: str
    lineage: list[OpeningLineageItem]


def _get_session_or_404(db: Session, session_id: uuid.UUID) -> GameSession:
    game_session = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not game_session:
        raise HTTPException(status_code=404, detail="Game session not found")
    return game_session


def _ensure_session_owned_by_user(game_session: GameSession, user: TokenPayload) -> None:
    if game_session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this game")


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


def _upsert_analysis_cache(
    db: Session,
    moves: list[SessionMoveInput],
) -> None:
    """Upsert browser-game analysis evidence into the global cache.

    Evals are converted from player-relative (as uploaded) to white-relative for
    storage. Rows are stamped with the non-authoritative ``browser-game-v1``
    profile (the upload contract carries no engine identity) and routed through
    the shared quality-aware writer: game uploads INSERT evidence for keys that
    have none and never replace existing canonical or legacy rows. Each row is
    classified per-shape into the most specific browser-allowed contract; rows
    matching no allowed contract are rejected (not stored)."""
    cache_values = []
    for move in moves:
        if not move.fen_before or not move.move_uci:
            continue
        if move.eval_cp is None and move.best_move_eval_cp is None:
            continue

        is_black = move.color == MoveColor.BLACK
        sign = -1 if is_black else 1
        played_eval = move.eval_cp * sign if move.eval_cp is not None else None
        # Mate count flips perspective by sign-negation, same as cp.
        played_eval_mate = move.eval_mate * sign if move.eval_mate is not None else None
        best_eval = move.best_move_eval_cp * sign if move.best_move_eval_cp is not None else None
        eval_delta = centipawn_loss(move.eval_delta)

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
        contract_id = select_browser_contract(row)
        if contract_id is None:
            # Row satisfies no allowed contract; skip rather than store a row it
            # does not satisfy (logged as INVALID_INCOMING_KEEP by the helper if
            # it were passed through; here we drop it deterministically).
            continue
        row["evidence_contract_id"] = contract_id
        cache_values.append(row)

    if not cache_values:
        return

    # The shared helper owns its own transaction; ensure the caller session is
    # clean (no pending state on this connection) before delegating.
    db.commit()
    write_analysis_cache_rows(db, cache_values)


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
    stage: str, *, session_id: uuid.UUID, user_id: int, move_count: int
):
    """Bracket one synchronous ``upsert_session_moves`` side effect with timing.

    Emits a single ``stage`` log line with ``elapsed_ms`` plus ``session_id``,
    ``user_id`` and ``move_count`` so prod logs can attribute /moves latency to a
    specific side effect (see g-zuym). Logged in ``finally`` so a raising side
    effect still records how long it ran before failing.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.info(
            "upsert_session_moves side_effect=%s session_id=%s user_id=%s "
            "move_count=%d elapsed_ms=%.1f",
            stage,
            session_id,
            user_id,
            move_count,
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
        if dialect_name == "postgresql":
            # set_config(name, value, is_local=true) is the txn-local form of
            # `SET LOCAL` and accepts a normal bind param — PG utility statements
            # like `SET LOCAL x = :v` are awkward/unsupported with server-side
            # bind params. Both timeouts reset at the evidence_commit below.
            db.execute(
                text("SELECT set_config('lock_timeout', :v, true)").bindparams(
                    v=GRAPH_LOCK_TIMEOUT
                )
            )
            db.execute(
                text("SELECT set_config('statement_timeout', :v, true)").bindparams(
                    v=GRAPH_STATEMENT_TIMEOUT
                )
            )
            db.execute(
                text("SELECT pg_advisory_xact_lock(:uid)").bindparams(uid=user_id)
            )
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
    with _timed_side_effect(
        "analysis_cache_write",
        session_id=session_id,
        user_id=user_id,
        move_count=move_count,
    ):
        _upsert_analysis_cache(db, evidence_moves)
    with _timed_side_effect(
        "recompute_enqueue",
        session_id=session_id,
        user_id=user_id,
        move_count=move_count,
    ):
        request_recompute(user_id, player_color)


def _emit_session_moves_uploaded(
    user_id: int, *, move_count: int, recompute_queued: bool
) -> None:
    """Emit ``session_moves_uploaded`` once an upload is durably committed.

    Shared by both the SQLite/Postgres and generic-dialect return paths so the
    event reflects a persisted upload, not merely a received request.
    """
    capture(
        str(user_id),
        "session_moves_uploaded",
        {"move_count": move_count, "recompute_queued": recompute_queued},
    )


@router.post(
    "/{session_id}/moves",
    response_model=SessionMovesResponse,
    response_model_exclude_none=True,
)
def upsert_session_moves(
    session_id: uuid.UUID,
    request: SessionMovesRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> SessionMovesResponse:
    game_session = _get_session_or_404(db, session_id)
    _ensure_session_owned_by_user(game_session, user)
    _validate_unique_move_keys(request.moves)

    if not request.moves:
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
                else:
                    db.add(SessionMove(**value))

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
        },
    )
    with _timed_side_effect(
        "session_moves_upsert",
        session_id=session_id,
        user_id=user.user_id,
        move_count=len(values),
    ):
        db.execute(statement)
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
        int(round(summary_row.average_centipawn_loss))
        if summary_row.average_centipawn_loss is not None
        else 0
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

    accuracy = compute_game_accuracy(
        [
            AccuracyMove(
                color=move.color.value if hasattr(move.color, "value") else str(move.color),
                eval_cp=move.eval_cp,
                eval_mate=move.eval_mate,
            )
            for move in session_moves
        ],
        player_color=game_session.player_color,
        expected_total_moves=expected_total_moves,
    )

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
                eval_cp=move.eval_cp,
                eval_mate=move.eval_mate,
                best_move_san=move.best_move_san,
                best_move_eval_cp=move.best_move_eval_cp,
                eval_delta=centipawn_loss(move.eval_delta),
                classification=move.classification,
                segment=move.segment,
            )
            for move in session_moves
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
    # opening root, append it to the chain (dedup consecutive). The order roots
    # are crossed in is broadest -> deepest along this game's DAG path.
    chain = played_opening_chain([move.fen_after for move in session_moves], roots_registry)

    if not chain:
        return SessionOpeningsResponse(player_color=player_color, lineage=[])

    # Direct-row lineage scores (matching the /openings card). Stale-while-
    # revalidate reader: warm reads serve the cached batch and schedule a
    # background recompute; only a cold cache blocks on the initial compute.
    _, cached_rows = load_cached_rows(db, user.user_id, player_color)
    rows_by_key = {row.opening_key: row for row in cached_rows}  # already snapshotted

    lineage: list[OpeningLineageItem] = []
    for index, root in enumerate(chain):
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
                path=[prev.opening_key for prev in chain[:index]],
            )
        )

    return SessionOpeningsResponse(player_color=player_color, lineage=lineage)
