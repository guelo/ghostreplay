from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import Enum

import chess
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
    radius_ply: int = OPPORTUNITY_ANCESTOR_RADIUS_PLY,
) -> set[int]:
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
            .filter(Move.to_position_id.in_(frontier))
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


def _upsert_opportunity_event(
    db: Session,
    *,
    session_id: uuid.UUID,
    blunder_id: int,
    occurred_at,
    opportunity: bool,
    reached: bool,
) -> None:
    values = {
        "session_id": session_id,
        "blunder_id": blunder_id,
        "occurred_at": occurred_at,
        "opportunity": opportunity,
        "reached": reached,
    }
    dialect_name = db.bind.dialect.name if db.bind else ""
    if dialect_name == "sqlite":
        stmt = sqlite_insert(BlunderOpportunityEvent).values(values)
    elif dialect_name == "postgresql":
        stmt = postgresql_insert(BlunderOpportunityEvent).values(values)
    else:
        existing = db.query(BlunderOpportunityEvent).filter(
            BlunderOpportunityEvent.session_id == session_id,
            BlunderOpportunityEvent.blunder_id == blunder_id,
        ).first()
        if existing:
            existing.occurred_at = occurred_at
            existing.opportunity = opportunity
            existing.reached = reached
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
    """Teach the ghost graph from ordinary uploaded game moves."""
    stats = GhostGraphUpsertStats()
    hash_to_position_id: dict[str, int] = {}
    # Edges added in this call but not yet flushed: the dedup query below cannot
    # see them (session autoflush is off), so track them in-memory to avoid
    # duplicate-key inserts when a game transposes back to the same edge.
    pending_edges: set[tuple[int, str]] = set()

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

    def ensure_position(fen: str) -> int | None:
        try:
            hash_val = fen_hash(fen)
            color = active_color(fen)
        except (IndexError, ValueError):
            return None

        existing_id = hash_to_position_id.get(hash_val)
        if existing_id is not None:
            return existing_id

        existing = (
            db.query(Position)
            .filter(Position.user_id == user_id, Position.fen_hash == hash_val)
            .first()
        )
        if existing:
            hash_to_position_id[hash_val] = existing.id
            return existing.id

        position = Position(
            user_id=user_id,
            fen_hash=hash_val,
            fen_raw=fen,
            active_color=color,
        )
        db.add(position)
        db.flush()
        hash_to_position_id[hash_val] = position.id
        stats.positions_created += 1
        return position.id

    for move in moves:
        if not move_matches_fens(move):
            stats.invalid_moves += 1
            continue
        stats.valid_moves += 1

        from_id = ensure_position(move.fen_before)
        to_id = ensure_position(move.fen_after)
        if from_id is None or to_id is None:
            continue

        edge_key = (from_id, move.move_san)
        if edge_key in pending_edges:
            stats.edges_existing += 1
            continue

        existing_move = (
            db.query(Move)
            .filter(Move.from_position_id == from_id, Move.move_san == move.move_san)
            .first()
        )
        if existing_move:
            stats.edges_existing += 1
            continue

        db.add(
            Move(
                from_position_id=from_id,
                move_san=move.move_san,
                to_position_id=to_id,
            )
        )
        pending_edges.add(edge_key)
        stats.edges_created += 1

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
    session_position_ids = (
        {
            row[0]
            for row in db.query(Position.id)
            .filter(Position.user_id == user_id, Position.fen_hash.in_(session_hashes))
            .all()
        }
        if session_hashes
        else set()
    )

    matched: dict[int, tuple[bool, bool]] = {}
    if session_position_ids:
        blunders = db.query(Blunder).filter(Blunder.user_id == user_id).all()
        for blunder in blunders:
            reached = blunder.position_id in session_position_ids
            ancestor_ids = _reverse_ancestor_position_ids(
                db,
                start_position_id=blunder.position_id,
                player_color=player_color,
            )
            opportunity_only = bool(session_position_ids.intersection(ancestor_ids))
            opportunity = opportunity_only or reached
            if opportunity:
                matched[blunder.id] = (opportunity, reached)

    existing_events = (
        db.query(BlunderOpportunityEvent)
        .join(Blunder, Blunder.id == BlunderOpportunityEvent.blunder_id)
        .filter(BlunderOpportunityEvent.session_id == session_id, Blunder.user_id == user_id)
        .all()
    )
    for event in existing_events:
        if event.blunder_id not in matched:
            db.delete(event)

    occurred_at = normal_play_started_at(game_session)
    for blunder_id, (opportunity, reached) in matched.items():
        _upsert_opportunity_event(
            db,
            session_id=session_id,
            blunder_id=blunder_id,
            occurred_at=occurred_at,
            opportunity=opportunity,
            reached=reached,
        )
    db.commit()


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
            _upsert_session_position_graph(
                db,
                user_id=user.user_id,
                moves=evidence_moves,
            )
            _compute_blunder_opportunity_events(
                db,
                session_id=session_id,
                user_id=user.user_id,
                player_color=game_session.player_color,
            )
            _upsert_analysis_cache(db, evidence_moves)
            request_recompute(user.user_id, game_session.player_color)
        # Emitted only after the upload is durable (post-commit) so a failed
        # insert/commit never produces a successful-looking analytics event.
        _emit_session_moves_uploaded(
            user.user_id, move_count=len(values), recompute_queued=bool(evidence_moves)
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
    db.execute(statement)
    db.commit()

    if evidence_moves:
        _upsert_session_position_graph(
            db,
            user_id=user.user_id,
            moves=evidence_moves,
        )
        _compute_blunder_opportunity_events(
            db,
            session_id=session_id,
            user_id=user.user_id,
            player_color=game_session.player_color,
        )
        _upsert_analysis_cache(db, evidence_moves)
        request_recompute(user.user_id, game_session.player_color)

    # Emitted only after the upload is durable (post-commit) so a failed
    # insert/commit never produces a successful-looking analytics event.
    _emit_session_moves_uploaded(
        user.user_id, move_count=len(values), recompute_queued=bool(evidence_moves)
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
            position_analysis[move.fen_before] = PositionAnalysis(
                best_move_uci=tp.best_move_uci,
                best_move_san=tp.best_move_san,
                best_move_eval_cp=best_move_eval_cp,
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
    if not is_visible_game_session(game_session):
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
