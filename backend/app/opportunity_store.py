"""Storage interfaces for SRS opportunity evidence: raw rows and folded summaries.

The raw ``blunder_opportunity_events`` rows and the per-blunder
``blunder_opportunity_summaries`` row are two halves of ONE number. A reader
that sees only one half is wrong, so every counter this bead exposes is read
from both in a single statement (``app.srs_opportunity``), and every writer that
deletes a raw row increments the matching summary in the same transaction.

Summaries are created EAGERLY — at blunder insertion, by migration backfill, and
(before readiness only) by a review that finds its row missing. Lazy creation
with ``COALESCE(..., 0)`` would be cheaper, but it cannot distinguish "never
folded" from "summary lost after the raw rows were deleted" without inventing
another durable marker. Eager creation makes absence itself the alarm.

Nothing here folds or deletes evidence. The fold transfer and its finite recovery
path belong to g-srs-fold-recovery; this module owns the interfaces it writes
through and the lifecycle guards that protect what it produced.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import (
    BlunderOpportunitySummary,
    GameSession,
    UserOpportunityRetentionState,
)
from app.opportunity_retention import (
    RetentionInvariantError,
    RetentionPolicy,
    load_policy,
    session_frozen_clause,
)


@dataclass(frozen=True)
class FoldedCounters:
    """The retained half of a blunder's broad counters.

    ``present`` is not decoration. After readiness a missing summary is an
    invariant failure, and only the reader can tell the difference between a row
    of zeros and no row at all.
    """

    present: bool = False
    folded_eligible_count: int = 0
    folded_opportunities_since_review: int = 0
    folded_reached_since_review: int = 0
    latest_review_id: int | None = None


def _insert_ignore(db: Session, model, values: dict):
    """Insert-if-absent on the dialects this project runs on.

    ON CONFLICT DO NOTHING rather than a read-then-write: the whole point of
    these helpers is that a concurrent review, backfill or blunder insert may
    legitimately create the same row first, and its values must survive. A
    generic dialect falls back to a guarded read, which is racy but is only ever
    exercised by hypothetical third dialects, not by SQLite or PostgreSQL.
    """
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(model)
    elif dialect == "sqlite":
        statement = sqlite_insert(model)
    else:  # pragma: no cover - neither production nor test dialect
        if db.get(model, tuple(values.values())[0]) is None:
            db.add(model(**values))
        return
    db.execute(statement.values(**values).on_conflict_do_nothing())


def ensure_summary(db: Session, blunder_id: int) -> None:
    """Create this blunder's ZERO summary if it has none.

    Called from the single blunder-insertion helper, before the caller's final
    evidence cursor bump, so auto and manual recording share one code path and a
    returned EXISTING blunder never has its totals reset.
    """
    _insert_ignore(db, BlunderOpportunitySummary, {"blunder_id": blunder_id})


def ensure_retention_state(db: Session, user_id: int) -> None:
    """Create this user's retention-state row if absent, with no prefix set.

    A NULL ``folded_through_started_at`` is the correct initial value: nothing
    has been folded, so nothing is frozen by prefix. It must not be seeded with
    a timestamp, which would freeze history that was never folded.

    Called by the whole-user purge, which needs the row to EXIST so its deferred
    completeness assertion — an AFTER DELETE trigger on this table — actually
    fires for a user who predates the migration or was created without one.

    Deliberately NOT called from the blunder-recording path, unlike
    :func:`ensure_summary`. The two rows are eager for different reasons. A
    missing SUMMARY is ambiguous — "never folded" or "lost after deletion?" — so
    it must exist before anything can fold. A missing retention-state row is not
    ambiguous at all: no row means no prefix, which every guard already reads as
    "nothing folded", the conservative answer. Making blunder recording depend
    on a ``users`` row it does not otherwise need would trade a real write path
    against bookkeeping that can safely arrive later. The migration backfills
    every existing user, and g-srs-retain-rollout owns full coverage before it
    enables folding.
    """
    _insert_ignore(db, UserOpportunityRetentionState, {"user_id": user_id})


def load_folded_counters(
    db: Session, blunder_ids: list[int]
) -> dict[int, FoldedCounters]:
    """Standalone summary read, for callers outside the combined counter query.

    ``app.srs_opportunity.load_opportunity_counters`` does NOT use this: it joins
    the summary into its own statement so live and retained halves share one
    snapshot. This exists for writers and repair paths that need the retained
    half alone.
    """
    if not blunder_ids:
        return {}
    unique_ids = list(dict.fromkeys(blunder_ids))
    rows = db.execute(
        select(BlunderOpportunitySummary).where(
            BlunderOpportunitySummary.blunder_id.in_(unique_ids)
        )
    ).scalars()
    return {
        row.blunder_id: FoldedCounters(
            present=True,
            folded_eligible_count=int(row.folded_eligible_count),
            folded_opportunities_since_review=int(
                row.folded_opportunities_since_review
            ),
            folded_reached_since_review=int(row.folded_reached_since_review),
            latest_review_id=row.latest_review_id,
        )
        for row in rows
    }


def session_evidence_frozen(
    db: Session,
    *,
    session_id: uuid.UUID,
    user_id: int,
    policy: RetentionPolicy | None = None,
) -> bool:
    """Is this session's opportunity evidence past the mutation boundary?

    One statement, evaluated with the DATABASE clock, so callers must invoke it
    AFTER taking whatever row lock protects the write they are about to make. A
    transaction that started before the deadline and then blocked on that lock
    reaches this test with a clock sample taken after it woke up, and is
    rejected — which is the entire point of not using transaction-start time.

    A session belonging to a different user returns False rather than raising:
    ownership is the caller's check, and conflating "not yours" with "frozen"
    would hand a caller a misleading error. A missing retention-state row simply
    contributes no prefix arm, leaving the age arm to decide.
    """
    resolved = policy if policy is not None else load_policy(db)
    frozen = db.execute(
        select(session_frozen_clause(db, policy=resolved))
        .select_from(GameSession)
        .outerjoin(
            UserOpportunityRetentionState,
            UserOpportunityRetentionState.user_id == user_id,
        )
        .where(GameSession.id == session_id, GameSession.user_id == user_id)
    ).scalar()
    return bool(frozen)


def record_review_basis(
    db: Session,
    *,
    blunder_id: int,
    review_id: int,
    reviewed_at: datetime,
    session_id: uuid.UUID,
    policy: RetentionPolicy,
) -> None:
    """Reset the folded SINCE-REVIEW counters and stamp the new review basis.

    Callers must already hold this blunder's ``FOR NO KEY UPDATE`` lock, which
    is the lock two concurrent reviews of the same blunder serialize on. No user
    advisory lock is taken or inherited here: a review must be accepted at every
    session age, and making it queue behind the evidence graph lock is exactly
    what would make an old-session review fail.

    Resetting to zero is unconditional and correct even when ``session_id`` is an
    old session. Every event that has ALREADY been folded necessarily predates
    this review, because folding only ever touches sessions past the mutation
    boundary while this review is happening now. The lifetime total is retained
    for the same reason: it is not a window.

    Before readiness this also INITIALIZES a missing summary, which is the only
    sanctioned place a review may do so — during rollout the backfill may not
    have reached this blunder yet. After readiness a missing row means folded
    evidence was lost, so it is left missing for the reader to reject rather than
    papered over with a fresh zero row that would erase the evidence of loss.
    """
    if not policy.readiness:
        ensure_summary(db, blunder_id)
    db.execute(
        update(BlunderOpportunitySummary)
        .where(BlunderOpportunitySummary.blunder_id == blunder_id)
        .values(
            folded_opportunities_since_review=0,
            folded_reached_since_review=0,
            latest_review_id=review_id,
            latest_review_at=reviewed_at,
            latest_review_session_id=session_id,
            policy_version=policy.version,
        )
    )


def frozen_session_ids(
    db: Session,
    *,
    user_id: int,
    session_ids: list[uuid.UUID],
    policy: RetentionPolicy | None = None,
) -> set[uuid.UUID]:
    """Batch form of :func:`session_evidence_frozen` for repair-style loops.

    One statement over the whole batch rather than one per session, but with the
    same post-lock database-clock semantics: the caller must already hold
    whatever lock protects the writes it is about to make, and must not cache the
    result across a commit — a batch that spans the deadline has to re-ask.
    """
    if not session_ids:
        return set()
    resolved = policy if policy is not None else load_policy(db)
    rows = db.execute(
        select(GameSession.id)
        .outerjoin(
            UserOpportunityRetentionState,
            UserOpportunityRetentionState.user_id == user_id,
        )
        .where(
            GameSession.id.in_(session_ids),
            GameSession.user_id == user_id,
            session_frozen_clause(db, policy=resolved),
        )
    ).scalars()
    return set(rows)


# The latest review of one blunder, ranked EXACTLY as ``load_opportunity_counters``
# ranks it. Any other tie-break would let the reconciler and the reader disagree
# about which of two same-instant reviews is current, which is the disagreement
# the reconciler exists to eliminate.
_LATEST_REVIEW = (
    "SELECT r2.id FROM blunder_reviews r2 WHERE r2.blunder_id = {blunder} "
    "ORDER BY r2.reviewed_at DESC, r2.id DESC LIMIT 1"
)


def review_basis_gaps(db: Session) -> dict[str, int]:
    """Read-only readiness audit: what still stands between here and readiness.

    ``missing_summaries`` counts blunders with no summary row at all;
    ``basis_mismatches`` counts summaries whose ``latest_review_id`` is not the
    blunder's live latest review. Readiness means BOTH are zero — not "the
    migration ran". Flipping the switch with either nonzero turns the reader's
    invariant check into a 500 on the ghost-move path.
    """
    missing = db.execute(
        text(
            "SELECT count(*) FROM blunders b WHERE NOT EXISTS ("
            "  SELECT 1 FROM blunder_opportunity_summaries s WHERE s.blunder_id = b.id)"
        )
    ).scalar_one()
    mismatched = db.execute(
        text(
            "SELECT count(*) FROM blunder_opportunity_summaries s "
            " WHERE COALESCE(s.latest_review_id, -1) <> COALESCE(("
            + _LATEST_REVIEW.format(blunder="s.blunder_id")
            + "), -1)"
        )
    ).scalar_one()
    return {"missing_summaries": int(missing), "basis_mismatches": int(mismatched)}


def reconcile_review_basis(
    db: Session, *, create_summaries: bool | None = None
) -> dict[str, int]:
    """Re-runnable form of the migration's eager backfill. Idempotent.

    The migration cannot be the last word. Between it and the end of the deploy,
    old application instances keep inserting blunders (no summary) and reviews
    (no basis), so the database drifts out of the state the migration left. This
    runs the same two statements again, as often as needed, until
    :func:`review_basis_gaps` reports zeros and readiness can be flipped.

    Resetting the since-review counters alongside the basis is deliberate, and is
    the same reset :func:`record_review_basis` performs: a basis that moved means
    a new review opened a new window, and folded evidence attributed to the old
    window does not belong in it. Before folding exists those counters are all
    zero anyway, which is why this is safe to run during the rollout.

    Summary CREATION stops at readiness. Before readiness a missing summary is a
    rollout gap and inserting a zero row closes it. After readiness it is the
    loss alarm this whole design exists to raise — "summary lost after its raw
    rows were deleted" — and a zero row would silently overwrite folded totals
    with zeros and erase the only evidence that anything went wrong. Basis
    REPAIR still runs, because correcting an existing row's review pointer
    destroys nothing. ``create_summaries=True`` forces creation anyway, for a
    recovery that has separately established those blunders never folded.

    Does NOT commit. The caller decides the transaction, so a large reconcile can
    be chunked and a dry run can roll back.
    """
    if create_summaries is None:
        create_summaries = not load_policy(db).readiness
    if not create_summaries:
        missing = review_basis_gaps(db)["missing_summaries"]
        if missing:
            raise RetentionInvariantError(
                f"{missing} blunders have no opportunity summary after readiness; "
                "creating zero rows would overwrite folded totals and erase the "
                "loss. Recover the summaries, or pass create_summaries=True once "
                "you have established that they never folded"
            )
        return {"summaries_created": 0, "basis_repaired": _repair_basis(db)}
    inserted = db.execute(
        text(
            "INSERT INTO blunder_opportunity_summaries "
            "  (blunder_id, latest_review_id, latest_review_at, latest_review_session_id) "
            "SELECT b.id, r.id, r.reviewed_at, r.session_id "
            "  FROM blunders b "
            "  LEFT JOIN blunder_reviews r ON r.id = ("
            + _LATEST_REVIEW.format(blunder="b.id")
            + ") WHERE NOT EXISTS ("
            "  SELECT 1 FROM blunder_opportunity_summaries s WHERE s.blunder_id = b.id) "
            "ON CONFLICT DO NOTHING"
        )
    ).rowcount
    return {"summaries_created": int(inserted), "basis_repaired": _repair_basis(db)}


def _repair_basis(db: Session) -> int:
    """Point every summary at its blunder's live latest review. Idempotent.

    Safe at any readiness, unlike summary creation: it rewrites a pointer on a
    row that already exists and never invents a row that does not.
    """
    return int(db.execute(
        text(
            "UPDATE blunder_opportunity_summaries AS s SET "
            "  latest_review_id = (" + _LATEST_REVIEW.format(blunder="s.blunder_id") + "), "
            "  latest_review_at = (SELECT r.reviewed_at FROM blunder_reviews r "
            "    WHERE r.id = (" + _LATEST_REVIEW.format(blunder="s.blunder_id") + ")), "
            "  latest_review_session_id = (SELECT r.session_id FROM blunder_reviews r "
            "    WHERE r.id = (" + _LATEST_REVIEW.format(blunder="s.blunder_id") + ")), "
            "  folded_opportunities_since_review = 0, "
            "  folded_reached_since_review = 0 "
            " WHERE COALESCE(s.latest_review_id, -1) <> COALESCE(("
            + _LATEST_REVIEW.format(blunder="s.blunder_id")
            + "), -1)"
        )
    ).rowcount)
