"""Real contention, real cancellation, real lock release for the SRS fold.

SQLite renders none of this. ``pg_try_advisory_xact_lock``, ``FOR UPDATE NOWAIT``,
``FOR NO KEY UPDATE NOWAIT``, ``statement_timeout`` cancelling a running query,
``idle_in_transaction_session_timeout`` terminating a stalled backend, and the
question this file exists to answer — *does the user get their locks back?* — are
all properties of the lock manager, and the test dialect has no lock manager.

The rule every case here checks is the same one: **the compactor never waits.**
Every acquisition is ``try_``/``NOWAIT``, so contention is always "skip this user,
fold on a later sweep" and never a queue behind a transaction that is serving a
move. And when a batch does have to give up — because it ran out of budget, was
cancelled mid-statement, or had its backend terminated under it — the locks it
held are gone by the time the next transaction asks for them, even if getting
them back costs the connection.

The arithmetic and the recovery protocol are in ``test_opportunity_compaction.py``;
the schema rollback rehearsal is in ``test_opportunity_compaction_migration.py``.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from app.fen import fen_hash
from app.models import (
    Blunder,
    BlunderOpportunityEvent,
    BlunderOpportunitySummary,
    GameSession,
    OpportunityFoldBatch,
    OpportunityRetentionPolicy,
    Position,
    User,
    UserOpportunityRetentionState,
)
from app import opportunity_fold, opportunity_fold_recovery
from app.opportunity_fold import (
    FoldLimits,
    FoldOutcome,
    _rollback_or_discard,
    fold_user_batch,
)
from app.opportunity_fold_recovery import (
    _stop_the_clock_if_nothing_is_folded,
    expire_fold_artifacts,
    restore_folded_evidence,
)
from app.opportunity_purge import purge_user_training_history
from app.opportunity_store import ensure_retention_state
from app.srs_target_admission import lock_state_for_fold
from conftest import pg_required

pytestmark = pg_required

USER_ID = 7701
FEN = "8/8/8/8/8/8/8/K6k w - - 0 1"
PATIENT = FoldLimits(transaction_deadline=30.0, user_cooldown=0.0)


@pytest.fixture(autouse=True)
def fold_exports(tmp_path, monkeypatch):
    monkeypatch.setenv("GHOSTREPLAY_SRS_FOLD_EXPORT_DIR", str(tmp_path / "exports"))
    return tmp_path / "exports"


def _seed(factory, *, rows: int = 2, oldest_first: bool = False,
          user_id: int = USER_ID) -> int:
    """One blunder with ``rows`` foldable pairs, and folding switched fully on.

    ``oldest_first`` makes the event ids ASCEND with session age, so the first
    batch a sweep folds — oldest sessions first — holds the LOWEST ids. That is
    the order the recovery tests need: it is the one in which a restore could
    drag the id generator back over ids a later batch still has to re-insert.

    ``user_id`` seeds a SECOND foldable user, which is what the anchor-race case
    needs: the restore it runs mid-fold has to take a different user's locks, or
    it would simply queue behind the fold that is calling it.
    """
    now = datetime.now(timezone.utc)
    with factory() as db:
        db.add(User(id=user_id, username=None, is_anonymous=True))
        db.flush()
        position = Position(user_id=user_id, fen_hash=fen_hash(FEN), fen_raw=FEN,
                            active_color="white")
        db.add(position)
        db.flush()
        blunder = Blunder(
            user_id=user_id, position_id=position.id, bad_move_san="bad",
            best_move_san="good", eval_loss_cp=200,
            created_at=now - timedelta(days=365),
        )
        db.add(blunder)
        db.flush()
        db.add(BlunderOpportunitySummary(blunder_id=blunder.id))
        db.add(UserOpportunityRetentionState(user_id=user_id))
        for step in range(rows):
            age = 120 + (rows - 1 - step if oldest_first else step)
            game_session = GameSession(
                id=uuid.uuid4(), user_id=user_id,
                started_at=now - timedelta(days=age),
                status="completed", engine_elo=1500, player_color="white",
            )
            db.add(game_session)
            db.flush()
            db.add(BlunderOpportunityEvent(
                blunder_id=blunder.id, session_id=game_session.id,
                occurred_at=game_session.started_at, opportunity=True,
                reached=step == 0,
            ))
        policy = db.get(OpportunityRetentionPolicy, 1)
        policy.readiness = True
        policy.freeze_enabled = True
        policy.cleanup_enabled = True
        db.commit()
        return blunder.id


def _remaining(factory) -> int:
    with factory() as db:
        return db.query(BlunderOpportunityEvent).count()


def _locks_are_free(factory) -> bool:
    """Can a fresh transaction take BOTH of the fold's user-level locks, now?

    ``pg_try_advisory_xact_lock`` returning false, or the ``NOWAIT`` raising
    55P03, is exactly what a leaked fold lock looks like to the next publication.
    This asks the question the same way that publication would.
    """
    with factory() as probe:
        try:
            if not probe.execute(
                text("SELECT pg_try_advisory_xact_lock(:u)"), {"u": USER_ID}
            ).scalar():
                return False
            probe.execute(
                text(
                    "SELECT user_id FROM user_opportunity_retention_state "
                    "WHERE user_id = :u FOR UPDATE NOWAIT"
                ),
                {"u": USER_ID},
            ).first()
        except OperationalError:
            return False
        finally:
            probe.rollback()
    return True


# ---------------------------------------------------------------------------
# Contention: skip, never wait
# ---------------------------------------------------------------------------


def test_pg_a_review_holding_the_blunder_row_defers_the_fold_immediately(
    pg_engine, pg_session_factory
):
    """A review must never queue behind a compactor, so the compactor gives way.

    ``FOR NO KEY UPDATE NOWAIT`` on the blunder is the point: the fold discovers
    the conflict and abandons the batch in milliseconds instead of holding this
    user's advisory lock and retention-state row for the length of a review.
    """
    blunder_id = _seed(pg_session_factory)
    with pg_session_factory() as reviewer:
        reviewer.execute(
            text("SELECT id FROM blunders WHERE id = :b FOR NO KEY UPDATE"),
            {"b": blunder_id},
        ).first()

        started = time.monotonic()
        result = fold_user_batch(pg_engine, user_id=USER_ID, limits=PATIENT)
        elapsed = time.monotonic() - started

        assert result.outcome is FoldOutcome.DEFERRED_ROW_BUSY
        # Nowhere near the 30 s budget this ran under: it did not wait at all.
        assert elapsed < 5.0
        reviewer.rollback()

    assert _remaining(pg_session_factory) == 2
    assert _locks_are_free(pg_session_factory)
    assert fold_user_batch(pg_engine, user_id=USER_ID, limits=PATIENT).rows_deleted == 2


def test_pg_a_publication_holding_the_state_row_skips_only_this_user(
    pg_engine, pg_session_factory
):
    """``FOR SHARE`` held by a publication: skip, and fold around the pin later.

    The compactor must NOT read this as "nothing to fold". The second fold, in a
    fresh transaction, proves the work was deferred rather than lost.
    """
    _seed(pg_session_factory)
    with pg_session_factory() as publisher:
        publisher.execute(
            text(
                "SELECT user_id FROM user_opportunity_retention_state "
                "WHERE user_id = :u FOR SHARE"
            ),
            {"u": USER_ID},
        ).first()

        result = fold_user_batch(pg_engine, user_id=USER_ID, limits=PATIENT)

        assert result.outcome is FoldOutcome.SKIPPED_PUBLICATION
        assert _remaining(pg_session_factory) == 2
        publisher.rollback()

    assert fold_user_batch(pg_engine, user_id=USER_ID, limits=PATIENT).rows_deleted == 2


def test_pg_an_evidence_writer_holding_the_user_lock_skips_the_fold(
    pg_engine, pg_session_factory
):
    """Same advisory namespace as every other same-user graph writer, try- only."""
    _seed(pg_session_factory)
    with pg_session_factory() as writer:
        writer.execute(text("SELECT pg_advisory_xact_lock(:u)"), {"u": USER_ID})

        result = fold_user_batch(pg_engine, user_id=USER_ID, limits=PATIENT)

        assert result.outcome is FoldOutcome.SKIPPED_USER_BUSY
        assert _remaining(pg_session_factory) == 2
        writer.rollback()

    assert fold_user_batch(pg_engine, user_id=USER_ID, limits=PATIENT).rows_deleted == 2


# ---------------------------------------------------------------------------
# Giving up: deadline, cancellation, a terminated backend — and the locks back
# ---------------------------------------------------------------------------


def test_pg_a_batch_that_overruns_its_deadline_rolls_back_and_frees_its_locks(
    pg_engine, pg_session_factory
):
    """The deadline is checked BETWEEN statements, where statement_timeout is blind.

    The stall is placed after every lock has been taken, which is the only
    interesting case: an expiry before the first acquisition has nothing to
    release. The idle bound is raised for this one test so the thing under test
    is the deadline rather than the backend terminator.
    """
    _seed(pg_session_factory)
    limits = FoldLimits(transaction_deadline=0.2, idle_in_transaction_ms=10_000)
    real_latest = opportunity_fold._latest_reviews

    def stall(db, **kwargs):
        time.sleep(0.4)
        return real_latest(db, **kwargs)

    with patch("app.opportunity_fold._latest_reviews", side_effect=stall):
        result = fold_user_batch(pg_engine, user_id=USER_ID, limits=limits)

    assert result.outcome is FoldOutcome.DEADLINE_EXCEEDED
    assert _remaining(pg_session_factory) == 2
    assert _locks_are_free(pg_session_factory)


def test_pg_a_cancelled_statement_rolls_back_and_frees_its_locks(
    pg_engine, pg_session_factory
):
    """``statement_timeout`` cancelling mid-statement is a deferral, not a crash.

    57014 inside the critical section can only be the bound this transaction
    armed on itself, so it is reported as a budget overrun — and, like every
    other way this batch can end, it deletes nothing and leaves no lock behind.
    """
    _seed(pg_session_factory)
    real_latest = opportunity_fold._latest_reviews

    def slow_query(db, **kwargs):
        db.execute(text("SELECT pg_sleep(5)"))
        return real_latest(db, **kwargs)

    with patch("app.opportunity_fold._latest_reviews", side_effect=slow_query):
        result = fold_user_batch(
            pg_engine, user_id=USER_ID,
            limits=FoldLimits(transaction_deadline=30.0, statement_timeout_ms=150),
        )

    assert result.outcome is FoldOutcome.DEADLINE_EXCEEDED
    assert _remaining(pg_session_factory) == 2
    assert _locks_are_free(pg_session_factory)


def test_pg_a_stalled_client_is_terminated_and_its_locks_are_released(
    pg_engine, pg_session_factory
):
    """``idle_in_transaction_session_timeout`` is the bound the other two cannot give.

    A client that stops driving the transaction between statements holds every
    lock it has taken for as long as it stays alive, and TCP keepalives only ever
    notice a peer that is already gone. Terminating the backend is the only way
    to get the locks back from one — so this asserts the termination, not the
    setting.
    """
    _seed(pg_session_factory)
    real_latest = opportunity_fold._latest_reviews

    def stall(db, **kwargs):
        time.sleep(0.6)
        return real_latest(db, **kwargs)

    with patch("app.opportunity_fold._latest_reviews", side_effect=stall):
        with pytest.raises(DBAPIError):
            fold_user_batch(
                pg_engine, user_id=USER_ID,
                limits=FoldLimits(transaction_deadline=30.0,
                                  idle_in_transaction_ms=100),
            )

    assert _remaining(pg_session_factory) == 2
    assert _locks_are_free(pg_session_factory)


def test_pg_a_connection_whose_rollback_fails_is_discarded_and_frees_its_locks(
    pg_engine, pg_session_factory
):
    """One connection is the right price for a user's locks back.

    Returning a backend that may still hold the advisory lock and the retention
    state to the pool would leak them for the life of that connection — which,
    for the affected user, means no target can be published until it happens to
    be recycled.
    """
    with pg_session_factory() as db:
        db.add(User(id=USER_ID, username=None, is_anonymous=True))
        db.add(UserOpportunityRetentionState(user_id=USER_ID))
        db.commit()

    connection = pg_engine.connect()
    stuck = Session(bind=connection)
    try:
        stuck.execute(text("SELECT pg_advisory_xact_lock(:u)"), {"u": USER_ID})
        stuck.execute(
            text(
                "SELECT user_id FROM user_opportunity_retention_state "
                "WHERE user_id = :u FOR UPDATE"
            ),
            {"u": USER_ID},
        ).first()
        assert not _locks_are_free(pg_session_factory)

        with patch.object(stuck, "rollback", side_effect=RuntimeError("wedged")):
            assert _rollback_or_discard(stuck, connection) is False

        assert _locks_are_free(pg_session_factory)
    finally:
        stuck.close()
        connection.close()


# ---------------------------------------------------------------------------
# The delete guard: the fold's allowance, and nobody else's
# ---------------------------------------------------------------------------


def test_pg_only_the_fold_transfer_may_delete_a_frozen_event_row(
    pg_engine, pg_session_factory
):
    """The guard, the allowance, and the fact that the allowance is transaction-local.

    A repair CLI cannot reach the escape: nothing outside the fold SQL sets the
    marker, and ``set_config(..., true)`` means it dies with the transaction that
    did. The third step is the one worth having a test for — an allowance that
    outlived its transaction would travel with a pooled connection.
    """
    _seed(pg_session_factory, rows=1)
    with pg_session_factory() as db:
        event_id = db.query(BlunderOpportunityEvent.id).scalar()

        with pytest.raises(DBAPIError, match="mutation boundary|fold prefix"):
            db.execute(
                text("DELETE FROM blunder_opportunity_events WHERE id = :i"),
                {"i": event_id},
            )
        db.rollback()

        db.execute(
            text("SELECT set_config('ghostreplay.srs_fold_mode', 'transfer', true)")
        )
        assert db.execute(
            text("DELETE FROM blunder_opportunity_events WHERE id = :i"),
            {"i": event_id},
        ).rowcount == 1
        db.rollback()

        # A fresh transaction on the same session carries nothing over.
        with pytest.raises(DBAPIError, match="mutation boundary|fold prefix"):
            db.execute(
                text("DELETE FROM blunder_opportunity_events WHERE id = :i"),
                {"i": event_id},
            )
        db.rollback()


def test_pg_a_committed_fold_leaves_a_manifest_and_a_verified_artifact(
    pg_engine, pg_session_factory, fold_exports
):
    """The manifest and the deletion are one transaction, on the real schema."""
    _seed(pg_session_factory, rows=2)

    result = fold_user_batch(pg_engine, user_id=USER_ID, limits=PATIENT)

    assert result.rows_deleted == 2
    with pg_session_factory() as db:
        batch = db.query(OpportunityFoldBatch).one()
        assert batch.row_count == 2
        assert batch.user_id == USER_ID
        assert batch.restored_at is None
        assert db.get(OpportunityRetentionPolicy, 1).first_fold_committed_at is not None
    assert len(list(fold_exports.glob("*.json"))) == 1


# ---------------------------------------------------------------------------
# Recovery against a real id generator
# ---------------------------------------------------------------------------


def test_pg_restoring_one_batch_leaves_the_generator_for_the_batches_behind_it(
    pg_engine, pg_session_factory
):
    """A restore must not move the sequence. SQLite has none, so this is the test.

    The ids being restored came from this sequence, so it is already past them.
    A ``setval`` to ``MAX(id)`` after a batch that restored the table's LOWEST ids
    walks the generator backwards; the next upload is then issued an id that a
    later batch of the same rollback still has to re-insert, that batch skips it
    as "already present", and the report calls the rollback complete over a row
    that is gone for good.

    Six rows, two batches, one ordinary upload in between — the whole failure in
    one sequence of events.
    """
    blunder_id = _seed(pg_session_factory, rows=6, oldest_first=True)
    with pg_session_factory() as db:
        original = sorted(row_id for (row_id,) in db.query(BlunderOpportunityEvent.id))

    small = FoldLimits(transaction_deadline=30.0, user_cooldown=0.0, max_pairs=3)
    assert fold_user_batch(pg_engine, user_id=USER_ID, limits=small).rows_deleted == 3
    assert fold_user_batch(pg_engine, user_id=USER_ID, limits=small).rows_deleted == 3
    assert _remaining(pg_session_factory) == 0

    with pg_session_factory() as db:
        db.get(OpportunityRetentionPolicy, 1).cleanup_enabled = False
        db.commit()
        ordered = [
            batch.batch_id
            for batch in db.query(OpportunityFoldBatch)
            .order_by(OpportunityFoldBatch.committed_at)
            .all()
        ]

    first = restore_folded_evidence(pg_engine, batch_ids=[ordered[0]])
    assert first.rows_restored == 3
    # Half a rollback is not a whole one, so the clock keeps running.
    assert first.anchor_cleared is False

    # An ordinary upload, between the two halves of the rollback.
    with pg_session_factory() as db:
        game_session = GameSession(
            id=uuid.uuid4(), user_id=USER_ID,
            started_at=datetime.now(timezone.utc), status="completed",
            engine_elo=1500, player_color="white",
        )
        db.add(game_session)
        db.flush()
        uploaded = BlunderOpportunityEvent(
            blunder_id=blunder_id, session_id=game_session.id,
            occurred_at=game_session.started_at, opportunity=True, reached=False,
        )
        db.add(uploaded)
        db.commit()
        uploaded_id = uploaded.id

    # The generator handed out an id above everything, including the rows that
    # are still deleted — which is exactly what it does when nobody moves it.
    assert uploaded_id > max(original)

    second = restore_folded_evidence(pg_engine)

    assert second.rows_restored == 3
    assert second.rows_skipped_present == 0
    assert second.anchor_cleared is True
    with pg_session_factory() as db:
        present = sorted(row_id for (row_id,) in db.query(BlunderOpportunityEvent.id))
    assert present == sorted([*original, uploaded_id])


def test_pg_a_generator_behind_the_restored_ids_refuses_instead_of_colliding(
    pg_engine, pg_session_factory
):
    """The one case where the sequence really can be behind: say so, do not fix it.

    A dump and reload taken while these rows were folded sets the sequence from
    the data that survived. Restoring on top of that would hand a future upload
    an id this batch has just re-inserted — and a silent ``setval`` to repair it
    races every live writer, which is how the old repair became a bug. The
    refusal names the command and leaves the batch for a later run.
    """
    _seed(pg_session_factory, rows=2)
    assert fold_user_batch(pg_engine, user_id=USER_ID, limits=PATIENT).rows_deleted == 2
    with pg_session_factory() as db:
        db.get(OpportunityRetentionPolicy, 1).cleanup_enabled = False
        sequence = db.execute(text(
            "SELECT pg_get_serial_sequence('blunder_opportunity_events', 'id')"
        )).scalar()
        db.execute(text("SELECT setval(:s, 1, false)").bindparams(s=sequence))
        db.commit()

    report = restore_folded_evidence(pg_engine)

    assert report.rows_restored == 0
    assert len(report.failures) == 1
    assert "id generator is at 1" in report.failures[0][1]
    assert _remaining(pg_session_factory) == 0
    with pg_session_factory() as db:
        assert db.query(OpportunityFoldBatch).one().restored_at is None


def test_pg_a_cancelled_interlock_acquisition_is_not_a_publication_skip(
    pg_engine, pg_session_factory
):
    """57014 out of the interlock is the caller's budget, and must not read as a skip.

    The one step in there that can WAIT is the create-if-absent insert, and what
    it waits on is another transaction inserting the same row. Bounded by the
    fold's own ``statement_timeout``, that wait ends in a cancellation — and
    returning "a publication holds this row" for it would file a deadline overrun
    under the one outcome that is expected to be nonzero in normal operation.
    """
    _seed(pg_session_factory, rows=1)
    # A user with no retention-state row yet, so the interlock has to create it.
    newcomer = USER_ID + 1
    with pg_session_factory() as db:
        db.add(User(id=newcomer, username=None, is_anonymous=True))
        db.commit()

    with pg_session_factory() as blocker:
        # Inserted, not committed: the interlock's own insert now has to wait on
        # this transaction rather than on a lock it could refuse with NOWAIT.
        blocker.add(UserOpportunityRetentionState(user_id=newcomer))
        blocker.flush()

        with pg_session_factory() as folder:
            folder.execute(
                text("SELECT set_config('statement_timeout', '100ms', true)")
            )
            with pytest.raises(OperationalError) as caught:
                lock_state_for_fold(folder, user_id=newcomer)
            folder.rollback()

        # 57014, not the 55P03 the 500 ms lock ceiling would have raised: the
        # statement budget is what ended the wait.
        assert caught.value.orig.sqlstate == "57014"
        blocker.rollback()


def test_pg_a_fold_in_flight_cannot_commit_into_a_window_a_restore_just_cleared(
    pg_engine, pg_session_factory
):
    """Clearing the anchor must not strand a batch that is still on its way in.

    The clock is stopped when a rollback leaves nothing folded. A fold that is
    already running has read the policy by then, and if it decides in PYTHON
    whether to stamp the anchor, it decides on a value that this restore is about
    to erase: its manifest commits a second later with no anchor at all — an
    unrestored batch with no deadline, and the NEXT fold opens a window its
    manifest expires inside. The re-stamp is therefore unconditional, and the
    ``WHERE first_fold_committed_at IS NULL`` is the whole guard.

    The hook sits between the fold's policy read and its manifest insert, which
    is exactly the interval the old code was wrong in. The neighbour is a second
    user because the restore runs on this thread: sharing the folding user would
    queue it behind the very transaction it is meant to interleave with.
    """
    neighbour = USER_ID + 2
    _seed(pg_session_factory, rows=2)
    _seed(pg_session_factory, rows=2, user_id=neighbour)

    assert fold_user_batch(pg_engine, user_id=USER_ID, limits=PATIENT).rows_deleted == 2
    with pg_session_factory() as db:
        anchor = db.get(OpportunityRetentionPolicy, 1).first_fold_committed_at
    assert anchor is not None

    rollbacks: list = []
    unhooked = opportunity_fold._latest_reviews

    def roll_back_the_other_user_mid_fold(db, *, blunder_ids):
        if not rollbacks:
            # The operator's real sequence, and the only one that reaches this
            # window: folding is switched off — which this batch, having already
            # read the policy, never learns — and the rollback starts while it is
            # still running.
            with pg_session_factory() as operator:
                operator.get(OpportunityRetentionPolicy, 1).cleanup_enabled = False
                operator.commit()
            rollbacks.append(restore_folded_evidence(pg_engine))
        return unhooked(db, blunder_ids=blunder_ids)

    # The idle and statement budgets are widened only so the hook has room to run
    # on another connection; nothing here depends on their real values, and the
    # ordering the test is about is unchanged by them.
    hooked = FoldLimits(transaction_deadline=30.0, user_cooldown=0.0,
                        statement_timeout_ms=30_000, idle_in_transaction_ms=30_000)
    with patch.object(opportunity_fold, "_latest_reviews",
                      roll_back_the_other_user_mid_fold):
        folded = fold_user_batch(pg_engine, user_id=neighbour, limits=hooked)

    # The interleaving really happened: the rollback saw nothing folded and
    # stopped the clock, while this batch was between its policy read and its
    # commit.
    assert rollbacks[0].anchor_cleared is True
    assert folded.outcome is FoldOutcome.FOLDED
    assert folded.rows_deleted == 2

    with pg_session_factory() as db:
        batch = db.query(OpportunityFoldBatch).filter(
            OpportunityFoldBatch.restored_at.is_(None)
        ).one()
        restamped = db.get(OpportunityRetentionPolicy, 1).first_fold_committed_at
    # Not merely "set": set to THIS batch, so the window it is recoverable in is
    # the one its own expiry was written against.
    assert restamped is not None
    assert restamped == batch.committed_at
    assert batch.user_id == neighbour


def test_pg_the_anchor_clear_waits_for_a_manifest_that_has_not_committed(
    pg_engine, pg_session_factory
):
    """The other half of the same interlock, from the other side.

    An unconditional re-stamp only saves a fold that has not inserted its
    manifest yet. One that HAS is invisible to the clearing side's count — it is
    uncommitted — and the re-stamp has already gone past. So the clear takes
    SHARE ROW EXCLUSIVE on the manifest table first, which conflicts with the
    ROW EXCLUSIVE an INSERT holds, and waits for that transaction to end before
    it counts anything.

    Waiting is the assertion, and a short ``lock_timeout`` is how a test states
    it without a thread: hitting it proves the lock was requested and conflicted,
    where the bug would have counted zero and cleared.

    The wait is bounded, so what a conflict this long produces is not an error
    but ``False`` — the anchor is left standing, which is the truth about it, and
    the next restore clears it. Only that path can return False here: the
    blocker's manifest is uncommitted, so a count that got as far as running
    would see nothing folded and clear the anchor.
    """
    _seed(pg_session_factory, rows=2)
    assert fold_user_batch(pg_engine, user_id=USER_ID, limits=PATIENT).rows_deleted == 2
    with pg_session_factory() as db:
        db.get(OpportunityRetentionPolicy, 1).cleanup_enabled = False
        db.commit()
    assert restore_folded_evidence(pg_engine).rows_restored == 2

    # A live window again, and a fold that has inserted its manifest but not yet
    # committed it — the state the count cannot see.
    committed_at = datetime.now(timezone.utc)
    with pg_session_factory() as db:
        db.get(OpportunityRetentionPolicy, 1).first_fold_committed_at = committed_at
        db.commit()

    with pg_session_factory() as blocker:
        blocker.add(OpportunityFoldBatch(
            batch_id=uuid.uuid4(), user_id=USER_ID, artifact_uri="in-flight",
            artifact_sha256="0" * 64, rowset_hash="0" * 64, hash_version=1,
            row_count=1, policy_version=1, committed_at=committed_at,
            expires_at=committed_at + timedelta(days=7),
            max_session_started_at=committed_at, contributions=[],
        ))
        blocker.flush()

        with pg_session_factory() as clearing:
            # The real bound is 30s, which is right for an operator and wrong for
            # a test. The path under test is the same one.
            with patch.object(
                opportunity_fold_recovery, "RESTORE_LOCK_WAIT", "150ms"
            ):
                assert _stop_the_clock_if_nothing_is_folded(clearing) is False

        blocker.rollback()

    with pg_session_factory() as db:
        assert db.get(OpportunityRetentionPolicy, 1).first_fold_committed_at is not None


def test_pg_a_training_history_purge_takes_the_fold_manifest_and_its_export(
    pg_engine, pg_session_factory
):
    """Deleting training history has to reach evidence that was already folded.

    The raw rows are gone by then, so what is left is the manifest that describes
    them and the export it points at. Nothing cascades: this purge keeps the
    ACCOUNT, which is the FK the manifest hangs off.

    The export is a file, so it cannot go in the purge transaction, and the
    manifest is therefore expired rather than deleted — a tombstone that still
    names the file for the next sweep. The user playing on in the middle of this
    is the point: their retention-state row comes back at their next served
    target, so a sweep that decided "purged" from the absence of that row would
    hand this export back to the seven-day age rule.
    """
    _seed(pg_session_factory, rows=2)
    assert fold_user_batch(pg_engine, user_id=USER_ID, limits=PATIENT).rows_deleted == 2
    with pg_session_factory() as db:
        artifact = Path(db.query(OpportunityFoldBatch).one().artifact_uri)
    assert artifact.exists()

    with pg_session_factory() as db:
        counts = purge_user_training_history(db, user_id=USER_ID)
        db.commit()

    assert counts["fold_batches"] == 1
    with pg_session_factory() as db:
        tombstone = db.query(OpportunityFoldBatch).one()
        assert tombstone.expires_at == tombstone.restored_at
        # Restored, in the only sense that column claims: nothing it describes is
        # deleted-but-recoverable any more. So it cannot hold the recovery anchor
        # open, and it cannot refuse the schema downgrade.
        assert tombstone.restored_at is not None
        assert tombstone.contributions == []
        # The account itself survives: this is a history deletion, not a closure.
        assert db.get(User, USER_ID) is not None
        # ... and the user keeps playing. This is what admit_target_publication
        # does the next time it serves them a target.
        ensure_retention_state(db, USER_ID)
        db.commit()

    report = expire_fold_artifacts(pg_engine)
    assert (report.manifests_deleted, report.artifacts_deleted) == (1, 1)
    assert not artifact.exists()
    with pg_session_factory() as db:
        assert db.query(OpportunityFoldBatch).count() == 0
