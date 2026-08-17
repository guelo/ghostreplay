"""Repair verified historical rows exposed by the fresh opening-line proof.

Planning delegates to :mod:`app.opening_line_proof_audit` and is read-only.
Applying rechecks each candidate under the serving writer's session lock, uses
the exact sparse terminal-PGN reconciler, recomputes cached accuracy, and bumps
the evidence cursor last. Each session commits independently, so a rerun is
idempotent and a later failure cannot roll back earlier verified repairs.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.accuracy import recompute_session_accuracy
from app.models import GameSession
from app.opening_cache import bump_evidence_seq
from app.opening_line_proof_audit import (
    OpeningLineProofRolloutPlan,
    plan_rollout,
)
from app.row_locks import for_no_key_update
from app.terminal_row_reconcile import (
    OUTCOME_DERIVED,
    reconcile_terminal_move_rows,
)

SessionFactory = Callable[[], Session]


@contextmanager
def _aggregate_only_logging():
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


@dataclass
class OpeningLineProofRepairOutcome:
    sessions_attempted: int = 0
    sessions_repaired: int = 0
    rows_inserted: int = 0
    evidence_sessions_bumped: int = 0
    sessions_no_longer_repairable: int = 0


@dataclass(frozen=True)
class OpeningLineProofRepairRun:
    plan: OpeningLineProofRolloutPlan
    outcome: OpeningLineProofRepairOutcome
    applied: bool


def _is_evidence_eligible(session: GameSession) -> bool:
    return session.status == "ended" or (
        session.drill_state == "failed"
        and session.drill_terminal_reason == "accuracy"
    )


def _apply_one_session(db: Session, session_id: UUID) -> int:
    session = for_no_key_update(
        db.query(GameSession).filter(GameSession.id == session_id)
    ).first()
    if session is None or not _is_evidence_eligible(session):
        return 0

    reconcile = reconcile_terminal_move_rows(
        db,
        session,
        allow_sparse=True,
        log=False,
    )
    if reconcile.outcome != OUTCOME_DERIVED or reconcile.derived_rows == 0:
        return 0

    session.derived_tail_rows = (
        (session.derived_tail_rows or 0) + reconcile.derived_rows
    )
    session.terminal_line_reconciled = True
    db.flush()
    recompute_session_accuracy(
        db,
        session,
        expected_total_moves=reconcile.expected_plies,
    )
    db.flush()
    bump_evidence_seq(db, session.user_id, session.player_color)
    return reconcile.derived_rows


def apply_rollout_backfill(
    session_factory: SessionFactory,
    plan: OpeningLineProofRolloutPlan,
) -> OpeningLineProofRepairOutcome:
    """Apply a reviewed plan with fresh per-session locks and guard rechecks."""
    outcome = OpeningLineProofRepairOutcome()
    with _aggregate_only_logging():
        for session_id in plan.repairable_session_ids:
            outcome.sessions_attempted += 1
            db = session_factory()
            try:
                inserted = _apply_one_session(db, session_id)
                if inserted == 0:
                    db.rollback()
                    outcome.sessions_no_longer_repairable += 1
                    continue
                db.commit()
                outcome.sessions_repaired += 1
                outcome.rows_inserted += inserted
                outcome.evidence_sessions_bumped += 1
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
) -> OpeningLineProofRepairRun:
    """Plan from a read-only snapshot and optionally apply verified repairs."""
    read_db = session_factory()
    try:
        plan = plan_rollout(read_db)
    finally:
        read_db.rollback()
        read_db.close()
    outcome = (
        apply_rollout_backfill(session_factory, plan)
        if apply
        else OpeningLineProofRepairOutcome()
    )
    return OpeningLineProofRepairRun(plan=plan, outcome=outcome, applied=apply)
