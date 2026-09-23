"""Atomic SRS opportunity folding: transfer, delete, advance, in one transaction.

This is the only code in the project that deletes a ``blunder_opportunity_events``
row on purpose. What it does, in order, is the whole design:

**Outside every lock** it samples the database clock, picks a small candidate
rowset, serializes those rows' exact original facts and writes a verified
recovery export (:mod:`app.opportunity_fold_export`). Filesystem I/O has no
useful worst case, so none of it happens while a lock is held.

**Inside one short transaction on a dedicated connection** it takes the user's
advisory lock (``pg_try_advisory_xact_lock`` — succeed now or skip this user),
the retention-state row (``lock_state_for_fold``, the publication interlock),
then the affected blunders and their summaries in ascending id with
``FOR NO KEY UPDATE NOWAIT``. It re-reads policy, prefix and ``clock_timestamp()``
AFTER those locks — that N, not the export's preview time, decides freeze, grace
and target pins. It rechecks the candidate ids, count, canonical hash and
eligibility against the database, not against the export. Then it adds the three
contributions to the summaries, deletes exactly the validated rows, commits the
manifest and advances the prefix and the targeted watermark. Commit. An
interruption anywhere rolls the entire transfer back.

Three properties are worth stating on their own, because each one is a decision
that could plausibly have gone the other way:

* **Nonblocking acquisition.** Every lock is ``try``/``NOWAIT``. A fold that
  waited would hold a per-user lock against a transaction that is serving a move.
  Contention means "skip this user, fold on a later sweep" — never "nothing to
  fold", and never a wait.
* **A whole-transaction deadline, not just statement timeouts.**
  ``statement_timeout`` bounds one statement and says nothing about the gaps
  between them, so a monotonic 500 ms budget is enforced in Python between every
  statement, ``idle_in_transaction_session_timeout`` covers a stalled client, and
  a connection whose rollback cannot complete is discarded rather than returned
  to the pool holding locks.
* **The recheck reads the database, never the export.** The export proves what
  was written to disk. Only a fresh READ COMMITTED statement under the locks can
  prove what is still true, and a fold that validated itself against its own
  export would delete rows that had changed since it was taken.

Scheduling — when a sweep runs, how it avoids active users, the 12h/18h
escalation — is NOT here. ``g-srs-cleanup-schedule`` owns that. This module owns
one bounded batch, a rotation primitive over users, and the recovery protocol's
write half. Production folding stays off until ``cleanup_enabled`` is set, which
``g-srs-retain-rollout`` owns.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable

from sqlalchemy import (
    case,
    delete,
    exists,
    func,
    insert,
    literal,
    select,
    text,
    true,
    tuple_,
    update,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import (
    Blunder,
    BlunderOpportunityEvent,
    BlunderOpportunitySummary,
    BlunderReview,
    GameSession,
    OpponentDecision,
    OpponentTargetFact,
    OpportunityFoldBatch,
    OpportunityRetentionPolicy,
    UserOpportunityRetentionState,
)
from app.opponent_target_facts import current_target_pairs, target_source
from app.opportunity_fold_export import (
    ExportedBatch,
    FoldExportError,
    FoldRow,
    canonical_rowset_hash,
    discard_export,
    write_export,
)
from app.opportunity_retention import (
    POLICY_ID,
    RetentionInvariantError,
    RetentionPolicy,
    database_clock,
    foldable_cutoff,
    load_policy,
    shifted_clock,
)
from app.opportunity_store import ensure_retention_state
from app.srs_math import as_utc
from app.srs_opportunity import TARGETED_WINDOW_DAYS
from app.srs_target_admission import lock_state_for_fold

logger = logging.getLogger(__name__)

# How long a committed batch stays recoverable. The same number bounds the
# artifact, the manifest row and the full-legacy rollback window, because they
# are one guarantee: "the raw rows can be put back". Mirrored as a literal in
# migration 20260920_01, which must keep describing the schema it created.
RECOVERY_WINDOW = timedelta(days=7)


class FoldOutcome(str, Enum):
    """Why a batch attempt ended. Every value except FOLDED deleted nothing."""

    FOLDED = "folded"
    # Folding is switched off (cleanup_enabled is false). The common state.
    DISABLED = "disabled"
    NOTHING_ELIGIBLE = "nothing_eligible"
    # A target publication holds this user's retention state. NOT "nothing to
    # fold": a later sweep sees the committed pin and folds around it.
    SKIPPED_PUBLICATION = "skipped_publication"
    # Another writer holds the user's advisory lock right now.
    SKIPPED_USER_BUSY = "skipped_user_busy"
    # A review or another writer holds a blunder/summary row. Defer, do not wait.
    DEFERRED_ROW_BUSY = "deferred_row_busy"
    # The source facts moved between preparation and the locks. Reprepare.
    STALE_REPREPARE = "stale_reprepare"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    EXPORT_FAILED = "export_failed"
    DEFERRED_ACTIVITY = "deferred_activity"
    DEFERRED_BUDGET = "deferred_budget"


# The outcomes a sweep treats as "this user is unavailable right now", as opposed
# to "this user has nothing to do". Both stop the per-user loop; only the first
# group is worth retrying on the next sweep.
TRANSIENT_OUTCOMES = frozenset({
    FoldOutcome.SKIPPED_PUBLICATION,
    FoldOutcome.SKIPPED_USER_BUSY,
    FoldOutcome.DEFERRED_ROW_BUSY,
    FoldOutcome.STALE_REPREPARE,
    FoldOutcome.DEADLINE_EXCEEDED,
    FoldOutcome.EXPORT_FAILED,
})


@dataclass(frozen=True)
class FoldLimits:
    """The bounded-batch and timing contract, in one place so a test can shrink it.

    The pair/blunder caps START here and only ever adapt DOWN under budget
    pressure. Raising them is a benchmark decision under the same time ceiling,
    owned by ``g-srs-retention-gates`` — not something this module discovers at
    runtime, because the only evidence it has locally is that the last batch fit.
    """

    max_pairs: int = 100
    max_blunders: int = 16
    # Hard end-to-end ceiling on the critical transaction, commit included.
    transaction_deadline: float = 0.5
    # The p99 target for the same interval. Exceeding it is not a failure; it is
    # the signal that halves the next batch.
    hold_target: float = 0.25
    # Transaction-local guardrails. lock_timeout is deliberately tiny: every lock
    # in the critical section is NOWAIT or try-, so the only thing it bounds is an
    # incidental internal wait, and waiting is never the right answer here.
    lock_timeout_ms: int = 25
    statement_timeout_ms: int = 100
    idle_in_transaction_ms: int = 100
    # Between two batches for the SAME user, outside any transaction. Immediate
    # reacquisition can starve a publication that has been waiting for the row.
    user_cooldown: float = 1.0
    # Smallest batch the adaptive shrink will go to before giving up on the user.
    min_pairs: int = 5


@dataclass(frozen=True)
class FoldResult:
    """One batch attempt: what happened, and the two timings that are not the same.

    ``export_seconds`` and ``lock_seconds`` are traced separately on purpose. The
    500 ms gate is about the LOCK interval; a slow disk inflates the export and
    must not be mistaken for lock pressure, or the adaptive shrink would react to
    the wrong signal.
    """

    outcome: FoldOutcome
    user_id: int
    batch_id: uuid.UUID | None = None
    rows_deleted: int = 0
    blunders: int = 0
    prepare_seconds: float = 0.0
    export_seconds: float = 0.0
    lock_seconds: float = 0.0
    detail: str | None = None

    @property
    def folded(self) -> bool:
        return self.outcome is FoldOutcome.FOLDED


@dataclass
class SweepReport:
    """Aggregate outcome of a sweep. Per-user failures are counted, never fatal."""

    batches: int = 0
    rows_deleted: int = 0
    # Users the sweep STARTED with, not users it reached: the rotation drops a
    # user the moment it runs out of work, and the outcome counts below are what
    # say how many were actually attempted.
    candidates: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    max_lock_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def record(self, result: FoldResult) -> None:
        self.outcomes[result.outcome.value] = self.outcomes.get(result.outcome.value, 0) + 1
        self.max_lock_seconds = max(self.max_lock_seconds, result.lock_seconds)
        if result.folded:
            self.batches += 1
            self.rows_deleted += result.rows_deleted


class FoldDeadlineExceeded(RuntimeError):
    """The whole-transaction budget expired. The caller rolls back; nothing committed."""


class _Deadline:
    """A monotonic whole-transaction budget, checked between statements.

    ``statement_timeout`` cannot do this job alone. It bounds one statement and
    says nothing about the gap between two, so a transaction of ten 100 ms
    statements is a one-second transaction with every statement inside budget.
    The clock is ``time.monotonic`` because this measures an elapsed interval,
    and a wall clock that steps backwards during an NTP correction would extend
    a lock hold instead of ending it.
    """

    def __init__(self, budget: float) -> None:
        self.budget = budget
        self.started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def remaining(self) -> float:
        return self.budget - self.elapsed

    def check(self, step: str) -> None:
        if self.remaining <= 0:
            raise FoldDeadlineExceeded(
                f"fold transaction exceeded {self.budget:.3f}s before {step} "
                f"({self.elapsed:.3f}s elapsed)"
            )


# ---------------------------------------------------------------------------
# Transaction-local guardrails
# ---------------------------------------------------------------------------


def _set_config(db: Session, name: str, value: str) -> None:
    """``SET LOCAL`` with a bind parameter. PostgreSQL only; elsewhere a no-op."""
    _set_configs(db, **{name: value})


def _set_configs(db: Session, **settings: str) -> None:
    """Arm several transaction-local settings in ONE round trip.

    ``set_config(name, value, is_local=true)`` is the txn-local form of
    ``SET LOCAL`` and, unlike the utility statement, accepts bind parameters.
    They all go in one target list because this runs inside a 500 ms budget and
    every avoidable round trip is lock hold time. The names are module constants,
    never caller input.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    names = list(settings)
    db.execute(
        select(
            *(
                func.set_config(name, settings[name], True)
                for name in names
            )
        )
    )


class _StatementBudget:
    """Keeps ``statement_timeout`` at min(configured, remaining deadline).

    Re-arming before every statement would double the round trips for a bound
    that almost never binds, so the armed value is tracked here and only rewritten
    when the remaining deadline drops below it — which happens once, in the tail
    of a batch that is running late. Normally this costs nothing after the first
    arming.
    """

    def __init__(self, db: Session, deadline: _Deadline, ceiling_ms: int) -> None:
        self.db = db
        self.deadline = deadline
        self.ceiling_ms = ceiling_ms
        self.armed_ms = ceiling_ms

    def before(self, step: str) -> None:
        self.deadline.check(step)
        want_ms = min(self.ceiling_ms, max(1, int(self.deadline.remaining * 1000)))
        if want_ms < self.armed_ms:
            _set_config(self.db, "statement_timeout", f"{want_ms}ms")
            self.armed_ms = want_ms


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def _pinned_pair(db: Session, *, user_id: int, blunder_ids: list[int] | None):
    """EXISTS: an eligible current target still needs this pair's evidence.

    A pair served inside the targeted window is in the ``targeted_30d``
    DENOMINATOR, and its raw row is the only source of the matching reach in the
    numerator. Folding it would delete a measured reach while the attempt it
    belongs to stayed counted, which inflates nothing and deflates ``p_reach`` —
    silently, and in the direction that suppresses a target the player is still
    being steered at.

    Reads the same source the counters read (``current_target_pairs``), so a
    deployment that has cut over to compact targeting facts pins from the facts.
    That is the g-retain-decisions coordination gate: the pin lookup must consume
    the shared facts once they are backfilled and ready, not the envelopes that
    are about to be pruned.

    ``blunder_ids`` narrows the subquery to the candidate set. Unscoped it is a
    correlated aggregate over a user's whole decision history, which is fine in
    preparation and is not fine inside a 100 ms statement budget.

    The window is measured from the DATABASE clock, like every other retention
    decision in this epic, while the counter reader measures it from the caller's
    ``now``. The two can differ by whatever the clocks differ by — which cannot
    matter at the configured horizon, where a foldable pair is already 60 days
    past the targeted window's edge, and under a synthetic short M the database
    clock is the one to prefer anyway.
    """
    pins = current_target_pairs(
        db,
        cutoff=shifted_clock(db, timedelta(days=TARGETED_WINDOW_DAYS)),
        user_id=user_id,
        blunder_ids=blunder_ids,
    )
    return exists(
        select(literal(1))
        .select_from(pins)
        .where(
            pins.c.session_id == BlunderOpportunityEvent.session_id,
            pins.c.blunder_id == BlunderOpportunityEvent.blunder_id,
        )
    )


def _eligible_rows_query(
    db: Session, *, user_id: int, policy: RetentionPolicy, blunder_ids: list[int] | None
):
    """The rows this user is allowed to fold right now, with the facts to fold them.

    Scoped by BOTH ``blunders.user_id`` and ``game_sessions.user_id``. They agree
    for every row the application writes; requiring both means a cross-owner row —
    which would put one user's evidence behind another user's prefix — is left
    alone for an invariant check to find rather than quietly folded.

    Deliberately NOT filtered by the fold prefix. Behind the prefix are the holes
    that pins and legacy rows left behind, and they are exactly the rows a later
    sweep has to come back for. The prefix only ever forbids WRITES; it is not a
    high-water mark of completed work.
    """
    query = (
        select(
            BlunderOpportunityEvent.id,
            BlunderOpportunityEvent.blunder_id,
            BlunderOpportunityEvent.session_id,
            BlunderOpportunityEvent.occurred_at,
            BlunderOpportunityEvent.created_at,
            BlunderOpportunityEvent.opportunity,
            BlunderOpportunityEvent.reached,
            GameSession.started_at.label("session_started_at"),
            Blunder.created_at.label("blunder_created_at"),
        )
        .select_from(BlunderOpportunityEvent)
        .join(GameSession, GameSession.id == BlunderOpportunityEvent.session_id)
        .join(Blunder, Blunder.id == BlunderOpportunityEvent.blunder_id)
        .where(
            Blunder.user_id == user_id,
            GameSession.user_id == user_id,
            GameSession.started_at <= foldable_cutoff(db, policy=policy),
            ~_pinned_pair(db, user_id=user_id, blunder_ids=blunder_ids),
            # Legacy NULL/mismatched timestamps must also predate folding.
            func.coalesce(BlunderOpportunityEvent.occurred_at,
                          BlunderOpportunityEvent.created_at) < database_clock(db),
        )
    )
    if blunder_ids is not None:
        query = query.where(BlunderOpportunityEvent.blunder_id.in_(blunder_ids))
    return query


def _row_from(record) -> FoldRow:
    return FoldRow(
        id=int(record.id),
        blunder_id=int(record.blunder_id),
        session_id=record.session_id,
        occurred_at=record.occurred_at,
        created_at=record.created_at,
        opportunity=bool(record.opportunity),
        reached=bool(record.reached),
        session_started_at=record.session_started_at,
        blunder_created_at=record.blunder_created_at,
    )


def _capped(rows: list[FoldRow], *, limits: FoldLimits, max_pairs: int) -> list[FoldRow]:
    """Apply BOTH bounds: at most ``max_pairs`` rows over at most N blunders.

    The blunder cap is not decoration. Each distinct blunder costs two row locks
    and one CASE arm in the summary update, and it is the blunder rows a
    concurrent review contends on — so a batch that touched a hundred blunders
    would be a hundred chances to collide inside one 500 ms budget.
    """
    kept: list[FoldRow] = []
    seen: set[int] = set()
    for row in rows:
        if row.blunder_id not in seen:
            if len(seen) >= limits.max_blunders:
                continue
            seen.add(row.blunder_id)
        kept.append(row)
        if len(kept) >= max_pairs:
            break
    return kept


# ---------------------------------------------------------------------------
# Contributions: what each summary gains from the rows about to be deleted
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ReviewBasis:
    """The blunder's CURRENT latest review, read under its row lock."""

    review_id: int | None = None
    session_id: uuid.UUID | None = None
    reviewed_at: datetime | None = None


@dataclass(frozen=True)
class _Contribution:
    """What one blunder's summary gains, and the review window it gains it in."""

    blunder_id: int
    eligible: int
    opportunities_since_review: int
    reached_since_review: int
    review_basis_id: int | None

    def as_json(self) -> dict:
        return {
            "blunder_id": self.blunder_id,
            "eligible": self.eligible,
            "opportunities_since_review": self.opportunities_since_review,
            "reached_since_review": self.reached_since_review,
            "review_basis_id": self.review_basis_id,
        }


def _contributions(
    rows: list[FoldRow], *, bases: dict[int, _ReviewBasis]
) -> list[_Contribution]:
    """The shared five-counter predicates, applied to the rows being deleted.

    Exactly the arithmetic ``load_opportunity_counters`` performs over live rows,
    which is the whole point: a folded row must add to the summary precisely what
    it was contributing to the live aggregate a moment earlier, or the counter
    changes when the storage changes.

    ``t >= c`` uses ``COALESCE(blunder.created_at, event.created_at)`` and ``S``
    is evaluated against the CURRENT locked review, never an exported snapshot.
    A review that landed after the export already reset this summary's
    since-review counters to zero and moved its basis; folding against the old
    review would attribute pre-review evidence to the new window.
    """
    totals: dict[int, list[int]] = {}
    for row in rows:
        basis = bases.get(row.blunder_id, _ReviewBasis())
        event_time = row.event_time
        created = (
            as_utc(row.blunder_created_at)
            if row.blunder_created_at is not None
            else as_utc(row.created_at)
        )
        eligible = row.opportunity and event_time >= created
        since_review = basis.reviewed_at is None or (
            row.session_id != basis.session_id
            and event_time > as_utc(basis.reviewed_at)
        )
        bucket = totals.setdefault(row.blunder_id, [0, 0, 0])
        if eligible:
            bucket[0] += 1
            if since_review:
                bucket[1] += 1
                if row.reached:
                    bucket[2] += 1
    return [
        _Contribution(
            blunder_id=blunder_id,
            eligible=bucket[0],
            opportunities_since_review=bucket[1],
            reached_since_review=bucket[2],
            review_basis_id=bases.get(blunder_id, _ReviewBasis()).review_id,
        )
        for blunder_id, bucket in sorted(totals.items())
    ]


# ---------------------------------------------------------------------------
# Preparation, outside every lock
# ---------------------------------------------------------------------------


class _Defer(RuntimeError):
    """A recoverable reason to abandon this batch. Carries the outcome to report.

    Every one of these means nothing was deleted. They are returned to the sweep
    as outcomes rather than raised to the caller, because contention, a disabled
    policy and an empty candidate set are all ordinary states of a compactor and
    none of them is a failure.
    """

    def __init__(self, outcome: FoldOutcome, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


@dataclass(frozen=True)
class _Prepared:
    """A candidate rowset and the preview policy/clock it was chosen under.

    The policy here CANNOT authorize a deletion. It selects candidates and
    nothing else; the transfer re-reads the policy after locking and compares
    versions, so a policy change between these two moments aborts the batch
    rather than deleting under a horizon that has been superseded.
    """

    user_id: int
    policy: RetentionPolicy
    preview_now: datetime
    rows: list[FoldRow]
    rowset_hash: str

    @property
    def event_ids(self) -> list[int]:
        return [row.id for row in self.rows]

    @property
    def blunder_ids(self) -> list[int]:
        return sorted({row.blunder_id for row in self.rows})

    @property
    def pairs(self) -> list[tuple]:
        return [(row.session_id, row.blunder_id) for row in self.rows]


def _prepare(
    db: Session, *, user_id: int, limits: FoldLimits, max_pairs: int
) -> _Prepared:
    """Pick a batch and serialize its exact facts. Reads only; commits one row.

    The one write is ``ensure_retention_state``, and it is here rather than in
    the critical section for a specific reason: ``lock_state_for_fold`` creates
    the row if it is absent, and THAT insert is the one step of the interlock
    that can WAIT (on a concurrent publication inserting the same key). Committing
    it out here means the critical transaction only ever locks a row that already
    exists, so its acquisition is genuinely non-waiting. A NULL-prefix state row
    freezes nothing and authorizes nothing, so creating it early costs nothing.
    """
    policy = load_policy(db)
    if not policy.cleanup_enabled:
        # The normal state of every deployment until g-srs-retain-rollout says
        # otherwise. Not an error, and not "nothing eligible" either: the
        # difference matters to whoever is reading a sweep report wondering why
        # nothing shrank.
        raise _Defer(FoldOutcome.DISABLED, "cleanup_enabled is false")
    preview_now = as_utc(db.execute(select(database_clock(db))).scalar_one())
    records = db.execute(
        _eligible_rows_query(db, user_id=user_id, policy=policy, blunder_ids=None)
        # Oldest sessions first: the prefix advances over what is actually folded,
        # so working forward in time keeps it a tight bound instead of jumping it
        # over a mountain of unfolded holes on the first batch.
        .order_by(
            GameSession.started_at,
            BlunderOpportunityEvent.blunder_id,
            BlunderOpportunityEvent.id,
        )
        .limit(max_pairs)
    ).all()
    rows = _capped([_row_from(record) for record in records], limits=limits,
                   max_pairs=max_pairs)
    if not rows:
        raise _Defer(
            FoldOutcome.NOTHING_ELIGIBLE,
            f"user {user_id} has no unpinned evidence past M + G",
        )
    ensure_retention_state(db, user_id)
    db.commit()
    return _Prepared(
        user_id=user_id,
        policy=policy,
        preview_now=preview_now,
        rows=rows,
        rowset_hash=canonical_rowset_hash(rows),
    )


# ---------------------------------------------------------------------------
# The critical transaction
# ---------------------------------------------------------------------------


def _acquire_user(db: Session, *, user_id: int) -> bool:
    """``pg_try_advisory_xact_lock`` — the graph-write lock, taken without waiting.

    The SAME one-key namespace the upload path and both repair modes serialize
    on (``app.graph_write_lock``), deliberately: those are the writers that can
    be rewriting the very evidence this batch is about to delete. The difference
    is ``try_``. A compactor that queued behind an upload would hold nothing
    useful and delay a user's own write; skipping and folding on a later sweep
    costs nothing, because the rows are already past M + G and are not going
    anywhere.
    """
    if db.get_bind().dialect.name != "postgresql":
        return True
    return bool(
        db.execute(
            text("SELECT pg_try_advisory_xact_lock(:uid)").bindparams(uid=user_id)
        ).scalar()
    )


def _lock_rows(db: Session, *, blunder_ids: list[int], budget: _StatementBudget) -> dict[int, int | None]:
    """Lock blunders then summaries, both ascending, both NOWAIT.

    Ascending id, and blunders before summaries, is the order every other writer
    of this pair uses (a review locks the blunder, then writes the summary), so
    the two cannot build a cycle. NOWAIT rather than a wait is what keeps a
    review from ever queueing behind a fold: if a review holds the row, this
    batch gives up immediately and the review proceeds untouched.

    Returns each blunder's recorded summary basis, which is read in the same
    locking statement — there is no point taking the lock and then asking a
    second time what it protects.
    """
    budget.before("blunder lock")
    locked = db.execute(
        select(Blunder.id)
        .where(Blunder.id.in_(blunder_ids))
        .order_by(Blunder.id)
        .with_for_update(key_share=True, nowait=True)
    ).scalars().all()
    if set(locked) != set(blunder_ids):
        # A blunder vanished between preparation and here (a single-blunder
        # delete cascades its events away, which is legitimate). Reprepare.
        raise _Defer(
            FoldOutcome.STALE_REPREPARE,
            f"blunders {sorted(set(blunder_ids) - set(locked))} are gone",
        )

    budget.before("summary lock")
    summaries = db.execute(
        select(
            BlunderOpportunitySummary.blunder_id,
            BlunderOpportunitySummary.latest_review_id,
        )
        .where(BlunderOpportunitySummary.blunder_id.in_(blunder_ids))
        .order_by(BlunderOpportunitySummary.blunder_id)
        .with_for_update(key_share=True, nowait=True)
    ).all()
    basis = {int(row.blunder_id): row.latest_review_id for row in summaries}
    missing = sorted(set(blunder_ids) - set(basis))
    if missing:
        # NOT a reprepare. Folding requires cleanup_enabled, which requires
        # freeze_enabled, which requires readiness — and after readiness a missing
        # summary means folded evidence was lost. Creating one here would give
        # this batch somewhere to add its totals and destroy the alarm.
        raise RetentionInvariantError(
            f"blunders {missing} have no opportunity summary; folding into a "
            "missing summary would silently discard the evidence it is supposed "
            "to preserve"
        )
    return basis


def _latest_reviews(db: Session, *, blunder_ids: list[int]) -> dict[int, _ReviewBasis]:
    """Each blunder's current latest review, ranked exactly as the reader ranks it.

    ``(reviewed_at DESC, id DESC)`` — the same tie-break as
    ``load_opportunity_counters`` and ``opportunity_store._LATEST_REVIEW``. Any
    other ordering would let the fold and the reader disagree about which of two
    same-instant reviews opened the current window, and the folded counters would
    be attributed to a window the reader does not believe in.
    """
    ranked = (
        select(
            BlunderReview.blunder_id.label("blunder_id"),
            BlunderReview.id.label("id"),
            BlunderReview.session_id.label("session_id"),
            BlunderReview.reviewed_at.label("reviewed_at"),
            func.row_number()
            .over(
                partition_by=BlunderReview.blunder_id,
                order_by=(BlunderReview.reviewed_at.desc(), BlunderReview.id.desc()),
            )
            .label("rn"),
        )
        .where(BlunderReview.blunder_id.in_(blunder_ids))
        .subquery()
    )
    rows = db.execute(
        select(ranked.c.blunder_id, ranked.c.id, ranked.c.session_id,
               ranked.c.reviewed_at).where(ranked.c.rn == 1)
    ).all()
    return {
        int(row.blunder_id): _ReviewBasis(
            review_id=int(row.id), session_id=row.session_id,
            reviewed_at=row.reviewed_at,
        )
        for row in rows
    }


def _discarded_served_at(db: Session, *, user_id: int, pairs: list[tuple]) -> datetime | None:
    """Newest ``served_at`` among targeting rows for the pairs being discarded.

    This is the ONLY thing that advances ``targeted_discarded_max_served_at``, and
    it advances it only for pairs that were actually targeted. Untargeted folding
    returns NULL and the watermark does not move: it bounds targeted availability
    alone and has nothing to say about broad evidence, so letting ordinary folding
    push it forward would narrow targeted history for free.

    Exact pairs, via a row-value IN rather than ``session_id IN ... AND
    blunder_id IN ...``. The cross product of a batch's sessions and blunders
    contains pairs this batch is not folding, and picking up their ``served_at``
    would forbid a targeted window that is still perfectly answerable.

    Reads the SELECTED source, like the pin check, so the watermark and the pin
    can never disagree about what targeting history exists.
    """
    if not pairs:
        return None
    selected = target_source()
    if selected == "facts":
        source = OpponentTargetFact
        session_column = OpponentTargetFact.session_id
        blunder_column = OpponentTargetFact.blunder_id
        served_column = OpponentTargetFact.last_served_at
    else:
        source = OpponentDecision
        session_column = OpponentDecision.session_id
        blunder_column = OpponentDecision.target_blunder_id
        served_column = OpponentDecision.served_at
    value = db.execute(
        select(func.max(served_column))
        .select_from(source)
        .join(Blunder, Blunder.id == blunder_column)
        .where(
            Blunder.user_id == user_id,
            tuple_(session_column, blunder_column).in_(pairs),
        )
    ).scalar()
    return as_utc(value) if value is not None else None


def _summary_update(contributions: list[_Contribution]):
    """ONE statement for every summary in the batch, CASE-indexed by blunder.

    A per-blunder UPDATE loop would make the statement count — and therefore the
    lock hold — proportional to the batch, which is exactly what the bounded
    fixed-statement-count rule forbids. Sixteen CASE arms is the cap.
    """
    def arm(attribute: str):
        return case(
            *(
                (BlunderOpportunitySummary.blunder_id == item.blunder_id,
                 getattr(item, attribute))
                for item in contributions
            ),
            else_=0,
        )

    return (
        update(BlunderOpportunitySummary)
        .where(
            BlunderOpportunitySummary.blunder_id.in_(
                [item.blunder_id for item in contributions]
            )
        )
        .values(
            folded_eligible_count=(
                BlunderOpportunitySummary.folded_eligible_count + arm("eligible")
            ),
            folded_opportunities_since_review=(
                BlunderOpportunitySummary.folded_opportunities_since_review
                + arm("opportunities_since_review")
            ),
            folded_reached_since_review=(
                BlunderOpportunitySummary.folded_reached_since_review
                + arm("reached_since_review")
            ),
        )
        .execution_options(synchronize_session=False)
    )


def _rollback_or_discard(db: Session, connection) -> bool:
    """Roll back; if that cannot complete, throw the connection away.

    A rollback that fails leaves a backend that may still be holding this user's
    advisory lock and its retention-state row. Returning that connection to the
    pool would leak those locks for as long as it lived, so it is invalidated
    instead: closing the socket is what makes PostgreSQL release them. The cost
    is one connection; the alternative is a user nobody can publish a target for.
    """
    try:
        db.rollback()
        return True
    except Exception:  # pragma: no cover - exercised by the PostgreSQL gate
        logger.exception("fold rollback failed; discarding the dedicated connection")
        try:
            connection.invalidate()
        except Exception:
            logger.exception("fold connection could not be invalidated")
        return False


def _transfer(
    db: Session,
    connection,
    *,
    prepared: _Prepared,
    exported: ExportedBatch,
    limits: FoldLimits,
) -> FoldResult:
    """The whole critical section. Commits everything or deletes nothing."""
    user_id = prepared.user_id
    event_ids = prepared.event_ids
    blunder_ids = prepared.blunder_ids
    deadline = _Deadline(limits.transaction_deadline)
    budget = _StatementBudget(db, deadline, limits.statement_timeout_ms)
    commit_attempted = False

    try:
        # One round trip for all three guardrails. idle_in_transaction is the one
        # the other two cannot replace: it is the only bound on a client that
        # stops driving the transaction between statements, and a stalled client
        # holding this user's locks is indistinguishable from a live one until it
        # is terminated.
        _set_configs(
            db,
            lock_timeout=f"{limits.lock_timeout_ms}ms",
            statement_timeout=f"{limits.statement_timeout_ms}ms",
            idle_in_transaction_session_timeout=f"{limits.idle_in_transaction_ms}ms",
        )

        budget.before("user advisory lock")
        if not _acquire_user(db, user_id=user_id):
            raise _Defer(FoldOutcome.SKIPPED_USER_BUSY,
                         f"user {user_id} is being written to right now")

        budget.before("retention state lock")
        if not lock_state_for_fold(db, user_id=user_id):
            # NOT "nothing to fold". A publication holds the row; a later sweep
            # sees its committed pin and folds around it.
            raise _Defer(FoldOutcome.SKIPPED_PUBLICATION,
                         f"a target publication holds user {user_id}")

        # Policy, prefix, watermark and the clock in ONE statement, AFTER the
        # locks. This N — not the export's preview time — decides freeze, grace
        # and pins, however long ago the export was taken.
        budget.before("policy and prefix read")
        state = db.execute(
            select(
                OpportunityRetentionPolicy.mutation_window_days,
                OpportunityRetentionPolicy.grace_seconds,
                OpportunityRetentionPolicy.version,
                OpportunityRetentionPolicy.freeze_enabled,
                OpportunityRetentionPolicy.cleanup_enabled,
                OpportunityRetentionPolicy.readiness,
                UserOpportunityRetentionState.folded_through_started_at,
                UserOpportunityRetentionState.targeted_discarded_max_served_at,
                database_clock(db).label("now"),
            )
            # An explicit unconditional join, not two FROM entries: both sides
            # are single rows selected by primary key, and writing the cross
            # product out loud is what keeps it from looking like a missing ON
            # clause to a reader or to SQLAlchemy.
            .select_from(OpportunityRetentionPolicy)
            .join(UserOpportunityRetentionState, true())
            .where(
                OpportunityRetentionPolicy.id == POLICY_ID,
                UserOpportunityRetentionState.user_id == user_id,
            )
        ).first()
        if state is None:
            raise _Defer(FoldOutcome.STALE_REPREPARE,
                         f"policy or retention state for user {user_id} is gone")
        locked = RetentionPolicy(
            mutation_window_days=int(state.mutation_window_days),
            grace_seconds=int(state.grace_seconds),
            version=int(state.version),
            freeze_enabled=bool(state.freeze_enabled),
            cleanup_enabled=bool(state.cleanup_enabled),
            readiness=bool(state.readiness),
        )
        if not locked.cleanup_enabled:
            raise _Defer(FoldOutcome.DISABLED, "cleanup was disabled after preparation")
        if locked.version != prepared.policy.version:
            # No cached preparation policy may authorize a deletion. A version
            # change means the horizon moved while this batch was on disk.
            raise _Defer(
                FoldOutcome.STALE_REPREPARE,
                f"policy moved from version {prepared.policy.version} to "
                f"{locked.version} during preparation",
            )
        now = as_utc(state.now)

        basis = _lock_rows(db, blunder_ids=blunder_ids, budget=budget)

        budget.before("latest review read")
        bases = _latest_reviews(db, blunder_ids=blunder_ids)
        for blunder_id in blunder_ids:
            live = bases.get(blunder_id, _ReviewBasis()).review_id
            if basis[blunder_id] != live:
                # The reader raises on exactly this, so folding into it would add
                # evidence to a window nobody agrees on. Repair the basis first.
                raise RetentionInvariantError(
                    f"blunder {blunder_id} folded review basis {basis[blunder_id]} "
                    f"disagrees with latest review {live}; reconcile before folding"
                )

        # The recheck reads the DATABASE, under the locks, in a fresh statement.
        # Not the export: the export proves what was written to disk, and only
        # this can prove what is still true.
        budget.before("candidate recheck")
        fresh = [
            _row_from(record)
            for record in db.execute(
                _eligible_rows_query(
                    db, user_id=user_id, policy=locked, blunder_ids=blunder_ids
                ).where(BlunderOpportunityEvent.id.in_(event_ids))
            ).all()
        ]
        if {row.id for row in fresh} != set(event_ids) or len(fresh) != len(event_ids):
            raise _Defer(
                FoldOutcome.STALE_REPREPARE,
                f"{len(event_ids) - len(fresh)} candidate rows are no longer "
                "eligible; reprepare outside the locks",
            )
        if canonical_rowset_hash(fresh) != prepared.rowset_hash:
            raise _Defer(
                FoldOutcome.STALE_REPREPARE,
                "candidate facts changed after the export was verified",
            )

        contributions = _contributions(fresh, bases=bases)
        max_started_at = max(as_utc(row.session_started_at) for row in fresh)

        budget.before("targeted watermark read")
        discarded = _discarded_served_at(db, user_id=user_id, pairs=prepared.pairs)

        budget.before("summary transfer")
        updated = db.execute(_summary_update(contributions)).rowcount
        if updated != len(contributions):
            raise RetentionInvariantError(
                f"expected to add folded evidence to {len(contributions)} "
                f"summaries for user {user_id}, updated {updated}"
            )

        # The event delete guard refuses a frozen row to everything except this
        # transaction and a parent-blunder cascade. Armed immediately before the
        # DELETE and cleared immediately after, so the allowance covers one
        # statement rather than the rest of the transaction.
        budget.before("fold mode")
        _set_config(db, "ghostreplay.srs_fold_mode", "transfer")
        budget.before("raw delete")
        deleted = db.execute(
            delete(BlunderOpportunityEvent)
            .where(BlunderOpportunityEvent.id.in_(event_ids))
            .execution_options(synchronize_session=False)
        ).rowcount
        _set_config(db, "ghostreplay.srs_fold_mode", "")
        if deleted != len(event_ids):
            raise RetentionInvariantError(
                f"fold for user {user_id} deleted {deleted} of {len(event_ids)} "
                "validated rows"
            )

        budget.before("manifest insert")
        db.execute(
            insert(OpportunityFoldBatch.__table__).values(
                batch_id=exported.batch_id,
                user_id=user_id,
                artifact_uri=str(exported.path),
                artifact_sha256=exported.artifact_sha256,
                rowset_hash=exported.rowset_hash,
                hash_version=exported.hash_version,
                row_count=exported.row_count,
                policy_version=locked.version,
                committed_at=now,
                expires_at=now + RECOVERY_WINDOW,
                max_session_started_at=max_started_at,
                targeted_discarded_max_served_at=discarded,
                contributions=[item.as_json() for item in contributions],
            )
        )

        # Computed in Python against the values read under this row's own
        # FOR UPDATE, not with GREATEST: SQLite has no GREATEST, and nothing else
        # can have changed the row since we locked it, so the maximum is exact.
        prefix = state.folded_through_started_at
        new_prefix = (
            max_started_at if prefix is None
            else max(as_utc(prefix), max_started_at)
        )
        watermark = state.targeted_discarded_max_served_at
        if discarded is None:
            new_watermark = watermark
        elif watermark is None:
            new_watermark = discarded
        else:
            new_watermark = max(as_utc(watermark), discarded)
        budget.before("prefix advance")
        db.execute(
            update(UserOpportunityRetentionState)
            .where(UserOpportunityRetentionState.user_id == user_id)
            .values(
                folded_through_started_at=new_prefix,
                targeted_discarded_max_served_at=new_watermark,
                sweep_progress_started_at=max_started_at,
            )
            .execution_options(synchronize_session=False)
        )

        # The recovery-window anchor. Unconditional on purpose, and the WHERE is
        # the whole guard: two first folds racing cannot both write it, because
        # the second finds no row to update.
        #
        # There used to be an `if state.first_fold_committed_at is None` around
        # this, and it was a race. That read happened before the manifest insert,
        # so a restore that cleared the anchor in between (nothing was folded at
        # the moment it counted) would leave THIS batch committed with no anchor
        # at all — an unrestored batch with no deadline, and a later fold stamping
        # a window that its manifest expires inside. Re-running the UPDATE
        # unconditionally matches no row in the normal case and costs a
        # primary-key lookup; the interlock's other half is the table lock the
        # clearing side takes (app.opportunity_fold_recovery).
        budget.before("recovery anchor")
        db.execute(
            update(OpportunityRetentionPolicy)
            .where(
                OpportunityRetentionPolicy.id == POLICY_ID,
                OpportunityRetentionPolicy.first_fold_committed_at.is_(None),
            )
            .values(first_fold_committed_at=now)
            .execution_options(synchronize_session=False)
        )

        budget.before("commit")
        commit_attempted = True
        db.commit()
    except _Defer as deferred:
        _rollback_or_discard(db, connection)
        _drop_orphan(exported, committed=commit_attempted)
        logger.info(
            "fold batch %s for user %s deferred: %s",
            exported.batch_id, user_id, deferred.detail,
        )
        return FoldResult(
            outcome=deferred.outcome, user_id=user_id, batch_id=exported.batch_id,
            lock_seconds=deadline.elapsed, detail=deferred.detail,
        )
    except FoldDeadlineExceeded as expired:
        _rollback_or_discard(db, connection)
        _drop_orphan(exported, committed=commit_attempted)
        logger.warning("fold batch %s for user %s: %s",
                       exported.batch_id, user_id, expired)
        return FoldResult(
            outcome=FoldOutcome.DEADLINE_EXCEEDED, user_id=user_id,
            batch_id=exported.batch_id, lock_seconds=deadline.elapsed,
            detail=str(expired),
        )
    except OperationalError as err:
        outcome = _contention_outcome(err)
        if outcome is None:
            _rollback_or_discard(db, connection)
            _drop_orphan(exported, committed=commit_attempted)
            raise
        _rollback_or_discard(db, connection)
        _drop_orphan(exported, committed=commit_attempted)
        logger.info("fold batch %s for user %s hit contention: %s",
                    exported.batch_id, user_id, err)
        return FoldResult(
            outcome=outcome, user_id=user_id, batch_id=exported.batch_id,
            lock_seconds=deadline.elapsed, detail=str(err),
        )
    except Exception:
        _rollback_or_discard(db, connection)
        _drop_orphan(exported, committed=commit_attempted)
        raise

    return FoldResult(
        outcome=FoldOutcome.FOLDED,
        user_id=user_id,
        batch_id=exported.batch_id,
        rows_deleted=len(event_ids),
        blunders=len(blunder_ids),
        lock_seconds=deadline.elapsed,
    )


def _contention_outcome(err: OperationalError) -> FoldOutcome | None:
    """Map the two contention SQLSTATEs; let everything else propagate.

    ``55P03`` is a NOWAIT/lock_timeout refusal — a review or another writer holds
    a row, so defer. ``57014`` is a cancellation, which in this transaction can
    only come from the statement timeout the deadline armed. Any other
    OperationalError is a real fault and must not be degraded into a routine
    "skipped" line nobody reads.
    """
    orig = getattr(err, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate == "55P03":
        return FoldOutcome.DEFERRED_ROW_BUSY
    if sqlstate == "57014":
        return FoldOutcome.DEADLINE_EXCEEDED
    return None


def _drop_orphan(exported: ExportedBatch, *, committed: bool) -> None:
    """Remove the artifact of a batch that provably committed nothing.

    ``committed`` is "a COMMIT was ATTEMPTED", not "a COMMIT succeeded", and the
    distinction is the point: a connection that dies during COMMIT leaves the
    outcome genuinely unknown, and deleting the artifact of a batch that did
    commit would delete its recovery. Those are left for the expiry sweep, which
    removes them on age without having to decide anything.
    """
    if committed:
        return
    try:
        discard_export(exported.path)
    except FoldExportError:
        logger.warning("could not remove orphaned fold export %s", exported.path)


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


def fold_user_batch(
    engine, *, user_id: int, limits: FoldLimits | None = None,
    max_pairs: int | None = None,
    activity_check: Callable[[], bool] | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> FoldResult:
    """Prepare, export and fold ONE bounded batch for one user.

    Takes an Engine and opens its own connection, because this is the compactor
    and it must not share a connection with a request. That connection may be
    discarded outright if a rollback cannot complete, which is not something to
    do to a session somebody else is using.

    Never raises for contention, a disabled policy or an empty candidate set —
    those are outcomes. It DOES raise ``RetentionInvariantError``: a missing
    summary or a disagreeing review basis means folded evidence is already
    unaccounted for, and continuing to delete rows on top of that would deepen a
    loss instead of reporting it.
    """
    limits = limits or FoldLimits()
    max_pairs = max_pairs or limits.max_pairs
    if stop_check is not None and stop_check():
        return FoldResult(FoldOutcome.DEFERRED_BUDGET, user_id)
    if activity_check is not None and activity_check():
        return FoldResult(FoldOutcome.DEFERRED_ACTIVITY, user_id)
    connection = engine.connect()
    db = Session(bind=connection)
    started = time.monotonic()
    try:
        try:
            prepared = _prepare(db, user_id=user_id, limits=limits, max_pairs=max_pairs)
        except _Defer as deferred:
            db.rollback()
            return FoldResult(outcome=deferred.outcome, user_id=user_id,
                              prepare_seconds=time.monotonic() - started,
                              detail=deferred.detail)
        prepare_seconds = time.monotonic() - started

        if stop_check is not None and stop_check():
            return FoldResult(FoldOutcome.DEFERRED_BUDGET, user_id)

        # Outside every lock, and before the transaction is even opened.
        batch_id = uuid.uuid4()
        export_started = time.monotonic()
        try:
            exported = write_export(
                prepared.rows, batch_id=batch_id, user_id=user_id,
                prepared_at=prepared.preview_now,
            )
        except FoldExportError as err:
            # No export, no deletion. A slow or full disk costs a sweep, not a row.
            logger.warning("fold export for user %s failed: %s", user_id, err)
            return FoldResult(
                outcome=FoldOutcome.EXPORT_FAILED, user_id=user_id,
                prepare_seconds=prepare_seconds,
                export_seconds=time.monotonic() - export_started, detail=str(err),
            )
        export_seconds = time.monotonic() - export_started

        if stop_check is not None and stop_check():
            _drop_orphan(exported, committed=False)
            return FoldResult(FoldOutcome.DEFERRED_BUDGET, user_id,
                              prepare_seconds=prepare_seconds, export_seconds=export_seconds)
        if activity_check is not None and activity_check():
            _drop_orphan(exported, committed=False)
            return FoldResult(FoldOutcome.DEFERRED_ACTIVITY, user_id,
                              prepare_seconds=prepare_seconds, export_seconds=export_seconds)

        result = _transfer(db, connection, prepared=prepared, exported=exported,
                           limits=limits)
        if result.folded:
            # AFTER the commit, never inside the lock interval. The two timings
            # are reported apart because they answer different questions: a slow
            # disk inflates the export and says nothing about lock pressure, and
            # only the second number is measured against the 250 ms target.
            logger.info(
                "folded batch %s for user %s: %s rows over %s blunders "
                "(prepare %.3fs, export %.3fs, lock %.3fs)",
                result.batch_id, user_id, result.rows_deleted, result.blunders,
                prepare_seconds, export_seconds, result.lock_seconds,
            )
        return FoldResult(
            outcome=result.outcome, user_id=result.user_id, batch_id=result.batch_id,
            rows_deleted=result.rows_deleted, blunders=result.blunders,
            prepare_seconds=prepare_seconds, export_seconds=export_seconds,
            lock_seconds=result.lock_seconds, detail=result.detail,
        )
    finally:
        db.close()
        connection.close()


def eligible_users(engine, *, limit: int | None = 1000) -> list[int]:
    """Users with at least one raw row old enough to fold. A cheap prefilter.

    Deliberately skips the target-pin check, which is per pair and belongs in
    preparation: a user whose every candidate turns out to be pinned simply
    reports ``nothing_eligible`` and drops out of the rotation. Doing the
    expensive check here would make discovery scale with decision history.
    """
    with Session(bind=engine) as db:
        policy = load_policy(db)
        if not policy.cleanup_enabled:
            return []
        rows = db.execute(
            select(Blunder.user_id)
            .select_from(BlunderOpportunityEvent)
            .join(Blunder, Blunder.id == BlunderOpportunityEvent.blunder_id)
            .join(GameSession, GameSession.id == BlunderOpportunityEvent.session_id)
            .where(
                GameSession.user_id == Blunder.user_id,
                GameSession.started_at <= foldable_cutoff(db, policy=policy),
            )
            .group_by(Blunder.user_id)
            .order_by(func.min(GameSession.started_at))
            .limit(limit)
        ).scalars().all()
    return [int(user_id) for user_id in rows]


def sweep(
    engine, *, user_ids: list[int] | None = None, limits: FoldLimits | None = None,
    max_batches: int = 50,
) -> SweepReport:
    """Rotate over users, one bounded batch at a time, adapting size DOWNWARD.

    Rotation and the per-user cooldown are one mechanism, not two: a user with
    work left goes to the back of the queue and cannot be revisited for
    ``user_cooldown``, while whoever is ready gets folded in the meantime.
    Immediately reacquiring the same user's locks would let a compactor that is
    always ready starve a publication that has been waiting for the
    retention-state row since before the batch started. A user whose batch came
    up short without hitting the blunder cap has nothing left and simply leaves
    the rotation.

    Batch size only ever shrinks. A batch that overran the hold target is
    evidence that this user's rows are expensive right now; a batch that fit is
    NOT evidence that a bigger one would, so growth is a benchmark decision under
    the same ceiling (``g-srs-retention-gates``), not a runtime guess.

    WHEN a sweep runs, and how it avoids active users, belongs to
    ``g-srs-cleanup-schedule``. This is the primitive it drives.
    """
    limits = limits or FoldLimits()
    report = SweepReport()
    pending = list(user_ids if user_ids is not None else eligible_users(engine))
    report.candidates = len(pending)
    sizes: dict[int, int] = {}
    ready_at: dict[int, float] = {}
    attempts = 0
    # Two attempts per allowed batch: a deferral costs an attempt without
    # producing a batch, and an unbounded loop over a permanently busy user is
    # exactly the starvation this rotation exists to avoid.
    attempt_ceiling = max_batches * 2

    while pending and report.batches < max_batches and attempts < attempt_ceiling:
        moment = time.monotonic()
        # The first user whose cooldown has elapsed — so a cooling user is
        # skipped over rather than waited on while somebody else is ready.
        user_id = next(
            (candidate for candidate in pending
             if ready_at.get(candidate, 0.0) <= moment),
            None,
        )
        if user_id is None:
            # Everyone is cooling. Sleep until the earliest one is due rather
            # than spinning through the queue for the rest of the interval.
            time.sleep(max(0.0, min(ready_at[candidate] for candidate in pending)
                           - moment))
            continue
        pending.remove(user_id)
        attempts += 1
        size = sizes.get(user_id, limits.max_pairs)
        try:
            result = fold_user_batch(engine, user_id=user_id, limits=limits,
                                     max_pairs=size)
        except Exception as err:  # noqa: BLE001 - one user must not end the sweep
            logger.exception("fold sweep failed for user %s", user_id)
            report.errors.append(f"user {user_id}: {err}")
            continue
        report.record(result)
        ready_at[user_id] = time.monotonic() + limits.user_cooldown

        # Two different signals. Missing the p99 HOLD TARGET is not a failure —
        # the batch committed — it is the evidence that the next one should be
        # smaller. Missing the hard DEADLINE is a failure, and a user that cannot
        # meet it even at the minimum batch is not folded at all this sweep:
        # retrying it would spend the whole budget losing.
        if (
            result.outcome is FoldOutcome.DEADLINE_EXCEEDED
            or result.lock_seconds > limits.hold_target
        ) and size > limits.min_pairs:
            sizes[user_id] = max(limits.min_pairs, size // 2)
            logger.info("fold batch size for user %s reduced to %s after %.3fs",
                        user_id, sizes[user_id], result.lock_seconds)
        if result.outcome is FoldOutcome.DEADLINE_EXCEEDED and size <= limits.min_pairs:
            logger.warning(
                "fold for user %s cannot meet the %.3fs deadline at %s pairs; "
                "stopping this user for this sweep",
                user_id, limits.transaction_deadline, size,
            )
            continue
        # A short batch that did NOT hit the blunder cap means the candidate
        # query ran out, not that the batch was truncated — so this user has
        # nothing left and requeueing them would spend a whole cooldown
        # rediscovering that. When the cap bound, the shortfall says nothing
        # about what remains and the user goes back in the queue.
        if (
            result.folded
            and result.rows_deleted < size
            and result.blunders < limits.max_blunders
        ):
            continue
        if result.folded or result.outcome in TRANSIENT_OUTCOMES:
            pending.append(user_id)
    return report
