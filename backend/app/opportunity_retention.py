"""Shared SRS opportunity retention policy (g-srs-retention-state).

This module owns the ONE definition of "is this session's opportunity evidence
still mutable?" so the upload path, the deferred evidence worker, all four
repair modes, exclusion handling and individual deletes cannot drift apart.

Three rules hold everywhere in here:

1. **The database clock decides, after the lock.** Application and worker clocks
   disagree with each other and with the compactor. Every freeze decision that
   guards a write samples ``clock_timestamp()`` (PostgreSQL) AFTER the caller's
   row lock, so a transaction that started before the deadline and then blocked
   across it is still rejected. Transaction-start time (``now()``) would
   silently grant that transaction the old verdict.
2. **The fold prefix only ever forbids.** ``folded_through_started_at`` is
   conservative: everything at or below it is frozen, but the absence of a
   prefix proves nothing about older sessions, because targeted and legacy pins
   leave unfolded holes behind it. It is therefore ORed into the freeze test and
   never subtracted from it.
3. **Cleanup stays disabled here.** Nothing in this module folds, deletes or
   activates anything. ``g-srs-retain-rollout`` owns activation, and P0-B
   approval — not code completion — authorizes a baseline or shorter M.

Freezing an evidence WRITE is a skip, not an error. A user uploading moves for a
long-finished session still gets their moves and receipt persisted; only the
derived opportunity evidence is left alone. Returning an HTTP error there would
break ordinary uploads to protect a derived aggregate. A broken retention
INVARIANT is the opposite — see ``RetentionInvariantError``, which is raised.

M (``mutation_window_days``) is the freeze boundary and G (``grace_seconds``) is
the extra drain the compactor waits out on top of it: evidence stops being
writable at ``started_at + M`` and becomes foldable no earlier than
``started_at + M + G``. Both are global, never copied per user: a per-user copy would let two users disagree about which raw rows are
foldable while sharing one blunder's summary arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import (
    ColumnElement,
    DateTime,
    Interval,
    func,
    insert,
    literal,
    or_,
    select,
    type_coerce,
)
from sqlalchemy.orm import Session

from app.models import (
    GameSession,
    OpportunityRetentionPolicy,
    UserOpportunityRetentionState,
)
from app.srs_math import as_utc


class RetentionInvariantError(RuntimeError):
    """Folded evidence is unaccounted for and a counter cannot be trusted.

    Raised, never swallowed and never degraded to zeros. Silently serving a
    smaller number would change SRS dueness and hide the loss for good, so every
    retention invariant failure surfaces as this one error type.
    """


POLICY_ID = 1
# The DECIDED P0-B horizon (g-compact-srs-events, 2026-09-20): M = 60 days,
# G = 1 hour. The migration seeds these and a database that predates the policy
# table falls back to them, so no deployment can ever freeze at a horizon nobody
# chose. Seeding a placeholder instead would mean an operator who flipped the
# switches without also setting M got the 30 days this project explicitly
# rejected. The numbers are inert on their own: the age arm is gated on
# freeze_enabled, which is gated on readiness, and deletion needs
# cleanup_enabled on top of both.
DEFAULT_MUTATION_WINDOW_DAYS = 60
DEFAULT_GRACE_SECONDS = 3600


@dataclass(frozen=True)
class RetentionPolicy:
    """An immutable snapshot of the singleton policy row.

    Callers snapshot ONCE per operation and pass the snapshot down. Re-reading
    mid-operation would let a concurrent policy change split one fold across two
    policy versions, which is exactly what ``version`` exists to make provable.
    """

    mutation_window_days: int = DEFAULT_MUTATION_WINDOW_DAYS
    grace_seconds: int = DEFAULT_GRACE_SECONDS
    version: int = 1
    freeze_enabled: bool = False
    cleanup_enabled: bool = False
    readiness: bool = False

    @property
    def mutable_age(self) -> timedelta:
        """How far back from the database clock evidence stays mutable: M alone.

        G is deliberately NOT in here. Freezing at M and folding no earlier than
        M + G is what makes G a writer-drain gap: every writer that passed the
        freeze test has G to finish and commit before the compactor is allowed
        to touch the same rows. Folding at the same instant it freezes — which
        is what adding G here would produce — closes that gap and puts the
        compactor in a race with an in-flight, already-authorized write.
        """
        return timedelta(days=self.mutation_window_days)

    @property
    def foldable_age(self) -> timedelta:
        """How far back evidence must be before the compactor may fold it: M + G.

        Always at or beyond ``mutable_age``, never before it. Nothing in this
        bead folds; ``g-srs-fold-recovery`` owns the only caller. It lives here
        so the two ages cannot be defined in two places and drift.
        """
        return timedelta(days=self.mutation_window_days, seconds=self.grace_seconds)


def load_policy(db: Session) -> RetentionPolicy:
    """Read the singleton policy, tolerating a database that predates it.

    The defaults are the decided horizon with every switch off, so a caller on
    an un-migrated database still behaves exactly as it did before this bead.
    What makes that safe is ``freeze_enabled`` being false rather than the size
    of M: with no freeze there is no age arm to evaluate at all. Defaulting M to
    the chosen 60 rather than to a placeholder means no code path can act on a
    horizon that was never decided.

    A COLUMN select, not ``Session.get`` and not an entity select. Both of those
    resolve through the identity map, so a caller still holding a reference to
    the ORM row would be served the values this session loaded before the last
    commit. Reading columns bypasses the map entirely and always hits the
    database, which is the only correct behaviour for a switch another
    transaction is allowed to flip underneath us.
    """
    row = db.execute(
        select(
            OpportunityRetentionPolicy.mutation_window_days,
            OpportunityRetentionPolicy.grace_seconds,
            OpportunityRetentionPolicy.version,
            OpportunityRetentionPolicy.freeze_enabled,
            OpportunityRetentionPolicy.cleanup_enabled,
            OpportunityRetentionPolicy.readiness,
        ).where(OpportunityRetentionPolicy.id == POLICY_ID)
    ).first()
    if row is None:
        return RetentionPolicy()
    return RetentionPolicy(
        mutation_window_days=int(row.mutation_window_days),
        grace_seconds=int(row.grace_seconds),
        version=int(row.version),
        freeze_enabled=bool(row.freeze_enabled),
        cleanup_enabled=bool(row.cleanup_enabled),
        readiness=bool(row.readiness),
    )


def ensure_retention_policy_row(engine) -> None:
    """Install the singleton policy row that migration ``20260919_04`` seeds.

    ``Base.metadata.create_all`` builds the table and stops, and the two readers
    disagree about what that means on purpose: :func:`load_policy` tolerates a
    missing row because an un-migrated database must behave as it did before this
    epic, while target publication REFUSES to pin a practice target against a
    policy it cannot read — a defaulted M is a horizon nobody chose. So a
    create_all database serves every drill untargeted, logging
    ``missing_retention_policy`` each time, until this row exists. That is what
    it is for: the e2e seed database and the PostgreSQL gate's post-TRUNCATE
    restore, which must both match a migrated deployment.

    Every column is written explicitly, from the same defaults
    :class:`RetentionPolicy` falls back to, rather than left to the table's
    ``server_default``, so the values provably match what ``load_policy`` would
    have returned. (Before ``g-bool-default-quote`` the boolean defaults were also
    stored as the text ``'false'`` under SQLite — which is not ``false`` — and an
    id-only INSERT failed the readiness/freeze ladder CHECK; a database built
    before that fix still carries that DDL.)

    An existing row is left alone: this heals a missing singleton, it does not
    reset a configured one. Takes an Engine, because both callers hold one and
    the seed belongs in its own committed transaction either way.
    """
    defaults = RetentionPolicy()
    with engine.begin() as conn:
        present = conn.execute(
            select(OpportunityRetentionPolicy.id).where(
                OpportunityRetentionPolicy.id == POLICY_ID
            )
        ).first()
        if present is not None:
            return
        conn.execute(
            insert(OpportunityRetentionPolicy.__table__).values(
                id=POLICY_ID,
                mutation_window_days=defaults.mutation_window_days,
                grace_seconds=defaults.grace_seconds,
                version=defaults.version,
                freeze_enabled=defaults.freeze_enabled,
                cleanup_enabled=defaults.cleanup_enabled,
                readiness=defaults.readiness,
            )
        )


def database_clock(db: Session) -> ColumnElement[datetime]:
    """Statement-time database clock, NOT transaction-start time.

    Deliberately a local definition rather than a shared import from the
    opponent-decision retention lane: the two lanes must be able to change their
    clock handling independently, and a three-line dialect shim is a smaller
    liability than a cross-epic dependency.
    """
    if db.get_bind().dialect.name == "postgresql":
        return func.clock_timestamp(type_=DateTime(timezone=True))
    # SQLite is a test dialect only. strftime with %f gives milliseconds; the
    # trailing zeros pad to the microsecond precision the comparisons expect.
    return type_coerce(
        func.strftime("%Y-%m-%d %H:%M:%f", "now").concat("000"), DateTime()
    )


def mutable_cutoff(db: Session, *, policy: RetentionPolicy) -> ColumnElement[datetime]:
    """``database_clock - M`` as a SQL expression, per dialect.

    PostgreSQL subtracts a bound INTERVAL. SQLite has no interval arithmetic on
    its TIMESTAMP text, so the shift is pushed into the same ``strftime`` call
    that produces the clock sample — one expression, one sample, same textual
    shape as the stored column so the comparison stays a valid ordering.
    """
    if db.get_bind().dialect.name == "postgresql":
        return database_clock(db) - literal(policy.mutable_age, type_=Interval())
    seconds = int(policy.mutable_age.total_seconds())
    return type_coerce(
        func.strftime(
            "%Y-%m-%d %H:%M:%f", "now", f"-{seconds} seconds"
        ).concat("000"),
        DateTime(),
    )


def frozen_by_age(started_at: datetime, *, now: datetime, policy: RetentionPolicy) -> bool:
    """Pure age test, inclusive at the boundary.

    Inclusive matches the fold prefix, which is inclusive by construction
    (``MAX(started_at)`` of actually folded pairs). A session exactly at the
    cutoff is frozen; splitting the two tests would make a session at the
    boundary mutable on one path and immutable on the other.
    """
    return started_at <= now - policy.mutable_age


def session_frozen_clause(
    db: Session, *, policy: RetentionPolicy
) -> ColumnElement[bool]:
    """SQL predicate: this ``game_sessions`` row's opportunity evidence is frozen.

    Two independent reasons, ORed, never ANDed: the session is older than M by
    the database clock, OR it sits at or below this user's permanent fold prefix.
    The prefix arm is what makes an M *increase* unable to reopen already-folded
    evidence: widening the window moves the age arm back, but the prefix does
    not move at all.

    Only the AGE arm is gated on ``freeze_enabled``. The prefix arm is
    unconditional, because turning the policy back off does not bring deleted
    raw rows back: a writer allowed to rewrite that session would recreate rows
    a summary has already absorbed and double-count them forever. Off means
    "stop freezing NEW history", never "unfreeze what was folded".
    """
    prefix_frozen = GameSession.started_at <= (
        UserOpportunityRetentionState.folded_through_started_at
    )
    if not policy.freeze_enabled:
        return prefix_frozen
    return or_(GameSession.started_at <= mutable_cutoff(db, policy=policy), prefix_frozen)


def require_targeted_window(db: Session, *, user_id: int, cutoff: datetime) -> None:
    """Refuse a targeted window that reaches into discarded targeting history.

    ``targeted_discarded_max_served_at`` is the newest ``served_at`` among
    targeted rows that folding has discarded for this user. A cutoff at or below
    it would join against a pinned set that is no longer complete, and the
    resulting denominator would be quietly too small — which is worse than an
    error, because a shrinking denominator silently inflates p_reach.

    Equal counts as inside: the watermark is the newest DISCARDED time, so a
    cutoff exactly there already excludes a row that existed. Only a strictly
    later cutoff is safe.

    Untargeted folding must never advance this watermark; it bounds targeted
    availability alone and has nothing to say about broad evidence.
    """
    watermark = db.execute(
        select(UserOpportunityRetentionState.targeted_discarded_max_served_at).where(
            UserOpportunityRetentionState.user_id == user_id
        )
    ).scalar()
    if watermark is None:
        return
    if as_utc(cutoff) <= as_utc(watermark):
        raise RetentionInvariantError(
            f"targeted window from {cutoff} reaches discarded targeting history "
            f"for user {user_id} (discarded through {watermark})"
        )
