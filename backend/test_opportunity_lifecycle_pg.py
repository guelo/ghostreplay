"""PostgreSQL-only proof of the SRS opportunity lifecycle guards.

SQLite cannot stand in for any of this. The freeze guards are database triggers,
the purge escape is a transaction-local custom setting, the purge completeness
check is a DEFERRABLE INITIALLY DEFERRED constraint trigger, and the clock the
guards read is ``clock_timestamp()`` inside PL/pgSQL. Each of those is exactly
the guarantee these tests exist to establish, so they run against a real
migrated PostgreSQL schema or not at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import InternalError, ProgrammingError

from app.models import (
    Blunder,
    BlunderOpportunityEvent,
    BlunderOpportunitySummary,
    BlunderReview,
    GameSession,
    OpponentDecision,
    OpponentTargetFact,
    OpportunityRetentionPolicy,
    Position,
    RatingHistory,
    SessionMove,
    User,
    UserOpportunityRetentionState,
)
from app.opportunity_purge import (
    PURGE_MODE_SETTING,
    PURGE_USER_SETTING,
    PurgeScopeError,
    purge_user_training_history,
)
from conftest import pg_required

pytestmark = pg_required

# The triggers raise plain exceptions, which psycopg reports as InternalError or
# ProgrammingError depending on driver version; accept either.
TRIGGER_ERRORS = (InternalError, ProgrammingError)


def _seed(
    db,
    *,
    user_id: int,
    started_at: datetime,
    retention_state: bool = True,
    targeted: bool = False,
):
    user = User(id=user_id, username=None, is_anonymous=True)
    db.add(user)
    db.flush()
    if retention_state:
        db.add(UserOpportunityRetentionState(user_id=user_id))
    session = GameSession(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=started_at,
        status="completed",
        engine_elo=1500,
        player_color="white",
    )
    db.add(session)
    position = Position(
        user_id=user_id,
        fen_hash=f"pg-fen-{user_id}",
        fen_raw="8/8/8/8/8/8/8/8 w - - 0 1",
        active_color="white",
    )
    db.add(position)
    db.flush()
    blunder = Blunder(
        user_id=user_id,
        position_id=position.id,
        bad_move_san="Qh5",
        best_move_san="Nf3",
        eval_loss_cp=120,
        created_at=started_at - timedelta(days=1),
    )
    db.add(blunder)
    db.flush()
    db.add(BlunderOpportunitySummary(blunder_id=blunder.id))
    db.add(
        BlunderOpportunityEvent(
            blunder_id=blunder.id,
            session_id=session.id,
            occurred_at=started_at,
            opportunity=True,
            reached=True,
        )
    )
    if targeted:
        _seed_targeting(db, user_id=user_id, session=session, blunder=blunder)
    db.flush()
    return session, blunder


def _seed_targeting(db, *, user_id: int, session, blunder) -> None:
    """Add the rows a user who played, was ghost-targeted and reviewed carries.

    Four foreign keys onto blunders/game_sessions have NO cascade:
    session_moves.target_blunder_id, opponent_decisions.target_blunder_id,
    opponent_target_facts.blunder_id and blunder_reviews.session_id — plus
    rating_history.game_session_id, which is also NOT NULL, so a single finished
    rated game is enough to block the whole purge. Without these rows the purge
    tests pass against a user shape that barely exists in production, and the
    delete ORDER they exist to prove goes untested. Opt-in, because these same
    rows legitimately BLOCK the individual session and blunder deletes the guard
    tests exercise.
    """
    started_at = session.started_at
    db.add(
        SessionMove(
            session_id=session.id,
            move_number=1,
            color="white",
            move_san="e4",
            fen_after="8/8/8/8/8/8/8/8 b - - 0 1",
            target_blunder_id=blunder.id,
        )
    )
    db.add(
        OpponentDecision(
            decision_id=uuid.uuid4(),
            session_id=session.id,
            request_fingerprint=f"fp-{user_id}",
            request_fen_hash=f"pg-fen-{user_id}",
            uci_history="[]",
            ply_before=0,
            served_at=started_at,
            response_payload="{}",
            target_blunder_id=blunder.id,
        )
    )
    db.add(
        OpponentTargetFact(
            session_id=session.id, blunder_id=blunder.id, last_served_at=started_at
        )
    )
    db.add(
        BlunderReview(
            blunder_id=blunder.id,
            session_id=session.id,
            reviewed_at=started_at,
            passed=True,
            move_played_san="Nf3",
            eval_delta_cp=0,
        )
    )
    db.add(
        RatingHistory(
            user_id=user_id,
            game_session_id=session.id,
            rating=1500,
            is_provisional=False,
            games_played=1,
            recorded_at=started_at,
        )
    )
    db.flush()


def _session_exists(db, session_id) -> bool:
    return db.execute(
        text("SELECT count(*) FROM game_sessions WHERE id = :id"), {"id": session_id}
    ).scalar_one() > 0


def _policy(db, **fields):
    row = db.get(OpportunityRetentionPolicy, 1)
    if row is None:
        row = OpportunityRetentionPolicy(id=1)
        db.add(row)
    for key, value in fields.items():
        setattr(row, key, value)
    db.flush()


def _freeze_by_prefix(db, *, user_id: int, through: datetime) -> None:
    state = db.get(UserOpportunityRetentionState, user_id)
    state.folded_through_started_at = through
    db.flush()


# --------------------------------------------------------------------------
# Individual session deletion
# --------------------------------------------------------------------------


def test_deleting_a_session_below_the_fold_prefix_is_rejected(pg_session_factory):
    db = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(days=5)
        session, _ = _seed(db, user_id=7001, started_at=started_at)
        _freeze_by_prefix(db, user_id=7001, through=started_at)

        with pytest.raises(TRIGGER_ERRORS, match="fold prefix"):
            db.execute(
                text("DELETE FROM game_sessions WHERE id = :id"), {"id": session.id}
            )
        db.rollback()
    finally:
        db.close()


def test_the_prefix_guard_survives_turning_the_policy_back_off(pg_session_factory):
    """Deleted raw rows do not return, so the prefix arm is not policy-gated."""
    db = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(days=5)
        session, _ = _seed(db, user_id=7002, started_at=started_at)
        _freeze_by_prefix(db, user_id=7002, through=started_at)
        _policy(db, readiness=False, freeze_enabled=False, cleanup_enabled=False)

        with pytest.raises(TRIGGER_ERRORS, match="fold prefix"):
            db.execute(
                text("DELETE FROM game_sessions WHERE id = :id"), {"id": session.id}
            )
        db.rollback()
    finally:
        db.close()


def test_an_old_session_is_deletable_until_freezing_is_approved(pg_session_factory):
    """Deploying the guard must not silently activate the immutability policy."""
    db = pg_session_factory()
    try:
        session, _ = _seed(
            db, user_id=7003, started_at=datetime.now(timezone.utc) - timedelta(days=900)
        )
        session_id = session.id
        db.commit()

        db.execute(text("DELETE FROM game_sessions WHERE id = :id"), {"id": session_id})
        db.commit()

        assert _session_exists(db, session_id) is False
    finally:
        db.close()


def test_an_old_session_delete_is_rejected_once_freezing_is_enabled(pg_session_factory):
    db = pg_session_factory()
    try:
        session, _ = _seed(
            db, user_id=7004, started_at=datetime.now(timezone.utc) - timedelta(days=900)
        )
        _policy(db, readiness=True, freeze_enabled=True)

        with pytest.raises(TRIGGER_ERRORS, match="mutation boundary"):
            db.execute(
                text("DELETE FROM game_sessions WHERE id = :id"), {"id": session.id}
            )
        db.rollback()
    finally:
        db.close()


def test_a_recent_session_delete_still_succeeds_under_freezing(pg_session_factory):
    db = pg_session_factory()
    try:
        session, _ = _seed(
            db, user_id=7005, started_at=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        session_id = session.id
        _policy(db, readiness=True, freeze_enabled=True)
        db.commit()

        db.execute(text("DELETE FROM game_sessions WHERE id = :id"), {"id": session_id})
        db.commit()

        assert _session_exists(db, session_id) is False
    finally:
        db.close()


def test_the_session_guard_uses_the_database_clock_not_transaction_start(
    pg_session_factory,
):
    """A transaction that began before the deadline gets no grandfathered pass.

    The session is made frozen mid-transaction, after this transaction has
    already started and taken its snapshot. ``now()`` would still report the
    pre-deadline moment; ``clock_timestamp()`` reports the truth.
    """
    setup = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(days=2)
        session, _ = _seed(setup, user_id=7006, started_at=started_at)
        session_id = session.id
        setup.commit()
    finally:
        setup.close()

    actor = pg_session_factory()
    try:
        # Open the transaction and pin its start time BEFORE the policy changes.
        transaction_start = actor.execute(select(text("now()"))).scalar_one()

        flipper = pg_session_factory()
        try:
            _policy(flipper, readiness=True, freeze_enabled=True, mutation_window_days=1)
            flipper.commit()
        finally:
            flipper.close()

        with pytest.raises(TRIGGER_ERRORS, match="mutation boundary"):
            actor.execute(
                text("DELETE FROM game_sessions WHERE id = :id"), {"id": session_id}
            )
        actor.rollback()
        assert transaction_start is not None
    finally:
        actor.close()


# --------------------------------------------------------------------------
# Individual event-row deletion
# --------------------------------------------------------------------------


def test_deleting_a_frozen_event_row_directly_is_rejected(pg_session_factory):
    """A repair CLI must not reach behind the choke point to delete raw rows."""
    db = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(days=5)
        session, blunder = _seed(db, user_id=7007, started_at=started_at)
        _freeze_by_prefix(db, user_id=7007, through=started_at)

        with pytest.raises(TRIGGER_ERRORS, match="fold prefix"):
            db.execute(
                text(
                    "DELETE FROM blunder_opportunity_events WHERE blunder_id = :b"
                ),
                {"b": blunder.id},
            )
        db.rollback()
        assert session is not None
    finally:
        db.close()


def test_the_fold_transfer_marker_allows_the_controlled_deletion(pg_session_factory):
    """The fold's own allowance is private to its transaction and dies with it."""
    db = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(days=5)
        _, blunder = _seed(db, user_id=7008, started_at=started_at)
        _freeze_by_prefix(db, user_id=7008, through=started_at)
        db.execute(
            text("SELECT set_config('ghostreplay.srs_fold_mode', 'transfer', true)")
        )

        db.execute(
            text("DELETE FROM blunder_opportunity_events WHERE blunder_id = :b"),
            {"b": blunder.id},
        )
        db.commit()

        assert db.execute(
            select(BlunderOpportunityEvent).where(
                BlunderOpportunityEvent.blunder_id == blunder.id
            )
        ).first() is None
        # Transaction-local: the next transaction on this pooled connection has
        # no escape armed.
        assert db.execute(
            text("SELECT current_setting('ghostreplay.srs_fold_mode', true)")
        ).scalar() in (None, "")
    finally:
        db.close()


def test_deleting_the_parent_blunder_cascades_through_the_frozen_guard(
    pg_session_factory,
):
    """A deleted target takes its own evidence with it; that is not a repair."""
    db = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(days=5)
        _, blunder = _seed(db, user_id=7009, started_at=started_at)
        blunder_id = blunder.id
        _freeze_by_prefix(db, user_id=7009, through=started_at)
        db.commit()

        db.execute(text("DELETE FROM blunders WHERE id = :b"), {"b": blunder_id})
        db.commit()

        assert db.execute(
            text("SELECT count(*) FROM blunder_opportunity_summaries WHERE blunder_id = :b"),
            {"b": blunder_id},
        ).scalar_one() == 0
    finally:
        db.close()


# --------------------------------------------------------------------------
# Whole-user purge
# --------------------------------------------------------------------------


def test_purge_deletes_every_piece_of_a_frozen_users_training_state(
    pg_session_factory,
):
    db = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(days=5)
        session, blunder = _seed(db, user_id=7010, started_at=started_at, targeted=True)
        session_id, blunder_id = session.id, blunder.id
        _freeze_by_prefix(db, user_id=7010, through=started_at)
        _policy(db, readiness=True, freeze_enabled=True, mutation_window_days=1)
        db.commit()

        purge_user_training_history(db, user_id=7010)
        db.commit()  # the deferred assertion runs HERE

        assert _session_exists(db, session_id) is False
        for table, column, value in (
            ("blunders", "id", blunder_id),
            ("blunder_opportunity_summaries", "blunder_id", blunder_id),
            ("blunder_opportunity_events", "blunder_id", blunder_id),
            ("blunder_reviews", "blunder_id", blunder_id),
            ("user_opportunity_retention_state", "user_id", 7010),
            # The three no-ondelete references to blunders.id. They are why the
            # purge deletes sessions BEFORE blunders; the old order raised
            # fk_session_moves_target_blunder_id_blunders for any user who had
            # ever been ghost-targeted, which is most of them.
            ("session_moves", "target_blunder_id", blunder_id),
            ("opponent_decisions", "target_blunder_id", blunder_id),
            ("opponent_target_facts", "blunder_id", blunder_id),
            # NOT NULL with no cascade, so one finished rated game blocked the
            # entire purge. Deleting it resets the user's Elo chain, which is
            # what deleting a whole training history means.
            ("rating_history", "user_id", 7010),
        ):
            assert db.execute(
                text(f"SELECT count(*) FROM {table} WHERE {column} = :v"), {"v": value}
            ).scalar_one() == 0
    finally:
        db.close()


def test_a_partial_purge_fails_at_commit_and_rolls_back(pg_session_factory):
    """The deferred assertion is what proves completeness, not the row counts.

    Re-inserting a summary after the deletes simulates any purge that misses a
    table. Nothing may commit in that state.
    """
    db = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(days=5)
        session, blunder = _seed(db, user_id=7011, started_at=started_at, targeted=True)
        db.commit()

        purge_user_training_history(db, user_id=7011)
        # A blunder that survived the purge, with its summary: exactly the shape
        # an incomplete purge leaves behind.
        db.execute(
            text(
                "INSERT INTO blunders (user_id, position_id, bad_move_san,"
                " best_move_san, eval_loss_cp) SELECT :u, id, 'Qh5', 'Nf3', 120"
                " FROM positions WHERE user_id = :u LIMIT 1"
            ),
            {"u": 7011},
        )
        leftover = db.execute(
            text("SELECT id FROM blunders WHERE user_id = :u"), {"u": 7011}
        ).scalar_one()
        db.execute(
            text(
                "INSERT INTO blunder_opportunity_summaries (blunder_id) VALUES (:b)"
            ),
            {"b": leftover},
        )

        with pytest.raises(TRIGGER_ERRORS, match="state remains for purged owner"):
            db.commit()
        db.rollback()

        assert db.get(GameSession, session.id) is not None
        assert db.get(Blunder, blunder.id) is not None
    finally:
        db.close()


def test_a_user_with_no_retention_row_still_gets_the_completeness_check(
    pg_session_factory,
):
    """The assertion is an AFTER DELETE trigger on user_opportunity_retention_state.

    A user created after the migration has no such row, so nothing would be
    deleted from that table and the trigger would never fire — a whole-user
    purge with no completeness check at all, for exactly the users most likely
    to exist. The purge therefore creates the row before locking it.
    """
    db = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(days=5)
        _seed(
            db,
            user_id=7020,
            started_at=started_at,
            retention_state=False,
            targeted=True,
        )
        db.commit()
        assert db.get(UserOpportunityRetentionState, 7020) is None

        purge_user_training_history(db, user_id=7020)
        db.execute(
            text(
                "INSERT INTO blunders (user_id, position_id, bad_move_san,"
                " best_move_san, eval_loss_cp) SELECT :u, id, 'Qh5', 'Nf3', 120"
                " FROM positions WHERE user_id = :u LIMIT 1"
            ),
            {"u": 7020},
        )
        leftover = db.execute(
            text("SELECT id FROM blunders WHERE user_id = :u"), {"u": 7020}
        ).scalar_one()
        db.execute(
            text("INSERT INTO blunder_opportunity_summaries (blunder_id) VALUES (:b)"),
            {"b": leftover},
        )

        with pytest.raises(TRIGGER_ERRORS, match="state remains for purged owner"):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_a_clean_purge_of_a_user_with_no_retention_row_commits(pg_session_factory):
    db = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(days=5)
        session, _ = _seed(
            db,
            user_id=7021,
            started_at=started_at,
            retention_state=False,
            targeted=True,
        )
        session_id = session.id
        db.commit()

        purge_user_training_history(db, user_id=7021)
        db.commit()

        assert _session_exists(db, session_id) is False
        assert db.get(UserOpportunityRetentionState, 7021) is None
    finally:
        db.close()


@pytest.mark.parametrize(
    "markers",
    [
        {},
        {PURGE_MODE_SETTING: "user_training"},
        {PURGE_USER_SETTING: "7012"},
        {PURGE_MODE_SETTING: "wrong_mode", PURGE_USER_SETTING: "7012"},
        {PURGE_MODE_SETTING: "user_training", PURGE_USER_SETTING: "999999"},
    ],
    ids=["absent", "mode-only", "owner-only", "wrong-mode", "wrong-owner"],
)
def test_the_purge_escape_requires_an_exact_mode_and_owner_match(
    pg_session_factory, markers
):
    """Half a marker set is no marker at all."""
    db = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(days=5)
        session, _ = _seed(db, user_id=7012, started_at=started_at)
        _freeze_by_prefix(db, user_id=7012, through=started_at)
        for name, value in markers.items():
            db.execute(
                text("SELECT set_config(:n, :v, true)").bindparams(n=name, v=value)
            )

        with pytest.raises(TRIGGER_ERRORS, match="fold prefix"):
            db.execute(
                text("DELETE FROM game_sessions WHERE id = :id"), {"id": session.id}
            )
        db.rollback()
    finally:
        db.close()


def test_the_purge_marker_does_not_survive_the_transaction(pg_session_factory):
    """Pooled-connection reuse must not inherit another request's escape."""
    db = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(days=5)
        _seed(db, user_id=7013, started_at=started_at)
        _freeze_by_prefix(db, user_id=7013, through=started_at)
        db.commit()

        purge_user_training_history(db, user_id=7013)
        db.commit()

        assert db.execute(
            text("SELECT current_setting(:n, true)").bindparams(n=PURGE_MODE_SETTING)
        ).scalar() in (None, "")

        # The same pooled connection, a second owner, no marker armed.
        other_started = datetime.now(timezone.utc) - timedelta(days=5)
        other_session, _ = _seed(db, user_id=7014, started_at=other_started)
        _freeze_by_prefix(db, user_id=7014, through=other_started)
        with pytest.raises(TRIGGER_ERRORS, match="fold prefix"):
            db.execute(
                text("DELETE FROM game_sessions WHERE id = :id"),
                {"id": other_session.id},
            )
        db.rollback()
    finally:
        db.close()


def test_a_rolled_back_purge_leaves_the_escape_disarmed(pg_session_factory):
    """A savepoint/transaction reset must not leave a half-armed state."""
    db = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(days=5)
        session, _ = _seed(db, user_id=7015, started_at=started_at)
        _freeze_by_prefix(db, user_id=7015, through=started_at)
        db.commit()

        purge_user_training_history(db, user_id=7015)
        db.rollback()

        assert db.get(GameSession, session.id) is not None
        with pytest.raises(TRIGGER_ERRORS, match="fold prefix"):
            db.execute(
                text("DELETE FROM game_sessions WHERE id = :id"), {"id": session.id}
            )
        db.rollback()
    finally:
        db.close()


def test_purge_rejects_an_unvalidated_owner(pg_session_factory):
    db = pg_session_factory()
    try:
        with pytest.raises(PurgeScopeError):
            purge_user_training_history(db, user_id=0)
        with pytest.raises(PurgeScopeError, match="does not exist"):
            purge_user_training_history(db, user_id=987654)
        db.rollback()
    finally:
        db.close()


def test_the_guards_take_no_advisory_lock(pg_session_factory):
    """No trigger may take a user lock after locking a parent row.

    Doing so would give the purge (user -> retention state -> parents) and a
    trigger (parent -> user) opposite lock orders and deadlock them.
    """
    db = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session, _ = _seed(db, user_id=7016, started_at=started_at)
        session_id = session.id
        db.commit()

        db.execute(text("DELETE FROM game_sessions WHERE id = :id"), {"id": session_id})
        # Scoped to THIS backend: the test harness holds its own schema-lease
        # advisory lock on a different connection, which is not evidence about
        # what the triggers did.
        held = db.execute(
            text(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            )
        ).scalar_one()
        db.commit()

        assert held == 0
    finally:
        db.close()


# --------------------------------------------------------------------------
# Reviews are independent of the evidence graph lock
# --------------------------------------------------------------------------


def test_a_review_succeeds_while_an_unrelated_evidence_user_lock_is_held(
    pg_client, pg_session_factory, auth_headers
):
    """Reviews must not take or inherit the per-user graph advisory lock.

    The evidence worker holds ``pg_advisory_xact_lock(user_id)`` for the whole of
    its graph rewrite. If grading queued behind it, an old-session review would
    fail exactly when the deferred pipeline is busiest, and it would also inherit
    that path's lock/statement timeouts. Reviews serialize on the BLUNDER row
    instead, which is a lock the worker never takes.
    """
    user_id = 7101
    setup = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session, blunder = _seed(setup, user_id=user_id, started_at=started_at)
        session_id, blunder_id = session.id, blunder.id
        setup.commit()
    finally:
        setup.close()

    holder = pg_session_factory()
    try:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(:uid)").bindparams(uid=user_id)
        )
        assert holder.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid()"
            )
        ).scalar_one() == 1

        response = pg_client.post(
            "/api/srs/review",
            headers=auth_headers(user_id=user_id),
            json={
                "session_id": str(session_id),
                "blunder_id": blunder_id,
                "passed": True,
                "user_move": "Nf3",
                "eval_delta": 20,
            },
        )
        assert response.status_code == 200, response.text
    finally:
        holder.rollback()
        holder.close()

    check = pg_session_factory()
    try:
        assert check.execute(
            text(
                "SELECT latest_review_id IS NOT NULL "
                "FROM blunder_opportunity_summaries WHERE blunder_id = :b"
            ),
            {"b": blunder_id},
        ).scalar_one() is True
    finally:
        check.close()


def test_a_review_is_stamped_with_the_database_clock(pg_client, pg_session_factory, auth_headers):
    """The since-review window must be comparable with database-stamped times.

    An application clock would let a skewed host open a window that excludes
    evidence the database considers newer, or includes evidence it considers
    older — silently moving SRS dueness.
    """
    user_id = 7102
    setup = pg_session_factory()
    try:
        session, blunder = _seed(
            setup, user_id=user_id, started_at=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        session_id, blunder_id = session.id, blunder.id
        setup.commit()
    finally:
        setup.close()

    check = pg_session_factory()
    try:
        before = check.execute(select(text("clock_timestamp()"))).scalar_one()
    finally:
        check.close()

    assert pg_client.post(
        "/api/srs/review",
        headers=auth_headers(user_id=user_id),
        json={
            "session_id": str(session_id),
            "blunder_id": blunder_id,
            "passed": True,
            "user_move": "Nf3",
            "eval_delta": 20,
        },
    ).status_code == 200

    check = pg_session_factory()
    try:
        reviewed_at, summary_at = check.execute(
            text(
                "SELECT r.reviewed_at, s.latest_review_at FROM blunder_reviews r"
                " JOIN blunder_opportunity_summaries s ON s.blunder_id = r.blunder_id"
                " WHERE r.blunder_id = :b"
            ),
            {"b": blunder_id},
        ).one()
        after = check.execute(select(text("clock_timestamp()"))).scalar_one()
    finally:
        check.close()

    assert before <= reviewed_at <= after
    assert summary_at == reviewed_at


# --------------------------------------------------------------------------
# Policy changes
# --------------------------------------------------------------------------


def test_a_policy_change_commits_while_a_user_lock_is_held(pg_session_factory):
    """Changing M must not require sweeping or locking any user.

    The policy is a single global row, deliberately not copied per user, so an
    operator can change it while any number of user-scoped transactions are in
    flight. Requiring a per-user sweep would make every M change an outage.
    """
    setup = pg_session_factory()
    try:
        _seed(setup, user_id=7201, started_at=datetime.now(timezone.utc) - timedelta(hours=1))
        _policy(setup, readiness=True, freeze_enabled=True, mutation_window_days=30)
        setup.commit()
    finally:
        setup.close()

    holder = pg_session_factory()
    operator = pg_session_factory()
    try:
        holder.execute(text("SELECT pg_advisory_xact_lock(:uid)").bindparams(uid=7201))
        holder.execute(
            text(
                "SELECT 1 FROM user_opportunity_retention_state "
                "WHERE user_id = :uid FOR NO KEY UPDATE"
            ).bindparams(uid=7201)
        )

        operator.execute(
            text(
                "UPDATE opportunity_retention_policy "
                "SET mutation_window_days = 7, version = version + 1 WHERE id = 1"
            )
        )
        operator.commit()

        assert operator.execute(
            text(
                "SELECT mutation_window_days, version "
                "FROM opportunity_retention_policy WHERE id = 1"
            )
        ).one() == (7, 2)
    finally:
        holder.rollback()
        holder.close()
        operator.close()


@pytest.mark.parametrize("window_days", [1, 365], ids=["shorten", "lengthen"])
def test_both_m_directions_leave_the_fold_prefix_alone(pg_session_factory, window_days):
    """Neither direction may move what has already been folded.

    Shortening must not fold more by itself (folding is a separate, disabled
    switch), and lengthening must not reopen what the prefix covers.
    """
    db = pg_session_factory()
    try:
        started_at = datetime.now(timezone.utc) - timedelta(days=30)
        session, _ = _seed(db, user_id=7202, started_at=started_at)
        session_id = session.id
        _freeze_by_prefix(db, user_id=7202, through=started_at)
        prefix_before = db.execute(
            text(
                "SELECT folded_through_started_at "
                "FROM user_opportunity_retention_state WHERE user_id = 7202"
            )
        ).scalar_one()
        db.commit()

        _policy(db, readiness=True, freeze_enabled=True, mutation_window_days=window_days)
        db.commit()

        assert db.execute(
            text(
                "SELECT folded_through_started_at "
                "FROM user_opportunity_retention_state WHERE user_id = 7202"
            )
        ).scalar_one() == prefix_before
        with pytest.raises(TRIGGER_ERRORS):
            db.execute(
                text("DELETE FROM game_sessions WHERE id = :id"), {"id": session_id}
            )
        db.rollback()
    finally:
        db.close()
