"""Target-publication interlock against SRS opportunity folding (g-srs-target-publish).

One rule holds here: **retention may remove a target, but it must never fail a
move.** Everything in this module either admits a targeted publication or names
the reason it will not, and every reason is answerable with a legal, persisted,
target-free move by the caller. Nothing in here raises an HTTP error, invents a
counter or writes a target sample.

The interlock exists because a target pin and a fold are the two halves of one
decision about the same evidence. ``blunder_opportunity_events`` rows behind a
user's fold prefix are gone for good, and a target published against a session
the compactor has already folded would pin history that no longer exists — a
``targeted_30d`` denominator that silently shrinks and inflates ``p_reach``. An
UNLOCKED freeze check cannot prevent that: the compactor can commit its prefix
between the check and the INSERT.

So publication and folding serialize on ONE row, ``user_opportunity_retention_state``:

* **publication** takes it ``FOR SHARE``, waiting at most ``STATE_LOCK_WAIT``,
  then re-reads policy, prefix and the DATABASE clock in a fresh statement and
  holds the share lock through the decision INSERT and COMMIT.
* **the compactor** (``g-srs-fold-recovery``) takes the SAME row
  ``FOR UPDATE NOWAIT`` before it changes target eligibility or the prefix.

Both orders are then safe and neither can hang. SHARE first: the compactor's
NOWAIT fails immediately, it skips this user, and a later sweep sees the
committed pin. UPDATE first: publication waits up to the budget, and either
wakes to a fresh statement that now SEES the new prefix (suppress) or times out
(suppress). No unlocked check may authorize a target, in either direction.

Shared lock, not exclusive: concurrent publications for one user are not in
conflict with each other, only with folding. Making them exclusive would
serialize a user's own moves behind each other for no invariant.

**The compactor must create this row before locking it.** Lock ordering is
``users`` → ``user_opportunity_retention_state`` → parent/blunder rows, matching
the whole-user purge. A missing row cannot be locked at all, so both sides
create-if-absent first; the insert itself then serializes the two through the
primary-key conflict before either takes its row lock.

This module takes NO session, blunder or replay lock, and must not start: the
ghost/engine computation that produced the candidate runs before it and holds
none, which is what keeps the retention lock hold time down to the decision
INSERT itself (see RETAIN_SRS_OPPORTUNITIES.md for the publication-lifetime
bound that makes G = 1 hour supportable).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models import (
    GameSession,
    OpportunityRetentionPolicy,
    UserOpportunityRetentionState,
)
from app.opportunity_retention import (
    POLICY_ID,
    RetentionPolicy,
    database_clock,
    frozen_by_age,
)
from app.opportunity_store import ensure_retention_state
from app.srs_math import as_utc

logger = logging.getLogger(__name__)

# How long a publication waits for the state row. A move is being served, so the
# budget is a user-visible latency cost, not a correctness knob: waiting longer
# buys nothing, because the fallback below is already a correct answer.
STATE_LOCK_WAIT = "750ms"
# Strictly greater than the wait budget. With statement_timeout <= lock_timeout the
# acquisition would be cancelled (57014) before lock_timeout could ever fire, which
# would turn every contended publication into an indistinguishable cancellation.
STATE_LOCK_STATEMENT_TIMEOUT = "5s"

# What the REST of the publication runs under, once the share lock is held: the
# freeze check, the decision INSERT, the targeting-fact upsert and the COMMIT.
#
# These bound each STATEMENT in that window. The share lock is held for the whole
# of it, so an unbounded window is a lock a fold can be starved behind
# forever — and G is a drain gap for in-flight writers, which only means anything
# if "in flight" has a finite length. The connection's default is no bound at all,
# and an HTTP/proxy timeout is not a substitute: cancelling the request does not
# roll back a backend still blocked inside the database.
#
# The lock bound covers the ONE wait in here that is not ours: a concurrent
# identical request that has speculatively inserted the same
# (session_id, request_fingerprint) and not yet committed. Two seconds is already
# far beyond what that costs, and exceeding it degrades to a non-targeted move
# rather than failing one. Both reset at COMMIT/ROLLBACK, which is precisely the
# critical section.
PUBLICATION_LOCK_WAIT = "2s"
PUBLICATION_STATEMENT_TIMEOUT = "10s"

# The third side of the same bound, and the one the other two cannot give.
# lock_timeout and statement_timeout bound individual STATEMENTS; neither bounds
# the gap BETWEEN them, so a worker that stalls between the freeze check and the
# COMMIT — a blocked thread, a paused process, a live connection with nobody
# driving it — would hold the share lock for as long as it stayed alive, and TCP
# keepalives only ever notice a peer that is gone. This terminates such a backend,
# which is the only way to get the lock back from one. Armed for the WHOLE
# publication transaction rather than a phase of it: every gap in the window is
# Python work measured in microseconds, so a bound in seconds can only fire on a
# genuine stall. It resets at COMMIT/ROLLBACK like the other two.
#
# The real publication-lifetime bound is therefore the sum over the handful of
# statements in the window, not any single one of these numbers.
PUBLICATION_IDLE_TIMEOUT = "5s"

# lock_not_available (55P03, from lock_timeout) and query_canceled (57014, from
# statement_timeout). Only these two are treated as contention; every other
# OperationalError — a dropped connection, a real failure — propagates unchanged,
# because degrading an unknown database error to "no target" would hide it.
_ACQUISITION_TIMEOUT_SQLSTATES = frozenset({"55P03", "57014"})

# Suppression reasons. Internal diagnostics only: no public schema, enum or
# response field carries them, because a degraded move is still an ordinary
# non-targeted move to every client and to root confirmation.
REASON_STATE_LOCK_TIMEOUT = "state_lock_timeout"
REASON_MISSING_STATE = "missing_retention_state"
REASON_MISSING_POLICY = "missing_retention_policy"
REASON_SESSION_UNAVAILABLE = "session_unavailable"
REASON_TARGETING_AFTER_FOLD = "targeting_after_fold"
REASON_MUTATION_WINDOW_EXPIRED = "mutation_window_expired"
REASON_PUBLICATION_TIMEOUT = "publication_timeout"
# Raised by ``load_opportunity_counters`` (missing summary after readiness, a
# lagging review basis, a targeted window reaching discarded history, or an
# exclusion of a frozen session) and caught on the ghost-move path. The reader
# deliberately has no fallback of its own: the invariant must surface, and the
# move-serving endpoint is where it becomes a suppressed target rather than a 500.
REASON_COUNTERS_UNAVAILABLE = "retention_counters_unavailable"


class TargetPublicationSuppressed(Exception):
    """This decision may not carry a target; serve a target-free move instead.

    Raised only AFTER the publication transaction has been rolled back, so the
    caller holds no retention lock and no aborted transaction when it starts
    choosing its fallback.
    """

    def __init__(self, reason: str):
        super().__init__(f"target publication suppressed: {reason}")
        self.reason = reason


def _acquisition_timeout(err: OperationalError) -> str | None:
    """Return the SQLSTATE if ``err`` is lock/statement contention, else None.

    psycopg3 exposes the SQLSTATE on the wrapped DBAPI error as ``.sqlstate``;
    psycopg2 calls it ``pgcode``. Matching the repo's existing graph-lock shape.
    """
    orig = getattr(err, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate if sqlstate in _ACQUISITION_TIMEOUT_SQLSTATES else None


def publication_timed_out(err: OperationalError) -> bool:
    """Did the bounded publication window expire, rather than something else fail?

    Only the two contention SQLSTATEs count. Every other ``OperationalError`` — a
    dropped connection, a genuine fault — propagates, because answering it with a
    degraded move would hide a failure that has nothing to do with retention.
    """
    return _acquisition_timeout(err) is not None


def _set_timeouts(db: Session, **settings: str) -> None:
    """Arm transaction-local guardrails in ONE round trip.

    ``set_config(name, value, is_local=true)`` is the txn-local form of SET LOCAL
    and accepts a bind param, which PG utility statements do not. All of them go
    in one target list because this runs while a move is being served and every
    avoidable round trip is latency the player pays. The names are module
    constants, never caller input. On any dialect but PostgreSQL there is nothing
    to arm.
    """
    if not settings or db.get_bind().dialect.name != "postgresql":
        return
    names = list(settings)
    db.execute(
        text(
            "SELECT "
            + ", ".join(
                f"set_config('{name}', :v{i}, true)" for i, name in enumerate(names)
            )
        ).bindparams(**{f"v{i}": settings[name] for i, name in enumerate(names)})
    )


def _arm_timeouts(db: Session, **settings: str) -> dict[str, str]:
    """Arm guardrails AND return the settings to restore afterwards.

    The previous values are READ rather than assumed: this runs inside a caller
    that may already have its own guardrails armed, and clobbering them with a
    hardcoded default would silently widen or narrow an unrelated bound. Only the
    fold side needs that, because it hands the transaction back and keeps working
    in it; publication moves straight from one budget to the next and uses the
    cheaper ``_set_timeouts``.
    """
    if db.get_bind().dialect.name != "postgresql":
        return {}
    names = list(settings)
    previous = db.execute(
        select(*(func.current_setting(name) for name in names))
    ).one()
    _set_timeouts(db, **settings)
    return dict(zip(names, previous))


def _restore_timeouts(db: Session, previous: dict[str, str]) -> None:
    """Put the caller's timeouts back once an acquisition is over.

    For the FOLD side, whose own work follows the acquisition and belongs to
    ``g-srs-fold-recovery``: a ceiling borrowed to bound one lock wait must not
    silently become the budget for everything the compactor does next. A rollback
    would reset these anyway; restoring explicitly is what makes the committed
    path correct too.

    Publication does not use this. It moves from the acquisition budget straight
    to the publication budget, because the caller's default is typically no bound
    at all and the share lock it now holds is exactly what must stay finite.
    """
    _set_timeouts(db, **previous)


def _lock_state_row(db: Session, user_id: int) -> bool:
    """``SELECT ... FOR SHARE`` the user's retention state. True if it exists.

    FOR SHARE, not FOR NO KEY UPDATE: this transaction reads the row and must
    only block a fold, never another publication. It WAITS rather than using
    NOWAIT because the compactor's hold is bounded and short, and waiting it out
    keeps a target that NOWAIT would have thrown away for a few milliseconds of
    overlap.

    On SQLite the row clause is not rendered at all. That is the test dialect and
    it offers no locking evidence; every other step of the protocol still runs,
    which is what the deterministic suite covers.
    """
    return (
        db.execute(
            select(UserOpportunityRetentionState.user_id)
            .where(UserOpportunityRetentionState.user_id == user_id)
            .with_for_update(read=True)
        ).scalar()
        is not None
    )


def _acquire_state_share(db: Session, user_id: int) -> str | None:
    """Hold the interlock row FOR SHARE, creating it if this user has none.

    A missing row is NOT evidence that anything was folded — folding advances the
    prefix on this row, so it cannot have happened without one. It is evidence
    that there is nothing to interlock ON: the migration backfilled every user
    that existed then, but users created since have no row until something makes
    one. Suppressing every target for them would break targeting for new accounts
    to protect against a fold that cannot happen yet.

    Creating it instead makes the interlock total, because the compactor creates
    it the same way before its ``FOR UPDATE NOWAIT``: whichever transaction
    inserts first holds the row, and the second one blocks on the primary-key
    conflict rather than proceeding unserialized. The insert is idempotent
    (``ON CONFLICT DO NOTHING``) and leaves ``folded_through_started_at`` NULL,
    which every guard already reads as "nothing folded".

    Two ways this still ends in a suppression. The insert can violate the
    ``users`` foreign key, which aborts the transaction and is reported as such;
    ownership was validated upstream, so that is a genuine invariant failure. Or
    the row can be absent again on the second lock, which means a concurrent
    whole-user purge deleted it between the insert and the lock — there is then
    nothing left to pin a target against either. Both are reported, neither is
    papered over, and both are still answered with a move.
    """
    if _lock_state_row(db, user_id):
        return None
    try:
        ensure_retention_state(db, user_id)
    except IntegrityError:
        # The transaction is aborted here; the caller rolls back before any
        # further SQL. Nothing below may touch the database.
        logger.error(
            "srs retention state could not be created for user_id=%s; "
            "suppressing target publication",
            user_id,
        )
        return REASON_MISSING_STATE
    if _lock_state_row(db, user_id):
        return None
    return REASON_MISSING_STATE


def admit_target_publication(
    db: Session, *, user_id: int, session_id: uuid.UUID
) -> str | None:
    """Decide whether this session may publish a NEW target. None == admitted.

    On admission the caller holds the state row FOR SHARE and MUST keep holding
    it through the decision INSERT and its COMMIT. Releasing early — an
    intervening commit or rollback — reopens exactly the window this exists to
    close, because the compactor is free the instant the lock drops.

    A non-None return means the caller's transaction is finished with: it is
    either holding locks it must drop before doing fallback work, or already
    aborted by a timeout. Either way the caller rolls back FIRST and runs no
    other SQL in between.
    """
    # The acquisition budget replaces whatever the caller had; a rollback restores
    # it, and an admitted publication moves straight to the publication budget
    # below. Nothing downstream runs under the 750 ms wait.
    _set_timeouts(
        db,
        lock_timeout=STATE_LOCK_WAIT,
        statement_timeout=STATE_LOCK_STATEMENT_TIMEOUT,
        # Armed once, here, because it is the only one of the three that must
        # cover the gaps between statements rather than a statement: it stays in
        # force from this point to COMMIT, across both budgets below.
        idle_in_transaction_session_timeout=PUBLICATION_IDLE_TIMEOUT,
    )
    try:
        reason = _acquire_state_share(db, user_id)
    except OperationalError as err:
        if _acquisition_timeout(err) is None:
            raise
        logger.warning(
            "srs retention state lock timed out after %s for user_id=%s "
            "session_id=%s; suppressing target publication",
            STATE_LOCK_WAIT,
            user_id,
            session_id,
        )
        return REASON_STATE_LOCK_TIMEOUT
    if reason is not None:
        return reason
    # Not the caller's settings back: the PUBLICATION bound, for the rest of this
    # transaction. The caller's default is typically no bound at all, and the
    # share lock now being held is exactly what must not be held indefinitely.
    _set_timeouts(
        db,
        lock_timeout=PUBLICATION_LOCK_WAIT,
        statement_timeout=PUBLICATION_STATEMENT_TIMEOUT,
    )

    # ONE statement for policy, prefix, session and clock, read AFTER the lock.
    # Fresh matters twice over: this transaction may have started long before the
    # ghost search finished, and it may have just woken from a wait during which
    # the compactor committed. READ COMMITTED gives this statement its own
    # snapshot, so it sees that commit; the transaction snapshot would not.
    # Sampling the clock in the SAME statement is what stops a publication that
    # blocked across the deadline from being judged on the time it started.
    row = db.execute(
        select(
            GameSession.started_at,
            database_clock(db).label("db_now"),
            UserOpportunityRetentionState.folded_through_started_at.label("prefix"),
            OpportunityRetentionPolicy.id.label("policy_id"),
            OpportunityRetentionPolicy.mutation_window_days,
            OpportunityRetentionPolicy.grace_seconds,
            OpportunityRetentionPolicy.version,
            OpportunityRetentionPolicy.freeze_enabled,
        )
        .select_from(GameSession)
        .outerjoin(
            UserOpportunityRetentionState,
            UserOpportunityRetentionState.user_id == user_id,
        )
        .outerjoin(
            OpportunityRetentionPolicy, OpportunityRetentionPolicy.id == POLICY_ID
        )
        .where(GameSession.id == session_id, GameSession.user_id == user_id)
    ).first()
    if row is None:
        # Ownership was validated before any of this; losing the row here means a
        # concurrent purge or delete. Nothing to pin a target against.
        logger.warning(
            "session %s is no longer available to user_id=%s; suppressing "
            "target publication",
            session_id,
            user_id,
        )
        return REASON_SESSION_UNAVAILABLE
    if row.policy_id is None:
        # load_policy tolerates a missing policy row, because a database that
        # predates the migration must keep behaving as it did. Publication does
        # not get that tolerance: without the row there is no M to hold the
        # target against, and a defaulted M is a horizon nobody set.
        logger.error(
            "opportunity retention policy row %s is missing; suppressing target "
            "publication for user_id=%s",
            POLICY_ID,
            user_id,
        )
        return REASON_MISSING_POLICY

    started_at = as_utc(row.started_at)
    # The prefix arm is unconditional and is checked FIRST, because it is the one
    # that means damage may already be done. Reaching it is not an expected
    # degradation: the fold prefix only advances over evidence the compactor
    # already deleted, so a live session steering behind it is a bug in the
    # eligibility or scheduling side. Alarm, do not merely count.
    if row.prefix is not None and started_at <= as_utc(row.prefix):
        logger.error(
            "targeting after fold: session %s (started_at=%s) is at or below the "
            "fold prefix %s for user_id=%s; target suppressed",
            session_id,
            started_at,
            as_utc(row.prefix),
            user_id,
        )
        return REASON_TARGETING_AFTER_FOLD

    policy = RetentionPolicy(
        mutation_window_days=int(row.mutation_window_days),
        grace_seconds=int(row.grace_seconds),
        version=int(row.version),
        freeze_enabled=bool(row.freeze_enabled),
    )
    # Ordinary expiry: this session's evidence is past M, so it can be folded at
    # any moment after G and must not accept a new pin. Gated on freeze_enabled
    # exactly like every other age test, and inclusive at the boundary through
    # the shared helper so publication and the write guards cannot disagree about
    # a session sitting exactly on the cutoff.
    if policy.freeze_enabled and frozen_by_age(
        started_at, now=as_utc(row.db_now), policy=policy
    ):
        logger.info(
            "session %s is past the %s-day mutation window for user_id=%s; "
            "target suppressed",
            session_id,
            policy.mutation_window_days,
            user_id,
        )
        return REASON_MUTATION_WINDOW_EXPIRED
    return None


# The compactor's ceiling on the same row, stated here so the two halves of the
# interlock cannot drift into different budgets. It is deliberately below
# publication's wait: a fold is background work that can always be retried on the
# next sweep, and a move is not. g-srs-fold-recovery owns what happens after the
# acquisition — this module owns only the acquisition contract.
FOLD_LOCK_CEILING = "500ms"


def lock_state_for_fold(db: Session, *, user_id: int) -> bool:
    """The compactor's half of the interlock: ``FOR UPDATE NOWAIT``. False == skip.

    NOWAIT, never a wait: a fold that queues behind a publication would hold the
    ceiling above against a transaction that is serving a move, and it has nothing
    to gain by waiting. Returning False means "a publication holds this user's
    row"; the compactor skips that user and a later sweep, in a fresh transaction,
    sees the committed pin and folds around it. It must NOT reinterpret False as
    "nothing to fold".

    The row is created first, with the same idempotent insert publication uses.
    That is what makes the interlock total for a user who has no row yet: whichever
    side inserts first holds it, and the other blocks on the primary-key conflict
    instead of proceeding unserialized. A fold that locked without creating could
    run beside a publication that was creating, and neither would see the other.

    Call this BEFORE changing target eligibility, folding evidence or advancing
    ``folded_through_started_at``, and hold it to the fold's COMMIT.

    The ceiling is armed here rather than left to the caller because the
    create-if-absent step is the one part of this that can WAIT: an insert that
    conflicts with a concurrent one blocks on the other transaction, and NOWAIT
    says nothing about that. Without the bound a fold could hang on a publication
    exactly where it is supposed to give way.

    **On False the caller's transaction is still usable and needs no rollback.**
    That is not free: a lock timeout aborts a PostgreSQL transaction, so the
    acquisition runs inside a SAVEPOINT and the failure is rolled back to it. A
    compactor sweeping many users in one transaction can therefore skip one and
    go straight on to the next, which is the whole point of a per-user skip.
    """
    previous = _arm_timeouts(db, lock_timeout=FOLD_LOCK_CEILING)
    try:
        # The acquisition, and ONLY the acquisition, is inside the savepoint: a
        # 55P03/57014 here aborts the transaction, and ROLLBACK TO SAVEPOINT is
        # what makes "skip this user" a local decision rather than the end of the
        # caller's work. The ceiling is armed outside it on purpose, so that
        # restoring it below runs on a healthy transaction either way.
        with db.begin_nested():
            ensure_retention_state(db, user_id)
            locked = (
                db.execute(
                    select(UserOpportunityRetentionState.user_id)
                    .where(UserOpportunityRetentionState.user_id == user_id)
                    .with_for_update(nowait=True)
                ).scalar()
                is not None
            )
    except OperationalError as err:
        if _acquisition_timeout(err) is None:
            raise
        logger.info(
            "user_id=%s retention state is held by a target publication; "
            "skipping this fold",
            user_id,
        )
        locked = False
    finally:
        # Runs on both paths, and on both the transaction is healthy: the
        # savepoint rollback above cleared the failed one, and neither it nor a
        # released savepoint reverts a setting armed outside.
        try:
            _restore_timeouts(db, previous)
        except DBAPIError:
            # Except when the connection itself is gone. The restore is hygiene
            # for a transaction that CONTINUES; there is none to protect here,
            # and raising would replace whatever actually went wrong — visible
            # only through exception chaining — with a second symptom of it. The
            # caller's next statement reports the disconnect on its own terms.
            logger.warning(
                "user_id=%s could not restore lock_timeout after a fold "
                "acquisition; the connection is gone",
                user_id,
            )
    return locked
