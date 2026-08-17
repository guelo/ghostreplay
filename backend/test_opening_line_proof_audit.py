"""Read-only rollout census for historical fresh-proof exposure."""

from __future__ import annotations

from datetime import datetime, timezone

from app.models import GameSession, OpeningScoreCursor, SessionMove
from app.opening_line_proof_backfill import run_backfill
from app.opening_line_proof_audit import plan_rollout_audit
from app.terminal_pgn import replay_pgn_mainline
from conftest import TestingSessionLocal


PGN_FOUR = '[Result "*"]\n\n1. e4 e5 2. Nf3 Nc6 *'
PGN_TWO = '[Result "*"]\n\n1. e4 e5 *'
PGN_OTHER = '[Result "*"]\n\n1. d4 d5 *'


def _session(
    db,
    user_id: int,
    *,
    pgn: str | None,
    ended: bool = True,
    accuracy_failed: bool = False,
):
    session = GameSession(
        user_id=user_id,
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc) if ended else None,
        status="ended" if ended else "active",
        engine_elo=1500,
        is_rated=not accuracy_failed,
        player_color="white",
        pgn=pgn,
        session_mode="drill" if accuracy_failed else "normal",
        drill_state="failed" if accuracy_failed else None,
        drill_terminal_reason="accuracy" if accuracy_failed else None,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _add_rows(
    db,
    session: GameSession,
    source_pgn: str,
    count: int,
    *,
    null_fen_indices: set[int] | None = None,
) -> None:
    replay = replay_pgn_mainline(source_pgn)
    assert replay is not None and len(replay) >= count
    null_fen_indices = null_fen_indices or set()
    for index, ply in enumerate(replay[:count]):
        db.add(
            SessionMove(
                session_id=session.id,
                move_number=ply.move_number,
                color=ply.color,
                move_san=ply.san,
                fen_before=None if index in null_fen_indices else ply.fen_before,
                fen_after=ply.fen_after,
                segment="normal",
            )
        )
    db.commit()


def test_rollout_audit_counts_physical_and_filtered_short_cohorts_read_only(
    db_session, create_user
):
    user = create_user("proof-audit", "password")
    physical_short = _session(db_session, user.id, pgn=PGN_FOUR)
    _add_rows(db_session, physical_short, PGN_FOUR, 2)

    null_fen_tail = _session(db_session, user.id, pgn=PGN_FOUR)
    _add_rows(
        db_session,
        null_fen_tail,
        PGN_FOUR,
        4,
        null_fen_indices={3},
    )

    surplus = _session(db_session, user.id, pgn=PGN_TWO)
    _add_rows(db_session, surplus, PGN_FOUR, 3)

    exact = _session(db_session, user.id, pgn=PGN_TWO)
    _add_rows(db_session, exact, PGN_TWO, 2)

    accuracy_failed = _session(
        db_session,
        user.id,
        pgn=None,
        ended=False,
        accuracy_failed=True,
    )
    _add_rows(db_session, accuracy_failed, PGN_TWO, 1)

    invalid_pgn = _session(db_session, user.id, pgn="not pgn")
    _add_rows(db_session, invalid_pgn, PGN_TWO, 1)

    _session(db_session, user.id, pgn=PGN_TWO)  # no visible rows

    rows_before = db_session.query(SessionMove).count()
    audit = plan_rollout_audit(db_session)
    rows_after = db_session.query(SessionMove).count()

    assert rows_after == rows_before
    assert audit.evidence_eligible_sessions == 7
    assert audit.sessions_with_visible_rows == 6
    assert audit.accuracy_failed_sessions == 1
    assert audit.accuracy_failed_without_pgn == 1
    assert audit.bounded_pgn_sessions == 5
    assert audit.pgn_unknown_sessions == 2
    assert audit.bounded_pgn_without_visible_rows == 1
    assert audit.exact_visible_row_sessions == 1
    assert audit.surplus_visible_row_sessions == 1
    assert audit.proof_short_sessions == 2
    assert audit.proof_short_missing_rows == 3
    assert audit.physical_row_short_sessions == 1
    assert audit.physical_missing_rows == 2
    assert audit.null_fen_short_sessions == 1
    assert audit.null_fen_filtered_rows == 1
    assert audit.repairable_physical_short_sessions == 1
    assert audit.unrepairable_physical_short_sessions == 0


def test_rollout_backfill_repairs_verified_rows_once_and_bumps_evidence(
    db_session, create_user
):
    user = create_user("proof-repair", "password")
    short = _session(db_session, user.id, pgn=PGN_FOUR)
    _add_rows(db_session, short, PGN_FOUR, 2)

    dry = run_backfill(TestingSessionLocal, apply=False)
    assert dry.applied is False
    assert dry.plan.audit.repairable_physical_short_sessions == 1
    assert dry.outcome.sessions_attempted == 0

    applied = run_backfill(TestingSessionLocal, apply=True)
    assert applied.outcome.sessions_attempted == 1
    assert applied.outcome.sessions_repaired == 1
    assert applied.outcome.rows_inserted == 2
    assert applied.outcome.evidence_sessions_bumped == 1
    assert applied.outcome.sessions_no_longer_repairable == 0

    db_session.expire_all()
    repaired = db_session.get(GameSession, short.id)
    assert repaired is not None
    assert repaired.derived_tail_rows == 2
    assert repaired.terminal_line_reconciled is True
    assert (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == short.id)
        .count()
        == 4
    )
    cursor = db_session.get(OpeningScoreCursor, (user.id, "white"))
    assert cursor is not None and cursor.evidence_seq == 1
    db_session.rollback()

    again = run_backfill(TestingSessionLocal, apply=True)
    assert again.plan.audit.proof_short_sessions == 0
    assert again.outcome.sessions_attempted == 0


def test_rollout_plan_refuses_a_short_row_that_contradicts_the_pgn(
    db_session, create_user
):
    user = create_user("proof-refusal", "password")
    contradicted = _session(db_session, user.id, pgn=PGN_FOUR)
    _add_rows(db_session, contradicted, PGN_OTHER, 1)

    audit = plan_rollout_audit(db_session)

    assert audit.proof_short_sessions == 1
    assert audit.physical_row_short_sessions == 1
    assert audit.repairable_physical_short_sessions == 0
    assert audit.unrepairable_physical_short_sessions == 1
