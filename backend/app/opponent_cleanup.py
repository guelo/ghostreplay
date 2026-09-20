"""Bounded, restart-safe pruning of expired replay envelopes and stale facts.

Deletion is OFF by default and gated twice over: the retention policy itself
(``OPPONENT_DECISION_RETENTION_ENABLED``, see :mod:`app.opponent_retention`) plus
this module's own maintenance switch and recorded not-before instant. The default
sweep is read-only and reports exactly the rows a real run would have removed.

**The work queue is the remaining rows, never the session history.** Candidate
sessions are paged as ``DISTINCT session_id`` off ``opponent_decisions`` using the
session-leading replay index, so a session drops out of every future run the
moment its last envelope is gone. Driving the sweep from ``game_sessions``
instead would re-examine every historical expired session forever, and would need
a deadline index this design deliberately does not add.

No durable cursor, no OFFSET, no per-row commit. Cursors live in memory for one
run; a restart, an interrupted batch, an earlier-UUID insert and a row skipped
because a live request held its lock are all simply revisited next run. Each
batch is its own short transaction, so progress is never lost and no lock is held
across the sweep.

Margins: envelopes survive until ``deadline + D``, facts until
``last_served_at + 30 days + D``, both strictly — exact equality RETAINS. ``D`` is
one hour of insurance against routine delay and the app/database clock difference
``S + B < D`` documented in ``scripts/RETAIN_OPPONENT_DECISIONS.md``, not a
request-lifetime guarantee. Every predicate reads a fresh database clock, both
when a row is chosen and again after its lock is acquired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import os
import time
from typing import Callable
import uuid

from sqlalchemy import DateTime, and_, delete, distinct, func, or_, select, type_coerce
from sqlalchemy.orm import Session

from app.models import GameSession, OpponentDecision, OpponentTargetFact
from app.opponent_retention import database_clock, retention_enabled
from app.opponent_target_facts import TARGET_SOURCE_ENV, target_source
from app.srs_math import as_utc

# Insurance margin D. Shared by both cutoffs; see the module docstring.
DELETION_MARGIN = timedelta(hours=1)
# The targeted p_reach window a fact must outlive. Must stay >= the window
# ``app.srs_opportunity.load_opportunity_counters`` subtracts from its caller's
# ``now``; the extra D covers that clock being the APPLICATION's, not the
# database's. test_opponent_decision_retention.py pins the joint edge.
FACT_WINDOW = timedelta(days=30)

CLEANUP_ENABLED_ENV = "OPPONENT_DECISION_CLEANUP_ENABLED"
CLEANUP_NOT_BEFORE_ENV = "OPPONENT_DECISION_CLEANUP_NOT_BEFORE"

# One keyset page of candidate sessions, and one bounded batch of rows inside a
# session. 100 rows is ~3.6 average normal sessions' worth of envelopes; the byte
# budget takes over for the long drill histories that dominate payload size.
SESSION_PAGE = 100
BATCH_ROWS = 100
BATCH_BYTES = 4 * 1024 * 1024
# A finite run budget. The hourly job exits when either is reached and leaves the
# rest for the next hour; nothing has to drain in one invocation.
RUN_ROWS = 20_000
RUN_SECONDS = 600.0
# Healthy steady-state lag. Beyond this the backlog is not draining hourly, and
# the run reports an alert rather than quietly falling further behind.
HEALTHY_LAG = timedelta(hours=24)


class CleanupRefused(Exception):
    """Deletion was requested without its activation controls."""


def cleanup_enabled() -> bool:
    raw = os.getenv(CLEANUP_ENABLED_ENV, "0")
    if raw not in {"0", "1"}:
        raise ValueError(f"{CLEANUP_ENABLED_ENV} must be 0 or 1")
    return raw == "1"


def cleanup_not_before() -> datetime | None:
    """Operator-recorded instant before which no row may be deleted.

    The rollout sets this to the recorded activation time plus seven days. It is
    configuration, not persisted state: re-reading it every run is what lets the
    job be paused or held back without a deployment.
    """
    raw = os.getenv(CLEANUP_NOT_BEFORE_ENV, "")
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError(f"{CLEANUP_NOT_BEFORE_ENV} must carry a UTC offset")
    return parsed.astimezone(timezone.utc)


def check_configuration() -> None:
    """Read every switch up front and turn an unreadable one into a refusal.

    A typo in a maintenance variable is a configuration problem, not a crash: it
    has to leave the job's "refused" exit status, which monitoring reads as
    "nothing ran", rather than the "alerting" status a real run produces.
    """
    try:
        retention_enabled()
        target_source()
        cleanup_enabled()
        cleanup_not_before()
    except (ValueError, RuntimeError) as misconfigured:
        raise CleanupRefused(
            f"{type(misconfigured).__name__}: {misconfigured}"
        ) from misconfigured


def authorize_deletion(db: Session) -> datetime:
    """Return the fresh database clock that authorized this run's deletes.

    Refuses rather than degrading to a dry run: an operator who asked to delete
    must learn the switch is off, not read a zero-row report as success.
    """
    if not retention_enabled():
        raise CleanupRefused("Opponent retention policy is disabled; expiry is not enforced")
    if target_source() != "facts":
        # Pruning is only lossless once the counters read facts. With the reader
        # still on the envelopes, deleting at R < 30 days silently removes days
        # R+1..30 from the targeted_30d denominator and inflates p_reach, with
        # nothing to alert on. The runbook switches the reader before activation;
        # this refuses the run if that step was skipped or reverted.
        raise CleanupRefused(
            f"{TARGET_SOURCE_ENV} must be facts before pruning; counters still read "
            "opponent_decisions and would lose the attempts this deletes"
        )
    if not cleanup_enabled():
        raise CleanupRefused(f"{CLEANUP_ENABLED_ENV} is not 1")
    not_before = cleanup_not_before()
    if not_before is None:
        raise CleanupRefused(f"{CLEANUP_NOT_BEFORE_ENV} must record the activation+7d instant")
    now = as_utc(db.scalar(select(database_now(db))))
    if now < not_before:
        raise CleanupRefused(f"Cleanup not-before {not_before.isoformat()} has not been reached")
    return now


def database_now(db: Session):
    """Fresh statement-time clock, never transaction start.

    ``now()``/``CURRENT_TIMESTAMP`` freeze at BEGIN and never move again, so every
    statement in a batch would authorize itself with a reading taken before the
    work — before the locking SELECT, before any wait it did, and before the row
    became eligible. Only ``clock_timestamp()`` advances inside the transaction.
    """
    return database_clock(db)


def _clock_minus(db: Session, delta: timedelta):
    if db.get_bind().dialect.name == "postgresql":
        return database_now(db) - delta
    seconds = delta.total_seconds()
    return type_coerce(
        func.strftime("%Y-%m-%d %H:%M:%f", "now", f"-{seconds} seconds").concat("000"),
        DateTime(),
    )


def envelope_cutoff(db: Session):
    """A deadline strictly before this instant has outlived R + D."""
    return _clock_minus(db, DELETION_MARGIN)


def fact_cutoff(db: Session):
    """A ``last_served_at`` strictly before this has outlived 30 days + D."""
    return _clock_minus(db, FACT_WINDOW + DELETION_MARGIN)


def payload_bytes(db: Session):
    if db.get_bind().dialect.name == "postgresql":
        return func.octet_length(OpponentDecision.response_payload)
    return func.length(OpponentDecision.response_payload)


@dataclass
class SweepReport:
    """Aggregate only. No session, target, user or payload ever appears here.

    Under ``applied=False`` the deleted counts are what a real run would have
    removed from the same rows, not an estimate: the dry run takes the same locks
    and evaluates the same predicates, it only withholds the DELETE.
    """

    sessions_scanned: int = 0
    sessions_expired: int = 0
    envelopes_deleted: int = 0
    envelope_bytes_deleted: int = 0
    facts_deleted: int = 0
    # Enabled policy with no deadline is an invariant violation, not an expiry.
    missing_deadline_sessions: int = 0
    batches: int = 0
    budget_exhausted: bool = False
    applied: bool = False
    duration_seconds: float = 0.0
    eligible_envelopes: int = 0
    eligible_envelope_bytes: int = 0
    eligible_facts: int = 0
    oldest_overdue_expiry: datetime | None = None
    lag_seconds: float | None = None
    alerts: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.alerts


def eligible_backlog(db: Session, report: SweepReport) -> None:
    """Remaining work after this run: what the next hour inherits.

    Counted over remaining envelopes rather than expired sessions, for the same
    reason the sweep is: an emptied session must stop costing anything.
    """
    rows, payload, oldest = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(payload_bytes(db)), 0),
            func.min(GameSession.opponent_decisions_expires_at),
        )
        .select_from(OpponentDecision)
        .join(GameSession, GameSession.id == OpponentDecision.session_id)
        .where(GameSession.opponent_decisions_expires_at < envelope_cutoff(db))
    ).one()
    report.eligible_envelopes = int(rows)
    report.eligible_envelope_bytes = int(payload)
    report.oldest_overdue_expiry = None if oldest is None else as_utc(oldest)
    report.eligible_facts = int(db.scalar(
        select(func.count()).select_from(OpponentTargetFact)
        .where(OpponentTargetFact.last_served_at < fact_cutoff(db))
    ))
    if report.oldest_overdue_expiry is not None:
        now = as_utc(db.scalar(select(database_now(db))))
        report.lag_seconds = (now - report.oldest_overdue_expiry).total_seconds()
        if report.lag_seconds > (HEALTHY_LAG + DELETION_MARGIN).total_seconds():
            report.alerts.append("backlog lag exceeds the healthy hourly window")
    if report.missing_deadline_sessions:
        # Read the policy rather than assert it: a dry run before the rollout
        # initializes deadlines is a different situation from the invariant
        # violation, and the alert has to say which one this is.
        state = "enabled" if retention_enabled() else "disabled"
        report.alerts.append(f"sessions missing a deadline; retention policy is {state}")


@dataclass
class _Budget:
    """Finite work allowance for one invocation, in rows and wall-clock seconds."""

    rows: int
    deadline: float
    clock: Callable[[], float]
    spent: int = 0

    def exhausted(self) -> bool:
        return self.spent >= self.rows or self.clock() >= self.deadline


def candidate_sessions_query(
    db: Session, cursor: uuid.UUID | None = None, *, page: int = SESSION_PAGE,
):
    """The sweep's work queue: one keyset page of sessions that still own rows.

    DISTINCT over the replay index's leading column may still walk many index
    entries — this is not a claim of a skip scan. At this table's size the
    straightforward query is what the measured plan wants.

    The parent deadline is fetched as a CORRELATED SCALAR SUBQUERY, not a join.
    Joining lets the planner hash the whole of ``game_sessions`` — measured, at
    20k sessions: ``Hash Join -> Seq Scan on game_sessions``, once per page —
    which puts session history back on the sweep's critical path, the one cost
    this design exists to avoid. A scalar subquery is a ``game_sessions_pkey``
    probe per candidate and stays that shape as the history grows.

    Built here, alone, so the plan gate in test_opponent_decision_retention.py
    and the sizing harness EXPLAIN what the sweep runs and not a paraphrase.
    """
    sessions = select(distinct(OpponentDecision.session_id).label("session_id"))
    if cursor is not None:
        sessions = sessions.where(OpponentDecision.session_id > cursor)
    sessions = sessions.order_by(OpponentDecision.session_id).limit(page).subquery()
    deadline = (
        select(GameSession.opponent_decisions_expires_at)
        .where(GameSession.id == sessions.c.session_id)
        .scalar_subquery()
    )
    return select(
        sessions.c.session_id,
        deadline.label("deadline"),
        envelope_cutoff(db).label("cutoff"),
    ).order_by(sessions.c.session_id)


def _candidate_sessions(db: Session, cursor: uuid.UUID | None, page: int):
    """Page the queue and classify each candidate against the run's clock.

    The cutoff is selected as a column and compared in Python so the parent costs
    exactly one index probe rather than one per predicate. Both values come out
    of the same statement, so this is the reading either form would compare, and
    it only decides whether a session is worth a batch at all: the authoritative
    predicate is restated in SQL inside the batch and again inside its DELETE.
    """
    return [
        (
            session_id,
            None if deadline is None else as_utc(deadline),
            deadline is not None and as_utc(deadline) < as_utc(cutoff),
        )
        for session_id, deadline, cutoff in db.execute(
            candidate_sessions_query(db, cursor, page=page)
        ).all()
    ]


def envelope_batch_query(
    db: Session, session_id: uuid.UUID, cursor: uuid.UUID | None = None, *,
    rows: int = BATCH_ROWS,
):
    """One bounded, lock-taking page of one session's remaining envelopes.

    The join to the parent is a single-row lookup by primary key here, so unlike
    the candidate source it costs nothing to express as a join. Shared with the
    plan gate and the sizing harness for the same reason as the query above.
    """
    selected = select(OpponentDecision.decision_id, payload_bytes(db)).join(
        GameSession, GameSession.id == OpponentDecision.session_id,
    ).where(
        OpponentDecision.session_id == session_id,
        GameSession.opponent_decisions_expires_at < envelope_cutoff(db),
    )
    if cursor is not None:
        selected = selected.where(OpponentDecision.decision_id > cursor)
    return (
        selected.order_by(OpponentDecision.decision_id).limit(rows)
        .with_for_update(of=OpponentDecision, skip_locked=True)
    )


def fact_batch_query(
    db: Session, cursor: tuple[datetime, uuid.UUID, int] | None = None, *,
    rows: int = BATCH_ROWS,
):
    """One bounded, lock-taking page of stale facts in keyset order."""
    fact = OpponentTargetFact
    key = (fact.last_served_at, fact.session_id, fact.blunder_id)
    selected = select(*key).where(fact.last_served_at < fact_cutoff(db))
    if cursor is not None:
        served_at, session_id, blunder_id = cursor
        # Spelled out rather than as a row comparison: SQLite and PostgreSQL agree
        # on this form, and the fact table is small enough not to need the index
        # sarg a row-value predicate would buy.
        selected = selected.where(or_(
            fact.last_served_at > served_at,
            and_(fact.last_served_at == served_at, fact.session_id > session_id),
            and_(fact.last_served_at == served_at, fact.session_id == session_id,
                 fact.blunder_id > blunder_id),
        ))
    return selected.order_by(*key).limit(rows).with_for_update(skip_locked=True)


def _envelope_batch(
    db: Session, session_id: uuid.UUID, cursor: uuid.UUID | None, *,
    rows: int, max_bytes: int, apply: bool,
) -> tuple[uuid.UUID | None, int, int, int]:
    """One bounded batch: lock, recheck the fresh clock, delete, report progress.

    ``FOR UPDATE OF opponent_decisions ... SKIP LOCKED`` locks only the envelopes.
    The parent is joined for its immutable deadline and deliberately NOT locked —
    a maintenance job must never put a live request behind it. Rows another
    transaction holds are skipped and revisited next run.

    Returns ``(cursor, examined, removed, bytes)``. Progress and budget are
    measured by rows examined; the report is what the DELETE itself returned, so
    a batch the restated predicate shortened is never counted as a deletion.
    """
    locked = db.execute(envelope_batch_query(db, session_id, cursor, rows=rows)).all()
    if not locked:
        return None, 0, 0, 0
    # Size by payload as well as by count: one long drill history is orders of
    # magnitude larger than an opening-move envelope. Always keep the first row so
    # a single oversized payload cannot stall the sweep.
    batch, total = [], 0
    for decision_id, size in locked:
        if batch and total + int(size) > max_bytes:
            break
        batch.append(decision_id)
        total += int(size)
    if not apply:
        # What a real run would have taken from these same locked rows.
        return batch[-1], len(batch), len(batch), total
    # Restate the predicate around the ids this transaction actually locked: the
    # authorizing clock is sampled again, after the SELECT and any wait it did.
    # RETURNING is what makes the report the DELETE's own count and bytes rather
    # than the batch we hoped to remove.
    removed = db.execute(
        delete(OpponentDecision).where(
            OpponentDecision.decision_id.in_(batch),
            OpponentDecision.session_id.in_(
                select(GameSession.id).where(
                    GameSession.id == session_id,
                    GameSession.opponent_decisions_expires_at < envelope_cutoff(db),
                )
            ),
        ).returning(payload_bytes(db))
    ).scalars().all()
    return batch[-1], len(batch), len(removed), sum(int(size) for size in removed)


def _fact_batch(
    db: Session, cursor: tuple[datetime, uuid.UUID, int] | None, *, rows: int, apply: bool,
) -> tuple[tuple[datetime, uuid.UUID, int] | None, int, int]:
    """Facts age on their own timestamp, not on their session's deadline.

    A fact outlives its envelopes by the whole counter window, so this is a
    separate keyset pass over ``(last_served_at, session_id, blunder_id)``. The
    timestamp is re-evaluated after the lock, so a winning decision that advanced
    a candidate while this batch waited keeps its fact. If the delete wins that
    race instead, only a genuinely NEW decision can reinsert the pair — a replay
    never does — so an old envelope cannot resurrect an expired fact.
    """
    fact = OpponentTargetFact
    locked = db.execute(fact_batch_query(db, cursor, rows=rows)).all()
    if not locked:
        return None, 0, 0
    last = locked[-1]
    cursor = (last[0], last[1], last[2])
    if not apply:
        return cursor, len(locked), len(locked)
    # A pair whose timestamp advanced while this batch waited fails the restated
    # predicate and survives, so removed can legitimately be short of locked.
    removed = db.execute(
        delete(fact).where(
            or_(*[
                and_(fact.session_id == session_id, fact.blunder_id == blunder_id)
                for _, session_id, blunder_id in locked
            ]),
            fact.last_served_at < fact_cutoff(db),
        )
    ).rowcount
    return cursor, len(locked), removed


def sweep(
    session_factory: Callable[[], Session], *, apply: bool = False,
    session_page: int = SESSION_PAGE, batch_rows: int = BATCH_ROWS,
    batch_bytes: int = BATCH_BYTES, run_rows: int = RUN_ROWS,
    run_seconds: float = RUN_SECONDS, clock: Callable[[], float] | None = None,
) -> SweepReport:
    """Page remaining envelopes, then stale facts, inside a finite run budget.

    Every batch owns its transaction, so an interrupted run leaves a partially
    drained table and no orphaned lock, and the next run resumes from the rows
    that are still there. ``apply=True`` is authorized once, up front, against a
    fresh database clock.
    """
    clock = clock or time.monotonic
    check_configuration()
    report = SweepReport(applied=apply)
    started = clock()
    if apply:
        with session_factory() as db, db.begin():
            authorize_deletion(db)
    budget = _Budget(rows=run_rows, deadline=started + run_seconds, clock=clock)
    cursor: uuid.UUID | None = None
    while not budget.exhausted():
        with session_factory() as db, db.begin():
            candidates = _candidate_sessions(db, cursor, session_page)
        if not candidates:
            break
        cursor = candidates[-1][0]
        for session_id, deadline, expired in candidates:
            report.sessions_scanned += 1
            if deadline is None:
                # Never treated as expired: a NULL deadline under an enabled
                # policy is a broken invariant, and deleting on one would be
                # unrecoverable. eligible_backlog reads the policy to say which
                # of the two situations this run actually found.
                report.missing_deadline_sessions += 1
                continue
            if not expired:
                continue
            row_cursor: uuid.UUID | None = None
            removed_any = False
            while not budget.exhausted():
                with session_factory() as db, db.begin():
                    row_cursor, examined, removed, size = _envelope_batch(
                        db, session_id, row_cursor,
                        rows=batch_rows, max_bytes=batch_bytes, apply=apply,
                    )
                # Progress is measured by rows examined, not rows removed: a batch
                # the restated predicate shortened still consumed budget and still
                # advanced the cursor.
                if not examined:
                    break
                removed_any = True
                report.batches += 1
                report.envelopes_deleted += removed
                report.envelope_bytes_deleted += size
                budget.spent += examined
            report.sessions_expired += removed_any
            if budget.exhausted():
                break
    fact_cursor: tuple[datetime, uuid.UUID, int] | None = None
    while not budget.exhausted():
        with session_factory() as db, db.begin():
            fact_cursor, examined, removed = _fact_batch(
                db, fact_cursor, rows=batch_rows, apply=apply,
            )
        if not examined:
            break
        report.batches += 1
        report.facts_deleted += removed
        budget.spent += examined
    report.budget_exhausted = budget.exhausted()
    with session_factory() as db, db.begin():
        eligible_backlog(db, report)
    report.duration_seconds = clock() - started
    return report
