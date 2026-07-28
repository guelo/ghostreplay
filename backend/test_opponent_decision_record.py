"""Authoritative, replayable opponent-decision log (g-ghost-target-server-record).

Every served opponent decision writes exactly one ``opponent_decisions`` row before
the response leaves the endpoint, and a duplicate request REPLAYS that row's stored
payload rather than recomputing. The record is server-authoritative: nothing here
depends on the client's ``session_moves.target_blunder_id`` echo, which is what makes
it usable as the denominator of a targeted p_reach.

The row is an ENVELOPE, not the response: ``served_at`` and ``decision_id`` are
envelope-level, and ``target_blunder_id`` / ``resulting_fen`` / ``reaches_drill_root``
are extracted off the stored response for indexing and validation.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import chess
import pytest
from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateTable

from app.api.game import _decision_fingerprint
from app.fen import fen_hash, normalize_fen
from app.models import GameSession, OpponentDecision
from test_drill_api import ROOT_FEN, START_FEN, _roots_for, _steering_graph

AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
AFTER_E4_E5_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"
# What ``resulting_fen`` actually stores: python-chess emits the en-passant square
# only when an EP capture is legal, so the same position renders with "-" here.
# normalize_fen canonicalizes the two to the same hash.
AFTER_E4_E5_PLAYED_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


def _decisions(db_session, session_id: str) -> list[OpponentDecision]:
    return (
        db_session.query(OpponentDecision)
        .filter(OpponentDecision.session_id == uuid.UUID(session_id))
        .order_by(OpponentDecision.served_at)
        .all()
    )


def _post(client, auth_headers, session_id, fen, *, moves=None, user_id=123):
    return client.post(
        "/api/game/next-opponent-move",
        json={"session_id": session_id, "fen": fen, "moves": moves or []},
        headers=auth_headers(user_id=user_id),
    )


def _engine_move(uci: str = "e7e5", san: str = "e5"):
    from app.opponent_move_controller import ControllerMove

    return ControllerMove(uci=uci, san=san, method="maia3_api")


# ---------------------------------------------------------------------------
# Fingerprint injectivity
#
# A collision is not cosmetic: the fingerprint IS the replay key, so two distinct
# requests that hash alike let the second be served the first's stored decision.
# ---------------------------------------------------------------------------

FP_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # NextOpponentMoveRequest.moves is list[str] with no per-element validation,
        # and these two JSON arrays reach Maia as different inputs. A space-join
        # collapsed them onto one key.
        (["e2e4 e7e5"], ["e2e4", "e7e5"]),
        ([""], []),
        (["e2e4", ""], ["e2e4"]),
        (["e2", "e4"], ["e2e4"]),
        (["e2e4", "e7e5"], ["e2e4", "e7e5", ""]),
    ],
    ids=["embedded-space", "empty-element", "trailing-empty", "split", "extra-empty"],
)
def test_fingerprint_is_injective_over_move_lists(left, right):
    assert _decision_fingerprint(FP_FEN, left) != _decision_fingerprint(FP_FEN, right)


def test_normalized_fen_can_contain_the_former_field_separator():
    """The FEN field is not newline-free, so "\\n" was never a sound field delimiter.

    normalize_fen splits on " " while chess.Board splits on ANY whitespace, so a FEN
    carrying a newline parses AND survives normalization. This much is
    endpoint-reachable.

    It is NOT a demonstrated collision, and no raw-FEN pair is known that produces
    one: a boundary shift needs one normalized output to be a "\\n"-terminated prefix
    of another, and normalize_fen unconditionally overwrites parts[3] with "-" or a
    square name, so every output's final space-field is newline-free and that shape
    cannot arise. The framing below is therefore DEFENSIVE — it removes the need to
    keep re-deriving that non-obvious invariant whenever normalize_fen changes.
    """
    assert "\n" in normalize_fen(
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR\nb KQkq - 0 1"
    )


def test_fingerprint_framing_is_not_forgeable_from_field_content():
    """Field-level property: no content can be read as a boundary (defensive)."""
    # Content shifted across the fen/history boundary. Hand-built inputs — see
    # test_normalized_fen_can_contain_the_former_field_separator for why no raw-FEN
    # pair is known to produce this pair.
    assert _decision_fingerprint(
        "rnbq/8/8/8/8/8/8/RNBQ w KQkq -\nSTOLEN", ["e2e4"]
    ) != _decision_fingerprint("rnbq/8/8/8/8/8/8/RNBQ w KQkq -", ["STOLEN\ne2e4"])
    # The framing itself must not be forgeable from field content either.
    assert _decision_fingerprint("1:fen", []) != _decision_fingerprint("fen", [])


# ---------------------------------------------------------------------------
# Engine decisions
# ---------------------------------------------------------------------------


def test_engine_decision_is_recorded(
    client, auth_headers, create_game_session, db_session
):
    """One row per served engine decision, with the payload carrying its own id."""
    session_id = create_game_session(user_id=123, player_color="white")

    with patch("app.opponent_move_controller.choose_move", return_value=_engine_move()):
        response = _post(client, auth_headers, session_id, AFTER_E4_FEN, moves=["e2e4"])

    assert response.status_code == 200
    data = response.json()

    rows = _decisions(db_session, session_id)
    assert len(rows) == 1
    row = rows[0]

    assert data["decision_id"] == str(row.decision_id)
    assert row.request_fen_hash == fen_hash(AFTER_E4_FEN)
    assert row.request_fingerprint == _decision_fingerprint(
        normalize_fen(AFTER_E4_FEN), ["e2e4"]
    )
    assert row.uci_history == '["e2e4"]'
    assert json.loads(row.uci_history) == ["e2e4"]
    assert row.ply_before == 1
    assert row.served_at is not None
    assert row.target_blunder_id is None
    assert row.reaches_drill_root is False
    assert row.resulting_fen == AFTER_E4_E5_PLAYED_FEN

    # The payload is the served response verbatim, and already knows the id of the
    # row it is stored in — the allocate-before-serialize ordering.
    payload = json.loads(row.response_payload)
    assert payload["decision_id"] == str(row.decision_id)
    assert payload["move"] == {"uci": "e7e5", "san": "e5"}
    assert payload["mode"] == "engine"
    assert payload["decision_source"] == "backend_engine"


def test_identical_retry_replays_instead_of_recomputing(
    client, auth_headers, create_game_session, db_session
):
    """getNextOpponentMove retries twice; the same request must not compute twice."""
    session_id = create_game_session(user_id=123, player_color="white")

    with patch(
        "app.opponent_move_controller.choose_move", return_value=_engine_move()
    ) as mock_choose:
        first = _post(client, auth_headers, session_id, AFTER_E4_FEN, moves=["e2e4"])
        second = _post(client, auth_headers, session_id, AFTER_E4_FEN, moves=["e2e4"])

    assert first.status_code == 200
    assert second.status_code == 200
    # Replay, not recompute: the decision-maker ran exactly once.
    mock_choose.assert_called_once()
    assert second.json() == first.json()
    assert second.json()["decision_id"] == first.json()["decision_id"]
    assert len(_decisions(db_session, session_id)) == 1


def test_same_fen_different_history_creates_a_new_decision(
    client, auth_headers, create_game_session, db_session
):
    """The post-revert branch: same session, same ply, truncated/other history.

    rewindBoardLocally truncates move history and the revert flow continues on the
    SAME session, so a branch that legitimately re-asks at the same position must get
    a NEW decision rather than a conflict or a stale replay.
    """
    session_id = create_game_session(user_id=123, player_color="white")

    # Same FEN by transposition: 1.e4 vs 1.Nf3 Nf6 2.Ng1 Ng8 3.e4 reach the same
    # position with different histories.
    board = chess.Board()
    long_history = ["g1f3", "g8f6", "f3g1", "f6g8", "e2e4"]
    for uci in long_history:
        board.push_uci(uci)
    assert normalize_fen(board.fen()) == normalize_fen(AFTER_E4_FEN)

    with patch("app.opponent_move_controller.choose_move", return_value=_engine_move()):
        first = _post(client, auth_headers, session_id, AFTER_E4_FEN, moves=["e2e4"])
        second = _post(
            client, auth_headers, session_id, board.fen(), moves=long_history
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["decision_id"] != first.json()["decision_id"]

    rows = _decisions(db_session, session_id)
    assert len(rows) == 2
    assert {r.ply_before for r in rows} == {1, 5}
    # Same normalized position, different fingerprints — history is part of the key.
    assert rows[0].request_fen_hash == rows[1].request_fen_hash
    assert rows[0].request_fingerprint != rows[1].request_fingerprint


def test_move_entry_containing_a_space_does_not_replay_a_split_history(
    client, auth_headers, create_game_session, db_session
):
    """End-to-end proof that the fingerprint collision cannot cross-replay.

    ``["e2e4 e7e5"]`` and ``["e2e4", "e7e5"]`` are distinct arrays forwarded verbatim
    to Maia. Under a space-joined key they shared a fingerprint, so the second request
    was served the first's stored decision instead of its own.
    """
    session_id = create_game_session(user_id=123, player_color="white")

    with patch(
        "app.opponent_move_controller.choose_move",
        side_effect=[_engine_move(uci="e7e5", san="e5"), _engine_move(uci="c7c5", san="c5")],
    ) as mock_choose:
        joined = _post(
            client, auth_headers, session_id, AFTER_E4_FEN, moves=["e2e4 e7e5"]
        )
        split = _post(
            client, auth_headers, session_id, AFTER_E4_FEN, moves=["e2e4", "e7e5"]
        )

    assert joined.status_code == 200
    assert split.status_code == 200
    # Each request got its OWN decision — the second was not answered from the first.
    assert mock_choose.call_count == 2
    assert split.json()["decision_id"] != joined.json()["decision_id"]
    assert split.json()["move"]["uci"] == "c7c5"

    rows = _decisions(db_session, session_id)
    assert len(rows) == 2
    assert rows[0].request_fingerprint != rows[1].request_fingerprint
    assert {r.ply_before for r in rows} == {1, 2}
    # ...and the stored history separates them too, rather than collapsing to one text.
    assert {r.uci_history for r in rows} == {'["e2e4 e7e5"]', '["e2e4","e7e5"]'}


def test_uci_history_distinguishes_equal_length_ambiguous_splits(
    client, auth_headers, create_game_session, db_session
):
    """The stored history is the array the client sent, not a lossy join.

    ``["e2e4 e7e5", "g1f3"]`` and ``["e2e4", "e7e5 g1f3"]`` space-join to identical
    text AND have the same length, so neither the joined form nor ``ply_before``
    could separate them — which contradicts the column's "full UCI history" contract.
    """
    session_id = create_game_session(user_id=123, player_color="white")
    left, right = ["e2e4 e7e5", "g1f3"], ["e2e4", "e7e5 g1f3"]
    assert " ".join(left) == " ".join(right)
    assert len(left) == len(right)

    with patch(
        "app.opponent_move_controller.choose_move",
        side_effect=[_engine_move(), _engine_move(uci="c7c5", san="c5")],
    ):
        assert _post(client, auth_headers, session_id, AFTER_E4_FEN, moves=left).status_code == 200
        assert _post(client, auth_headers, session_id, AFTER_E4_FEN, moves=right).status_code == 200

    rows = _decisions(db_session, session_id)
    assert len(rows) == 2
    assert {r.ply_before for r in rows} == {2}
    assert {r.uci_history for r in rows} == {
        '["e2e4 e7e5","g1f3"]',
        '["e2e4","e7e5 g1f3"]',
    }
    assert sorted(json.loads(r.uci_history) for r in rows) == sorted([left, right])


def test_empty_history_round_trips(
    client, auth_headers, create_game_session, db_session
):
    """A black-playing user's first request is ply 0 with no history."""
    session_id = create_game_session(user_id=123, player_color="black")
    start_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    with patch(
        "app.opponent_move_controller.choose_move",
        return_value=_engine_move(uci="e2e4", san="e4"),
    ) as mock_choose:
        first = _post(client, auth_headers, session_id, start_fen, moves=[])
        second = _post(client, auth_headers, session_id, start_fen, moves=[])

    assert first.status_code == 200
    mock_choose.assert_called_once()
    assert second.json() == first.json()

    rows = _decisions(db_session, session_id)
    assert len(rows) == 1
    assert rows[0].uci_history == "[]"
    assert rows[0].ply_before == 0


# ---------------------------------------------------------------------------
# Ghost decisions
# ---------------------------------------------------------------------------


def _seed_ghost_target(db_session, user_id: int) -> int:
    """Seed positions/edge/blunder so find_ghost_move steers 1...e5. Returns the id."""
    for fen, color in ((AFTER_E4_FEN, "black"), (AFTER_E4_E5_FEN, "white")):
        db_session.execute(
            text(
                """
                INSERT INTO positions (user_id, fen_hash, fen_raw, active_color)
                VALUES (:uid, :hash, :fen, :color)
                """
            ),
            {"uid": user_id, "hash": fen_hash(fen), "fen": fen, "color": color},
        )
    db_session.flush()

    pos_a_id = db_session.execute(
        text("SELECT id FROM positions WHERE fen_hash = :h"),
        {"h": fen_hash(AFTER_E4_FEN)},
    ).scalar_one()
    pos_b_id = db_session.execute(
        text("SELECT id FROM positions WHERE fen_hash = :h"),
        {"h": fen_hash(AFTER_E4_E5_FEN)},
    ).scalar_one()

    db_session.execute(
        text(
            """
            INSERT INTO moves (from_position_id, move_san, to_position_id)
            VALUES (:from_id, 'e5', :to_id)
            """
        ),
        {"from_id": pos_a_id, "to_id": pos_b_id},
    )
    db_session.execute(
        text(
            """
            INSERT INTO blunders
                (user_id, position_id, bad_move_san, best_move_san, eval_loss_cp, created_at)
            VALUES (:uid, :pid, 'Nf6', 'd5', 150, :created_at)
            """
        ).bindparams(bindparam("created_at", type_=DateTime(timezone=True))),
        {
            "uid": user_id,
            "pid": pos_b_id,
            "created_at": datetime.now(timezone.utc) - timedelta(hours=5),
        },
    )
    db_session.commit()

    return db_session.execute(
        text("SELECT id FROM blunders WHERE position_id = :pid"), {"pid": pos_b_id}
    ).scalar_one()


def test_ghost_decision_records_target_and_resulting_fen(
    client, auth_headers, create_game_session, db_session
):
    """The served target is recorded SERVER-side, not echoed back by the client."""
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    blunder_id = _seed_ghost_target(db_session, user_id)

    response = _post(client, auth_headers, session_id, AFTER_E4_FEN, moves=["e2e4"])

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "ghost"
    assert data["target_blunder_id"] == blunder_id

    rows = _decisions(db_session, session_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.target_blunder_id == blunder_id
    assert row.resulting_fen == AFTER_E4_E5_PLAYED_FEN
    assert row.reaches_drill_root is False
    assert json.loads(row.response_payload)["target_blunder_srs"] is not None


def _seed_decision(db_session, *, session_id: str, blunder_id: int, served_at) -> None:
    """An already-served steer at ``blunder_id``, as the decision log would hold it."""
    db_session.add(
        OpponentDecision(
            decision_id=uuid.uuid4(),
            session_id=uuid.UUID(session_id),
            request_fingerprint=uuid.uuid4().hex,
            request_fen_hash=fen_hash(AFTER_E4_FEN),
            uci_history="[]",
            ply_before=0,
            served_at=served_at,
            response_payload="{}",
            target_blunder_id=blunder_id,
            resulting_fen=None,
            reaches_drill_root=False,
        )
    )


def test_ghost_srs_snapshot_excludes_the_serving_session(
    client, auth_headers, create_game_session, db_session
):
    """The snapshot is the evidence that CHOSE the target, not a read of live state.

    find_ghost_move scores with the serving session excluded, so the payload has to
    be scoped the same way. An unscoped read would count the decision this very
    request records — and any earlier steer in the same session — leaving a frozen
    payload that contradicts its own score from the moment it is stored, and every
    replay serving that contradiction.
    """
    from app.srs_opportunity import compute_p_reach

    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    other_session_id = create_game_session(user_id=user_id, player_color="white")
    blunder_id = _seed_ghost_target(db_session, user_id)

    served_at = datetime.now(timezone.utc) - timedelta(hours=1)
    _seed_decision(db_session, session_id=session_id, blunder_id=blunder_id, served_at=served_at)
    _seed_decision(
        db_session, session_id=other_session_id, blunder_id=blunder_id, served_at=served_at
    )
    db_session.commit()

    response = _post(client, auth_headers, session_id, AFTER_E4_FEN, moves=["e2e4"])

    assert response.status_code == 200
    srs = response.json()["target_blunder_srs"]
    # Only the other session. Neither the same-session steer seeded above nor the
    # one this request just wrote is inside its own denominator.
    assert srs["targeted_30d"] == 1
    assert srs["targeted_reached_30d"] == 0
    assert srs["p_reach"] == pytest.approx(round(compute_p_reach(0, 1), 4))
    assert len(_decisions(db_session, session_id)) == 2


def test_ghost_srs_snapshot_is_the_scored_read_not_a_second_one(
    client, auth_headers, create_game_session, db_session
):
    """One counter read per served ghost decision — the one the score used.

    Re-reading to build the payload would take a second READ COMMITTED snapshot: a
    later clock (so a drifted 30-day cutoff) and visibility of decisions other
    sessions committed in between. The payload is frozen and replayed verbatim, so
    that drift would be stored permanently. The perturbed second return here makes a
    reintroduced re-read show up in the served numbers, not just in the call count.
    """
    from dataclasses import replace

    from app.api import game as game_api

    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    other_session_id = create_game_session(user_id=user_id, player_color="white")
    blunder_id = _seed_ghost_target(db_session, user_id)
    _seed_decision(
        db_session,
        session_id=other_session_id,
        blunder_id=blunder_id,
        served_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.commit()

    real = game_api.load_opportunity_counters
    calls: list[dict] = []

    def counting(*args, **kwargs):
        result = real(*args, **kwargs)
        calls.append(kwargs)
        if len(calls) > 1:
            return {k: replace(v, targeted_30d=v.targeted_30d + 100) for k, v in result.items()}
        return result

    with patch.object(game_api, "load_opportunity_counters", counting):
        response = _post(client, auth_headers, session_id, AFTER_E4_FEN, moves=["e2e4"])

    assert response.status_code == 200
    assert response.json()["mode"] == "ghost"
    assert len(calls) == 1
    assert response.json()["target_blunder_srs"]["targeted_30d"] == 1


def test_ghost_retry_replays_the_srs_snapshot_verbatim(
    client, auth_headers, create_game_session, db_session
):
    """The stored payload — not a reconstruction — is what a retry gets back.

    ``target_blunder_srs`` snapshots counters that move between the original request
    and its retry. A field-by-field reconstruction would either re-query that mutable
    state or answer the retry with a different response than the one first served.
    """
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    blunder_id = _seed_ghost_target(db_session, user_id)

    first = _post(client, auth_headers, session_id, AFTER_E4_FEN, moves=["e2e4"])
    assert first.status_code == 200
    assert first.json()["target_blunder_srs"]["pass_streak"] == 0

    # Move the counters the response snapshotted.
    db_session.execute(
        text("UPDATE blunders SET pass_streak = 7 WHERE id = :id"), {"id": blunder_id}
    )
    db_session.commit()

    second = _post(client, auth_headers, session_id, AFTER_E4_FEN, moves=["e2e4"])

    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.json()["target_blunder_srs"]["pass_streak"] == 0
    assert len(_decisions(db_session, session_id)) == 1


# ---------------------------------------------------------------------------
# Pre-root drill route decisions
# ---------------------------------------------------------------------------


def _start_steering_drill(client, auth_headers, *, user_id: int = 123) -> str:
    with patch("app.api.drills.get_opening_roots", return_value=_roots_for(ROOT_FEN)):
        start = client.post(
            "/api/drills/start",
            json={
                "opening_key": ROOT_FEN,
                "player_color": "black",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(user_id=user_id),
        )
    assert start.status_code == 201
    return start.json()["session_id"]


def test_preroot_route_decision_is_recorded_with_root_flag(
    client, auth_headers, db_session
):
    """A route decision carries no target, so a target-only record could not log it."""
    session_id = _start_steering_drill(client, auth_headers)

    with patch("app.api.game.get_opening_graph", return_value=_steering_graph()):
        response = _post(client, auth_headers, session_id, START_FEN, moves=[])

    assert response.status_code == 200
    data = response.json()
    assert data["move"] == {"uci": "e2e4", "san": "e4"}
    assert data["drill_route"]["status"] == "root_reached"

    rows = _decisions(db_session, session_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.target_blunder_id is None
    assert row.reaches_drill_root is True
    assert row.resulting_fen == data["drill_route"]["resulting_fen"]
    assert row.ply_before == 0
    assert json.loads(row.response_payload)["decision_source"] == "ghost_path"

    # The drill_state write rides the same commit as the decision that produced it.
    session = (
        db_session.query(GameSession)
        .filter(GameSession.id == uuid.UUID(session_id))
        .one()
    )
    assert session.drill_state == "root_reached"


def test_route_retry_after_root_commit_replays_instead_of_falling_through(
    client, auth_headers, db_session
):
    """The concrete hazard the replay lookup's placement closes.

    The serve path commits ``drill_state='root_reached'`` BEFORE returning the route
    move that would reach it. Without replay, a first response lost after that commit
    makes the retry read ``drill_state != 'active'``, release the lock, fall through to
    the normal ghost/engine path, and answer from a still-pre-root FEN with a
    DIFFERENT move — leaving the database claiming a root no client ever applied.
    """
    session_id = _start_steering_drill(client, auth_headers)

    with patch("app.api.game.get_opening_graph", return_value=_steering_graph()):
        first = _post(client, auth_headers, session_id, START_FEN, moves=[])
    assert first.status_code == 200

    session = (
        db_session.query(GameSession)
        .filter(GameSession.id == uuid.UUID(session_id))
        .one()
    )
    assert session.drill_state == "root_reached"

    # The retry now takes the normal ghost/engine dispatch — so if it fell through, the
    # engine would answer. It must not even be consulted.
    with patch(
        "app.opponent_move_controller.choose_move",
        return_value=_engine_move(uci="d2d4", san="d4"),
    ) as mock_choose:
        second = _post(client, auth_headers, session_id, START_FEN, moves=[])

    assert second.status_code == 200
    mock_choose.assert_not_called()
    assert second.json() == first.json()
    assert second.json()["drill_route"]["status"] == "root_reached"
    assert len(_decisions(db_session, session_id)) == 1


# ---------------------------------------------------------------------------
# Concurrency and failure
# ---------------------------------------------------------------------------


def test_conflicting_insert_returns_the_winners_payload(
    client, auth_headers, create_game_session, db_session
):
    """The loser of an insert race serves the WINNER's move, discarding its own.

    ``find_ghost_move`` is randomized and the ghost/engine path holds no row lock, so
    two concurrent identical requests can both compute and disagree. A loser that
    returned its own move would serve a move no stored decision records, and root
    confirmation would then fail its resulting_fen check against the winner's row.

    The single-connection SQLite test engine cannot stage a real race, so the conflict
    is staged at the lookup: a winner row already exists, but the replay lookup is
    forced to miss it once, driving compute -> INSERT -> conflict -> re-SELECT.
    """
    session_id = create_game_session(user_id=123, player_color="white")

    winner_id = uuid.uuid4()
    winner_payload = json.dumps(
        {
            "mode": "engine",
            "move": {"uci": "c7c5", "san": "c5"},
            "target_blunder_id": None,
            "target_blunder_srs": None,
            "target_fen": None,
            "decision_source": "backend_engine",
            "drill_route": None,
            "decision_id": str(winner_id),
        }
    )
    db_session.add(
        OpponentDecision(
            decision_id=winner_id,
            session_id=uuid.UUID(session_id),
            request_fingerprint=_decision_fingerprint(
                normalize_fen(AFTER_E4_FEN), ["e2e4"]
            ),
            request_fen_hash=fen_hash(AFTER_E4_FEN),
            uci_history='["e2e4"]',
            ply_before=1,
            served_at=datetime.now(timezone.utc),
            response_payload=winner_payload,
            target_blunder_id=None,
            resulting_fen=None,
            reaches_drill_root=False,
        )
    )
    db_session.commit()

    from app.api.game import _replay_decision as original_replay

    calls = {"n": 0}

    def miss_once(db, session_id_arg, fingerprint):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_replay(db, session_id_arg, fingerprint)

    # A different move from the winner's, so serving the local computation would show.
    with (
        patch("app.api.game._replay_decision", side_effect=miss_once),
        patch(
            "app.opponent_move_controller.choose_move",
            return_value=_engine_move(uci="e7e5", san="e5"),
        ),
    ):
        response = _post(client, auth_headers, session_id, AFTER_E4_FEN, moves=["e2e4"])

    assert response.status_code == 200
    data = response.json()
    # The winner's payload, and therefore the winner's decision_id — the id a client
    # later confirms against always names a committed row.
    assert data["move"] == {"uci": "c7c5", "san": "c5"}
    assert data["decision_id"] == str(winner_id)
    assert len(_decisions(db_session, session_id)) == 1


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("INSERT", {}, Exception("boom")),
        # ValueError specifically: the engine branch maps it to 400 "Invalid input",
        # so recording INSIDE that try would answer a write failure with a move
        # already chosen and never recorded.
        ValueError("boom"),
    ],
    ids=["operational", "value"],
)
def test_engine_decision_write_failure_serves_no_move(
    client, auth_headers, create_game_session, db_session, error
):
    """Fail closed: an unrecorded served decision would bias p_reach upward."""
    session_id = create_game_session(user_id=123, player_color="white")

    with (
        patch("app.opponent_move_controller.choose_move", return_value=_engine_move()),
        patch("app.api.game._record_decision", side_effect=error),
    ):
        with pytest.raises(type(error)):
            _post(client, auth_headers, session_id, AFTER_E4_FEN, moves=["e2e4"])

    assert _decisions(db_session, session_id) == []


def test_ghost_decision_write_failure_does_not_fall_through_to_the_engine(
    client, auth_headers, create_game_session, db_session
):
    """A write failure must not be mistaken for "SAN parsing failed".

    The ghost branch's except clause catches ValueError to fall through to the engine.
    Recording inside it would turn a database error into a silent engine move that no
    decision records.
    """
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    _seed_ghost_target(db_session, user_id)

    with (
        patch("app.api.game._record_decision", side_effect=ValueError("boom")),
        patch(
            "app.opponent_move_controller.choose_move", return_value=_engine_move()
        ) as mock_choose,
    ):
        with pytest.raises(ValueError):
            _post(client, auth_headers, session_id, AFTER_E4_FEN, moves=["e2e4"])

    mock_choose.assert_not_called()
    assert _decisions(db_session, session_id) == []


# ---------------------------------------------------------------------------
# Schema parity
# ---------------------------------------------------------------------------


def test_served_at_ddl_backstop_is_the_statement_clock_on_both_dialects():
    """Never now(): Postgres defines it as TRANSACTION-start time, which for a request
    that waited on the drill row lock would predate the insert."""
    sqlite_ddl = str(
        CreateTable(OpponentDecision.__table__).compile(dialect=sqlite.dialect())
    )
    assert "CURRENT_TIMESTAMP" in sqlite_ddl

    postgres_ddl = str(
        CreateTable(OpponentDecision.__table__).compile(dialect=postgresql.dialect())
    )
    assert "statement_timestamp()" in postgres_ddl
    assert "now()" not in postgres_ddl


def test_migration_ddl_default_matches_the_model_construct():
    """The revision's frozen local copy must not drift from the model's construct."""
    revision_path = (
        Path(__file__).parent
        / "alembic"
        / "versions"
        / "20260726_01_create_opponent_decisions.py"
    )
    spec = importlib.util.spec_from_file_location(
        "revision_20260726_01", revision_path
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    from app.models import statement_timestamp as model_construct

    for dialect in (sqlite.dialect(), postgresql.dialect()):
        assert str(
            migration.statement_timestamp().compile(dialect=dialect)
        ) == str(model_construct().compile(dialect=dialect))
