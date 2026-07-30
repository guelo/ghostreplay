"""Legacy-only reconstruction of persisted drill evidence boundaries.

The live root-confirmation flow proves a boundary from server-recorded opponent
decisions and writes it once. Historical drills predate that proof record, so their
only recoverable evidence is the uploaded FEN sequence. This module is deliberately a
backfill, never a runtime fallback: a current session that failed live confirmation
must not later acquire a boundary merely because an upload happened to contain the
target FEN.

All-session runs therefore require a frozen ``started_before`` cutoff. Operators choose
that instant before boundary-aware runtime activation and reuse the exact same value on
every retry. A single-session run is an explicit diagnostic/repair action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import Callable
import uuid

from sqlalchemy.orm import Session

from app.evidence_boundary import observed_position_ply_bounds
from app.fen import fen_hash
from app.models import GameSession
from app.session_contracts import DRILL_SESSION_MODE

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class BoundaryBackfillReport:
    """Outcome of one committed boundary-reconstruction page."""

    cohort_sessions: int
    selected_sessions: int
    stamped: int
    already_stamped: int
    missing_target: int
    invalid_target: int
    target_not_observed: int
    remaining_null: int
    last_session_id: uuid.UUID | None

    @property
    def unreconstructable(self) -> int:
        return self.missing_target + self.invalid_target + self.target_not_observed


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("started_before must include a UTC offset")
    return value.astimezone(timezone.utc)


def _cohort_query(
    db: Session,
    *,
    session_id: uuid.UUID | None,
    started_before: datetime | None,
):
    query = db.query(GameSession).filter(
        GameSession.session_mode == DRILL_SESSION_MODE
    )
    if session_id is not None:
        return query.filter(GameSession.id == session_id)
    if started_before is None:
        raise ValueError("started_before is required for an all-session boundary backfill")
    return query.filter(GameSession.started_at < _as_utc(started_before))


def run_boundary_backfill(
    db: Session,
    *,
    session_id: uuid.UUID | None = None,
    started_before: datetime | None = None,
    after_session_id: uuid.UUID | None = None,
    limit: int | None = None,
    progress_every: int = 100,
    progress: ProgressCallback = partial(print, flush=True),
) -> BoundaryBackfillReport:
    """Reconstruct and persist one page of legacy drill boundaries.

    Exactly one selection shape is required:

    * ``session_id`` for an explicit diagnostic/repair; or
    * ``started_before`` for the frozen legacy cohort.

    UUID keyset ordering makes ``after_session_id`` a durable checkpoint. Every
    selected session is committed independently, so interruption loses at most the
    current row and a restart after the last printed UUID is exact. Starting over is
    also safe: both the Python branch and the UPDATE predicate are write-once.
    """
    if session_id is not None and started_before is not None:
        raise ValueError("choose session_id or started_before, not both")
    if session_id is not None and (after_session_id is not None or limit is not None):
        raise ValueError("after_session_id and limit require an all-session run")
    if session_id is None and started_before is None:
        raise ValueError("started_before is required for an all-session boundary backfill")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be greater than zero")
    if progress_every < 0:
        raise ValueError("progress_every must be non-negative")

    cohort = _cohort_query(
        db,
        session_id=session_id,
        started_before=started_before,
    )
    cohort_sessions = cohort.count()
    if session_id is not None and cohort_sessions == 0:
        raise RuntimeError(f"Drill session not found: {session_id}")

    selected_query = cohort
    if after_session_id is not None:
        selected_query = selected_query.filter(GameSession.id > after_session_id)
    selected_query = selected_query.order_by(GameSession.id.asc())
    if limit is not None:
        selected_query = selected_query.limit(limit)
    sessions = selected_query.all()

    stamped = 0
    already_stamped = 0
    missing_target = 0
    invalid_target = 0
    target_not_observed = 0
    last_session_id: uuid.UUID | None = None

    for index, game_session in enumerate(sessions, start=1):
        if game_session.drill_root_reached_ply is not None:
            already_stamped += 1
        elif not game_session.drill_opening_key:
            missing_target += 1
        else:
            try:
                target_hash = fen_hash(game_session.drill_opening_key)
            except ValueError:
                invalid_target += 1
            else:
                observed = observed_position_ply_bounds(
                    db, session_id=game_session.id
                ).get(target_hash)
                if observed is None:
                    target_not_observed += 1
                else:
                    changed = (
                        db.query(GameSession)
                        .filter(
                            GameSession.id == game_session.id,
                            GameSession.drill_root_reached_ply.is_(None),
                        )
                        .update(
                            {
                                GameSession.drill_root_reached_ply: observed.earliest
                            },
                            synchronize_session="fetch",
                        )
                    )
                    if changed == 1:
                        stamped += 1
                    else:
                        # A live confirmation won the write-once race.
                        already_stamped += 1

        db.commit()
        last_session_id = game_session.id
        if progress_every > 0 and index % progress_every == 0:
            progress(
                f"boundary_sessions={index}/{len(sessions)} "
                f"last_session_id={last_session_id} stamped={stamped} "
                f"unreconstructable="
                f"{missing_target + invalid_target + target_not_observed}"
            )

    remaining_null = cohort.filter(
        GameSession.drill_root_reached_ply.is_(None)
    ).count()
    return BoundaryBackfillReport(
        cohort_sessions=cohort_sessions,
        selected_sessions=len(sessions),
        stamped=stamped,
        already_stamped=already_stamped,
        missing_target=missing_target,
        invalid_target=invalid_target,
        target_not_observed=target_not_observed,
        remaining_null=remaining_null,
        last_session_id=last_session_id,
    )
