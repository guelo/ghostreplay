"""Real contention between target publication and SRS opportunity folding.

SQLite cannot stand in for any of this. ``FOR SHARE`` / ``FOR UPDATE NOWAIT``, the
750 ms acquisition budget, ``clock_timestamp()`` sampled after a wait, and a
transaction that a lock timeout has ABORTED are the guarantees under test, and the
test dialect renders none of them.

Every case here uses independent connections and real barriers. A sleep standing
in for a commit would prove nothing: the whole question is what each side sees
BEFORE and AFTER the other's COMMIT, and only the lock manager can answer that.

The compactor itself belongs to ``g-srs-fold-recovery``. What is exercised here is
the INTERFACE it must use — ``lock_state_for_fold`` — so the two halves are agreed
and proven before anything is allowed to delete a row.

One thing to know before debugging a flake here: a publication arms
``idle_in_transaction_session_timeout`` (5 s), so every test below that parks a
publisher on an ``Event`` has a real 5 s ceiling on that pause, well under the
generous ``wait()`` arguments. On an idle machine the barriers clear in
milliseconds. If these start failing together with a dropped connection in the
traceback, that ceiling is the first place to look — not the lock manager.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import chess
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.fen import fen_hash, normalize_fen
from app.opponent_move_controller import ControllerMove
from app.opportunity_retention import POLICY_ID
from app.models import (
    Blunder,
    GameSession,
    OpponentDecision,
    OpponentTargetFact,
    OpportunityRetentionPolicy,
    Position,
    User,
    UserOpportunityRetentionState,
)
from app import srs_target_admission
from app.srs_target_admission import (
    REASON_MUTATION_WINDOW_EXPIRED,
    REASON_PUBLICATION_TIMEOUT,
    REASON_STATE_LOCK_TIMEOUT,
    REASON_TARGETING_AFTER_FOLD,
    TargetPublicationSuppressed,
    admit_target_publication,
    lock_state_for_fold,
)
from conftest import await_pg_lock, pg_required

pytestmark = pg_required

USER_ID = 4321
# Black to move: the opponent's turn for a white player.
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
# White to move: the player's colour, so a blunder here is ghost-eligible.
AFTER_E4_E5_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"
AFTER_E4_E5_PLAYED_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
# A suppressed request loses its target, not its opponent: it goes to the engine
# like any untargeted move. Patched so these tests never touch the network.
MAIA_MOVE = ControllerMove(uci="e7e5", san="e5", method="maia3_api")


def _seed(db, *, started_at: datetime | None = None) -> tuple[uuid.UUID, int]:
    """A user, a session, and a one-edge ghost graph with a due blunder.

    The real topology, not just a blunder row: these tests drive the endpoint
    through ``find_ghost_move``, so the ghost path has to actually select this
    blunder before the interlock has anything to admit or suppress.

    The migration-seeded policy row is restored by the PostgreSQL fixture, as a
    migrated deployment always has one; publication refusing to pin without it is
    its own case in the deterministic suite.
    """
    db.add(User(id=USER_ID, username=None, is_anonymous=True))
    db.flush()
    # As the migration left every user that existed when it ran: a row with no
    # prefix. The create-if-absent path for newer users is covered separately.
    db.add(UserOpportunityRetentionState(user_id=USER_ID))
    session = GameSession(
        id=uuid.uuid4(),
        user_id=USER_ID,
        started_at=started_at or datetime.now(timezone.utc),
        status="active",
        engine_elo=1500,
        player_color="white",
    )
    db.add(session)
    positions = {}
    for fen, color in ((AFTER_E4_FEN, "black"), (AFTER_E4_E5_FEN, "white")):
        position = Position(
            user_id=USER_ID,
            fen_hash=fen_hash(fen),
            fen_raw=fen,
            active_color=color,
        )
        db.add(position)
        db.flush()
        positions[fen] = position
    db.execute(
        text(
            "INSERT INTO moves (from_position_id, move_san, to_position_id) "
            "VALUES (:from_id, 'e5', :to_id)"
        ),
        {
            "from_id": positions[AFTER_E4_FEN].id,
            "to_id": positions[AFTER_E4_E5_FEN].id,
        },
    )
    blunder = Blunder(
        user_id=USER_ID,
        position_id=positions[AFTER_E4_E5_FEN].id,
        bad_move_san="Nf6",
        best_move_san="d5",
        eval_loss_cp=150,
        # Old enough to be due for review, so the ghost selector picks it.
        created_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    db.add(blunder)
    db.flush()
    session_id, blunder_id = session.id, blunder.id
    db.commit()
    return session_id, blunder_id


def _record_target(db, session_id, blunder_id, fingerprint="pg-target"):
    from app.api.game import NextOpponentMoveResponse, _record_decision

    return _record_decision(
        db,
        session_id=session_id,
        request_fingerprint=fingerprint,
        request_fen_hash=fen_hash(AFTER_E4_FEN),
        uci_history='["e2e4"]',
        ply_before=1,
        response=NextOpponentMoveResponse(
            mode="ghost",
            move={"uci": "e7e5", "san": "e5"},
            target_blunder_id=blunder_id,
            decision_source="ghost_path",
        ),
        resulting_fen=AFTER_E4_E5_PLAYED_FEN,
        user_id=USER_ID,
    )


def _record_fallback(db, session_id, fingerprint="pg-target"):
    """What the endpoint persists after suppression: same request, no target."""
    from app.api.game import NextOpponentMoveResponse, _record_decision

    return _record_decision(
        db,
        session_id=session_id,
        request_fingerprint=fingerprint,
        request_fen_hash=fen_hash(AFTER_E4_FEN),
        uci_history='["e2e4"]',
        ply_before=1,
        response=NextOpponentMoveResponse(
            mode="engine",
            move={"uci": "e7e5", "san": "e5"},
            target_blunder_id=None,
            decision_source="backend_engine",
        ),
        resulting_fen=AFTER_E4_E5_PLAYED_FEN,
    )


def _freeze(db, *, days: int = 61) -> None:
    db.execute(
        update(OpportunityRetentionPolicy)
        .where(OpportunityRetentionPolicy.id == POLICY_ID)
        .values(readiness=True, freeze_enabled=True)
    )
    db.execute(
        update(GameSession)
        .where(GameSession.user_id == USER_ID)
        .values(started_at=datetime.now(timezone.utc) - timedelta(days=days))
    )
    db.commit()


def test_pg_a_fold_is_skipped_while_a_publication_holds_the_share_lock(
    pg_session_factory, pg_engine
):
    """SHARE wins: the compactor's NOWAIT gives way, and the pin survives.

    The publication is paused between its admission and its COMMIT — the exact
    window in which an unlocked freeze check would have let the fold through.
    """
    with pg_session_factory() as db:
        session_id, blunder_id = _seed(db)

    admitted, release, fold_attempted = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    outcome: dict[str, object] = {}

    def publisher():
        with pg_session_factory() as db:
            assert (
                admit_target_publication(
                    db, user_id=USER_ID, session_id=session_id
                )
                is None
            )
            outcome["pid"] = db.scalar(text("SELECT pg_backend_pid()"))
            admitted.set()
            assert fold_attempted.wait(10)
            # Only now does the decision land, still under the same SHARE lock.
            _record_target(db, session_id, blunder_id)
            release.set()

    def compactor():
        with pg_session_factory() as db:
            assert admitted.wait(10)
            outcome["locked"] = lock_state_for_fold(db, user_id=USER_ID)
            db.rollback()
            fold_attempted.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(publisher)
        folding = pool.submit(compactor)
        publishing.result(timeout=20)
        folding.result(timeout=20)

    assert outcome["locked"] is False, "the fold must skip, never wait or proceed"
    assert release.is_set()
    # A later sweep, in a fresh transaction, sees the committed pin it has to
    # fold around. Skipping is a deferral, not a loss.
    with pg_session_factory() as db:
        assert lock_state_for_fold(db, user_id=USER_ID) is True
        assert db.query(OpponentDecision).one().target_blunder_id == blunder_id


def test_pg_a_skipped_fold_leaves_the_sweep_transaction_usable(
    pg_session_factory, pg_engine
):
    """False means "skip this user", not "your transaction is finished".

    A lock timeout ABORTS a PostgreSQL transaction, so a compactor sweeping many
    users in one transaction would hit ``InFailedSqlTransaction`` on the very next
    user if the skip were merely swallowed. The acquisition runs inside a SAVEPOINT
    for exactly this reason, and this is the case that would catch its removal:
    two users, one transaction, and no rollback in between.
    """
    other_user = USER_ID + 1
    with pg_session_factory() as db:
        session_id, _ = _seed(db)
        db.add(User(id=other_user, username=None, is_anonymous=True))
        db.commit()

    admitted, swept = threading.Event(), threading.Event()
    outcome: dict[str, object] = {}

    def publisher():
        with pg_session_factory() as db:
            assert (
                admit_target_publication(
                    db, user_id=USER_ID, session_id=session_id
                )
                is None
            )
            admitted.set()
            assert swept.wait(30)
            db.rollback()

    def sweeper():
        with pg_session_factory() as db:
            assert admitted.wait(10)
            try:
                before = db.scalar(text("SELECT current_setting('lock_timeout')"))
                outcome["held"] = lock_state_for_fold(db, user_id=USER_ID)
                # Same transaction, no rollback: this is the assertion.
                outcome["next"] = lock_state_for_fold(db, user_id=other_user)
                outcome["alive"] = db.scalar(text("SELECT 1"))
                outcome["restored"] = (
                    db.scalar(text("SELECT current_setting('lock_timeout')"))
                    == before
                )
                db.rollback()
            finally:
                swept.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(publisher)
        sweeping = pool.submit(sweeper)
        sweeping.result(timeout=40)
        publishing.result(timeout=40)

    assert outcome["held"] is False
    assert outcome["next"] is True, "a skipped user must not end the sweep"
    assert outcome["alive"] == 1
    assert outcome["restored"], "the fold ceiling must not outlive the acquisition"


def test_pg_publication_waits_for_a_short_fold_then_rechecks_the_prefix(
    pg_session_factory, pg_engine
):
    """UPDATE wins: publication waits, wakes, and sees the committed prefix.

    This is what makes the wait worth having. The fold commits WHILE publication
    is blocked, so the freeze check that runs after the wait must be a fresh
    statement — a transaction-snapshot read would still see the old prefix and
    publish a target against evidence that has just been deleted.
    """
    with pg_session_factory() as db:
        session_id, _ = _seed(db)
        started_at = db.get(GameSession, session_id).started_at

    holding, publisher_ready = threading.Event(), threading.Event()
    publisher_pid: list[int] = []
    reasons: list[str | None] = []

    def compactor():
        with pg_session_factory() as db:
            assert lock_state_for_fold(db, user_id=USER_ID) is True
            db.execute(
                update(UserOpportunityRetentionState)
                .where(UserOpportunityRetentionState.user_id == USER_ID)
                .values(folded_through_started_at=started_at)
            )
            holding.set()
            assert publisher_ready.wait(10)
            with pg_engine.connect() as observer:
                assert await_pg_lock(observer, publisher_pid[0]), (
                    "publication never blocked on the fold's row lock"
                )
            # Well inside the 750 ms budget, so the publication WAKES rather
            # than timing out.
            db.commit()

    def publisher():
        with pg_session_factory() as db:
            publisher_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
            assert holding.wait(10)
            publisher_ready.set()
            reasons.append(
                admit_target_publication(
                    db, user_id=USER_ID, session_id=session_id
                )
            )
            db.rollback()

    with ThreadPoolExecutor(max_workers=2) as pool:
        folding = pool.submit(compactor)
        publishing = pool.submit(publisher)
        folding.result(timeout=20)
        publishing.result(timeout=20)

    assert reasons == [REASON_TARGETING_AFTER_FOLD]


def test_pg_a_long_fold_times_the_publication_out_and_leaks_no_lock(
    pg_session_factory, pg_engine, caplog
):
    """Past the budget, publication gives up — with the transaction left clean."""
    from app.srs_target_admission import STATE_LOCK_WAIT

    with pg_session_factory() as db:
        session_id, blunder_id = _seed(db)

    holding, timed_out = threading.Event(), threading.Event()
    outcome: dict[str, object] = {}

    def compactor():
        with pg_session_factory() as db:
            assert lock_state_for_fold(db, user_id=USER_ID) is True
            holding.set()
            # Held past the 750 ms budget, so the publication must give up
            # rather than the fold giving way.
            assert timed_out.wait(20)
            db.rollback()

    def publisher():
        with pg_session_factory() as db:
            assert holding.wait(10)
            with caplog.at_level(logging.WARNING):
                outcome["reason"] = admit_target_publication(
                    db, user_id=USER_ID, session_id=session_id
                )
            # The timeout ABORTED this transaction: the caller must roll back
            # before any further SQL, which is what _record_decision does.
            db.rollback()
            # Release/rollback happens before the fallback work, and the
            # fallback itself takes no retention lock at all.
            outcome["fallback"] = _record_fallback(db, session_id)
            timed_out.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        folding = pool.submit(compactor)
        publishing = pool.submit(publisher)
        publishing.result(timeout=30)
        folding.result(timeout=30)

    assert outcome["reason"] == REASON_STATE_LOCK_TIMEOUT
    assert STATE_LOCK_WAIT in caplog.text
    served, replayed = outcome["fallback"]
    assert not replayed
    assert served.target_blunder_id is None

    with pg_session_factory() as db:
        decision = db.query(OpponentDecision).one()
        assert decision.target_blunder_id is None
        # No targeting sample was invented for a target that was never served.
        assert db.query(OpponentTargetFact).count() == 0
        # Nothing of the timed-out transaction survives: a fresh fold acquires
        # the row immediately.
        assert lock_state_for_fold(db, user_id=USER_ID) is True
        assert db.get(Blunder, blunder_id) is not None


def test_pg_the_freeze_check_reads_the_clock_after_the_wait(
    pg_session_factory, pg_engine
):
    """A publication that blocks across the deadline is judged on when it WOKE.

    Transaction-start time would grant it the verdict it would have had before it
    blocked, which is exactly the hole ``clock_timestamp()`` exists to close.
    """
    with pg_session_factory() as db:
        session_id, _ = _seed(db)

    holding, publisher_ready = threading.Event(), threading.Event()
    publisher_pid: list[int] = []
    reasons: list[str | None] = []

    def compactor():
        with pg_session_factory() as db:
            assert lock_state_for_fold(db, user_id=USER_ID) is True
            holding.set()
            assert publisher_ready.wait(10)
            with pg_engine.connect() as observer:
                assert await_pg_lock(observer, publisher_pid[0])
            # The session crosses M while the publication is asleep on the lock.
            _freeze(db)

    def publisher():
        with pg_session_factory() as db:
            publisher_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
            # Start the transaction BEFORE the freeze, so transaction time and
            # statement time genuinely disagree.
            db.execute(text("SELECT 1"))
            assert holding.wait(10)
            publisher_ready.set()
            reasons.append(
                admit_target_publication(
                    db, user_id=USER_ID, session_id=session_id
                )
            )
            db.rollback()

    with ThreadPoolExecutor(max_workers=2) as pool:
        folding = pool.submit(compactor)
        publishing = pool.submit(publisher)
        folding.result(timeout=20)
        publishing.result(timeout=20)

    assert reasons == [REASON_MUTATION_WINDOW_EXPIRED]


def test_pg_a_rolled_back_publication_publishes_nothing_and_frees_the_row(
    pg_session_factory,
):
    """Rollback or disconnect mid-publication leaves no pin and no held lock."""
    with pg_session_factory() as db:
        session_id, blunder_id = _seed(db)

    with pg_session_factory() as db:
        assert (
            admit_target_publication(db, user_id=USER_ID, session_id=session_id)
            is None
        )
        db.rollback()

    with pg_session_factory() as db:
        assert db.query(OpponentDecision).count() == 0
        assert db.query(OpponentTargetFact).count() == 0
        assert lock_state_for_fold(db, user_id=USER_ID) is True


def test_pg_a_suppressed_publication_replays_a_committed_targeted_winner(
    pg_session_factory,
):
    """A raced duplicate gets the winner's envelope, target included.

    Replay creates no new sample, so it is exempt from the freeze: the decision
    it serves was already published and already counted.
    """
    from app.api.game import _replay_decision

    with pg_session_factory() as db:
        session_id, blunder_id = _seed(db)
        started_at = db.get(GameSession, session_id).started_at

    with pg_session_factory() as db:
        winner, replayed = _record_target(db, session_id, blunder_id)
        assert not replayed

    # The fold then advances over this session; a retry of the same request
    # arrives after it.
    with pg_session_factory() as db:
        assert lock_state_for_fold(db, user_id=USER_ID) is True
        db.execute(
            update(UserOpportunityRetentionState)
            .where(UserOpportunityRetentionState.user_id == USER_ID)
            .values(folded_through_started_at=started_at)
        )
        db.commit()

    with pg_session_factory() as db:
        assert (
            admit_target_publication(db, user_id=USER_ID, session_id=session_id)
            == REASON_TARGETING_AFTER_FOLD
        )
        db.rollback()
        raced = _replay_decision(db, session_id, "pg-target")

    assert raced is not None
    assert raced.target_blunder_id == blunder_id
    assert raced.decision_id == winner.decision_id


def test_pg_a_suppressed_target_is_refused_before_anything_is_written(
    pg_session_factory,
):
    """_record_decision is the sink, so a refused target writes no row at all."""
    with pg_session_factory() as db:
        session_id, blunder_id = _seed(db)
        started_at = db.get(GameSession, session_id).started_at
        db.merge(
            UserOpportunityRetentionState(
                user_id=USER_ID, folded_through_started_at=started_at
            )
        )
        db.commit()

    with pg_session_factory() as db:
        with pytest.raises(TargetPublicationSuppressed) as raised:
            _record_target(db, session_id, blunder_id)
        assert raised.value.reason == REASON_TARGETING_AFTER_FOLD
        # Rolled back by the interlock itself, so the caller can immediately
        # persist its fallback on the same session.
        served, replayed = _record_fallback(db, session_id)

    assert served.target_blunder_id is None
    assert not replayed
    with pg_session_factory() as db:
        assert db.query(OpponentDecision).one().target_blunder_id is None
        assert db.query(OpponentTargetFact).count() == 0


def test_pg_two_publications_for_one_user_do_not_block_each_other(
    pg_session_factory, pg_engine
):
    """SHARE, not exclusive: a user's own moves never queue behind each other.

    Only folding conflicts with publication. Making the lock exclusive would
    serialize concurrent requests from one player for no invariant at all.
    """
    with pg_session_factory() as db:
        session_id, blunder_id = _seed(db)

    first_admitted = threading.Event()
    reasons: list[str | None] = []

    def first():
        with pg_session_factory() as db:
            reasons.append(
                admit_target_publication(
                    db, user_id=USER_ID, session_id=session_id
                )
            )
            first_admitted.set()
            _record_target(db, session_id, blunder_id, "first")

    def second():
        with pg_session_factory() as db:
            assert first_admitted.wait(10)
            reasons.append(
                admit_target_publication(
                    db, user_id=USER_ID, session_id=session_id
                )
            )
            _record_target(db, session_id, blunder_id, "second")

    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = pool.submit(first), pool.submit(second)
        one.result(timeout=20)
        two.result(timeout=20)

    assert reasons == [None, None]
    with pg_session_factory() as db:
        assert db.query(OpponentDecision).count() == 2


def test_pg_the_endpoint_targets_normally_when_nothing_holds_the_row(
    pg_client, pg_session_factory, auth_headers
):
    """The baseline this file's contention cases are measured against."""
    with pg_session_factory() as db:
        session_id, blunder_id = _seed(db)

    with patch("app.opponent_move_controller.choose_move") as maia:
        response = pg_client.post(
            "/api/game/next-opponent-move",
            json={"session_id": str(session_id), "fen": AFTER_E4_FEN, "moves": ["e2e4"]},
            headers=auth_headers(user_id=USER_ID),
        )

    assert response.status_code == 200
    assert response.json()["target_blunder_id"] == blunder_id
    maia.assert_not_called()
    with pg_session_factory() as db:
        assert db.query(OpponentDecision).one().target_blunder_id == blunder_id


def test_pg_the_endpoint_serves_a_persisted_legal_move_under_contention(
    pg_client, pg_session_factory, auth_headers
):
    """End to end: a fold holds the row, the player still gets a recorded move.

    The ghost path selects a real target here — the baseline above proves it —
    so this exercises the whole degradation: acquisition times out, the target is
    dropped, and an ordinary untargeted move is recorded in its place. No
    retention HTTP error, and nothing left holding the row.
    """
    with pg_session_factory() as db:
        session_id, _ = _seed(db)

    holding, answered = threading.Event(), threading.Event()

    def compactor():
        with pg_session_factory() as db:
            assert lock_state_for_fold(db, user_id=USER_ID) is True
            holding.set()
            assert answered.wait(30)
            db.rollback()

    with ThreadPoolExecutor(max_workers=1) as pool:
        folding = pool.submit(compactor)
        try:
            assert holding.wait(10)
            with patch(
                "app.opponent_move_controller.choose_move", return_value=MAIA_MOVE
            ) as maia:
                response = pg_client.post(
                    "/api/game/next-opponent-move",
                    json={
                        "session_id": str(session_id),
                        "fen": AFTER_E4_FEN,
                        "moves": ["e2e4"],
                    },
                    headers=auth_headers(user_id=USER_ID),
                )
        finally:
            answered.set()
        folding.result(timeout=30)

    assert response.status_code == 200
    body = response.json()
    assert body["target_blunder_id"] is None
    assert body["target_blunder_srs"] is None
    assert body["target_fen"] is None
    maia.assert_called_once()
    board = chess.Board(AFTER_E4_FEN)
    assert chess.Move.from_uci(body["move"]["uci"]) in board.legal_moves

    with pg_session_factory() as db:
        decision = db.query(OpponentDecision).one()
        assert decision.target_blunder_id is None
        assert str(decision.decision_id) == body["decision_id"]
        assert decision.request_fen_hash == fen_hash(normalize_fen(AFTER_E4_FEN))
        assert db.query(OpponentTargetFact).count() == 0


def test_pg_a_contended_insert_is_bounded_and_degrades(pg_session_factory, pg_engine):
    """The share lock cannot be held indefinitely behind an uncommitted duplicate.

    G is a drain gap for in-flight writers, which means nothing unless "in flight"
    has a finite length. The one wait inside the publication window that is not
    ours is a concurrent identical request holding a speculative insert on the
    same fingerprint; the bound turns that into a degraded move instead of an
    unbounded hold that would starve every fold for this user.
    """
    from sqlalchemy.orm import Session

    with pg_session_factory() as db:
        session_id, blunder_id = _seed(db)

    inserted, finish, loser_ready = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original_commit = Session.commit
    loser_pid: list[int] = []
    outcome: dict[str, object] = {}

    def winner():
        with pg_session_factory() as db:

            def held_commit():
                inserted.set()
                assert finish.wait(20)
                original_commit(db)

            db.commit = held_commit
            return _record_target(db, session_id, blunder_id, "contended")

    def loser():
        with pg_session_factory() as db:
            loser_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
            loser_ready.set()
            # Patched down from 2 s purely to keep the timeout path fast; the
            # mechanism under test is the bound existing at all.
            with patch("app.srs_target_admission.PUBLICATION_LOCK_WAIT", "250ms"):
                try:
                    _record_target(db, session_id, blunder_id, "contended")
                except TargetPublicationSuppressed as exc:
                    outcome["reason"] = exc.reason
                    return
            outcome["reason"] = None

    with ThreadPoolExecutor(max_workers=2) as pool:
        winning = pool.submit(winner)
        try:
            assert inserted.wait(10)
            losing = pool.submit(loser)
            assert loser_ready.wait(10)
            with pg_engine.connect() as observer:
                assert await_pg_lock(observer, loser_pid[0]), (
                    "the second publication never blocked on the speculative insert"
                )
            losing.result(timeout=20)
        finally:
            finish.set()
        winning.result(timeout=20)

    assert outcome["reason"] == REASON_PUBLICATION_TIMEOUT
    with pg_session_factory() as db:
        # The winner's row stands alone, and the timed-out publication left no
        # lock behind for the next fold to trip over.
        assert db.query(OpponentDecision).one().target_blunder_id == blunder_id
        assert lock_state_for_fold(db, user_id=USER_ID) is True


def test_pg_a_disconnected_publisher_holds_nothing(pg_session_factory):
    """A backend that dies mid-publication leaves no pin and no lock behind.

    The share lock and both transaction-local budgets are tied to the
    transaction, so a crash is the same as a rollback: there is nothing to
    reap and nothing for the next fold to wait on.
    """
    with pg_session_factory() as db:
        session_id, _ = _seed(db)

    db = pg_session_factory()
    assert (
        admit_target_publication(db, user_id=USER_ID, session_id=session_id) is None
    )
    # Kill the connection without committing, as a crashed process would.
    db.connection().invalidate()
    db.close()

    with pg_session_factory() as other:
        assert lock_state_for_fold(other, user_id=USER_ID) is True
        assert other.query(OpponentDecision).count() == 0
        assert other.query(OpponentTargetFact).count() == 0


def test_pg_an_idle_publication_is_terminated_and_frees_the_row(pg_session_factory):
    """The idle ceiling fires, and the row comes back without anyone reaping it.

    ``lock_timeout`` and ``statement_timeout`` bound STATEMENTS. Neither bounds
    the gap between two of them, so the only thing between a worker that stalls
    mid-publication — alive, connected, holding FOR SHARE, running nothing — and a
    fold lane blocked for that user until someone notices is
    ``idle_in_transaction_session_timeout``. TCP keepalives do not cover this and
    neither does the disconnect test above: the peer is not dead, it is idle.

    The ceiling is driven down to 500 ms and the stall is real, so the bound the
    runbook quotes is a tested property rather than a setting that is merely
    spelled correctly.
    """
    with pg_session_factory() as db:
        session_id, _ = _seed(db)

    stalled = pg_session_factory()
    with patch.object(srs_target_admission, "PUBLICATION_IDLE_TIMEOUT", "500ms"):
        assert (
            admit_target_publication(stalled, user_id=USER_ID, session_id=session_id)
            is None
        )
    # Not one statement from here on. This is the stalled worker.
    deadline = time.monotonic() + 30
    while True:
        with pg_session_factory() as sweeper:
            freed = lock_state_for_fold(sweeper, user_id=USER_ID)
            sweeper.rollback()
        if freed:
            break
        assert time.monotonic() < deadline, (
            "an idle publication held the share lock past its ceiling"
        )
        time.sleep(0.1)

    # What freed it was the backend being terminated, not a commit: the stalled
    # transaction is gone, and everything it would have written went with it.
    with pytest.raises(DBAPIError) as caught:
        stalled.execute(text("SELECT 1"))
    # 25P03 — the one SQLSTATE this module deliberately does NOT read as
    # contention. The publication did not lose a race for the row; PostgreSQL
    # took the connection away for stalling on it, which is the whole mechanism.
    assert caught.value.orig.sqlstate == "25P03"
    stalled.invalidate()

    with pg_session_factory() as other:
        assert other.query(OpponentDecision).count() == 0
        assert other.query(OpponentTargetFact).count() == 0


def test_pg_publication_does_not_contend_with_a_session_row_lock(pg_session_factory):
    """No session, blunder or replay lock is taken ahead of the state acquisition.

    The opponent-decision retention lane (``g-retain-decisions``) locks session
    rows, and this interlock must not start queueing behind them — or deadlocking
    against them by taking its locks in the opposite order. It reads the session
    without a row lock; the decision INSERT's foreign key takes only FOR KEY
    SHARE, which a writer's FOR NO KEY UPDATE does not conflict with.
    """
    with pg_session_factory() as db:
        session_id, blunder_id = _seed(db)

    holder_ready, published = threading.Event(), threading.Event()
    outcome: dict[str, object] = {}

    def session_lock_holder():
        with pg_session_factory() as db:
            db.execute(
                select(GameSession.id)
                .where(GameSession.id == session_id)
                .with_for_update(key_share=True)
            )
            holder_ready.set()
            assert published.wait(20)
            db.rollback()

    def publisher():
        with pg_session_factory() as db:
            assert holder_ready.wait(10)
            outcome["reason"] = admit_target_publication(
                db, user_id=USER_ID, session_id=session_id
            )
            outcome["served"] = _record_target(db, session_id, blunder_id)
            published.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        holding = pool.submit(session_lock_holder)
        publishing = pool.submit(publisher)
        try:
            publishing.result(timeout=20)
        finally:
            published.set()
        holding.result(timeout=20)

    assert outcome["reason"] is None
    served, replayed = outcome["served"]
    assert not replayed
    assert served.target_blunder_id == blunder_id


def test_pg_a_first_publication_creates_the_row_and_a_racing_one_degrades(
    pg_session_factory, pg_engine
):
    """A user with no state row yet: the interlock is total from the first move.

    The row has to be created before it can be locked, and both sides create it
    the same way, so two first-time publications serialize on the primary-key
    conflict rather than running unserialized. Ordinarily the racer simply blocks
    for as long as the creator's insert is uncommitted and is then admitted; the
    creator is held past the 750 ms budget HERE so the losing branch is the one
    under test. Either way the interlock never lets an unserialized target
    through, and never fails the move.
    """
    with pg_session_factory() as db:
        session_id, blunder_id = _seed(db)
        db.execute(
            text(
                "DELETE FROM user_opportunity_retention_state WHERE user_id = :uid"
            ),
            {"uid": USER_ID},
        )
        db.commit()

    created, finish, loser_ready = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    loser_pid: list[int] = []
    outcome: dict[str, object] = {}

    def creator():
        with pg_session_factory() as db:
            assert (
                admit_target_publication(
                    db, user_id=USER_ID, session_id=session_id
                )
                is None
            )
            created.set()
            assert finish.wait(20)
            _record_target(db, session_id, blunder_id, "creator")

    def racer():
        with pg_session_factory() as db:
            loser_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
            assert created.wait(10)
            loser_ready.set()
            outcome["reason"] = admit_target_publication(
                db, user_id=USER_ID, session_id=session_id
            )
            db.rollback()

    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(creator)
        racing = pool.submit(racer)
        try:
            assert loser_ready.wait(10)
            with pg_engine.connect() as observer:
                assert await_pg_lock(observer, loser_pid[0]), (
                    "the racing publication never blocked on the created row"
                )
            racing.result(timeout=20)
        finally:
            finish.set()
        creating.result(timeout=20)

    assert outcome["reason"] == REASON_STATE_LOCK_TIMEOUT
    with pg_session_factory() as db:
        assert db.query(OpponentDecision).one().target_blunder_id == blunder_id
        assert lock_state_for_fold(db, user_id=USER_ID) is True
