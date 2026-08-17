"""Read-only rollout census for the fresh opening-line proof.

``raw-v8`` makes every replay-cache entry miss once, so historical sessions are
re-derived under the new PGN-backed proof. Forward terminal reconciliation only
repairs sessions ending after deployment. This module counts the older cohort
whose evidence-visible rows are shorter than a bounded PGN, including sessions
whose physical row count is complete but one or more ``fen_before`` values make
the rows invisible to ``_SESSION_ROWS_SQL``.

The census is deliberately aggregate-only. It returns no session/user/move data,
uses one PostgreSQL REPEATABLE READ / READ ONLY snapshot, and suppresses parser
logging because python-chess can include PGN content in diagnostics.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from app.checkmate_final_ply_backfill import begin_readonly_snapshot
from app.models import GameSession, SessionMove
from app.terminal_pgn import bounded_replay_pgn_mainline
from app.terminal_row_reconcile import (
    OUTCOME_DERIVED,
    reconcile_terminal_move_rows,
)


@contextmanager
def _aggregate_only_logging():
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


@dataclass(frozen=True, slots=True)
class OpeningLineProofAudit:
    evidence_eligible_sessions: int
    sessions_with_visible_rows: int
    accuracy_failed_sessions: int
    accuracy_failed_without_pgn: int
    bounded_pgn_sessions: int
    pgn_unknown_sessions: int
    bounded_pgn_without_visible_rows: int
    exact_visible_row_sessions: int
    surplus_visible_row_sessions: int
    proof_short_sessions: int
    proof_short_missing_rows: int
    physical_row_short_sessions: int
    physical_missing_rows: int
    null_fen_short_sessions: int
    null_fen_filtered_rows: int
    repairable_physical_short_sessions: int
    unrepairable_physical_short_sessions: int


@dataclass(frozen=True, slots=True)
class OpeningLineProofRolloutPlan:
    audit: OpeningLineProofAudit
    repairable_session_ids: tuple[UUID, ...]


def plan_rollout(db: Session) -> OpeningLineProofRolloutPlan:
    """Classify exposure and verified derivability from one read-only snapshot.

    ``visible_rows`` mirrors the replay/digest ``fen_before IS NOT NULL`` filter.
    Sessions with no such row never enter the replay probe, so they are counted
    separately rather than described as newly proof-excluded.
    """
    with _aggregate_only_logging():
        begin_readonly_snapshot(db)
        eligible = or_(
            GameSession.status == "ended",
            and_(
                GameSession.drill_state == "failed",
                GameSession.drill_terminal_reason == "accuracy",
            ),
        )
        rows = (
            db.query(
                GameSession.id,
                GameSession.pgn,
                GameSession.drill_state,
                GameSession.drill_terminal_reason,
                func.count(SessionMove.id).label("total_rows"),
                func.coalesce(
                    func.sum(
                        case((SessionMove.fen_before.isnot(None), 1), else_=0)
                    ),
                    0,
                ).label("visible_rows"),
            )
            .outerjoin(SessionMove, SessionMove.session_id == GameSession.id)
            .filter(GameSession.session_mode.in_(("normal", "drill")), eligible)
            .group_by(
                GameSession.id,
                GameSession.pgn,
                GameSession.drill_state,
                GameSession.drill_terminal_reason,
            )
            .order_by(GameSession.id)
            .all()
        )

        sessions_with_visible_rows = 0
        accuracy_failed_sessions = 0
        accuracy_failed_without_pgn = 0
        bounded_pgn_sessions = 0
        pgn_unknown_sessions = 0
        bounded_pgn_without_visible_rows = 0
        exact_visible_row_sessions = 0
        surplus_visible_row_sessions = 0
        proof_short_sessions = 0
        proof_short_missing_rows = 0
        physical_row_short_sessions = 0
        physical_missing_rows = 0
        null_fen_short_sessions = 0
        null_fen_filtered_rows = 0
        repairable_physical_short_sessions = 0
        unrepairable_physical_short_sessions = 0
        repairable_session_ids: list[UUID] = []

        for row in rows:
            total_rows = int(row.total_rows or 0)
            visible_rows = int(row.visible_rows or 0)
            if visible_rows > 0:
                sessions_with_visible_rows += 1

            accuracy_failed = (
                row.drill_state == "failed"
                and row.drill_terminal_reason == "accuracy"
            )
            if accuracy_failed:
                accuracy_failed_sessions += 1
                if row.pgn is None:
                    accuracy_failed_without_pgn += 1

            replay = bounded_replay_pgn_mainline(row.pgn)
            if replay is None:
                pgn_unknown_sessions += 1
                continue

            bounded_pgn_sessions += 1
            expected = len(replay)
            if visible_rows == 0:
                bounded_pgn_without_visible_rows += 1
                continue
            if visible_rows > expected:
                surplus_visible_row_sessions += 1
                continue
            if visible_rows == expected:
                exact_visible_row_sessions += 1
                continue

            proof_short_sessions += 1
            proof_short_missing_rows += expected - visible_rows
            if total_rows < expected:
                physical_row_short_sessions += 1
                physical_missing_rows += expected - total_rows
                # The audit must run BEFORE the 20260814_01 schema migration,
                # when the live game_sessions table does not yet have the ORM's
                # move_line_revision column. stage=False only reads ``id`` and
                # ``pgn`` from this object, so do not load a full GameSession.
                game = SimpleNamespace(id=row.id, pgn=row.pgn)
                reconcile = reconcile_terminal_move_rows(
                    db,
                    game,
                    stage=False,
                    allow_sparse=True,
                )
                if (
                    reconcile.outcome == OUTCOME_DERIVED
                    and reconcile.derived_rows == expected - total_rows
                ):
                    repairable_physical_short_sessions += 1
                    repairable_session_ids.append(game.id)
                else:
                    unrepairable_physical_short_sessions += 1
            filtered_null_rows = total_rows - visible_rows
            if filtered_null_rows > 0:
                null_fen_short_sessions += 1
                null_fen_filtered_rows += filtered_null_rows

    return OpeningLineProofRolloutPlan(
        audit=OpeningLineProofAudit(
            evidence_eligible_sessions=len(rows),
            sessions_with_visible_rows=sessions_with_visible_rows,
            accuracy_failed_sessions=accuracy_failed_sessions,
            accuracy_failed_without_pgn=accuracy_failed_without_pgn,
            bounded_pgn_sessions=bounded_pgn_sessions,
            pgn_unknown_sessions=pgn_unknown_sessions,
            bounded_pgn_without_visible_rows=bounded_pgn_without_visible_rows,
            exact_visible_row_sessions=exact_visible_row_sessions,
            surplus_visible_row_sessions=surplus_visible_row_sessions,
            proof_short_sessions=proof_short_sessions,
            proof_short_missing_rows=proof_short_missing_rows,
            physical_row_short_sessions=physical_row_short_sessions,
            physical_missing_rows=physical_missing_rows,
            null_fen_short_sessions=null_fen_short_sessions,
            null_fen_filtered_rows=null_fen_filtered_rows,
            repairable_physical_short_sessions=(
                repairable_physical_short_sessions
            ),
            unrepairable_physical_short_sessions=(
                unrepairable_physical_short_sessions
            ),
        ),
        repairable_session_ids=tuple(repairable_session_ids),
    )


def plan_rollout_audit(db: Session) -> OpeningLineProofAudit:
    """Aggregate-only compatibility wrapper used by the census command."""
    return plan_rollout(db).audit
