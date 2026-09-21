"""The fold transfer, its bounded batch and its finite recovery (g-srs-fold-recovery).

Everything here is the deterministic half. Real ``FOR UPDATE NOWAIT`` contention,
statement cancellation and a discarded connection releasing its locks have no
SQLite equivalent and live in ``test_opportunity_compaction_pg.py``; the schema
rollback rehearsal lives in ``test_opportunity_compaction_migration.py``.

What this file is for is the arithmetic and the protocol: that a fold changes
where a counter is stored and never what it says, that it deletes only what it
exported and verified, that it refuses rather than guesses when the world moved
underneath it, and that seven days later the rows can still be put back exactly —
including alongside the reviews, writes and purges that happened in between.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from conftest import engine
from app.fen import fen_hash
from app.models import (
    Blunder,
    BlunderOpportunityEvent,
    BlunderOpportunitySummary,
    BlunderReview,
    GameSession,
    Move,
    OpponentDecision,
    OpportunityFoldBatch,
    OpportunityRetentionPolicy,
    Position,
    SessionMove,
    User,
    UserOpportunityRetentionState,
)
from app import opportunity_fold, opportunity_fold_recovery
from app.opportunity_fold import (
    FoldLimits,
    FoldOutcome,
    fold_user_batch,
    sweep,
)
from app.opportunity_fold_export import write_export as real_write_export
from app.opportunity_fold_export import export_dir, read_export
from app.opportunity_fold_recovery import (
    RecoveryExpired,
    RecoveryRefused,
    expire_fold_artifacts,
    fold_status,
    restore_folded_evidence,
)
from app.opportunity_retention import RetentionInvariantError
from app.opportunity_store import load_policy, record_review_basis
from app.srs_math import as_utc
from app.srs_opportunity import load_opportunity_counters, opportunity_priority

FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
# Four distinct legal positions, for the tests that need several blunders.
DISTINCT_FENS = (
    "8/8/8/8/8/8/8/K6k w - - 0 1",
    "8/8/8/8/8/8/K6k/8 w - - 0 1",
    "8/8/8/8/8/K6k/8/8 w - - 0 1",
    "8/8/8/8/K6k/8/8/8 w - - 0 1",
)
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
# Comfortably past M + G (60 days + 1 hour) for every session seeded below.
OLD = NOW - timedelta(days=120)
# A generous deadline: the deterministic suite is proving the protocol, not the
# 500 ms budget, and a real budget would make these tests fail on a loaded laptop
# for reasons that have nothing to do with what they assert.
PATIENT = FoldLimits(transaction_deadline=30.0, user_cooldown=0.0)


@pytest.fixture(autouse=True)
def fold_exports(tmp_path, monkeypatch):
    """Every artifact this file writes lands in a per-test directory."""
    monkeypatch.setenv("GHOSTREPLAY_SRS_FOLD_EXPORT_DIR", str(tmp_path / "exports"))
    return tmp_path / "exports"


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _user(db, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        user = User(id=user_id, username=None, is_anonymous=True)
        db.add(user)
        db.flush()
    return user


def _position(db, *, user_id: int, fen: str, active_color: str = "white") -> Position:
    position = Position(
        user_id=user_id, fen_hash=fen_hash(fen), fen_raw=fen, active_color=active_color
    )
    db.add(position)
    db.flush()
    return position


def _blunder(db, *, user_id: int, fen: str = FEN, created_at: datetime | None = None) -> Blunder:
    _user(db, user_id)
    position = _position(db, user_id=user_id, fen=fen)
    blunder = Blunder(
        user_id=user_id,
        position_id=position.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
        created_at=created_at or (NOW - timedelta(days=365)),
    )
    db.add(blunder)
    db.flush()
    db.add(BlunderOpportunitySummary(blunder_id=blunder.id))
    db.flush()
    return blunder


def _session(db, *, user_id: int, started_at: datetime) -> GameSession:
    game_session = GameSession(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=started_at,
        status="completed",
        engine_elo=1500,
        player_color="white",
    )
    db.add(game_session)
    db.flush()
    return game_session


def _event(
    db, *, blunder: Blunder, game_session: GameSession, occurred_at=...,
    opportunity: bool = True, reached: bool = False,
) -> BlunderOpportunityEvent:
    """One raw pair. ``occurred_at=None`` means a legacy NULL, not "use the default"."""
    event = BlunderOpportunityEvent(
        blunder_id=blunder.id,
        session_id=game_session.id,
        occurred_at=game_session.started_at if occurred_at is ... else occurred_at,
        opportunity=opportunity,
        reached=reached,
    )
    db.add(event)
    db.flush()
    return event


def _enable_folding(db, **overrides) -> None:
    """Climb the whole ladder: readiness, then freeze, then cleanup.

    The check constraints enforce the order, so a test that wants folding gets
    the entire activated state — there is no way to fold without also having
    frozen, and nothing here may pretend otherwise.
    """
    policy = db.get(OpportunityRetentionPolicy, 1)
    policy.readiness = True
    policy.freeze_enabled = True
    policy.cleanup_enabled = True
    for key, value in overrides.items():
        setattr(policy, key, value)
    db.flush()


def _utc(value):
    """SQLite hands back naive datetimes; every comparison here is in UTC."""
    return None if value is None else as_utc(value)


def _counters(db, blunder_id: int, *, user_id: int):
    return load_opportunity_counters(db, [blunder_id], user_id=user_id, now=NOW)[blunder_id]


def _simple(db, *, user_id: int, rows: int = 3, reached_first: bool = True):
    """One blunder, ``rows`` old sessions, one event per session. Committed."""
    blunder = _blunder(db, user_id=user_id)
    sessions = []
    for index in range(rows):
        game_session = _session(db, user_id=user_id, started_at=OLD - timedelta(days=index))
        _event(db, blunder=blunder, game_session=game_session,
               reached=reached_first and index == 0)
        sessions.append(game_session)
    _enable_folding(db)
    db.commit()
    return blunder, sessions


# ---------------------------------------------------------------------------
# The transfer is a storage change, not a counter change
# ---------------------------------------------------------------------------


def test_a_fold_moves_the_counters_without_changing_them(db_session):
    """The whole contract in one assertion: same numbers, different storage."""
    user_id = 9001
    blunder, _ = _simple(db_session, user_id=user_id, rows=3)
    before = _counters(db_session, blunder.id, user_id=user_id)
    db_session.commit()

    result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.outcome is FoldOutcome.FOLDED
    assert result.rows_deleted == 3
    db_session.expire_all()
    assert _counters(db_session, blunder.id, user_id=user_id) == before
    assert db_session.query(BlunderOpportunityEvent).count() == 0
    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    assert summary.folded_eligible_count == before.event_count == 3
    assert summary.folded_opportunities_since_review == before.opportunities_since_review
    assert summary.folded_reached_since_review == before.reached_since_review == 1


def test_the_prefix_advances_to_the_newest_session_actually_folded(db_session):
    user_id = 9002
    blunder, sessions = _simple(db_session, user_id=user_id, rows=3)
    newest = max(session.started_at for session in sessions)
    db_session.commit()

    fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    db_session.expire_all()
    state = db_session.get(UserOpportunityRetentionState, user_id)
    assert _utc(state.folded_through_started_at) == _utc(newest)
    # Untargeted evidence says nothing about targeted availability.
    assert state.targeted_discarded_max_served_at is None


def test_ineligible_rows_are_deleted_without_contributing(db_session):
    """An ``opportunity=false`` row is storage, not evidence.

    It is not in ``event_count`` and not in any counter, so the fold must remove
    it and add nothing for it. Leaving it behind would make the bounded-storage
    guarantee depend on how often the writer produced negative rows.
    """
    user_id = 9003
    blunder = _blunder(db_session, user_id=user_id)
    carrier = _session(db_session, user_id=user_id, started_at=OLD)
    _event(db_session, blunder=blunder, game_session=carrier, opportunity=False)
    empty = _session(db_session, user_id=user_id, started_at=OLD - timedelta(days=1))
    _event(db_session, blunder=blunder, game_session=empty, opportunity=True)
    _enable_folding(db_session)
    db_session.commit()
    before = _counters(db_session, blunder.id, user_id=user_id)
    db_session.commit()

    result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.rows_deleted == 2
    db_session.expire_all()
    assert _counters(db_session, blunder.id, user_id=user_id) == before
    assert db_session.get(BlunderOpportunitySummary, blunder.id).folded_eligible_count == 1


def test_an_event_dated_before_its_blunder_existed_folds_as_zero(db_session):
    """Legacy ``t < c`` rows: deleted, counted nowhere, exactly as they read."""
    user_id = 9004
    blunder = _blunder(db_session, user_id=user_id, created_at=NOW - timedelta(days=100))
    game_session = _session(db_session, user_id=user_id, started_at=OLD)
    _event(db_session, blunder=blunder, game_session=game_session,
           occurred_at=NOW - timedelta(days=150))
    _enable_folding(db_session)
    db_session.commit()
    before = _counters(db_session, blunder.id, user_id=user_id)
    assert before.event_count == 0
    db_session.commit()

    result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.rows_deleted == 1
    db_session.expire_all()
    assert _counters(db_session, blunder.id, user_id=user_id) == before
    assert db_session.get(BlunderOpportunitySummary, blunder.id).folded_eligible_count == 0


def test_a_legacy_null_occurred_at_row_is_exported_and_folded_by_created_at(db_session):
    user_id = 9005
    blunder = _blunder(db_session, user_id=user_id)
    game_session = _session(db_session, user_id=user_id, started_at=OLD)
    _event(db_session, blunder=blunder, game_session=game_session, occurred_at=None)
    _enable_folding(db_session)
    db_session.commit()
    before = _counters(db_session, blunder.id, user_id=user_id)
    db_session.commit()

    result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.outcome is FoldOutcome.FOLDED
    db_session.expire_all()
    assert _counters(db_session, blunder.id, user_id=user_id) == before
    batch = db_session.query(OpportunityFoldBatch).one()
    exported = read_export(Path(batch.artifact_uri))
    # The NULL is preserved, not normalized to created_at. A restore that had to
    # invent one would produce a row the counter predicates treat differently.
    assert exported.rows[0].occurred_at is None


# ---------------------------------------------------------------------------
# Target pins, holes and the targeted watermark
# ---------------------------------------------------------------------------


def _decision(db, *, game_session: GameSession, blunder: Blunder, served_at: datetime):
    db.add(
        OpponentDecision(
            decision_id=uuid.uuid4(),
            session_id=game_session.id,
            request_fingerprint=uuid.uuid4().hex,
            request_fen_hash="fen-hash",
            uci_history="[]",
            ply_before=0,
            served_at=served_at,
            response_payload="{}",
            target_blunder_id=blunder.id,
        )
    )
    db.flush()


def test_a_pair_an_eligible_target_still_needs_is_retained(db_session):
    """Selective retention: the pinned pair stays, its neighbours fold.

    The pin is what keeps ``targeted_reached_30d`` honest. Its attempt is in the
    denominator from ``opponent_decisions``, and the raw row is the ONLY source
    of the matching reach — folding it would delete a measured reach and leave
    the attempt counted.
    """
    user_id = 9101
    blunder = _blunder(db_session, user_id=user_id)
    pinned = _session(db_session, user_id=user_id, started_at=OLD)
    _event(db_session, blunder=blunder, game_session=pinned, reached=True)
    _decision(db_session, game_session=pinned, blunder=blunder,
              served_at=NOW - timedelta(days=2))
    loose = _session(db_session, user_id=user_id, started_at=OLD - timedelta(days=1))
    _event(db_session, blunder=blunder, game_session=loose)
    _enable_folding(db_session)
    db_session.commit()
    before = _counters(db_session, blunder.id, user_id=user_id)
    assert before.targeted_30d == 1 and before.targeted_reached_30d == 1
    db_session.commit()

    result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.rows_deleted == 1
    db_session.expire_all()
    survivor = db_session.query(BlunderOpportunityEvent).one()
    assert survivor.session_id == pinned.id
    assert _counters(db_session, blunder.id, user_id=user_id) == before


def test_an_expired_pin_leaves_a_hole_a_later_sweep_revisits(db_session):
    """The prefix jumps over a retained hole, and the hole still folds later.

    This is why the prefix may only ever FORBID: it is ``MAX(started_at)`` over
    what was actually folded, not a claim that everything older is gone. A sweep
    that filtered candidates by the prefix would strand this row permanently.
    """
    user_id = 9102
    blunder = _blunder(db_session, user_id=user_id)
    hole = _session(db_session, user_id=user_id, started_at=OLD - timedelta(days=5))
    _event(db_session, blunder=blunder, game_session=hole, reached=True)
    _decision(db_session, game_session=hole, blunder=blunder,
              served_at=NOW - timedelta(days=2))
    newer = _session(db_session, user_id=user_id, started_at=OLD)
    _event(db_session, blunder=blunder, game_session=newer)
    _enable_folding(db_session)
    db_session.commit()

    fold_user_batch(engine, user_id=user_id, limits=PATIENT)
    db_session.expire_all()
    state = db_session.get(UserOpportunityRetentionState, user_id)
    assert _utc(state.folded_through_started_at) == _utc(newer.started_at)
    assert db_session.query(BlunderOpportunityEvent).count() == 1

    # The pin ages out of the 30-day window. The hole is behind the prefix and
    # must still be found.
    db_session.query(OpponentDecision).update(
        {OpponentDecision.served_at: NOW - timedelta(days=200)}
    )
    db_session.commit()

    second = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert second.rows_deleted == 1
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 0
    state = db_session.get(UserOpportunityRetentionState, user_id)
    # The prefix does not move backwards to the hole it just filled.
    assert _utc(state.folded_through_started_at) == _utc(newer.started_at)


def test_discarding_a_targeted_pair_advances_the_targeted_watermark(db_session):
    user_id = 9103
    blunder = _blunder(db_session, user_id=user_id)
    game_session = _session(db_session, user_id=user_id, started_at=OLD)
    _event(db_session, blunder=blunder, game_session=game_session, reached=True)
    served = NOW - timedelta(days=200)
    _decision(db_session, game_session=game_session, blunder=blunder, served_at=served)
    _enable_folding(db_session)
    db_session.commit()

    fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    db_session.expire_all()
    state = db_session.get(UserOpportunityRetentionState, user_id)
    assert _utc(state.targeted_discarded_max_served_at) == _utc(served)
    # And the reader now refuses a targeted window that reaches back into it.
    with pytest.raises(RetentionInvariantError):
        load_opportunity_counters(
            db_session, [blunder.id], user_id=user_id,
            now=served + timedelta(days=15),
        )


def test_a_cross_pair_decision_does_not_advance_the_watermark(db_session):
    """The watermark is per PAIR, not per (session set x blunder set).

    Two folded pairs plus one decision that names a session from one and a
    blunder from the other: nothing about that decision was discarded, so
    narrowing targeted availability for it would forbid a window that is still
    perfectly answerable.
    """
    user_id = 9104
    first = _blunder(db_session, user_id=user_id)
    second = _blunder(db_session, user_id=user_id, fen=FEN.replace("w KQkq", "b KQkq"))
    session_a = _session(db_session, user_id=user_id, started_at=OLD)
    session_b = _session(db_session, user_id=user_id, started_at=OLD - timedelta(days=1))
    _event(db_session, blunder=first, game_session=session_a)
    _event(db_session, blunder=second, game_session=session_b)
    _decision(db_session, game_session=session_a, blunder=second,
              served_at=NOW - timedelta(days=200))
    _enable_folding(db_session)
    db_session.commit()

    result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.rows_deleted == 2
    db_session.expire_all()
    state = db_session.get(UserOpportunityRetentionState, user_id)
    assert state.targeted_discarded_max_served_at is None


# ---------------------------------------------------------------------------
# The protocol: export first, revalidate under the locks, refuse on doubt
# ---------------------------------------------------------------------------


def test_the_export_is_verified_before_any_lock_is_taken(db_session):
    """No export I/O happens while a user or blunder lock is held.

    Asserted by ORDER, not by timing: at the moment the export is written, the
    two acquisitions have not been attempted and every candidate row is still
    there. Filesystem latency has no useful worst case, so any of it inside the
    500 ms critical section would make that budget unenforceable.
    """
    user_id = 9201
    blunder, _ = _simple(db_session, user_id=user_id, rows=2)
    db_session.commit()
    observed = {}

    def spy(rows, **kwargs):
        observed["acquisitions"] = (acquire.call_count, interlock.call_count)
        observed["rows_present"] = db_session.query(BlunderOpportunityEvent).count()
        observed["manifests"] = db_session.query(OpportunityFoldBatch).count()
        db_session.commit()
        exported = real_write_export(rows, **kwargs)
        observed["verified_on_disk"] = exported.path.exists()
        return exported

    with patch("app.opportunity_fold._acquire_user", wraps=None) as acquire, patch(
        "app.opportunity_fold.lock_state_for_fold"
    ) as interlock, patch("app.opportunity_fold.write_export", side_effect=spy):
        acquire.return_value = True
        interlock.return_value = True
        result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.outcome is FoldOutcome.FOLDED
    assert observed["acquisitions"] == (0, 0)
    assert observed["rows_present"] == 2
    assert observed["manifests"] == 0
    assert observed["verified_on_disk"] is True
    # Export time and lock time are traced apart, so a slow disk never reads as
    # lock pressure.
    assert result.export_seconds > 0
    assert result.lock_seconds > 0


def test_a_row_that_changed_after_the_export_aborts_the_batch(db_session):
    """The recheck reads the database, not the export it just wrote."""
    user_id = 9202
    blunder, sessions = _simple(db_session, user_id=user_id, rows=2, reached_first=False)
    db_session.commit()

    def mutate(rows, **kwargs):
        exported = real_write_export(rows, **kwargs)
        db_session.query(BlunderOpportunityEvent).filter_by(
            session_id=sessions[0].id
        ).update({BlunderOpportunityEvent.reached: True})
        db_session.commit()
        return exported

    with patch("app.opportunity_fold.write_export", side_effect=mutate):
        result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.outcome is FoldOutcome.STALE_REPREPARE
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 2
    assert db_session.query(OpportunityFoldBatch).count() == 0
    assert db_session.get(BlunderOpportunitySummary, blunder.id).folded_eligible_count == 0
    # Nothing committed, so the artifact is provably an orphan and goes now.
    assert not list(export_dir().glob("*.json"))


def test_a_candidate_deleted_after_the_export_aborts_the_batch(db_session):
    user_id = 9203
    blunder, sessions = _simple(db_session, user_id=user_id, rows=2)
    db_session.commit()

    def remove(rows, **kwargs):
        exported = real_write_export(rows, **kwargs)
        db_session.query(BlunderOpportunityEvent).filter_by(
            session_id=sessions[0].id
        ).delete()
        db_session.commit()
        return exported

    with patch("app.opportunity_fold.write_export", side_effect=remove):
        result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.outcome is FoldOutcome.STALE_REPREPARE
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 1


def test_a_review_landing_after_the_export_is_folded_against_the_new_window(db_session):
    """A changed review alone is fine; the S predicate uses the LOCKED review.

    The review reset the folded since-review counters and moved the basis. Every
    event in this batch predates it, so their since-review contribution is zero —
    and the lifetime total, which no review resets, still gains all of them.
    """
    user_id = 9204
    blunder, sessions = _simple(db_session, user_id=user_id, rows=2, reached_first=True)
    db_session.commit()
    before = _counters(db_session, blunder.id, user_id=user_id)
    assert before.opportunities_since_review == 2
    db_session.commit()

    def review(rows, **kwargs):
        exported = real_write_export(rows, **kwargs)
        recent = _session(db_session, user_id=user_id, started_at=NOW - timedelta(days=1))
        row = BlunderReview(
            blunder_id=blunder.id, session_id=recent.id, reviewed_at=NOW - timedelta(hours=1),
            passed=True, move_played_san="good", eval_delta_cp=0,
        )
        db_session.add(row)
        db_session.flush()
        record_review_basis(
            db_session, blunder_id=blunder.id, review_id=row.id,
            reviewed_at=row.reviewed_at, session_id=recent.id,
            policy=load_policy(db_session),
        )
        db_session.commit()
        return exported

    with patch("app.opportunity_fold.write_export", side_effect=review):
        result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.outcome is FoldOutcome.FOLDED
    db_session.expire_all()
    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    assert summary.folded_eligible_count == 2
    assert summary.folded_opportunities_since_review == 0
    assert summary.folded_reached_since_review == 0
    after = _counters(db_session, blunder.id, user_id=user_id)
    assert after.event_count == 2
    assert after.opportunities_since_review == 0


def test_a_missing_summary_refuses_to_fold(db_session):
    """After readiness, a missing summary is the loss alarm — not a row to create."""
    user_id = 9205
    blunder, _ = _simple(db_session, user_id=user_id, rows=1)
    db_session.query(BlunderOpportunitySummary).filter_by(blunder_id=blunder.id).delete()
    db_session.commit()

    with pytest.raises(RetentionInvariantError, match="no opportunity summary"):
        fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 1


def test_a_summary_whose_review_basis_lags_refuses_to_fold(db_session):
    """The reader raises on this too; folding into it would deepen the disagreement."""
    user_id = 9206
    blunder, sessions = _simple(db_session, user_id=user_id, rows=1)
    review = BlunderReview(
        blunder_id=blunder.id, session_id=sessions[0].id,
        reviewed_at=NOW - timedelta(days=1), passed=True,
        move_played_san="good", eval_delta_cp=0,
    )
    db_session.add(review)
    db_session.commit()  # the summary still points at no review at all

    with pytest.raises(RetentionInvariantError, match="review basis"):
        fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 1


# ---------------------------------------------------------------------------
# Switches, boundaries and bounded batches
# ---------------------------------------------------------------------------


def test_folding_does_nothing_until_cleanup_is_enabled(db_session):
    user_id = 9301
    blunder, _ = _simple(db_session, user_id=user_id, rows=2)
    policy = db_session.get(OpportunityRetentionPolicy, 1)
    policy.cleanup_enabled = False
    db_session.commit()

    result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.outcome is FoldOutcome.DISABLED
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 2
    assert not export_dir().exists() or not list(export_dir().glob("*.json"))


def test_grace_is_a_drain_gap_on_top_of_the_freeze_boundary(db_session):
    """Frozen at M; foldable only at M + G. The gap is what G is.

    The session in the middle has stopped accepting evidence writes and is still
    not foldable: every writer that passed the freeze test has that hour to
    finish and commit before the compactor may touch the same rows.
    """
    user_id = 9302
    blunder = _blunder(db_session, user_id=user_id)
    policy = db_session.get(OpportunityRetentionPolicy, 1)
    frozen_only = _session(
        db_session, user_id=user_id,
        started_at=datetime.now(timezone.utc)
        - timedelta(days=policy.mutation_window_days, seconds=60),
    )
    _event(db_session, blunder=blunder, game_session=frozen_only)
    foldable = _session(
        db_session, user_id=user_id,
        started_at=datetime.now(timezone.utc)
        - timedelta(days=policy.mutation_window_days, seconds=policy.grace_seconds + 60),
    )
    _event(db_session, blunder=blunder, game_session=foldable)
    _enable_folding(db_session)
    db_session.commit()

    result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.rows_deleted == 1
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).one().session_id == frozen_only.id


def test_a_batch_is_bounded_by_pairs_and_by_distinct_blunders(db_session):
    user_id = 9303
    for index, fen in enumerate(DISTINCT_FENS):
        blunder = _blunder(db_session, user_id=user_id, fen=fen)
        for step in range(3):
            game_session = _session(
                db_session, user_id=user_id,
                started_at=OLD - timedelta(days=index * 10 + step),
            )
            _event(db_session, blunder=blunder, game_session=game_session)
    _enable_folding(db_session)
    db_session.commit()

    result = fold_user_batch(
        engine, user_id=user_id,
        limits=FoldLimits(transaction_deadline=30.0, max_pairs=10, max_blunders=2),
    )

    assert result.blunders == 2
    assert result.rows_deleted == 6
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 6


def test_a_batch_that_runs_out_of_budget_deletes_nothing(db_session):
    """The deadline is a whole-transaction bound, not a per-statement one."""
    user_id = 9304
    blunder, _ = _simple(db_session, user_id=user_id, rows=2)
    db_session.commit()

    result = fold_user_batch(
        engine, user_id=user_id, limits=FoldLimits(transaction_deadline=0.0),
    )

    assert result.outcome is FoldOutcome.DEADLINE_EXCEEDED
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 2
    assert db_session.query(OpportunityFoldBatch).count() == 0
    assert not list(export_dir().glob("*.json"))


@pytest.mark.parametrize(
    "seam,outcome",
    [
        ("app.opportunity_fold._acquire_user", FoldOutcome.SKIPPED_USER_BUSY),
        ("app.opportunity_fold.lock_state_for_fold", FoldOutcome.SKIPPED_PUBLICATION),
    ],
)
def test_a_busy_user_is_skipped_and_nothing_is_deleted(db_session, seam, outcome):
    """Contention is "skip this user", never "nothing to fold" and never a wait."""
    user_id = 9305
    blunder, _ = _simple(db_session, user_id=user_id, rows=2)
    db_session.commit()

    with patch(seam, return_value=False):
        result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.outcome is outcome
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 2
    assert db_session.query(OpportunityFoldBatch).count() == 0
    assert not list(export_dir().glob("*.json"))


def test_an_interrupted_transfer_rolls_back_the_entire_batch(db_session):
    """Increment, delete, manifest and prefix are one fact or none of them."""
    user_id = 9306
    blunder, _ = _simple(db_session, user_id=user_id, rows=3)
    db_session.commit()

    # The manifest INSERT is the last write before the commit, so a failure here
    # is the harshest form of "interrupted": the rows are already deleted and the
    # summaries already incremented inside the open transaction.
    with patch("app.opportunity_fold.insert", side_effect=RuntimeError("power cut")):
        with pytest.raises(RuntimeError, match="power cut"):
            fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 3
    assert db_session.get(BlunderOpportunitySummary, blunder.id).folded_eligible_count == 0
    assert db_session.query(OpportunityFoldBatch).count() == 0
    assert db_session.get(UserOpportunityRetentionState, user_id).folded_through_started_at is None
    assert not list(export_dir().glob("*.json"))


def test_replaying_an_already_committed_batch_cannot_double_count(db_session):
    """A duplicate apply finds its rows gone and aborts. Counters do not move."""
    user_id = 9307
    blunder, _ = _simple(db_session, user_id=user_id, rows=2)
    db_session.commit()
    captured = {}
    real_prepare = opportunity_fold._prepare

    def remember(db, **kwargs):
        captured["prepared"] = real_prepare(db, **kwargs)
        return captured["prepared"]

    with patch("app.opportunity_fold._prepare", side_effect=remember):
        first = fold_user_batch(engine, user_id=user_id, limits=PATIENT)
    assert first.outcome is FoldOutcome.FOLDED
    db_session.expire_all()
    after_first = _counters(db_session, blunder.id, user_id=user_id)
    db_session.commit()

    with patch("app.opportunity_fold._prepare", return_value=captured["prepared"]):
        replay = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert replay.outcome is FoldOutcome.STALE_REPREPARE
    db_session.expire_all()
    assert _counters(db_session, blunder.id, user_id=user_id) == after_first
    assert db_session.query(OpportunityFoldBatch).count() == 1


def test_a_sweep_rotates_users_and_shrinks_a_slow_batch(db_session):
    """Rotation, cooldown and downward adaptation, in one report.

    ``hold_target=0`` makes every batch "slow", which is the only honest way to
    exercise the shrink deterministically: a real timing threshold would make
    this test a measurement of the machine it runs on.
    """
    users = [9401, 9402]
    for user_id in users:
        blunder = _blunder(db_session, user_id=user_id)
        for index in range(8):
            game_session = _session(db_session, user_id=user_id,
                                    started_at=OLD - timedelta(days=index))
            _event(db_session, blunder=blunder, game_session=game_session)
    _enable_folding(db_session)
    db_session.commit()

    report = sweep(
        engine, user_ids=list(users),
        limits=FoldLimits(transaction_deadline=30.0, max_pairs=4, min_pairs=1,
                          hold_target=0.0, user_cooldown=0.0),
        max_batches=20,
    )

    assert report.rows_deleted == 16
    assert report.outcomes["folded"] >= 4
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 0
    sizes = [
        batch.row_count
        for batch in db_session.query(OpportunityFoldBatch)
        .filter_by(user_id=users[0])
        .order_by(OpportunityFoldBatch.committed_at)
    ]
    assert sizes[0] == 4 and min(sizes) < 4
    assert set(report.outcomes) <= {"folded", "nothing_eligible"}


# ---------------------------------------------------------------------------
# The named regression: a folded pair must never be reinserted by repair
# ---------------------------------------------------------------------------


def test_blunder_grain_repair_never_reinserts_a_folded_pair(db_session):
    """Fold a pair, then run the repair whose graph walk still matches it.

    ``UNIQUE(session_id, blunder_id)`` cannot protect a row that has already been
    DELETED, so the only thing standing between a repair and a double count is
    the session-deadline check — and it has to happen before the blunder-grain
    upsert, not only before the session-grain one.

    Repeated after increasing M on purpose. Widening the window moves the AGE arm
    back until it no longer covers this session, which leaves the permanent fold
    PREFIX as the only thing forbidding the write. That is exactly the arm a
    later policy change must not be able to relax.
    """
    from scripts.recompute_srs_opportunities import recompute_one_blunder

    user_id = 9501
    _user(db_session, user_id)
    ancestor_fen = "8/8/8/8/8/8/8/K6k w - - 0 1"
    opponent_fen = "8/8/8/8/8/8/8/1K5k b - - 0 1"
    blunder_fen = "8/8/8/8/8/8/8/2K4k w - - 0 2"
    ancestor = _position(db_session, user_id=user_id, fen=ancestor_fen)
    opponent = _position(db_session, user_id=user_id, fen=opponent_fen,
                         active_color="black")
    blunder_position = _position(db_session, user_id=user_id, fen=blunder_fen)
    db_session.add_all([
        Move(from_position_id=ancestor.id, move_san="a", to_position_id=opponent.id),
        Move(from_position_id=opponent.id, move_san="b",
             to_position_id=blunder_position.id),
    ])
    blunder = Blunder(
        user_id=user_id, position_id=blunder_position.id, bad_move_san="bad",
        best_move_san="good", eval_loss_cp=200,
        created_at=NOW - timedelta(days=365),
    )
    db_session.add(blunder)
    db_session.flush()
    db_session.add(BlunderOpportunitySummary(blunder_id=blunder.id))
    game_session = _session(db_session, user_id=user_id, started_at=OLD)
    db_session.add(
        SessionMove(
            session_id=game_session.id, move_number=1, color="white", move_san="a",
            fen_before=ancestor_fen, fen_after=opponent_fen,
        )
    )
    db_session.commit()

    # Control: the walk DOES match this session, so an unguarded repair would
    # recreate the row the fold is about to delete.
    recompute_one_blunder(db_session, blunder_id=blunder.id)
    db_session.commit()
    assert db_session.query(BlunderOpportunityEvent).count() == 1

    _enable_folding(db_session)
    db_session.commit()
    assert fold_user_batch(engine, user_id=user_id, limits=PATIENT).rows_deleted == 1
    db_session.expire_all()
    folded = _counters(db_session, blunder.id, user_id=user_id)
    priority = opportunity_priority(
        counters=folded, pass_streak=0, last_reviewed_at=None,
        created_at=blunder.created_at, now=NOW,
    )
    db_session.commit()

    for widened in (None, 3650):
        if widened is not None:
            db_session.get(OpportunityRetentionPolicy, 1).mutation_window_days = widened
            db_session.commit()

        recompute_one_blunder(db_session, blunder_id=blunder.id)
        db_session.commit()

        db_session.expire_all()
        assert db_session.query(BlunderOpportunityEvent).count() == 0
        summary = db_session.get(BlunderOpportunitySummary, blunder.id)
        assert summary.folded_eligible_count == 1
        assert _counters(db_session, blunder.id, user_id=user_id) == folded
        assert opportunity_priority(
            counters=_counters(db_session, blunder.id, user_id=user_id),
            pass_streak=0, last_reviewed_at=None,
            created_at=blunder.created_at, now=NOW,
        ) == priority
        db_session.commit()


# ---------------------------------------------------------------------------
# Finite recovery
# ---------------------------------------------------------------------------


def _pause_folding(db) -> None:
    """What an operator does before restoring: stop the compactor, keep the freeze.

    The ladder allows exactly this — cleanup off, freeze on — and restoring with
    the compactor still running would let a fold delete rows behind the restore
    and produce a state neither of them describes.
    """
    db.get(OpportunityRetentionPolicy, 1).cleanup_enabled = False
    db.commit()


def test_a_restore_returns_the_exact_rows_and_the_counters_they_carried(db_session):
    user_id = 9601
    blunder, sessions = _simple(db_session, user_id=user_id, rows=3)
    db_session.commit()
    original = {
        (row.id, row.session_id, row.occurred_at, row.opportunity, row.reached)
        for row in db_session.query(BlunderOpportunityEvent)
    }
    before = _counters(db_session, blunder.id, user_id=user_id)
    db_session.commit()

    fold_user_batch(engine, user_id=user_id, limits=PATIENT)
    _pause_folding(db_session)

    report = restore_folded_evidence(engine)

    assert report.batches == 1 and report.rows_restored == 3
    db_session.expire_all()
    restored = {
        (row.id, row.session_id, row.occurred_at, row.opportunity, row.reached)
        for row in db_session.query(BlunderOpportunityEvent)
    }
    assert restored == original
    assert _counters(db_session, blunder.id, user_id=user_id) == before
    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    assert summary.folded_eligible_count == 0
    state = db_session.get(UserOpportunityRetentionState, user_id)
    assert state.folded_through_started_at is None
    assert db_session.query(OpportunityFoldBatch).one().restored_at is not None


def test_a_review_after_the_fold_leaves_the_since_review_counters_alone(db_session):
    """The manifest's recorded basis is what makes this exact.

    The review already zeroed the two since-review counters and moved the basis,
    so the batch's share of them is gone. Subtracting the recorded deltas again
    would drive a counter negative for a window that no longer exists.
    """
    user_id = 9602
    blunder, sessions = _simple(db_session, user_id=user_id, rows=2)
    db_session.commit()
    fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    db_session.expire_all()
    recent = _session(db_session, user_id=user_id, started_at=NOW - timedelta(days=1))
    review = BlunderReview(
        blunder_id=blunder.id, session_id=recent.id, reviewed_at=NOW,
        passed=True, move_played_san="good", eval_delta_cp=0,
    )
    db_session.add(review)
    db_session.flush()
    record_review_basis(
        db_session, blunder_id=blunder.id, review_id=review.id,
        reviewed_at=review.reviewed_at, session_id=recent.id,
        policy=load_policy(db_session),
    )
    db_session.commit()
    _pause_folding(db_session)

    report = restore_folded_evidence(engine)

    assert report.since_review_left_alone == 1
    db_session.expire_all()
    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    assert summary.folded_eligible_count == 0
    assert summary.folded_opportunities_since_review == 0
    assert summary.folded_reached_since_review == 0
    assert db_session.query(BlunderOpportunityEvent).count() == 2


def test_a_purge_after_the_fold_is_not_undone_by_a_restore(db_session):
    """Deleting your training history outranks a rollback."""
    user_id = 9603
    kept = _blunder(db_session, user_id=user_id, fen=DISTINCT_FENS[0])
    purged = _blunder(db_session, user_id=user_id, fen=DISTINCT_FENS[1])
    for index, blunder in enumerate((kept, purged)):
        game_session = _session(db_session, user_id=user_id,
                                started_at=OLD - timedelta(days=index))
        _event(db_session, blunder=blunder, game_session=game_session)
    _enable_folding(db_session)
    db_session.commit()

    assert fold_user_batch(engine, user_id=user_id, limits=PATIENT).rows_deleted == 2
    db_session.expire_all()
    db_session.query(BlunderOpportunitySummary).filter_by(blunder_id=purged.id).delete()
    db_session.query(Blunder).filter_by(id=purged.id).delete()
    db_session.commit()
    _pause_folding(db_session)

    report = restore_folded_evidence(engine)

    assert report.rows_restored == 1
    assert report.rows_skipped_missing_parent == 1
    assert report.summaries_missing == 1
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).one().blunder_id == kept.id
    assert db_session.get(Blunder, purged.id) is None


def test_evidence_written_after_the_fold_survives_the_restore(db_session):
    """Restoration adds the missing ids back; it does not rewrite what is there."""
    user_id = 9604
    blunder, _ = _simple(db_session, user_id=user_id, rows=1)
    db_session.commit()
    fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    db_session.expire_all()
    later = _session(db_session, user_id=user_id, started_at=NOW - timedelta(days=1))
    _event(db_session, blunder=blunder, game_session=later, reached=True)
    db_session.commit()
    _pause_folding(db_session)

    restore_folded_evidence(engine)

    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 2
    assert (
        db_session.query(BlunderOpportunityEvent)
        .filter_by(session_id=later.id).one().reached is True
    )


def test_restoring_twice_is_a_no_op(db_session):
    user_id = 9605
    blunder, _ = _simple(db_session, user_id=user_id, rows=2)
    db_session.commit()
    fold_user_batch(engine, user_id=user_id, limits=PATIENT)
    _pause_folding(db_session)

    first = restore_folded_evidence(engine)
    second = restore_folded_evidence(engine)

    assert first.rows_restored == 2
    assert second.batches == 0 and second.rows_restored == 0
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 2
    assert db_session.get(BlunderOpportunitySummary, blunder.id).folded_eligible_count == 0


def test_a_restore_refuses_while_the_compactor_is_still_enabled(db_session):
    user_id = 9606
    _simple(db_session, user_id=user_id, rows=1)
    db_session.commit()
    fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    with pytest.raises(RecoveryRefused, match="folding is still enabled"):
        restore_folded_evidence(engine)


def test_a_restore_after_the_window_closes_refuses(db_session):
    """Seven days after the FIRST deletion, the exports are gone. So is recovery."""
    user_id = 9607
    _simple(db_session, user_id=user_id, rows=1)
    db_session.commit()
    fold_user_batch(engine, user_id=user_id, limits=PATIENT)
    _pause_folding(db_session)
    db_session.get(OpportunityRetentionPolicy, 1).first_fold_committed_at = (
        datetime.now(timezone.utc) - timedelta(days=8)
    )
    db_session.commit()

    with pytest.raises(RecoveryExpired, match="recovery window closed"):
        restore_folded_evidence(engine)

    status = fold_status(engine)
    assert status.expired is True
    assert status.unrestored_batches == 1


def test_a_tampered_artifact_refuses_to_restore(db_session):
    """Two digests, two questions. Intact bytes are not the same as the right rows."""
    user_id = 9608
    _simple(db_session, user_id=user_id, rows=2)
    db_session.commit()
    fold_user_batch(engine, user_id=user_id, limits=PATIENT)
    _pause_folding(db_session)
    db_session.expire_all()
    batch = db_session.query(OpportunityFoldBatch).one()
    artifact = Path(batch.artifact_uri)
    document = json.loads(artifact.read_text())
    document["rows"][0]["reached"] = not document["rows"][0]["reached"]
    artifact.write_text(json.dumps(document, sort_keys=True))

    report = restore_folded_evidence(engine)

    assert report.rows_restored == 0
    assert [failed for failed, _ in report.failures] == [batch.batch_id]
    assert "rowset" in report.failures[0][1] or "digest" in report.failures[0][1]
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 0
    # Reported, not restored: the batch stays claimed by its manifest so a later
    # run can retry it once the artifact is dealt with.
    assert db_session.query(OpportunityFoldBatch).one().restored_at is None
    assert fold_status(engine).first_fold_committed_at is not None


def test_one_unreadable_artifact_does_not_strand_the_batches_behind_it(db_session):
    """A rollback is not all-or-nothing across USERS, and never a lost report.

    The batch that cannot be read back stays unrestored — its prefix still covers
    its rows and a later run retries it — while every other batch goes back. The
    failure is in the report rather than in a traceback precisely because the
    report is what the operator needs most at that moment.
    """
    broken, intact = 9631, 9632
    _simple(db_session, user_id=broken, rows=2)
    _simple(db_session, user_id=intact, rows=2)
    db_session.commit()
    fold_user_batch(engine, user_id=broken, limits=PATIENT)
    fold_user_batch(engine, user_id=intact, limits=PATIENT)
    _pause_folding(db_session)
    db_session.expire_all()
    doomed = (
        db_session.query(OpportunityFoldBatch).filter_by(user_id=broken).one()
    )
    Path(doomed.artifact_uri).unlink()

    report = restore_folded_evidence(engine)

    assert report.rows_restored == 2
    assert report.batches == 1
    assert [failed for failed, _ in report.failures] == [doomed.batch_id]
    # One user is still folded, so the clock keeps running for them.
    assert report.anchor_cleared is False
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 2
    state = db_session.get(UserOpportunityRetentionState, broken)
    assert state.folded_through_started_at is not None


def test_an_id_held_by_a_different_pair_stops_the_restore(db_session):
    """"That id exists" is not "that row is back", and the difference is a row.

    An event id occupied by somebody else's pair means the id space has been
    rewritten under the rollback — the case a backwards ``setval`` used to
    create. Counting it as "already present" would report a clean restore over a
    row that is gone for good, so it stops and says what it found.
    """
    user_id = 9633
    blunder, _ = _simple(db_session, user_id=user_id, rows=2)
    folded_ids = sorted(
        event_id for (event_id,) in db_session.query(BlunderOpportunityEvent.id)
    )
    db_session.commit()
    fold_user_batch(engine, user_id=user_id, limits=PATIENT)
    _pause_folding(db_session)

    db_session.expire_all()
    squatter = _blunder(db_session, user_id=user_id, fen=DISTINCT_FENS[1])
    carrier = _session(db_session, user_id=user_id, started_at=NOW - timedelta(days=1))
    db_session.add(BlunderOpportunityEvent(
        id=folded_ids[0], blunder_id=squatter.id, session_id=carrier.id,
        occurred_at=carrier.started_at, opportunity=True, reached=False,
    ))
    db_session.commit()

    report = restore_folded_evidence(engine)

    assert report.rows_restored == 0
    assert len(report.failures) == 1
    assert "held by pair" in report.failures[0][1]
    db_session.expire_all()
    # The squatter is untouched and nothing was half-restored.
    assert db_session.query(BlunderOpportunityEvent).count() == 1
    assert db_session.query(OpportunityFoldBatch).one().restored_at is None


def test_a_complete_rollback_inside_the_window_stops_the_clock(db_session):
    """The anchor is not a memorial. It protects deleted rows, and only those.

    Without this a canary — fold a little, roll it back, look at the result —
    would spend the one recovery window the real rollout needs, and day fourteen
    would open with ``restore`` refusing artifacts it had just written and
    verified.
    """
    first, second = 9634, 9635
    _simple(db_session, user_id=first, rows=2)
    _simple(db_session, user_id=second, rows=2)
    db_session.commit()
    fold_user_batch(engine, user_id=first, limits=PATIENT)
    fold_user_batch(engine, user_id=second, limits=PATIENT)
    _pause_folding(db_session)
    assert fold_status(engine).first_fold_committed_at is not None

    partial = restore_folded_evidence(engine, user_ids=[first])

    # Half a rollback is still a rollback in progress: one user's rows are gone.
    assert partial.anchor_cleared is False
    assert fold_status(engine).first_fold_committed_at is not None

    rest = restore_folded_evidence(engine)

    assert rest.anchor_cleared is True
    status = fold_status(engine)
    assert status.first_fold_committed_at is None
    assert status.deadline is None and status.unrestored_batches == 0

    # And the next fold starts a fresh seven days rather than inheriting a spent
    # one, which is the whole point of clearing it.
    db_session.expire_all()
    _enable_folding(db_session)
    db_session.commit()
    assert fold_user_batch(engine, user_id=first, limits=PATIENT).rows_deleted == 2
    assert fold_status(engine).first_fold_committed_at is not None


def test_a_purged_owners_export_does_not_wait_out_the_window(db_session):
    """An export whose owner's evidence was deleted is not an orphan to age out.

    A deletion — of the account, or of the training history under it — cascades
    or removes the manifest and leaves the export behind. A restore could not use
    it either way: the parents are gone, so its rows would be skipped. Waiting
    out a window that cannot bring them back would only keep deleted history on
    disk.

    The owner is tested by its retention-state row rather than its ``users`` row,
    because a training-history purge removes the first and keeps the second, and
    both halves have to reach this.
    """
    user_id = 9636
    _user(db_session, user_id)
    db_session.add(UserOpportunityRetentionState(user_id=user_id))
    db_session.commit()
    directory = export_dir()
    directory.mkdir(parents=True, exist_ok=True)
    gone = directory / f"fold-424242-{uuid.uuid4()}.json"
    gone.write_text("{}")
    kept = directory / f"fold-{user_id}-{uuid.uuid4()}.json"
    kept.write_text("{}")

    report = expire_fold_artifacts(engine)

    assert report.orphans_deleted == 1
    assert not gone.exists()
    # A fresh orphan whose owner still has evidence is a fold that may still be
    # in flight — a fold commits that row before it writes an export. It waits
    # out the window like any other.
    assert kept.exists()

    # The training-history half: the account survives, its evidence does not.
    db_session.delete(db_session.get(UserOpportunityRetentionState, user_id))
    db_session.commit()

    assert expire_fold_artifacts(engine).orphans_deleted == 1
    assert not kept.exists()


def test_the_purged_owner_rule_is_off_where_two_databases_share_a_directory(
    db_session, tmp_path, monkeypatch
):
    """An unknown owner id only means "purged" when ONE database owns the files.

    The built-in default directory sits beside the backend package, so every
    local database a developer points at this checkout writes into it. There an
    id this database has never heard of is at least as likely to belong to
    another one, and deleting its export on the spot would take out a live
    recovery of a database this sweep was never run against — where the age rule
    it used to fall under would have waited seven days, by which time the window
    had closed everywhere. A configured directory is the operator saying which
    deployment owns it.
    """
    directory = tmp_path / "shared"
    directory.mkdir()
    monkeypatch.delenv("GHOSTREPLAY_SRS_FOLD_EXPORT_DIR")
    monkeypatch.setattr(opportunity_fold_recovery, "export_dir", lambda: directory)
    stranger = directory / f"fold-424242-{uuid.uuid4()}.json"
    stranger.write_text("{}")

    assert expire_fold_artifacts(engine).orphans_deleted == 0
    assert stranger.exists()


def test_an_orphan_ages_in_utc_not_in_the_hosts_local_time(db_session):
    """A filesystem mtime is epoch seconds, and reading it as local time lies.

    Read naively, a host west of Greenwich calls every file hours older than it
    is and sweeps away orphans that are still inside the window — on the one
    codepath whose entire job is to respect a seven-day boundary.
    """
    user_id = 9637
    _user(db_session, user_id)
    # Evidence still under this owner, so these files are aged rather than taken
    # as deleted-on-request — the age rule is what this test is about.
    db_session.add(UserOpportunityRetentionState(user_id=user_id))
    db_session.commit()
    directory = export_dir()
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    young = directory / f"fold-{user_id}-{uuid.uuid4()}.json"
    old = directory / f"fold-{user_id}-{uuid.uuid4()}.json"
    for path, age in ((young, timedelta(days=6, hours=23)),
                      (old, timedelta(days=7, hours=1))):
        path.write_text("{}")
        stamp = (now - age).timestamp()
        os.utime(path, (stamp, stamp))

    report = expire_fold_artifacts(engine)

    assert report.orphans_deleted == 1
    assert young.exists()
    assert not old.exists()


def test_a_cancelled_interlock_statement_is_a_deadline_not_a_skip(db_session):
    """57014 from the interlock is the budget running out, not a publication.

    A NOWAIT acquisition cannot wait long enough to be cancelled, so a
    cancellation there is the fold's own ``statement_timeout``. Filing it under
    ``skipped_publication`` would bury a deadline overrun in the one counter that
    is expected to be nonzero during normal operation.
    """
    user_id = 9638
    _simple(db_session, user_id=user_id, rows=2)
    db_session.commit()

    class _Cancelled(Exception):
        sqlstate = "57014"

    cancelled = OperationalError(
        "SELECT user_id FROM user_opportunity_retention_state", {},
        _Cancelled("canceling statement due to statement timeout"),
    )
    with patch("app.opportunity_fold.lock_state_for_fold", side_effect=cancelled):
        result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.outcome is FoldOutcome.DEADLINE_EXCEEDED
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 2
    assert not list(export_dir().glob("*.json"))


def test_expiry_removes_committed_manifests_their_artifacts_and_orphans(db_session):
    """Both kinds of leftover, and neither on anything but age.

    An orphan is a file whose batch never committed. Nothing will ever claim it,
    so it cannot be cleaned up by following a manifest — only by age.
    """
    user_id = 9609
    _simple(db_session, user_id=user_id, rows=2)
    db_session.commit()
    fold_user_batch(engine, user_id=user_id, limits=PATIENT)
    db_session.expire_all()
    batch = db_session.query(OpportunityFoldBatch).one()
    artifact = Path(batch.artifact_uri)
    assert artifact.exists()

    orphan = artifact.parent / f"fold-{user_id}-{uuid.uuid4()}.json"
    orphan.write_text("{}")
    partial = artifact.parent / f"fold-{user_id}-{uuid.uuid4()}.json.partial"
    partial.write_text("{")
    stale = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
    for path in (orphan, partial):
        os.utime(path, (stale, stale))
    fresh = artifact.parent / f"fold-{user_id}-{uuid.uuid4()}.json"
    fresh.write_text("{}")

    # Nothing has expired yet: the committed batch has seven days to run.
    quiet = expire_fold_artifacts(engine)
    assert quiet.manifests_deleted == 0
    assert quiet.orphans_deleted == 2
    assert artifact.exists() and fresh.exists()

    # Age the whole row, not just its expiry: ``expires_at > committed_at`` is a
    # check constraint, and a batch that expired before it committed is not a
    # state the schema allows to exist.
    batch.committed_at = datetime.now(timezone.utc) - timedelta(days=8)
    batch.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.commit()

    report = expire_fold_artifacts(engine)

    assert report.manifests_deleted == 1
    assert report.artifacts_deleted == 1
    assert not artifact.exists()
    db_session.expire_all()
    assert db_session.query(OpportunityFoldBatch).count() == 0


def test_the_operator_cli_imports_and_parses(monkeypatch):
    """Import-time and argparse smoke, before any database work.

    The module resolves the production engine at import, so this is deliberately
    the whole of the CLI's unit coverage: the behaviour it drives is tested
    directly above, and a test that pointed it at a database would be testing
    argparse against a fixture.
    """
    import sys

    from scripts.fold_srs_opportunities import parse_args

    assert parse_args(["status"]).verb == "status"
    assert parse_args(["sweep", "--user", "7", "--max-pairs", "5"]).users == [7]
    assert parse_args(["restore", "--batch", "abc"]).batches == ["abc"]
    assert parse_args(["expire"]).verb == "expire"
    monkeypatch.setattr(sys, "argv", ["fold_srs_opportunities.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        parse_args()
    assert exc.value.code == 0


def test_a_failed_export_costs_a_sweep_and_not_a_row(db_session):
    """No export, no deletion — a slow or full disk never loses evidence."""
    from app.opportunity_fold_export import FoldExportError

    user_id = 9701
    blunder, _ = _simple(db_session, user_id=user_id, rows=2)
    db_session.commit()

    with patch("app.opportunity_fold.write_export",
               side_effect=FoldExportError("no space left on device")):
        result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.outcome is FoldOutcome.EXPORT_FAILED
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 2
    assert db_session.query(OpportunityFoldBatch).count() == 0


def test_one_users_failure_does_not_end_the_sweep(db_session):
    """Per-user failures are counted and reported, never fatal to the rotation."""
    broken, healthy = 9801, 9802
    for user_id in (broken, healthy):
        blunder = _blunder(db_session, user_id=user_id, fen=DISTINCT_FENS[0]
                           if user_id == broken else DISTINCT_FENS[1])
        game_session = _session(db_session, user_id=user_id, started_at=OLD)
        _event(db_session, blunder=blunder, game_session=game_session)
    _enable_folding(db_session)
    db_session.commit()
    real_fold = opportunity_fold.fold_user_batch

    def explode(engine_, *, user_id, **kwargs):
        if user_id == broken:
            raise RuntimeError("disk on fire")
        return real_fold(engine_, user_id=user_id, **kwargs)

    with patch("app.opportunity_fold.fold_user_batch", side_effect=explode):
        report = sweep(engine, user_ids=[broken, healthy],
                       limits=FoldLimits(transaction_deadline=30.0, user_cooldown=0.0))

    assert report.errors and "disk on fire" in report.errors[0]
    assert report.rows_deleted == 1
    db_session.expire_all()
    # The broken user's row survives untouched; the healthy one's is folded.
    remaining = db_session.query(BlunderOpportunityEvent).one()
    assert db_session.get(Blunder, remaining.blunder_id).user_id == broken


def test_the_pin_lookup_follows_the_selected_targeting_source(db_session, monkeypatch):
    """The g-retain-decisions coordination gate, as a test.

    Before replay envelopes are pruned, the compactor's pin lookup must consume
    the same backfilled compact facts the counters consume. If it kept reading
    envelopes after the cutover it would stop seeing pins that still exist, and
    quietly delete the reaches behind them.
    """
    from app.opponent_target_facts import backfill_target_facts

    user_id = 9901
    blunder = _blunder(db_session, user_id=user_id)
    pinned = _session(db_session, user_id=user_id, started_at=OLD)
    _event(db_session, blunder=blunder, game_session=pinned, reached=True)
    _decision(db_session, game_session=pinned, blunder=blunder,
              served_at=NOW - timedelta(days=2))
    _enable_folding(db_session)
    backfill_target_facts(db_session)
    db_session.commit()
    # Cut over to facts and delete the envelope the pin used to come from.
    monkeypatch.setenv("OPPONENT_TARGET_SOURCE", "facts")
    db_session.query(OpponentDecision).delete()
    db_session.commit()

    result = fold_user_batch(engine, user_id=user_id, limits=PATIENT)

    assert result.outcome is FoldOutcome.NOTHING_ELIGIBLE
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 1


def test_a_cooling_user_is_rotated_past_not_waited_on(db_session):
    """The cooldown is a rotation, not a stall.

    With a cooldown longer than the whole test, a sweep that WAITED after the
    first user would never reach the second. Rotating past a cooling user is
    what makes the per-user cooldown free: it costs ordering, not throughput.
    """
    users = [9811, 9812]
    for user_id in users:
        blunder = _blunder(db_session, user_id=user_id,
                           fen=DISTINCT_FENS[users.index(user_id)])
        game_session = _session(db_session, user_id=user_id, started_at=OLD)
        _event(db_session, blunder=blunder, game_session=game_session)
    _enable_folding(db_session)
    db_session.commit()

    started = time.monotonic()
    report = sweep(
        engine, user_ids=list(users),
        limits=FoldLimits(transaction_deadline=30.0, user_cooldown=60.0),
        max_batches=4,
    )
    elapsed = time.monotonic() - started

    assert report.rows_deleted == 2
    assert elapsed < 10.0
    db_session.expire_all()
    assert db_session.query(BlunderOpportunityEvent).count() == 0
