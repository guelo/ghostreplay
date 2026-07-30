"""Repair historical row-short sessions from their own verified terminal PGN.

The historical half of ``g-short-move-rows``: 47 ended-visible sessions persist
a parseable PGN describing more mainline plies than they store as
``session_moves`` rows (a final upload that never committed, 2026-04-06 to
2026-07-21). The stored PGN is the terminal record the accuracy contract
already measures against, so a session whose stored rows are a verified prefix
of that PGN's mainline can have its missing tail rows reconstructed from the
PGN itself — moves the retained record positively describes, never guesses.
Evaluations are NOT reconstructed here: derived rows carry NULL evals, moving
the session from the short-row refusal to the eval-gap refusal, where the
shipped eval repairs (``residual_eval_gap_backfill``, the checkmate/draw
final-ply backfills) own any further healing from their own evidence rules.

A session whose stored rows disagree with the PGN mainline is counted and left
untouched — that is the g-discard-branch-rows shape, and repairing rows against
a record they contradict would be guessing.

Dry-run-first, mirroring :mod:`app.residual_eval_gap_backfill`:
:func:`plan_backfill` is a read-only classification; applying rechecks every
guard per session under the same ``FOR NO KEY UPDATE`` lock the serving
writers use, derives rows through the exact forward-path reconcile helper,
recomputes cached accuracy, and bumps the evidence cursor last.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.accuracy import recompute_session_accuracy
from app.checkmate_final_ply_backfill import begin_readonly_snapshot
from app.models import GameSession
from app.opening_cache import bump_evidence_seq
from app.row_locks import for_no_key_update
from app.session_contracts import is_visible_game_session, visible_session_filter
from app.terminal_row_reconcile import (
    OUTCOME_DERIVED,
    reconcile_terminal_move_rows,
)

SessionFactory = Callable[[], Session]


@contextmanager
def _aggregate_only_logging():
    """Suppress all logging while planning/applying (aggregate-only output).

    The work in here parses client PGNs and replays serving-path helpers whose
    failure branches log identifying data: ``chess.pgn`` logs the offending
    SAN, board FEN, and PGN headers for malformed movetext, and the
    reconcile/accuracy helpers log session UUIDs. In the serving process those
    logs are wanted; on the repair script's stdout/stderr only aggregate
    counters may appear, so everything below WARNING-included is disabled for
    the duration and the prior state restored after.
    """
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


@dataclass(frozen=True)
class RepairPlan:
    session_ids: tuple[UUID, ...]
    ended_visible_null_sessions: int
    sessions_rows_short: int
    missing_tail_rows: int
    verified_sessions: int
    unverifiable_sessions: int


@dataclass
class RepairOutcome:
    sessions_attempted: int = 0
    rows_inserted_actual: int = 0
    sessions_moved_off_none: int = 0
    sessions_still_none: int = 0
    evidence_sessions_bumped: int = 0


@dataclass(frozen=True)
class RepairRun:
    plan: RepairPlan
    outcome: RepairOutcome
    applied: bool


def plan_backfill(db: Session) -> RepairPlan:
    """Classify the historical cohort without mutating it or emitting row data."""
    with _aggregate_only_logging():
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
            .filter(
                GameSession.status == "ended",
                visible_session_filter(),
                GameSession.player_accuracy.is_(None),
                GameSession.pgn.isnot(None),
            )
            .order_by(GameSession.id)
            .all()
        )

        sessions_rows_short = 0
        missing_tail_rows = 0
        verified_sessions = 0
        unverifiable_sessions = 0
        repairable_session_ids: list[UUID] = []
        for game in games:
            # Same verify-then-derive decision the forward path makes, and the
            # plan's ONLY parse of each PGN — the deficit falls out of the same
            # result. stage=False keeps the read-only snapshot transaction
            # free of pending INSERTs.
            result = reconcile_terminal_move_rows(db, game, stage=False)
            deficit = (
                max(0, result.expected_plies - result.stored_rows)
                if result.expected_plies is not None
                else 0
            )
            if deficit == 0:
                continue
            sessions_rows_short += 1
            missing_tail_rows += deficit
            if result.outcome == OUTCOME_DERIVED:
                verified_sessions += 1
                repairable_session_ids.append(game.id)
            else:
                unverifiable_sessions += 1

    return RepairPlan(
        session_ids=tuple(repairable_session_ids),
        ended_visible_null_sessions=ended_visible_null_sessions,
        sessions_rows_short=sessions_rows_short,
        missing_tail_rows=missing_tail_rows,
        verified_sessions=verified_sessions,
        unverifiable_sessions=unverifiable_sessions,
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

    # log=False: the reconcile's warnings carry the session UUID, which must
    # never reach this script's aggregate-only stdout/stderr.
    result = reconcile_terminal_move_rows(db, game, log=False)
    if result.outcome != OUTCOME_DERIVED or result.derived_rows == 0:
        return 0, False

    # Same durable marker the forward path stamps: repaired sessions stay
    # distinguishable from organically complete ones after the rows land.
    game.derived_tail_rows = result.derived_rows
    db.flush()
    recompute_session_accuracy(
        db, game, expected_total_moves=result.expected_plies
    )
    db.flush()
    # Match the serving writer: rows + cached accuracy first, cursor sink last.
    bump_evidence_seq(db, game.user_id, game.player_color)
    return result.derived_rows, game.player_accuracy is not None


def apply_backfill(
    session_factory: SessionFactory,
    plan: RepairPlan,
) -> RepairOutcome:
    """Apply a previously classified plan, rechecking all guards per session."""
    outcome = RepairOutcome()
    with _aggregate_only_logging():
        for session_id in plan.session_ids:
            db = session_factory()
            try:
                outcome.sessions_attempted += 1
                inserted, moved_off_none = _apply_one_session(db, session_id)
                if inserted == 0:
                    db.rollback()
                    continue
                db.commit()
                outcome.rows_inserted_actual += inserted
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
