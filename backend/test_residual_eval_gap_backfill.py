"""Historical residual-eval repair from exact capability-granted cache evidence."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import chess
import chess.pgn

from app.analysis_profiles import (
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    CANONICAL_PROFILE_ID,
    stamp_profile_full,
)
from app.evidence_contracts import MOVE_COMPLETE
from app.fen import normalize_fen
from app.models import AnalysisCache, GameSession, SessionMove, User
from app.residual_eval_gap_backfill import run_backfill
from conftest import TestingSessionLocal


FOOLS_MATE = ["f3", "e5", "g4", "Qh4#"]


def _game_rows():
    board = chess.Board()
    rows = []
    for index, san in enumerate(FOOLS_MATE):
        fen_before = board.fen()
        move = board.parse_san(san)
        canonical_san = board.san(move)
        move_uci = move.uci()
        board.push(move)
        rows.append(
            {
                "move_number": index // 2 + 1,
                "color": "white" if index % 2 == 0 else "black",
                "move_san": canonical_san,
                "move_uci": move_uci,
                "fen_before": fen_before,
                "fen_after": board.fen(),
            }
        )
    game = chess.pgn.Game.from_board(board)
    pgn = game.accept(
        chess.pgn.StringExporter(
            headers=False,
            variations=False,
            comments=False,
        )
    ).strip()
    return rows, pgn


def _insert_gap_game(db, *, gap_index: int = 2, user_id: int = 123):
    rows, pgn = _game_rows()
    session_id = uuid.uuid4()
    db.add(
        GameSession(
            id=session_id,
            user_id=user_id,
            started_at=datetime.now(timezone.utc),
            status="ended",
            result="checkmate_loss",
            engine_elo=1500,
            player_color="white",
            session_mode="normal",
            is_rated=True,
            pgn=pgn,
            player_accuracy=None,
            player_accuracy_algo_version=1,
        )
    )
    for index, data in enumerate(rows):
        db.add(
            SessionMove(
                session_id=session_id,
                move_number=data["move_number"],
                color=data["color"],
                move_san=data["move_san"],
                fen_before=data["fen_before"],
                fen_after=data["fen_after"],
                eval_cp=None if index == gap_index else 20,
                eval_mate=None,
                eval_delta=None if index == gap_index else 0,
                classification=None if index == gap_index else "best",
            )
        )
    db.commit()
    return session_id, rows[gap_index]


def _insert_cache(db, move, *, profile_id: str, white_eval: int = -240):
    row = AnalysisCache(
        fen_before=move["fen_before"],
        normalized_fen_before=normalize_fen(move["fen_before"]),
        move_uci=move["move_uci"],
        move_san=move["move_san"],
        played_eval=white_eval,
        played_eval_mate=None,
        eval_delta=260,
        classification="blunder",
        source="precomputed",
        analysis_profile_id=profile_id,
        evidence_contract_id=MOVE_COMPLETE,
        **stamp_profile_full(profile_id),
    )
    db.add(row)
    db.commit()
    return row


def test_dry_run_classifies_without_writing_then_apply_heals_idempotently(
    db_session,
):
    db_session.add(User(id=123, username="eval-gap-owner", is_anonymous=True))
    db_session.commit()
    session_id, gap = _insert_gap_game(db_session)
    _insert_cache(db_session, gap, profile_id=CANONICAL_PROFILE_ID)

    dry = run_backfill(TestingSessionLocal)
    assert dry.applied is False
    assert dry.plan.sessions_with_eval_gaps == 1
    assert dry.plan.missing_eval_rows == 1
    assert dry.plan.trustworthy_retained_rows == 1
    assert dry.plan.unrecoverable_rows == 0
    assert dry.plan.fully_recoverable_sessions == 1
    assert dry.outcome.rows_filled_actual == 0

    db_session.expire_all()
    missing = (
        db_session.query(SessionMove)
        .filter(
            SessionMove.session_id == session_id,
            SessionMove.move_number == gap["move_number"],
            SessionMove.color == gap["color"],
        )
        .one()
    )
    assert missing.eval_cp is None

    applied = run_backfill(TestingSessionLocal, apply=True)
    assert applied.outcome.rows_filled_actual == 1
    assert applied.outcome.sessions_moved_off_none == 1
    assert applied.outcome.evidence_sessions_bumped == 1

    db_session.expire_all()
    repaired = (
        db_session.query(SessionMove)
        .filter(
            SessionMove.session_id == session_id,
            SessionMove.move_number == gap["move_number"],
            SessionMove.color == gap["color"],
        )
        .one()
    )
    assert repaired.eval_cp == -240  # white-relative == white-mover-relative
    assert repaired.eval_delta == 260
    assert (
        db_session.query(GameSession)
        .filter(GameSession.id == session_id)
        .one()
        .player_accuracy
        is not None
    )

    rerun = run_backfill(TestingSessionLocal, apply=True)
    assert rerun.outcome.rows_filled_actual == 0


def test_black_move_sign_conversion_and_owner_scoped_cache_refusal(db_session):
    db_session.add(User(id=123, username="eval-gap-owner", is_anonymous=True))
    db_session.commit()
    session_id, gap = _insert_gap_game(db_session, gap_index=3)
    _insert_cache(
        db_session,
        gap,
        profile_id=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        white_eval=-75,
    )

    # The browser-analysis profile is owner-scoped. With no submitter
    # association, an identity-valid exact row is still not reusable.
    refused = run_backfill(TestingSessionLocal)
    assert refused.plan.sessions_with_eval_gaps == 1
    assert refused.plan.trustworthy_retained_rows == 0
    assert refused.plan.unrecoverable_rows == 1

    # Replace with canonical evidence, which is unscoped and reusable.
    cache = db_session.query(AnalysisCache).one()
    db_session.delete(cache)
    db_session.commit()
    _insert_cache(
        db_session,
        gap,
        profile_id=CANONICAL_PROFILE_ID,
        white_eval=-75,
    )

    applied = run_backfill(TestingSessionLocal, apply=True)
    assert applied.outcome.rows_filled_actual == 1
    db_session.expire_all()
    repaired = (
        db_session.query(SessionMove)
        .filter(
            SessionMove.session_id == session_id,
            SessionMove.move_number == gap["move_number"],
            SessionMove.color == gap["color"],
        )
        .one()
    )
    assert repaired.eval_cp == 75  # cache white-relative -> black mover-relative


def test_mixed_retained_evidence_repairs_only_the_trustworthy_row(db_session):
    db_session.add(User(id=123, username="mixed-gap-owner", is_anonymous=True))
    db_session.commit()
    session_id, trusted_gap = _insert_gap_game(db_session, gap_index=2)
    _insert_cache(db_session, trusted_gap, profile_id=CANONICAL_PROFILE_ID)

    # Make a second interior row unavailable without retaining any exact cache
    # evidence for it. The plan must distinguish the two instead of guessing.
    unrecoverable = (
        db_session.query(SessionMove)
        .filter(
            SessionMove.session_id == session_id,
            SessionMove.move_number == 1,
            SessionMove.color == "white",
        )
        .one()
    )
    unrecoverable.eval_cp = None
    unrecoverable.eval_mate = None
    unrecoverable.eval_delta = None
    unrecoverable.classification = None
    db_session.commit()

    dry = run_backfill(TestingSessionLocal)
    assert dry.plan.missing_eval_rows == 2
    assert dry.plan.trustworthy_retained_rows == 1
    assert dry.plan.unrecoverable_rows == 1
    assert dry.plan.fully_recoverable_sessions == 0

    applied = run_backfill(TestingSessionLocal, apply=True)
    assert applied.outcome.rows_filled_actual == 1
    assert applied.outcome.sessions_moved_off_none == 0
    assert applied.outcome.sessions_still_none == 1

    db_session.expire_all()
    rows = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session_id)
        .all()
    )
    by_key = {(row.move_number, row.color): row for row in rows}
    assert by_key[(2, "white")].eval_cp == -240
    assert by_key[(1, "white")].eval_cp is None
    assert (
        db_session.query(GameSession)
        .filter(GameSession.id == session_id)
        .one()
        .player_accuracy
        is None
    )
