"""Atomic targeting facts and the shared current-counter / future pin source.

Reader selection is an operator checkpoint, not a coverage claim. See
scripts/RETAIN_OPPONENT_DECISIONS.md before enabling facts. No union of sources:
envelopes and facts describe the same attempts.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import Blunder, GameSession, OpponentDecision, OpponentTargetFact

TargetSource = Literal["decisions", "facts"]
TARGET_SOURCE_ENV = "OPPONENT_TARGET_SOURCE"


def target_source() -> TargetSource:
    source = os.environ.get(TARGET_SOURCE_ENV, "decisions")
    if source not in ("decisions", "facts"):
        raise ValueError(f"{TARGET_SOURCE_ENV} must be decisions or facts")
    return source


def fact_insert(db: Session):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return pg_insert(OpponentTargetFact)
    if dialect == "sqlite":
        return sqlite_insert(OpponentTargetFact)
    raise NotImplementedError(f"Opponent targeting facts do not support {dialect}")


def monotonic_fact_upsert(stmt):
    # The conditional update is portable and avoids touching equal/older facts.
    return stmt.on_conflict_do_update(
        index_elements=[OpponentTargetFact.session_id, OpponentTargetFact.blunder_id],
        set_={"last_served_at": stmt.excluded.last_served_at},
        where=OpponentTargetFact.last_served_at < stmt.excluded.last_served_at,
    )


def publish_target_fact(
    db: Session, *, session_id: uuid.UUID, blunder_id: int, served_at: datetime,
) -> None:
    """Publish within the winning envelope's transaction; never commit here."""
    db.execute(monotonic_fact_upsert(fact_insert(db).values(
        session_id=session_id, blunder_id=blunder_id, last_served_at=served_at,
    )))


def backfill_target_facts(db: Session) -> int:
    """Rerunnable MAX over all original targets, safe alongside live dual writers.

    Caller owns commit. Refuse once any retention deadline has been initialized:
    old envelopes must not resurrect facts that retention intentionally expired.
    Rollout must finish backfill before initializing deadlines; this preflight
    does not serialize a concurrent deadline initialization.
    """
    if db.scalar(select(GameSession.id).where(
        GameSession.opponent_decisions_expires_at.is_not(None),
    ).limit(1)) is not None:
        raise ValueError("Target fact backfill is disabled after retention deadlines are initialized")
    source = db.query(
        OpponentDecision.session_id,
        OpponentDecision.target_blunder_id,
        func.max(OpponentDecision.served_at),
    ).filter(OpponentDecision.target_blunder_id.is_not(None)).group_by(
        OpponentDecision.session_id, OpponentDecision.target_blunder_id,
    )
    stmt = fact_insert(db).from_select(
        ["session_id", "blunder_id", "last_served_at"], source.statement,
    )
    # psycopg can report rowcount=-1 for INSERT ... SELECT. RETURNING counts
    # only inserted/advanced pairs, including zero on an idempotent rerun.
    return sum(1 for _ in db.execute(monotonic_fact_upsert(stmt).returning(literal_column("1"))))


def current_target_pairs(
    db: Session, *, cutoff: datetime | ColumnElement[datetime],
    source: TargetSource | None = None,
    blunder_ids: list[int] | None = None, user_id: int | None = None,
    exclude_session_id: uuid.UUID | None = None,
):
    """One eligible (session, target) row for counters and future retention pins.

    MAX preserves both inclusive lower bounds. Do not add broad event eligibility
    here: a reached pair may need pinning even when its broad event is too old.
    Callers serving a user must supply both user_id and requested blunder_ids;
    the all-owner form is reserved for aggregate migration verification.

    ``cutoff`` may be a plain value or a SQL expression. The compactor passes a
    database-clock expression, because its pin lookup has to be decided by the
    same clock as every other retention test, not by the process running it.

    Targeting comes from server decisions or their atomic facts, never client
    uploads. A served target with no later upload must remain a FAILED steer in
    the denominator; deriving attempts from uploaded events would bias p_reach up.

    FILTER BEFORE GROUPING in the decisions branch: eligibility belongs to each
    decision row. Grouping first and testing MIN(served_at) would drop a session
    whose first attempt predates the cutoff or blunder creation but whose later
    attempt qualifies. Facts preserve MAX(served_at), which gives the same result
    for both inclusive lower bounds. Use served time, not session.started_at, so
    a late-session attempt is not dated back to the session's opening.
    """
    selected = target_source() if source is None else source
    if selected == "decisions":
        session_id = OpponentDecision.session_id
        blunder_id = OpponentDecision.target_blunder_id
        served_at = OpponentDecision.served_at
    elif selected == "facts":
        session_id = OpponentTargetFact.session_id
        blunder_id = OpponentTargetFact.blunder_id
        served_at = OpponentTargetFact.last_served_at
    else:
        raise ValueError(f"Unknown targeting source: {selected}")
    query = db.query(
        session_id.label("session_id"), blunder_id.label("blunder_id"),
    ).join(Blunder, Blunder.id == blunder_id).filter(
        served_at >= cutoff, served_at >= Blunder.created_at,
    )
    if user_id is not None:
        query = query.filter(Blunder.user_id == user_id)
    if blunder_ids is not None:
        query = query.filter(blunder_id.in_(blunder_ids))
    if exclude_session_id is not None:
        query = query.filter(session_id != exclude_session_id)
    if selected == "decisions":
        query = query.group_by(session_id, blunder_id)
    return query.subquery()
