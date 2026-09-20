"""Immutable opponent replay deadlines. Activation belongs to the rollout.

OPPONENT_DECISION_RETENTION_ENABLED defaults off. A positive
OPPONENT_DECISION_RETENTION_SECONDS selects R for new sessions, allowing writers
to initialize deadlines before enforcement; existing deadlines never move.
PostgreSQL is the production clock authority; SQLite is only a test dialect.
"""

import os
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import DateTime, func, literal, select, type_coerce
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import GameSession, OpponentDecision


def retention_seconds() -> int | None:
    raw = os.getenv("OPPONENT_DECISION_RETENTION_SECONDS")
    if raw is None or raw == "":
        return None
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise ValueError("OPPONENT_DECISION_RETENTION_SECONDS must be a positive integer") from exc
    if seconds <= 0:
        raise ValueError("OPPONENT_DECISION_RETENTION_SECONDS must be positive")
    return seconds


def retention_enabled() -> bool:
    raw = os.getenv("OPPONENT_DECISION_RETENTION_ENABLED", "0")
    if raw not in {"0", "1"}:
        raise ValueError("OPPONENT_DECISION_RETENTION_ENABLED must be 0 or 1")
    enabled = raw == "1"
    if enabled and retention_seconds() is None:
        raise RuntimeError("Opponent retention enabled without a selected duration")
    return enabled


def initialize_deadline(db: Session, session: GameSession) -> None:
    retention_enabled()  # Reject enabled-without-duration before creating a session.
    seconds = retention_seconds()
    if seconds is None:
        return
    db.flush()
    # Refresh the persisted value, including the model's server-default path.
    db.refresh(session, ["started_at", "opponent_decisions_expires_at"])
    if session.opponent_decisions_expires_at is None:
        session.opponent_decisions_expires_at = (
            session.started_at + timedelta(seconds=seconds)
        )


def database_clock(db: Session):
    if db.get_bind().dialect.name == "postgresql":
        return func.clock_timestamp(type_=DateTime(timezone=True))
    return type_coerce(func.strftime("%Y-%m-%d %H:%M:%f", "now").concat("000"), DateTime())


def expiry_error() -> HTTPException:
    return HTTPException(
        status_code=410,
        detail={
            "error_code": "OPPONENT_SESSION_EXPIRED",
            "message": "Opponent session expired. Start a new game or drill.",
        },
    )


def require_deadline(deadline: datetime | None) -> None:
    if deadline is None:
        raise RuntimeError("Opponent retention enabled with missing session deadline")


def check_deadline(db: Session, session_id: UUID) -> None:
    if not retention_enabled():
        return
    deadline, alive = db.execute(
        select(
            GameSession.opponent_decisions_expires_at,
            database_clock(db) < GameSession.opponent_decisions_expires_at,
        ).where(GameSession.id == session_id)
    ).one()
    require_deadline(deadline)
    if not alive:
        raise expiry_error()


def insert_decision(db: Session, values: dict[str, object]) -> datetime | None:
    """Return winning served_at or None on conflict; expiry is distinct.

    The materialized admission CTE samples the clock exactly once after callers'
    existing locks. It supplies BOTH the INSERT predicate and served_at. Returning
    admission alongside the INSERT outcome distinguishes expiry from a conflict,
    even when the winner is deleted before the subsequent reselect.
    """
    enabled = retention_enabled()
    model = OpponentDecision
    if db.get_bind().dialect.name == "postgresql":
        sample = (
            select(database_clock(db).label("served_at"))
            .cte("decision_clock")
            .prefix_with("MATERIALIZED")
        )
        admission = (
            select(
                sample.c.served_at,
                GameSession.opponent_decisions_expires_at.label("deadline"),
            )
            .select_from(sample)
            .join(GameSession, GameSession.id == values["session_id"])
            .cte("decision_admission")
            .prefix_with("MATERIALIZED")
        )
        columns = [
            literal(value, type_=model.__table__.c[key].type)
            for key, value in values.items()
        ]
        source = select(*columns, admission.c.served_at)
        if enabled:
            source = source.where(admission.c.served_at < admission.c.deadline)
        written = (
            postgresql_insert(model)
            .from_select([*values, "served_at"], source)
            .on_conflict_do_nothing(
                index_elements=[model.session_id, model.request_fingerprint]
            )
            .returning(model.served_at)
            .cte("written_decision")
        )
        deadline, alive, served_at = db.execute(
            select(
                admission.c.deadline,
                (admission.c.served_at < admission.c.deadline).label("alive"),
                written.c.served_at,
            ).select_from(admission.outerjoin(written, literal(True)))
        ).one()
        if enabled:
            require_deadline(deadline)
            if not alive:
                raise expiry_error()
        return served_at

    # SQLite has no data-modifying CTEs. It offers no locking evidence, but uses
    # one database sample for the same boundary and winning timestamp contracts.
    served_at, deadline = db.execute(
        select(
            database_clock(db), GameSession.opponent_decisions_expires_at,
        ).where(GameSession.id == values["session_id"])
    ).one()
    if enabled:
        require_deadline(deadline)
        if served_at >= deadline:
            raise expiry_error()
    return db.execute(
        sqlite_insert(model)
        .values(**values, served_at=served_at)
        .on_conflict_do_nothing(
            index_elements=[model.session_id, model.request_fingerprint]
        )
        .returning(model.served_at)
    ).scalar_one_or_none()
