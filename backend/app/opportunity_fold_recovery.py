"""Finite recovery for folded SRS opportunity evidence (g-srs-fold-recovery).

Folding deletes raw rows. This module is the other half of that bargain: for
seven days after the FIRST deletion anywhere, every deleted row can be put back
exactly as it was, and after those seven days it provably cannot.

Both halves are load-bearing. A recovery path that never expired would be a
permanent raw-history archive wearing a different name — the unbounded growth
this epic exists to remove — so the artifacts, the manifests and the rollback
eligibility all end together, and the schema downgrade that depends on them
starts refusing at the same moment (migration ``20260920_01``).

What restoration actually restores
----------------------------------

The exact original facts: ids, owners, pair keys, flags and timestamps including
a NULL ``occurred_at``. Not a reconstruction — a reconstruction would silently
normalize the legacy rows the counter predicates treat specially.

It restores them ALONGSIDE whatever happened since, which is the part that makes
this finite window worth having rather than a fiction:

* **Intervening reviews.** A review after the fold reset that summary's
  since-review counters to zero and moved its basis. The manifest records the
  basis each contribution was folded under, so a restore that finds a different
  basis subtracts only the lifetime total — the one counter no review resets.
  Recomputing the deltas from the restored rows instead would be wrong here, and
  silently so.
* **Intervening writes.** New raw rows for the same blunders are untouched; the
  restore only re-inserts ids that are absent.
* **Intervening purges.** A deleted account, session or blunder stays deleted. A
  restore that resurrected them would undo a user's deletion request to satisfy
  a rollback, so rows whose parents are gone are skipped and counted.

Restoration is an OPERATOR action, not a request path. It waits for its locks
instead of skipping, and it refuses to run while folding is still enabled: a
compactor deleting rows behind a restore would produce a state neither of them
describes.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import (
    Blunder,
    BlunderOpportunityEvent,
    BlunderOpportunitySummary,
    GameSession,
    OpportunityFoldBatch,
    OpportunityRetentionPolicy,
    UserOpportunityRetentionState,
)
from app.opportunity_fold import RECOVERY_WINDOW
from app.opportunity_fold_export import (
    EXPORT_DIR_ENV,
    FoldExportError,
    discard_export,
    export_dir,
    read_export,
)
from app.opportunity_retention import (
    POLICY_ID,
    RetentionInvariantError,
    database_clock,
)
from app.srs_math import as_utc

logger = logging.getLogger(__name__)

# How long a restore waits for one user's locks. Unlike the compactor, which
# skips a busy user and comes back, a restore that skipped one would leave a
# PARTIAL rollback — so it waits, and the bound exists only so a wedged backend
# surfaces as an error instead of a hang.
RESTORE_LOCK_WAIT = "30s"


class RecoveryExpired(RuntimeError):
    """The seven-day window has closed. Nothing here can put the raw rows back.

    Announced in advance (``scripts/RECOVER_SRS_FOLD.md``) precisely so it is
    never first encountered as an exception during an incident.
    """


class RecoveryRefused(RuntimeError):
    """The preconditions for a restore are not met, and forcing them would corrupt."""


@dataclass
class RestoreReport:
    """What a restore run actually did. Skips are reported, never hidden."""

    batches: int = 0
    rows_restored: int = 0
    rows_skipped_missing_parent: int = 0
    rows_skipped_present: int = 0
    summaries_adjusted: int = 0
    summaries_missing: int = 0
    since_review_left_alone: int = 0
    users: list[int] = field(default_factory=list)
    # A batch that could not be put back, and why. It stays unrestored and a
    # later run retries it; it does not strand the batches behind it.
    failures: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    anchor_cleared: bool = False


@dataclass(frozen=True)
class FoldStatus:
    """The recovery clock, as the database sees it."""

    first_fold_committed_at: datetime | None
    now: datetime
    deadline: datetime | None
    expired: bool
    unrestored_batches: int
    restored_batches: int
    expiring_batches: int

    @property
    def remaining(self) -> timedelta | None:
        if self.deadline is None:
            return None
        return self.deadline - self.now


def fold_status(engine) -> FoldStatus:
    """Read the recovery clock. Cheap, read-only, safe to run at any time."""
    with Session(bind=engine) as db:
        now = as_utc(db.execute(select(database_clock(db))).scalar_one())
        anchor = db.execute(
            select(OpportunityRetentionPolicy.first_fold_committed_at).where(
                OpportunityRetentionPolicy.id == POLICY_ID
            )
        ).scalar()
        unrestored = db.execute(
            select(func.count()).select_from(OpportunityFoldBatch).where(
                OpportunityFoldBatch.restored_at.is_(None)
            )
        ).scalar_one()
        restored = db.execute(
            select(func.count()).select_from(OpportunityFoldBatch).where(
                OpportunityFoldBatch.restored_at.is_not(None)
            )
        ).scalar_one()
        expiring = db.execute(
            select(func.count()).select_from(OpportunityFoldBatch).where(
                OpportunityFoldBatch.expires_at <= now
            )
        ).scalar_one()
    deadline = None if anchor is None else as_utc(anchor) + RECOVERY_WINDOW
    return FoldStatus(
        first_fold_committed_at=None if anchor is None else as_utc(anchor),
        now=now,
        deadline=deadline,
        expired=deadline is not None and now > deadline,
        unrestored_batches=int(unrestored),
        restored_batches=int(restored),
        expiring_batches=int(expiring),
    )


def _require_recoverable(db: Session) -> datetime:
    """Refuse a restore that is out of time or racing a live compactor.

    The expiry check comes first and is absolute. Inside the window no manifest
    can have expired yet — a batch committed on day three expires on day ten,
    well after the window closes on day seven — which is what lets a restore
    treat the surviving manifests as the COMPLETE record of what was deleted and
    move the fold prefix back to exactly the right place. Past the window that
    stops being true, and a prefix computed from a partial record would unfreeze
    sessions whose rows are gone for good.
    """
    now = as_utc(db.execute(select(database_clock(db))).scalar_one())
    row = db.execute(
        select(
            OpportunityRetentionPolicy.first_fold_committed_at,
            OpportunityRetentionPolicy.cleanup_enabled,
        ).where(OpportunityRetentionPolicy.id == POLICY_ID)
    ).first()
    if row is None:
        raise RecoveryRefused(
            "no retention policy row; this database was not built by the migrations"
        )
    if row.cleanup_enabled:
        raise RecoveryRefused(
            "folding is still enabled; set cleanup_enabled = false and let the "
            "compactor drain before restoring, or a fold will delete rows behind "
            "the restore"
        )
    anchor = row.first_fold_committed_at
    if anchor is None:
        return now
    deadline = as_utc(anchor) + RECOVERY_WINDOW
    if now > deadline:
        raise RecoveryExpired(
            f"the SRS fold recovery window closed at {deadline} (first fold "
            f"{as_utc(anchor)}); the exports that could refill the deleted raw "
            "rows have been pruned"
        )
    return now


def _load_verified(batch) -> object:
    """Read the artifact and prove it is THIS batch, unmodified.

    Three questions, and the file can fail any one of them while passing the rest:
    are the bytes the bytes we wrote, are the rows the rows we deleted, and is
    this the right batch at all. A restore that skipped the last one would
    happily refill a different batch's rows and call the rollback complete.

    Whether this release can reproduce the artifact's canonical encoding at all
    is settled earlier, by :func:`read_export`, which refuses a version it does
    not implement rather than comparing two incomparable digests.
    """
    path = Path(batch.artifact_uri)
    loaded = read_export(path)
    if loaded.artifact_sha256 != batch.artifact_sha256:
        raise FoldExportError(
            f"fold export {path} has digest {loaded.artifact_sha256}, manifest "
            f"records {batch.artifact_sha256}"
        )
    if loaded.rowset_hash != batch.rowset_hash:
        raise FoldExportError(
            f"fold export {path} describes rowset {loaded.rowset_hash}, manifest "
            f"records {batch.rowset_hash}"
        )
    if loaded.batch_id != batch.batch_id or loaded.user_id != batch.user_id:
        raise FoldExportError(
            f"fold export {path} belongs to batch {loaded.batch_id} / user "
            f"{loaded.user_id}, not {batch.batch_id} / {batch.user_id}"
        )
    if loaded.row_count != batch.row_count:
        raise FoldExportError(
            f"fold export {path} holds {loaded.row_count} rows, manifest records "
            f"{batch.row_count}"
        )
    return loaded


def _lock_user(db: Session, *, user_id: int) -> None:
    """Take the user's graph lock and retention state, WAITING for both.

    The compactor uses the try-/NOWAIT forms because it has somewhere else to be.
    A restore does not: it is a deliberate operator action with nothing to gain
    from skipping a busy user, and skipping one would leave a partial rollback,
    which is worse than a slow one.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    # Generous, but not unbounded. Waiting is right here; waiting FOREVER is not,
    # because a restore that hangs on a wedged backend reports nothing at all,
    # and an operator mid-rollback needs the name of what is blocking them.
    db.execute(
        text("SELECT set_config('lock_timeout', :v, true)").bindparams(
            v=RESTORE_LOCK_WAIT
        )
    )
    db.execute(text("SELECT pg_advisory_xact_lock(:uid)").bindparams(uid=user_id))
    db.execute(
        select(UserOpportunityRetentionState.user_id)
        .where(UserOpportunityRetentionState.user_id == user_id)
        .with_for_update()
    ).first()


def _require_sequence_ahead(db: Session, *, ids: list[int]) -> None:
    """Refuse a restore whose ids the id generator could still hand out. Never MOVES it.

    The ids being restored came from this sequence, so it is already past them:
    a sequence only ever moves forward, and deleting rows does not walk it back.
    That makes a ``setval`` here a no-op in every case it was written for — and
    silent data loss in one it was not. Called after a batch that restored the
    table's highest ids, it drags the generator back over ids that a LATER batch
    of the same rollback is about to re-insert; the second batch then finds those
    ids taken by rows written in between, skips them as "already present", and
    reports a clean restore over a row that is gone for good.

    The generator can only be behind these ids out of band. ``pg_dump`` is NOT
    that case — it carries the sequence's real position across a reload. What
    does: a logical-replication cutover, which replays rows without their
    sequence, and the ``setval(max(id))`` an operator then runs by hand, which
    reads a maximum these folded rows are missing from. That is a real collision
    — on a future upload, not here — so it is reported now, with the command that
    fixes it, rather than papered over by a read-then-``setval`` that is not
    atomic against a live writer anyway.
    """
    if db.get_bind().dialect.name != "postgresql" or not ids:
        return
    sequence = db.execute(
        text("SELECT pg_get_serial_sequence('blunder_opportunity_events', 'id')")
    ).scalar()
    if not sequence:
        return
    # The name comes from the catalog, already quoted; a sequence cannot be read
    # through a bind parameter.
    position = db.execute(text(f"SELECT last_value, is_called FROM {sequence}")).first()
    if position is None:
        return
    # Assuming an increment of one is the conservative direction: a larger step
    # only puts the next id further away than this arithmetic believes.
    next_value = int(position.last_value) + (1 if position.is_called else 0)
    highest = max(ids)
    if highest >= next_value:
        raise RecoveryRefused(
            f"the blunder_opportunity_events id generator is at {next_value}, but "
            f"this batch restores ids up to {highest}; a later upload would collide "
            f"on the primary key. Advance it past the highest id in EVERY batch "
            f"this run refused — they are all reported, so take the largest — "
            f"SELECT setval('{sequence}', <that id>); for this batch alone that is "
            f"{highest}. Then re-run the restore"
        )


def _restore_one(db: Session, batch, *, now: datetime, report: RestoreReport) -> None:
    """Put one batch's rows back and take its contributions out of the summaries."""
    loaded = _load_verified(batch)
    _lock_user(db, user_id=batch.user_id)

    ids = [row.id for row in loaded.rows]
    blunder_ids = sorted({row.blunder_id for row in loaded.rows})
    session_ids = sorted({row.session_id for row in loaded.rows}, key=str)

    # Keyed by id and carrying the PAIR, because "that id exists" and "that row
    # is back" are different claims, and only the second one is a restore.
    present = {
        int(row.id): (row.session_id, int(row.blunder_id))
        for row in db.execute(
            select(
                BlunderOpportunityEvent.id,
                BlunderOpportunityEvent.session_id,
                BlunderOpportunityEvent.blunder_id,
            ).where(BlunderOpportunityEvent.id.in_(ids))
        ).all()
    }
    # A pair can also be back under a DIFFERENT id, which the unique constraint
    # would reject — re-inserting it is not a restore, it is a duplicate.
    occupied = {
        (row.session_id, int(row.blunder_id))
        for row in db.execute(
            select(
                BlunderOpportunityEvent.session_id, BlunderOpportunityEvent.blunder_id
            ).where(BlunderOpportunityEvent.blunder_id.in_(blunder_ids))
        ).all()
    }
    live_blunders = set(
        db.execute(select(Blunder.id).where(Blunder.id.in_(blunder_ids))).scalars()
    )
    live_sessions = set(
        db.execute(
            select(GameSession.id).where(GameSession.id.in_(session_ids))
        ).scalars()
    )

    values = []
    for row in loaded.rows:
        if row.blunder_id not in live_blunders or row.session_id not in live_sessions:
            # A purge removed the owner, the session or the blunder after the
            # export. Deleting your training history outranks a rollback.
            report.rows_skipped_missing_parent += 1
            continue
        occupant = present.get(row.id)
        if occupant is not None:
            if occupant != (row.session_id, row.blunder_id):
                # Someone else's row is sitting on this id. Skipping it would
                # report a complete rollback over a row that is gone, so this
                # stops instead: the id space has been rewritten under us.
                raise RetentionInvariantError(
                    f"restoring batch {batch.batch_id} found event id {row.id} "
                    f"held by pair {occupant}, not {(row.session_id, row.blunder_id)}; "
                    "the id sequence has been moved backwards or the rows were "
                    "rewritten, and this restore cannot tell what is missing"
                )
            report.rows_skipped_present += 1
            continue
        if (row.session_id, row.blunder_id) in occupied:
            report.rows_skipped_present += 1
            continue
        values.append(row.restore_values())
    if values:
        _require_sequence_ahead(db, ids=[int(entry["id"]) for entry in values])
        db.execute(insert(BlunderOpportunityEvent.__table__), values)
        report.rows_restored += len(values)

    summaries = {
        int(row.blunder_id): row
        for row in db.execute(
            select(
                BlunderOpportunitySummary.blunder_id,
                BlunderOpportunitySummary.folded_eligible_count,
                BlunderOpportunitySummary.folded_opportunities_since_review,
                BlunderOpportunitySummary.folded_reached_since_review,
                BlunderOpportunitySummary.latest_review_id,
            )
            .where(BlunderOpportunitySummary.blunder_id.in_(blunder_ids))
            .with_for_update()
        ).all()
    }
    for entry in batch.contributions:
        blunder_id = int(entry["blunder_id"])
        summary = summaries.get(blunder_id)
        if summary is None:
            # The blunder was deleted, taking its summary with it. Nothing to
            # subtract from, and nothing lost: its raw rows went too.
            report.summaries_missing += 1
            continue
        eligible = int(summary.folded_eligible_count) - int(entry["eligible"])
        if summary.latest_review_id == entry["review_basis_id"]:
            opportunities = int(summary.folded_opportunities_since_review) - int(
                entry["opportunities_since_review"]
            )
            reached = int(summary.folded_reached_since_review) - int(
                entry["reached_since_review"]
            )
        else:
            # A review landed after the fold: it already zeroed these two and
            # moved the basis, so this batch's share of them is long gone.
            # Subtracting again would drive a counter negative for a window that
            # no longer exists.
            opportunities = int(summary.folded_opportunities_since_review)
            reached = int(summary.folded_reached_since_review)
            report.since_review_left_alone += 1
        if min(eligible, opportunities, reached) < 0:
            raise RetentionInvariantError(
                f"restoring batch {batch.batch_id} would drive blunder "
                f"{blunder_id}'s folded counters negative "
                f"({eligible}/{opportunities}/{reached}); the summary has been "
                "changed by something this manifest does not describe"
            )
        db.execute(
            update(BlunderOpportunitySummary)
            .where(BlunderOpportunitySummary.blunder_id == blunder_id)
            .values(
                folded_eligible_count=eligible,
                folded_opportunities_since_review=opportunities,
                folded_reached_since_review=reached,
            )
            .execution_options(synchronize_session=False)
        )
        report.summaries_adjusted += 1

    db.execute(
        update(OpportunityFoldBatch)
        .where(OpportunityFoldBatch.batch_id == batch.batch_id)
        .values(restored_at=now)
        .execution_options(synchronize_session=False)
    )
    _reset_prefix(db, user_id=batch.user_id)
    report.batches += 1
    if batch.user_id not in report.users:
        report.users.append(batch.user_id)


def _reset_prefix(db: Session, *, user_id: int) -> None:
    """Move the prefix and targeted watermark back to what is STILL folded.

    Recomputed from the surviving unrestored manifests rather than decremented,
    because the prefix is a maximum and there is no safe way to walk a maximum
    backwards one batch at a time. Inside the recovery window those manifests are
    the complete record of everything this user has had deleted, which is exactly
    why :func:`_require_recoverable` refuses outside it.

    Runs in the same transaction as the batch it follows, so an interrupted
    restore always leaves the prefix covering at least what is still deleted —
    conservative in the only direction that is safe, since the prefix may forbid
    a write but must never authorize one.
    """
    remaining = db.execute(
        select(
            func.max(OpportunityFoldBatch.max_session_started_at),
            func.max(OpportunityFoldBatch.targeted_discarded_max_served_at),
        ).where(
            OpportunityFoldBatch.user_id == user_id,
            OpportunityFoldBatch.restored_at.is_(None),
        )
    ).first()
    db.execute(
        update(UserOpportunityRetentionState)
        .where(UserOpportunityRetentionState.user_id == user_id)
        .values(
            folded_through_started_at=remaining[0],
            targeted_discarded_max_served_at=remaining[1],
        )
        .execution_options(synchronize_session=False)
    )


def restore_folded_evidence(
    engine, *, user_ids: list[int] | None = None,
    batch_ids: list[uuid.UUID] | None = None,
) -> RestoreReport:
    """Restore every unrestored batch (optionally narrowed), oldest first.

    One transaction per batch. A restore of a large rollback is minutes of work
    and a single transaction over all of it would hold every affected user's
    locks for its whole length; per batch, an interruption leaves a prefix that
    still covers everything that is still deleted, and re-running finishes the
    job. Re-running is safe in general: rows already back are skipped by id and
    by pair, and a batch already marked restored is not selected again.
    """
    report = RestoreReport()
    with Session(bind=engine) as db:
        now = _require_recoverable(db)
        query = select(OpportunityFoldBatch).where(
            OpportunityFoldBatch.restored_at.is_(None)
        )
        if user_ids is not None:
            query = query.where(OpportunityFoldBatch.user_id.in_(user_ids))
        if batch_ids is not None:
            query = query.where(OpportunityFoldBatch.batch_id.in_(batch_ids))
        pending = [
            row.batch_id
            for row in db.execute(
                query.order_by(OpportunityFoldBatch.committed_at)
            ).scalars()
        ]
        db.rollback()

        for batch_id in pending:
            batch = db.get(OpportunityFoldBatch, batch_id)
            if batch is None or batch.restored_at is not None:
                continue
            try:
                _restore_one(db, batch, now=now, report=report)
                db.commit()
            except (FoldExportError, RecoveryRefused, RetentionInvariantError) as bad:
                # One batch that cannot be put back must not strand the ones
                # behind it — including other users' — and must not take the
                # report of what DID come back with it into a traceback. It stays
                # unrestored, so the prefix still covers its rows, and a later run
                # retries it once the artifact or the summary is dealt with.
                db.rollback()
                report.failures.append((batch_id, str(bad)))
                logger.error("could not restore fold batch %s: %s", batch_id, bad)
            except Exception:
                db.rollback()
                raise

        report.anchor_cleared = _stop_the_clock_if_nothing_is_folded(db)
    return report


def _stop_the_clock_if_nothing_is_folded(db: Session) -> bool:
    """Clear the recovery anchor once no raw row is deleted anywhere.

    The anchor exists so the deadline keeps running after the manifests that
    describe it expire. It is NOT a permanent record that a fold once happened,
    and treating it as one has a sharp edge: a rehearsal or canary folded and
    restored on day zero would burn the only window the real rollout needs, and
    day fourteen would open with ``restore`` refusing fresh, verified artifacts.

    Inside the window — which :func:`_require_recoverable` has already
    established — no manifest can have expired, so "no unrestored manifest" means
    exactly "nothing is deleted". There is then nothing to recover, no deadline
    to enforce, and the next fold starts a fresh seven days by stamping it again.

    The count and the clear must exclude a fold that is between its manifest
    insert and its commit, or that batch commits into a window this just took
    away. SHARE ROW EXCLUSIVE conflicts with the ROW EXCLUSIVE an INSERT holds,
    which decides the order both ways round:

    * the fold inserted first — this waits for its commit and counts the batch;
    * the fold has not inserted yet — its insert waits out this transaction (or
      defers on the 25 ms lock_timeout), and the unconditional anchor UPDATE it
      runs afterwards (:mod:`app.opportunity_fold`) stamps a new window.

    Neither half is sufficient alone. SQLite has no LOCK TABLE and no concurrent
    fold to serialize against, so it skips this and relies on the count.

    The wait for that lock is BOUNDED, and this is the more exposed of the two
    waits a restore takes. It runs last, once every batch is already back, and
    what it can queue behind is not another fold but a whole-user purge or an
    account deletion, either of which holds the manifest table for as long as the
    rest of its transaction takes. Waiting there without a bound would stall the
    restore at its final step, defer every fold and queue every other purge — to
    clear a flag the next run clears just as well. A timeout is therefore reported
    as "not cleared", which is a true statement about a live anchor, and is what a
    restore that found something still folded would have returned anyway.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT set_config('lock_timeout', :v, true)").bindparams(
                v=RESTORE_LOCK_WAIT
            )
        )
        try:
            db.execute(
                text("LOCK TABLE opportunity_fold_batches IN SHARE ROW EXCLUSIVE MODE")
            )
        except OperationalError as busy:
            if not _lock_not_available(busy):
                raise
            db.rollback()
            logger.warning(
                "recovery anchor left standing: the fold manifest table was locked "
                "for longer than %s, most likely by a purge or an account deletion. "
                "Every batch is restored; re-run restore to clear it (%s)",
                RESTORE_LOCK_WAIT,
                busy,
            )
            return False
    if db.execute(
        select(func.count()).select_from(OpportunityFoldBatch).where(
            OpportunityFoldBatch.restored_at.is_(None)
        )
    ).scalar_one():
        db.rollback()
        return False
    cleared = db.execute(
        update(OpportunityRetentionPolicy)
        .where(
            OpportunityRetentionPolicy.id == POLICY_ID,
            OpportunityRetentionPolicy.first_fold_committed_at.is_not(None),
        )
        .values(first_fold_committed_at=None)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    return bool(cleared)



def _lock_not_available(error: OperationalError) -> bool:
    """SQLSTATE 55P03 and only 55P03 — the lock_timeout refusal, nothing else.

    psycopg3 exposes it as ``.sqlstate`` on the wrapped DBAPI error, psycopg2 as
    ``.pgcode``. Anything else is a real failure and propagates.
    """
    orig = getattr(error, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate == "55P03"


@dataclass
class ExpiryReport:
    """What the expiry sweep removed. Orphans are counted separately on purpose."""

    manifests_deleted: int = 0
    artifacts_deleted: int = 0
    orphans_deleted: int = 0


def expire_fold_artifacts(engine) -> ExpiryReport:
    """Delete manifests past ``expires_at``, their artifacts, and stale orphans.

    The manifest rows are committed FIRST and the files unlinked afterwards. The
    other order can leave a manifest pointing at a file that is gone, which reads
    as a corrupted recovery; this order can leave a file with no manifest, which
    is an orphan, and orphans are what the second half of this function is for.

    An orphan is a file whose batch never committed — the export was written and
    the transaction rolled back — or one whose manifest expired between the
    commit and the unlink. Either way nothing will ever claim it, so it goes on
    AGE alone, at the same seven days, and the threshold is four orders of
    magnitude beyond how long a batch spends between its export and its commit.
    The one exception is an artifact whose owner has had their SRS evidence
    deleted on request (:func:`_purged_users`): that one goes now.

    A whole-user training purge does not rely on that exception. It leaves its
    manifests behind already expired (:mod:`app.opportunity_purge`), so they come
    through the FIRST half here, by name, on the first run after the purge — no
    age rule, and no question about the owner to get wrong.
    """
    report = ExpiryReport()
    with Session(bind=engine) as db:
        now = as_utc(db.execute(select(database_clock(db))).scalar_one())
        expired = db.execute(
            select(OpportunityFoldBatch.batch_id, OpportunityFoldBatch.artifact_uri)
            .where(OpportunityFoldBatch.expires_at <= now)
        ).all()
        if expired:
            report.manifests_deleted = db.execute(
                delete(OpportunityFoldBatch).where(
                    OpportunityFoldBatch.batch_id.in_(
                        [row.batch_id for row in expired]
                    )
                )
            ).rowcount
            db.commit()
        live = set(
            db.execute(select(OpportunityFoldBatch.batch_id)).scalars()
        )
        purged = _purged_users(db)

    for row in expired:
        try:
            if discard_export(Path(row.artifact_uri)):
                report.artifacts_deleted += 1
        except FoldExportError:
            logger.warning("could not remove expired fold export %s", row.artifact_uri)

    report.orphans_deleted = _sweep_orphans(
        live, cutoff=now - RECOVERY_WINDOW, purged=purged
    )
    return report


def _purged_users(db: Session) -> set[int]:
    """Which artifact owners have no SRS evidence left for this file to belong to.

    Deleting an ACCOUNT cascades its manifests away, which leaves the exports as
    ordinary orphans, and orphans wait out the full seven days on age. These do
    not: the file holds rows that were deleted on request, the rollback it
    belonged to can no longer restore them anyway (the parents are gone, so a
    restore skips them), and a deletion request outranks a recovery window.
    Owners are read from the NAMES of the files, never from contents.

    A training-history purge is NOT decided here, and must not be: that purge
    keeps the account, so the only evidence of it is state a live user can
    recreate — a purged user who keeps playing is handed a retention-state row
    again at their next served target (:mod:`app.srs_target_admission`), and
    their export would quietly fall back to the seven-day age rule. It leaves an
    expired manifest instead, which the sweep takes by name.

    The test is still the retention-state row rather than the ``users`` row: it
    is what an account deletion removes, and it also catches the file a purge
    tombstone named but could not unlink. It cannot strand a live artifact — a
    fold commits that row before it writes an export, and a claimed file never
    reaches the age rule at all.
    """
    if not _dedicated_export_directory():
        return set()
    owners = _artifact_owners()
    if not owners:
        return set()
    protected = set(
        db.execute(
            select(UserOpportunityRetentionState.user_id).where(
                UserOpportunityRetentionState.user_id.in_(sorted(owners))
            )
        ).scalars()
    )
    return owners - protected


def _dedicated_export_directory() -> bool:
    """Whether this directory holds exactly one database's artifacts.

    The rule above reads a user id out of a file name and asks THIS database
    whether that user still has evidence. "No" only means "purged" when every
    file in the directory came from this database. The built-in default sits
    beside the backend package and is shared by every local database a developer
    points at the checkout, so an id missing here may simply belong to another
    one — and an ``expire`` run against a scratch database would delete another's
    live artifact on the spot, where before this rule existed it would have taken
    seven days, by which time the window had closed everywhere.

    A configured directory is the operator naming the deployment that owns it.
    ``g-srs-retain-rollout`` makes setting it a production gate; until then this
    costs a dev machine nothing but the age rule it always had.
    """
    return bool(os.environ.get(EXPORT_DIR_ENV))


def _artifact_owners() -> set[int]:
    """The user ids named by the files in the export directory."""
    directory = export_dir()
    if not directory.is_dir():
        return set()
    owners = set()
    for path in directory.iterdir():
        owner = _owner_of(path.name)
        if owner is not None:
            owners.add(owner)
    return owners


def _owner_of(name: str) -> int | None:
    """``fold-<user>-<batch uuid>.json`` -> the user id, or None if unparseable."""
    if not name.startswith("fold-"):
        return None
    raw, separator, _ = name[len("fold-"):].partition("-")
    if not separator or not raw.isdigit():
        return None
    return int(raw)


def _sweep_orphans(live: set, *, cutoff: datetime, purged: set[int]) -> int:
    """Remove artifact files older than the window that no manifest claims.

    A file is identified by the batch id in its NAME, never by reading it: a
    truncated or unparseable artifact is exactly the kind this has to be able to
    clean up, and one that cannot be parsed still cannot be recovered from.

    The age threshold is the recovery window itself — four orders of magnitude
    beyond the milliseconds a batch spends between writing its export and
    committing its manifest, so a fold in flight is never in reach of this. That
    slack is also what makes comparing a filesystem mtime against the DATABASE
    clock safe here: the two can disagree by seconds and the verdict is the same.
    The mtime is read AS UTC rather than as a naive local stamp, or a host west of
    Greenwich would call a file hours older than it is.

    ``purged`` are the owners whose SRS evidence has been deleted on request.
    Their files skip the age rule entirely — see :func:`_purged_users`.
    """
    directory = export_dir()
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.iterdir():
        if not path.is_file() or not path.name.startswith("fold-"):
            continue
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if _claimed(path.name, live):
            continue
        # Age is the rule for an orphan nobody will ever claim. A purged owner is
        # the exception: waiting out a window that can no longer restore those
        # rows would only keep deleted training history on disk.
        if modified > cutoff and _owner_of(path.name) not in purged:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            logger.warning("could not remove orphaned fold export %s", path)
    return removed


def _claimed(name: str, live: set) -> bool:
    """Does a surviving manifest own this file? ``fold-<user>-<batch uuid>.json``.

    A ``.partial`` left by a crashed write, or any name this cannot parse, is
    unclaimed by construction: no manifest can name a file that was never
    finished.
    """
    if not name.endswith(".json"):
        return False
    stem = name[len("fold-"): -len(".json")]
    _, separator, raw = stem.partition("-")
    if not separator:
        return False
    try:
        return uuid.UUID(raw) in live
    except ValueError:
        return False
