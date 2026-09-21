"""Target publication under SRS opportunity retention (g-srs-target-publish).

One guarantee is under test throughout: **retention bookkeeping may remove a
target, but it must never fail a move.** Every condition below — a fold prefix,
an expired mutation window, a missing policy or state row, a counter reader that
refuses — ends with a legal move, recorded in ``opponent_decisions``, carrying no
target and no invented counters, and answerable to a retry by replay.

The PostgreSQL file covers what SQLite cannot: real ``FOR SHARE`` / ``FOR UPDATE
NOWAIT`` contention, acquisition timeouts and commit-ordered races. What is here
is the decision logic, the branch routing and the persisted outcome.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import chess
from sqlalchemy import DateTime, bindparam, delete, text, update

from app.api.game import GhostSelection
from app.fen import fen_hash
from app.models import (
    GameSession,
    OpponentDecision,
    OpportunityRetentionPolicy,
    UserOpportunityRetentionState,
)
from app.opponent_move_controller import ControllerMove
from app.opportunity_retention import POLICY_ID
from app.srs_opportunity import OpportunityCounters
from app.srs_target_admission import (
    REASON_COUNTERS_UNAVAILABLE,
    REASON_MISSING_POLICY,
    REASON_MISSING_STATE,
    REASON_MUTATION_WINDOW_EXPIRED,
    REASON_SESSION_UNAVAILABLE,
    REASON_TARGETING_AFTER_FOLD,
    admit_target_publication,
)

USER_ID = 123
# What Maia answers with in these tests. A suppressed request loses its target,
# not its opponent: it goes to the engine exactly like any untargeted move, and
# only falls back to a local legal move if the engine itself is unavailable.
MAIA_MOVE = ControllerMove(uci="e7e5", san="e5", method="maia3_api")
# Black to move: the opponent's turn for a white player.
FEN_A = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
# White to move: the player's colour, so a blunder here is ghost-eligible.
FEN_B = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
def _seed_ghost_target(db_session, *, user_id: int = USER_ID) -> int:
    """A one-edge ghost graph whose FEN_B blunder is due, and its blunder id.

    Raw SQL for the graph rows, matching the existing endpoint tests: the ORM
    models for positions/moves are not what this suite is about, and the shape
    has to agree with what ``find_ghost_move``'s recursive CTE walks.
    """
    for fen, color in ((FEN_A, "black"), (FEN_B, "white")):
        db_session.execute(
            text(
                "INSERT INTO positions (user_id, fen_hash, fen_raw, active_color) "
                "VALUES (:uid, :hash, :fen, :color)"
            ),
            {"uid": user_id, "hash": fen_hash(fen), "fen": fen, "color": color},
        )
    db_session.flush()
    ids = {
        fen: db_session.execute(
            text("SELECT id FROM positions WHERE fen_hash = :h AND user_id = :uid"),
            {"h": fen_hash(fen), "uid": user_id},
        ).scalar_one()
        for fen in (FEN_A, FEN_B)
    }
    db_session.execute(
        text(
            "INSERT INTO moves (from_position_id, move_san, to_position_id) "
            "VALUES (:from_id, 'e5', :to_id)"
        ),
        {"from_id": ids[FEN_A], "to_id": ids[FEN_B]},
    )
    db_session.execute(
        text(
            "INSERT INTO blunders "
            "(user_id, position_id, bad_move_san, best_move_san, eval_loss_cp, created_at) "
            "VALUES (:uid, :pid, 'Nf6', 'd5', 150, :created_at)"
        ).bindparams(bindparam("created_at", type_=DateTime(timezone=True))),
        {
            "uid": user_id,
            "pid": ids[FEN_B],
            "created_at": datetime.now(timezone.utc) - timedelta(hours=5),
        },
    )
    db_session.commit()
    return db_session.execute(
        text("SELECT id FROM blunders WHERE user_id = :uid"), {"uid": user_id}
    ).scalar_one()


def _set_policy(db_session, **values) -> None:
    db_session.execute(
        update(OpportunityRetentionPolicy)
        .where(OpportunityRetentionPolicy.id == POLICY_ID)
        .values(**values)
    )
    db_session.commit()


def _set_prefix(db_session, *, user_id: int, prefix: datetime) -> None:
    db_session.merge(
        UserOpportunityRetentionState(
            user_id=user_id, folded_through_started_at=prefix
        )
    )
    db_session.commit()


def _game_session(db_session, session_id: str) -> GameSession:
    return db_session.get(GameSession, uuid.UUID(session_id))


def _decisions(db_session, session_id: str) -> list[OpponentDecision]:
    db_session.expire_all()
    return (
        db_session.query(OpponentDecision)
        .filter(OpponentDecision.session_id == uuid.UUID(session_id))
        .order_by(OpponentDecision.served_at)
        .all()
    )


def _post(client, auth_headers, session_id, *, fen=FEN_A, moves=None, user_id=USER_ID):
    return client.post(
        "/api/game/next-opponent-move",
        json={"session_id": session_id, "fen": fen, "moves": moves or []},
        headers=auth_headers(user_id=user_id),
    )


def _racing_ghost(db_session, blunder_id: int, *, fold):
    """A ghost search that finishes, then a fold commits before publication.

    This is the only ordering in which the interlock's own prefix and age arms
    are reachable: every unraced case is already refused by the counter reader,
    which will not exclude a frozen session from its own counters. Simulating it
    with a side effect keeps the ordering explicit and deterministic instead of
    depending on a sleep.
    """

    def _search(**_kwargs):
        fold()
        return GhostSelection("e5", blunder_id, None, None, OpportunityCounters())

    return _search


# ---------------------------------------------------------------------------
# admit_target_publication: the decision itself
# ---------------------------------------------------------------------------
def test_admission_creates_the_interlock_row_for_a_user_without_one(
    create_game_session, db_session
):
    """A missing state row is created, not treated as a fold.

    Only users that existed when the migration ran were backfilled. Suppressing
    every target for newer accounts would protect against a fold that cannot have
    happened — nothing folds without advancing the prefix on this very row.
    """
    session_id = create_game_session(user_id=USER_ID)
    db_session.execute(
        delete(UserOpportunityRetentionState).where(
            UserOpportunityRetentionState.user_id == USER_ID
        )
    )
    db_session.commit()

    reason = admit_target_publication(
        db_session, user_id=USER_ID, session_id=uuid.UUID(session_id)
    )

    assert reason is None
    state = db_session.get(UserOpportunityRetentionState, USER_ID)
    assert state is not None
    # Created with no prefix: nothing has been folded, and a seeded timestamp
    # would freeze history that never was.
    assert state.folded_through_started_at is None


def test_admission_refuses_a_session_at_or_below_the_fold_prefix(
    create_game_session, db_session, caplog
):
    session_id = create_game_session(user_id=USER_ID)
    started_at = _game_session(db_session, session_id).started_at
    # Exactly AT the prefix, which is inclusive by construction: the prefix is the
    # MAX(started_at) of pairs that were actually folded.
    _set_prefix(db_session, user_id=USER_ID, prefix=started_at)

    with caplog.at_level(logging.ERROR):
        reason = admit_target_publication(
            db_session, user_id=USER_ID, session_id=uuid.UUID(session_id)
        )

    assert reason == REASON_TARGETING_AFTER_FOLD
    # An alarm, not a counter: a live session steering behind the prefix means the
    # eligibility side let through evidence that has already been deleted.
    assert "targeting after fold" in caplog.text


def test_admission_allows_a_session_newer_than_the_fold_prefix(
    create_game_session, db_session
):
    session_id = create_game_session(user_id=USER_ID)
    started_at = _game_session(db_session, session_id).started_at
    _set_prefix(
        db_session, user_id=USER_ID, prefix=started_at - timedelta(seconds=1)
    )

    assert (
        admit_target_publication(
            db_session, user_id=USER_ID, session_id=uuid.UUID(session_id)
        )
        is None
    )


def test_admission_refuses_a_session_past_the_mutation_window(
    create_game_session, db_session
):
    session_id = create_game_session(user_id=USER_ID)
    _set_policy(db_session, readiness=True, freeze_enabled=True)
    db_session.execute(
        update(GameSession)
        .where(GameSession.id == uuid.UUID(session_id))
        .values(started_at=datetime.now(timezone.utc) - timedelta(days=61))
    )
    db_session.commit()

    assert (
        admit_target_publication(
            db_session, user_id=USER_ID, session_id=uuid.UUID(session_id)
        )
        == REASON_MUTATION_WINDOW_EXPIRED
    )


def test_admission_ignores_session_age_while_freezing_is_disabled(
    create_game_session, db_session
):
    """The age arm is gated on freeze_enabled, exactly like every other one.

    Shipping M = 60 inert must not change behaviour, whatever the number says.
    """
    session_id = create_game_session(user_id=USER_ID)
    db_session.execute(
        update(GameSession)
        .where(GameSession.id == uuid.UUID(session_id))
        .values(started_at=datetime.now(timezone.utc) - timedelta(days=400))
    )
    db_session.commit()

    assert (
        admit_target_publication(
            db_session, user_id=USER_ID, session_id=uuid.UUID(session_id)
        )
        is None
    )


def test_admission_refuses_when_the_policy_row_is_missing(
    create_game_session, db_session
):
    """``load_policy`` defaults a missing row; publication does not.

    Tolerating absence keeps an un-migrated database behaving as before, but a
    defaulted M is a horizon nobody chose, and a target pinned against it would
    outlive evidence no one agreed to keep.
    """
    session_id = create_game_session(user_id=USER_ID)
    db_session.execute(delete(OpportunityRetentionPolicy))
    db_session.commit()

    assert (
        admit_target_publication(
            db_session, user_id=USER_ID, session_id=uuid.UUID(session_id)
        )
        == REASON_MISSING_POLICY
    )


def test_admission_refuses_a_session_that_is_gone(create_game_session, db_session):
    create_game_session(user_id=USER_ID)

    assert (
        admit_target_publication(
            db_session, user_id=USER_ID, session_id=uuid.uuid4()
        )
        == REASON_SESSION_UNAVAILABLE
    )


def test_admission_refuses_a_session_owned_by_another_user(
    create_game_session, db_session
):
    """Ownership is the caller's check; the interlock still refuses to pin here."""
    create_game_session(user_id=USER_ID)
    session_id = create_game_session(user_id=456)

    assert (
        admit_target_publication(
            db_session, user_id=USER_ID, session_id=uuid.UUID(session_id)
        )
        == REASON_SESSION_UNAVAILABLE
    )


def test_admission_reports_a_state_row_it_cannot_create(db_session, caplog):
    """A state row that cannot exist suppresses targeting rather than failing a move.

    The row is created if absent, but the INSERT still references ``users``. A
    user that is not there is a real invariant failure — reported as one, and
    still answered with a move by the caller.
    """
    with caplog.at_level(logging.ERROR):
        reason = admit_target_publication(
            db_session, user_id=999, session_id=uuid.uuid4()
        )

    assert reason == REASON_MISSING_STATE
    assert "retention state could not be created" in caplog.text
    # Returned rather than raised, and the caller owns the rollback: the interlock
    # runs no further SQL on an aborted transaction.
    db_session.rollback()


# ---------------------------------------------------------------------------
# The endpoint: what the player actually gets
# ---------------------------------------------------------------------------
def test_targeted_ghost_move_publishes_normally(
    client, auth_headers, db_session, create_game_session
):
    """Baseline: nothing is frozen, so the interlock admits and the target lands."""
    session_id = create_game_session(user_id=USER_ID)
    blunder_id = _seed_ghost_target(db_session)
    db_session.execute(
        delete(UserOpportunityRetentionState).where(
            UserOpportunityRetentionState.user_id == USER_ID
        )
    )
    db_session.commit()

    with patch("app.opponent_move_controller.choose_move") as maia:
        response = _post(client, auth_headers, session_id)

    assert response.status_code == 200
    body = response.json()
    assert body["target_blunder_id"] == blunder_id
    assert body["decision_source"] == "ghost_path"
    maia.assert_not_called()

    decision = _decisions(db_session, session_id)[0]
    assert decision.target_blunder_id == blunder_id
    # The interlock created the row it locks, so the compactor has something to
    # contend with the next time this user is swept.
    assert db_session.get(UserOpportunityRetentionState, USER_ID) is not None


def test_fold_racing_publication_persists_a_legal_untargeted_move(
    client, auth_headers, db_session, create_game_session, caplog
):
    """The fold commits after the search and before the insert: no target, still a move."""
    session_id = create_game_session(user_id=USER_ID)
    blunder_id = _seed_ghost_target(db_session)
    started_at = _game_session(db_session, session_id).started_at

    def fold():
        _set_prefix(db_session, user_id=USER_ID, prefix=started_at)

    with (
        patch(
            "app.api.game.find_ghost_move",
            side_effect=_racing_ghost(db_session, blunder_id, fold=fold),
        ),
        patch(
            "app.opponent_move_controller.choose_move", return_value=MAIA_MOVE
        ) as maia,
        caplog.at_level(logging.WARNING),
    ):
        response = _post(client, auth_headers, session_id)

    assert response.status_code == 200
    body = response.json()
    # Target-only fields cleared, not merely absent from the response model.
    assert body["target_blunder_id"] is None
    assert body["target_blunder_srs"] is None
    assert body["target_fen"] is None
    assert body["mode"] == "engine"
    assert body["decision_source"] == "backend_engine"
    # Losing the target does not cost the player the engine as well.
    maia.assert_called_once()
    assert body["move"]["uci"] == MAIA_MOVE.uci
    assert REASON_TARGETING_AFTER_FOLD in caplog.text

    board = chess.Board(FEN_A)
    assert chess.Move.from_uci(body["move"]["uci"]) in board.legal_moves

    decision = _decisions(db_session, session_id)[0]
    assert decision.target_blunder_id is None
    assert decision.decision_id is not None
    board.push_uci(body["move"]["uci"])
    assert decision.resulting_fen == board.fen()
    # No targeting sample was invented for a target that was never served.
    assert (
        db_session.execute(
            text("SELECT count(*) FROM opponent_target_facts")
        ).scalar_one()
        == 0
    )


def test_expired_mutation_window_racing_publication_degrades(
    client, auth_headers, db_session, create_game_session
):
    """The ordinary expiry arm, reached the only way it can be: by crossing M mid-request."""
    session_id = create_game_session(user_id=USER_ID)
    blunder_id = _seed_ghost_target(db_session)

    def expire():
        _set_policy(db_session, readiness=True, freeze_enabled=True)
        db_session.execute(
            update(GameSession)
            .where(GameSession.id == uuid.UUID(session_id))
            .values(started_at=datetime.now(timezone.utc) - timedelta(days=61))
        )
        db_session.commit()

    with (
        patch(
            "app.api.game.find_ghost_move",
            side_effect=_racing_ghost(db_session, blunder_id, fold=expire),
        ),
        patch(
            "app.opponent_move_controller.choose_move", return_value=MAIA_MOVE
        ) as maia,
    ):
        response = _post(client, auth_headers, session_id)

    assert response.status_code == 200
    assert response.json()["target_blunder_id"] is None
    maia.assert_called_once()
    assert _decisions(db_session, session_id)[0].target_blunder_id is None


def test_missing_summary_after_readiness_serves_a_move_instead_of_500(
    client, auth_headers, db_session, create_game_session
):
    """The reader refuses; the endpoint must not pass that refusal to the player.

    ``load_opportunity_counters`` raises on a blunder with no summary once
    readiness is set — deliberately, because degrading a folded counter to zeros
    would change SRS dueness and hide the loss. This endpoint owns the only
    fallback: no target, a real move, recorded.
    """
    session_id = create_game_session(user_id=USER_ID)
    _seed_ghost_target(db_session)  # raw insert: no summary row exists
    _set_policy(db_session, readiness=True)

    with patch(
        "app.opponent_move_controller.choose_move", return_value=MAIA_MOVE
    ) as maia:
        response = _post(client, auth_headers, session_id)

    assert response.status_code == 200
    assert response.json()["target_blunder_id"] is None
    maia.assert_called_once()
    assert _decisions(db_session, session_id)[0].target_blunder_id is None


def test_frozen_in_progress_session_serves_a_move_instead_of_500(
    client, auth_headers, db_session, create_game_session
):
    """A game left open past M: the exclusion is refused, the move is not.

    The ghost route excludes the in-progress session from its own counters, and a
    frozen session cannot be excluded because its share may already be folded.
    Before this bead that was a 500 on the next move of a long-running game.
    """
    session_id = create_game_session(user_id=USER_ID)
    _seed_ghost_target(db_session)
    _set_policy(db_session, readiness=True, freeze_enabled=True)
    db_session.execute(
        update(GameSession)
        .where(GameSession.id == uuid.UUID(session_id))
        .values(started_at=datetime.now(timezone.utc) - timedelta(days=61))
    )
    db_session.commit()

    with patch(
        "app.opponent_move_controller.choose_move", return_value=MAIA_MOVE
    ) as maia:
        response = _post(client, auth_headers, session_id)

    assert response.status_code == 200
    assert response.json()["target_blunder_id"] is None
    maia.assert_called_once()


def test_degraded_move_survives_maia_being_unavailable(
    client, auth_headers, db_session, create_game_session
):
    """The guarantee cannot rest on a remote dependency that is allowed to be down.

    This is the ONLY way the local floor is reached: the target is already gone
    AND the engine that would normally answer is unavailable. An ordinary request
    in the same outage still gets its 503 — see the test below.
    """
    from app.maia3_client import Maia3Error

    session_id = create_game_session(user_id=USER_ID)
    blunder_id = _seed_ghost_target(db_session)
    started_at = _game_session(db_session, session_id).started_at

    with (
        patch(
            "app.api.game.find_ghost_move",
            side_effect=_racing_ghost(
                db_session,
                blunder_id,
                fold=lambda: _set_prefix(
                    db_session, user_id=USER_ID, prefix=started_at
                ),
            ),
        ),
        patch(
            "app.opponent_move_controller.choose_move",
            side_effect=Maia3Error("maia is down"),
        ),
    ):
        response = _post(client, auth_headers, session_id)

    assert response.status_code == 200
    body = response.json()
    assert body["target_blunder_id"] is None
    # Local, legal, and recorded — not the engine's move and not an error.
    board = chess.Board(FEN_A)
    assert chess.Move.from_uci(body["move"]["uci"]) in board.legal_moves
    board.push_uci(body["move"]["uci"])
    decision = _decisions(db_session, session_id)[0]
    assert decision.target_blunder_id is None
    assert decision.resulting_fen == board.fen()


def test_an_ordinary_request_still_fails_when_maia_is_unavailable(
    client, auth_headers, db_session, create_game_session
):
    """Retention does not make an engine outage acceptable for everyone else.

    The local floor is a retention degradation's floor, not a general one:
    silently answering every Maia outage with a move out of a sorted list would
    hide the outage and quietly change what the opponent plays for all users.
    """
    from app.maia3_client import Maia3Error

    session_id = create_game_session(user_id=USER_ID)

    with patch(
        "app.opponent_move_controller.choose_move",
        side_effect=Maia3Error("maia is down"),
    ):
        response = _post(client, auth_headers, session_id)

    assert response.status_code == 503
    assert _decisions(db_session, session_id) == []


def test_degraded_move_survives_maia_answering_with_an_illegal_move(
    client, auth_headers, db_session, create_game_session
):
    """The engine's other failure mode reaches the floor too, not a 400.

    `choose_move` derives Maia's position from `moves` and then validates the
    answer against the request FEN, raising ValueError when the two disagree.
    That is the engine failing, not the client: the position is playable and a
    legal move exists. Before retention this request would have been served its
    ghost move from the FEN alone, so letting the suppression turn it into a 400
    would mean retention bookkeeping had failed the move after all.
    """
    session_id = create_game_session(user_id=USER_ID)
    blunder_id = _seed_ghost_target(db_session)
    started_at = _game_session(db_session, session_id).started_at

    with (
        patch(
            "app.api.game.find_ghost_move",
            side_effect=_racing_ghost(
                db_session,
                blunder_id,
                fold=lambda: _set_prefix(
                    db_session, user_id=USER_ID, prefix=started_at
                ),
            ),
        ),
        patch(
            "app.opponent_move_controller.choose_move",
            side_effect=ValueError(f"Maia3 returned illegal move a1a2 for FEN {FEN_A}"),
        ),
    ):
        response = _post(client, auth_headers, session_id)

    assert response.status_code == 200
    body = response.json()
    assert body["target_blunder_id"] is None
    board = chess.Board(FEN_A)
    assert chess.Move.from_uci(body["move"]["uci"]) in board.legal_moves
    board.push_uci(body["move"]["uci"])
    decision = _decisions(db_session, session_id)[0]
    assert decision.target_blunder_id is None
    assert decision.resulting_fen == board.fen()


def test_an_ordinary_request_still_fails_when_maia_answers_illegally(
    client, auth_headers, db_session, create_game_session
):
    """The floor stays retention-only in this direction as well.

    The pair with the 503 above: an untargeted request that the engine answers
    illegally keeps its 400, so a Maia/history disagreement stays visible instead
    of being quietly absorbed into a move off a sorted list for every user.
    """
    session_id = create_game_session(user_id=USER_ID)

    with patch(
        "app.opponent_move_controller.choose_move",
        side_effect=ValueError(f"Maia3 returned illegal move a1a2 for FEN {FEN_A}"),
    ):
        response = _post(client, auth_headers, session_id)

    assert response.status_code == 400
    assert _decisions(db_session, session_id) == []


def test_degraded_decision_replays_exactly(
    client, auth_headers, db_session, create_game_session
):
    """A retry of the same request replays the stored degraded move, byte for byte."""
    session_id = create_game_session(user_id=USER_ID)
    blunder_id = _seed_ghost_target(db_session)
    started_at = _game_session(db_session, session_id).started_at

    with (
        patch(
            "app.api.game.find_ghost_move",
            side_effect=_racing_ghost(
                db_session,
                blunder_id,
                fold=lambda: _set_prefix(
                    db_session, user_id=USER_ID, prefix=started_at
                ),
            ),
        ),
        patch("app.opponent_move_controller.choose_move", return_value=MAIA_MOVE),
    ):
        first = _post(client, auth_headers, session_id)
        retry = _post(client, auth_headers, session_id)

    assert first.status_code == retry.status_code == 200
    assert first.json() == retry.json()
    assert len(_decisions(db_session, session_id)) == 1


def test_a_committed_targeted_winner_is_replayed_rather_than_degraded(
    client, auth_headers, db_session, create_game_session
):
    """A raced duplicate gets the real decision, target and all.

    Replay creates no new targeting sample, so it is exempt from this freeze: the
    committed envelope is simply the answer to a request that was already served.
    """
    from app.api.game import _decision_fingerprint
    from app.fen import normalize_fen

    session_id = create_game_session(user_id=USER_ID)
    blunder_id = _seed_ghost_target(db_session)
    started_at = _game_session(db_session, session_id).started_at
    fingerprint = _decision_fingerprint(normalize_fen(FEN_A), [])

    def race():
        """Commit a targeted winner for this fingerprint, then fold."""
        from test_opponent_decision_record import _record_target

        _record_target(db_session, uuid.UUID(session_id), blunder_id, fingerprint)
        _set_prefix(db_session, user_id=USER_ID, prefix=started_at)

    with (
        patch(
            "app.api.game.find_ghost_move",
            side_effect=_racing_ghost(db_session, blunder_id, fold=race),
        ),
        patch("app.opponent_move_controller.choose_move") as maia,
    ):
        response = _post(client, auth_headers, session_id)

    assert response.status_code == 200
    assert response.json()["target_blunder_id"] == blunder_id
    maia.assert_not_called()
    # The degraded fallback was never inserted; the winner stands alone.
    assert len(_decisions(db_session, session_id)) == 1


def test_untargeted_branches_take_no_retention_lock(
    client, auth_headers, db_session, create_game_session
):
    """An engine move pins nothing, so retention has nothing to say about it."""
    from app.opponent_move_controller import ControllerMove

    session_id = create_game_session(user_id=USER_ID)
    # A prefix that would freeze this session if the interlock ran at all.
    _set_prefix(
        db_session,
        user_id=USER_ID,
        prefix=_game_session(db_session, session_id).started_at,
    )

    with (
        patch("app.srs_target_admission.admit_target_publication") as admission,
        patch(
            "app.opponent_move_controller.choose_move",
            return_value=ControllerMove(uci="e7e5", san="e5", method="maia3_api"),
        ),
    ):
        response = _post(client, auth_headers, session_id)

    assert response.status_code == 200
    assert response.json()["decision_source"] == "backend_engine"
    admission.assert_not_called()


def test_structural_drill_move_takes_precedence_over_the_engine_path(
    client, auth_headers, create_game_session, db_session
):
    """A root-reached drill keeps its own topology instead of going to the engine.

    Where the drill already has structural moves, serving one keeps opponent play
    on score-relevant topology — the degradation drops the TARGET, not the drill,
    and not the tier either.
    """
    from test_drill_api import ROOT_FEN, _post_root_steering_graph, _roots_for

    graph, positions = _post_root_steering_graph()
    with patch("app.api.drills.get_opening_roots", return_value=_roots_for(ROOT_FEN)):
        start = client.post(
            "/api/drills/start",
            json={
                "opening_key": ROOT_FEN,
                "player_color": "white",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(user_id=USER_ID),
        )
    assert start.status_code == 201
    session_id = start.json()["session_id"]
    session = _game_session(db_session, session_id)
    session.drill_state = "root_reached"
    session.drill_root_reached_ply = 1
    db_session.commit()
    started_at = session.started_at
    blunder_id = _seed_ghost_target(db_session)

    with (
        patch("app.api.game.get_opening_graph", return_value=graph),
        patch(
            "app.api.game.find_ghost_move",
            side_effect=_racing_ghost(
                db_session,
                blunder_id,
                fold=lambda: _set_prefix(
                    db_session, user_id=USER_ID, prefix=started_at
                ),
            ),
        ),
        patch("app.opponent_move_controller.choose_move") as maia,
    ):
        response = client.post(
            "/api/game/next-opponent-move",
            json={
                "session_id": session_id,
                "fen": positions[1],
                "moves": ["e2e4"],
            },
            headers=auth_headers(user_id=USER_ID),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "ghost"
    assert body["decision_source"] == "ghost_path"
    assert body["target_blunder_id"] is None
    maia.assert_not_called()

    board = chess.Board(positions[1])
    assert chess.Move.from_uci(body["move"]["uci"]) in board.legal_moves
    assert _decisions(db_session, session_id)[0].target_blunder_id is None


def test_retention_degradation_is_classified_in_internal_telemetry(
    client, auth_headers, create_game_session, db_session
):
    """The reason is telemetry, never a response field, schema value or enum.

    Clients must not branch on why a move was degraded: it is an ordinary
    non-targeted move to them, and to root confirmation.
    """
    session_id = create_game_session(user_id=USER_ID)
    _seed_ghost_target(db_session)
    _set_policy(db_session, readiness=True)

    with (
        patch("app.api.game.capture") as capture,
        patch("app.opponent_move_controller.choose_move", return_value=MAIA_MOVE),
    ):
        response = _post(client, auth_headers, session_id)

    assert response.status_code == 200
    served = [
        call for call in capture.call_args_list if call.args[1] == "opponent_move_served"
    ]
    assert len(served) == 1
    properties = served[0].args[2]
    assert properties["retention_suppression"] == REASON_COUNTERS_UNAVAILABLE
    assert properties["has_target_blunder"] is False
    assert "retention_suppression" not in response.json()


def test_ordinary_serves_carry_no_suppression_classification(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=USER_ID)
    _seed_ghost_target(db_session)

    with (
        patch("app.api.game.capture") as capture,
        patch("app.opponent_move_controller.choose_move"),
    ):
        response = _post(client, auth_headers, session_id)

    assert response.json()["target_blunder_id"] is not None
    served = [
        call for call in capture.call_args_list if call.args[1] == "opponent_move_served"
    ]
    assert served[0].args[2]["retention_suppression"] is None
