"""Repair historical session eval gaps from retained, reusable evidence.

This is the historical half of ``g-residual-eval-gaps``. Unlike the deterministic
terminal-ply backfills, a nonterminal missing evaluation cannot be reconstructed
from the game result. A row is therefore repairable only when the exact
``(fen_before, move_uci)`` analysis-cache key still exists and its MOVE grain
holds ``GAME_ANALYSIS_REUSE`` for the session owner.

The module is dry-run-first: :func:`plan_backfill` is a read-only classification.
Applying a plan rechecks every guard under the same parent-session
``FOR NO KEY UPDATE`` lock used by ``POST /moves``, updates only real move-grain
evidence, recomputes cached accuracy, and bumps the evidence cursor last.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import UUID

import chess
from sqlalchemy import case
from sqlalchemy.orm import Session

from app.accuracy import (
    expected_total_moves_from_pgn,
    ply_coordinates_intact,
    recompute_session_accuracy,
)
from app.analysis_trust import cache_row_as_move_dict, move_trust_flags
from app.centipawn_loss import centipawn_loss
from app.checkmate_final_ply_backfill import begin_readonly_snapshot
from app.evidence_policy import Capability
from app.fen import normalize_fen
from app.models import (
    AnalysisCache,
    AnalysisCacheSubmission,
    GameSession,
    SessionMove,
)
from app.opening_cache import bump_evidence_seq
from app.row_locks import for_no_key_update
from app.session_contracts import is_visible_game_session, visible_session_filter

SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class RepairPlan:
    session_ids: tuple[UUID, ...]
    ended_visible_null_sessions: int
    sessions_with_eval_gaps: int
    missing_eval_rows: int
    trustworthy_retained_rows: int
    unrecoverable_rows: int
    fully_recoverable_sessions: int


@dataclass
class RepairOutcome:
    sessions_attempted: int = 0
    rows_filled_actual: int = 0
    sessions_moved_off_none: int = 0
    sessions_still_none: int = 0
    evidence_sessions_bumped: int = 0


@dataclass(frozen=True)
class RepairRun:
    plan: RepairPlan
    outcome: RepairOutcome
    applied: bool


def _verified_move_uci(row: SessionMove) -> str | None:
    """Return the exact played UCI only when the stored SAN/FEN chain verifies."""
    if not row.fen_before:
        return None
    try:
        board = chess.Board(row.fen_before)
        move = board.parse_san(row.move_san)
        canonical_san = board.san(move)
        board.push(move)
        if canonical_san != row.move_san:
            return None
        if normalize_fen(board.fen()) != normalize_fen(row.fen_after):
            return None
        return move.uci()
    except (ValueError, TypeError):
        return None


def _trusted_cache_row(
    db: Session,
    game_session: GameSession,
    move_row: SessionMove,
) -> AnalysisCache | None:
    """Resolve reusable MOVE evidence for one exact, verified session row."""
    move_uci = _verified_move_uci(move_row)
    if move_uci is None or move_row.fen_before is None:
        return None

    cache_row = (
        db.query(AnalysisCache)
        .filter(
            AnalysisCache.fen_before == move_row.fen_before,
            AnalysisCache.move_uci == move_uci,
        )
        .first()
    )
    if cache_row is None:
        return None

    viewer_associated = (
        db.query(AnalysisCacheSubmission)
        .filter(
            AnalysisCacheSubmission.analysis_cache_id == cache_row.id,
            AnalysisCacheSubmission.user_id == game_session.user_id,
        )
        .first()
        is not None
    )
    data = cache_row_as_move_dict(
        cache_row,
        viewer_associated=viewer_associated,
    )
    _, _, trusted = move_trust_flags(
        data,
        Capability.GAME_ANALYSIS_REUSE,
        game_session.user_id,
    )
    if not trusted:
        return None
    if cache_row.played_eval is None and cache_row.played_eval_mate is None:
        return None
    return cache_row


def _missing_rows(db: Session, session_id: UUID, *, lock: bool = False):
    query = db.query(SessionMove).filter(
        SessionMove.session_id == session_id,
        SessionMove.eval_cp.is_(None),
        SessionMove.eval_mate.is_(None),
    )
    if lock:
        query = query.with_for_update()
    return query.order_by(SessionMove.move_number, SessionMove.color).all()


def _ordered_rows(db: Session, session_id: UUID):
    color_order = case((SessionMove.color == "white", 0), else_=1)
    return (
        db.query(SessionMove)
        .filter(SessionMove.session_id == session_id)
        .order_by(SessionMove.move_number, color_order)
        .all()
    )


def _is_complete_eval_gap(db: Session, game: GameSession) -> bool:
    rows = _ordered_rows(db, game.id)
    expected = expected_total_moves_from_pgn(game.pgn)
    return (
        expected is not None
        and len(rows) >= expected
        and ply_coordinates_intact(rows)
        and any(row.eval_cp is None and row.eval_mate is None for row in rows)
    )


def plan_backfill(db: Session) -> RepairPlan:
    """Classify the historical cohort without mutating it or emitting row data."""
    begin_readonly_snapshot(db)
    ended_visible_null_sessions = (
        db.query(GameSession.id)
        .filter(
            GameSession.status == "ended",
            visible_session_filter(),
            GameSession.player_accuracy.is_(None),
        )
        .count()
    )
    games = (
        db.query(GameSession)
        .join(SessionMove, SessionMove.session_id == GameSession.id)
        .filter(
            GameSession.status == "ended",
            visible_session_filter(),
            GameSession.player_accuracy.is_(None),
            SessionMove.eval_cp.is_(None),
            SessionMove.eval_mate.is_(None),
        )
        .distinct()
        .order_by(GameSession.id)
        .all()
    )

    missing_eval_rows = 0
    trustworthy_retained_rows = 0
    fully_recoverable_sessions = 0
    sessions_with_eval_gaps = 0
    repairable_session_ids: list[UUID] = []
    for game in games:
        if not _is_complete_eval_gap(db, game):
            continue
        sessions_with_eval_gaps += 1
        rows = _missing_rows(db, game.id)
        trusted_count = sum(
            _trusted_cache_row(db, game, row) is not None for row in rows
        )
        missing_eval_rows += len(rows)
        trustworthy_retained_rows += trusted_count
        if trusted_count > 0:
            repairable_session_ids.append(game.id)
        if rows and trusted_count == len(rows):
            fully_recoverable_sessions += 1

    return RepairPlan(
        session_ids=tuple(repairable_session_ids),
        ended_visible_null_sessions=ended_visible_null_sessions,
        sessions_with_eval_gaps=sessions_with_eval_gaps,
        missing_eval_rows=missing_eval_rows,
        trustworthy_retained_rows=trustworthy_retained_rows,
        unrecoverable_rows=missing_eval_rows - trustworthy_retained_rows,
        fully_recoverable_sessions=fully_recoverable_sessions,
    )


def _apply_one_session(db: Session, session_id: UUID) -> tuple[int, bool]:
    game = for_no_key_update(
        db.query(GameSession).filter(GameSession.id == session_id)
    ).first()
    if (
        game is None
        or game.status != "ended"
        or not is_visible_game_session(game)
        or game.player_accuracy is not None
    ):
        return 0, False
    if not _is_complete_eval_gap(db, game):
        return 0, False

    filled = 0
    for row in _missing_rows(db, session_id, lock=True):
        cache_row = _trusted_cache_row(db, game, row)
        if cache_row is None:
            continue
        # analysis_cache is white-relative; session_moves is mover-relative.
        sign = -1 if row.color == "black" else 1
        row.eval_cp = (
            cache_row.played_eval * sign
            if cache_row.played_eval is not None
            else None
        )
        row.eval_mate = (
            cache_row.played_eval_mate * sign
            if cache_row.played_eval_mate is not None
            else None
        )
        row.eval_delta = centipawn_loss(cache_row.eval_delta)
        row.classification = cache_row.classification
        filled += 1

    if filled == 0:
        return 0, False

    db.flush()
    recompute_session_accuracy(db, game)
    db.flush()
    # Match the serving writer: move + cached accuracy first, cursor sink last.
    bump_evidence_seq(db, game.user_id, game.player_color)
    return filled, game.player_accuracy is not None


def apply_backfill(
    session_factory: SessionFactory,
    plan: RepairPlan,
) -> RepairOutcome:
    """Apply a previously classified plan, rechecking all trust/staleness guards."""
    outcome = RepairOutcome()
    for session_id in plan.session_ids:
        db = session_factory()
        try:
            outcome.sessions_attempted += 1
            filled, moved_off_none = _apply_one_session(db, session_id)
            if filled == 0:
                db.rollback()
                continue
            db.commit()
            outcome.rows_filled_actual += filled
            outcome.evidence_sessions_bumped += 1
            if moved_off_none:
                outcome.sessions_moved_off_none += 1
            else:
                outcome.sessions_still_none += 1
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    return outcome


def run_backfill(
    session_factory: SessionFactory,
    *,
    apply: bool = False,
) -> RepairRun:
    """Plan from one read-only snapshot and optionally apply with fresh sessions."""
    read_db = session_factory()
    try:
        plan = plan_backfill(read_db)
    finally:
        read_db.rollback()
        read_db.close()
    outcome = apply_backfill(session_factory, plan) if apply else RepairOutcome()
    return RepairRun(plan=plan, outcome=outcome, applied=apply)
