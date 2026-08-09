"""Decision-backed drill root confirmation (g-root-confirm-api).

The drill's evidence boundary — ``game_sessions.drill_root_reached_ply`` — is written
only by ``/route-check``, and only for an arrival it can PROVE. Which proof is owed is
derived from the route target, never from what the client chose to send: a FEN's active
colour fixes who moved into it, so the target alone decides whether the root is reached
by the player or by the opponent.

* OPPONENT arrival — the server served that move, so ``ply_before`` and ``resulting_fen``
  come off the recorded decision and nothing is client-asserted.
* PLAYER arrival — no record of a player move exists at confirmation time
  (``session_moves`` uploads are asynchronous), so the ply is anchored to the decision
  served two plies earlier, or is the fully-proven ply-1 opening move.

A claim that CONTRADICTS that evidence is a 422. A claim that is merely UNPROVABLE
transitions drill state without stamping: a NULL boundary already means "contributes no
reach evidence", so refusing would strand a live drill over a claim we cannot check.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import chess
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from app.drill_steering import (
    _reset_drill_route_cache_for_testing,
    replay_history_fen,
)
from app.fen import fen_hash
from app.models import GameSession, OpponentDecision, User
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opening_roots import OpeningRoot, OpeningRoots
from conftest import engine, pg_required


def _push_fen(board: chess.Board, uci: str) -> str:
    board.push(chess.Move.from_uci(uci))
    return " ".join(board.fen().split()[:4])


_board = chess.Board()
START_FEN = " ".join(_board.fen().split()[:4])
E4_FEN = _push_fen(_board, "e2e4")  # after 1.e4 — BLACK to move
E4_E5_FEN = _push_fen(_board, "e7e5")  # after 1...e5 — WHITE to move
NF3_FEN = _push_fen(_board, "g1f3")  # after 2.Nf3 — BLACK to move
D4_FEN = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -"  # off every route
ROUTE_LINE = ["e2e4", "e7e5", "g1f3"]  # the module's route, start -> NF3_FEN
# Knight shuffle first, then the same route: a LONGER legal history reaching the very
# same positions. Two decisions built from these lines therefore carry different,
# independently provable plies for one target — what a real convergence race needs.
SHUFFLED_ROUTE_LINE = ["g1f3", "g8f6", "f3g1", "f6g8", "e2e4", "e7e5", "g1f3"]

_HOLD = 0.5  # seconds a leader holds the row lock so the follower demonstrably waits


@pytest.fixture(autouse=True)
def _clean_route_cache():
    """The route map is cached by (graph fingerprint, target); reset so this module's
    graph can never be answered from another module's map for the same target."""
    _reset_drill_route_cache_for_testing()
    yield
    _reset_drill_route_cache_for_testing()


def _graph() -> OpeningGraph:
    """start -> 1.e4 -> 1...e5 -> 2.Nf3. Long enough that a root can be reached by
    either side and that a mid-route player arrival has a decision to anchor to."""
    nodes = {
        START_FEN: OpeningGraphNode(START_FEN, "white"),
        E4_FEN: OpeningGraphNode(E4_FEN, "black"),
        E4_E5_FEN: OpeningGraphNode(E4_E5_FEN, "white"),
        NF3_FEN: OpeningGraphNode(NF3_FEN, "black"),
    }
    for parent, uci, child in (
        (START_FEN, "e2e4", E4_FEN),
        (E4_FEN, "e7e5", E4_E5_FEN),
        (E4_E5_FEN, "g1f3", NF3_FEN),
    ):
        nodes[parent].children[uci] = child
        nodes[child].parents.add((parent, uci))
    graph = OpeningGraph(nodes, START_FEN)
    graph.freeze()
    return graph


def _roots_for(target_fen: str) -> OpeningRoots:
    root = OpeningRoot(
        opening_key=target_fen,
        opening_name="Test Root",
        opening_family="Test Root",
        eco="T00",
        depth=1,
        parent_keys=frozenset(),
        child_keys=frozenset(),
    )
    return OpeningRoots({target_fen: root}, {target_fen: frozenset([target_fen])})


def _start_drill(client, auth_headers, *, target: str, player_color: str, user_id: int = 123):
    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(target)),
        patch("app.api.drills.get_opening_graph", return_value=_graph()),
    ):
        response = client.post(
            "/api/drills/start",
            json={
                "opening_key": target,
                "player_color": player_color,
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(user_id=user_id),
        )
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def _route_check(client, auth_headers, session_id, *, user_id: int = 123, **body):
    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(_TARGET[session_id])),
        patch("app.api.drills.get_opening_graph", return_value=_graph()),
    ):
        return client.post(
            f"/api/drills/{session_id}/route-check",
            json=body,
            headers=auth_headers(user_id=user_id),
        )


def _controller_move(uci: str = "e7e5", san: str = "e5"):
    from app.opponent_move_controller import ControllerMove

    return ControllerMove(uci=uci, san=san, method="test")


def _opponent_move(client, auth_headers, session_id, fen, moves, *, user_id: int = 123):
    with patch("app.api.game.get_opening_graph", return_value=_graph()):
        return client.post(
            "/api/game/next-opponent-move",
            json={"session_id": session_id, "fen": fen, "moves": moves},
            headers=auth_headers(user_id=user_id),
        )


# Route-check re-derives the route map per request, so each helper call needs the same
# target the drill was started with.
_TARGET: dict[str, str] = {}


def _drill(client, auth_headers, *, target: str, player_color: str, user_id: int = 123) -> str:
    session_id = _start_drill(
        client, auth_headers, target=target, player_color=player_color, user_id=user_id
    )
    _TARGET[session_id] = target
    return session_id


def _session(db_session, session_id) -> GameSession:
    db_session.expire_all()
    return (
        db_session.query(GameSession)
        .filter(GameSession.id == uuid.UUID(str(session_id)))
        .one()
    )


@contextlib.contextmanager
def _capture(target_engine):
    stmts: list[str] = []

    def _on(conn, cursor, statement, parameters, context, executemany) -> None:
        stmts.append(statement.lower())

    event.listen(target_engine, "before_cursor_execute", _on)
    try:
        yield stmts
    finally:
        event.remove(target_engine, "before_cursor_execute", _on)


def _gs_updates(stmts: list[str]) -> list[str]:
    return [s for s in stmts if s.lstrip().startswith("update game_sessions")]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_boundary_rejects_a_negative_ply(db_session):
    session = GameSession(
        id=uuid.uuid4(),
        user_id=501,
        started_at=datetime.now(timezone.utc),
        status="active",
        engine_elo=1500,
        player_color="white",
        session_mode="drill",
        drill_state="active",
        drill_opening_key=E4_FEN,
        is_rated=False,
        drill_root_reached_ply=-1,
    )
    db_session.add(session)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_boundary_rejects_a_root_ply_on_a_normal_session(db_session):
    """A normal session has no route and no root, so a ply there can only be a bug."""
    session = GameSession(
        id=uuid.uuid4(),
        user_id=502,
        started_at=datetime.now(timezone.utc),
        status="active",
        engine_elo=1500,
        player_color="white",
        session_mode="normal",
        drill_state=None,
        is_rated=True,
        drill_root_reached_ply=4,
    )
    db_session.add(session)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# Player-reached root
# ---------------------------------------------------------------------------


def test_player_root_at_ply_one_stamps_state_and_boundary_together(
    client, auth_headers, db_session
):
    """1.e4 IS the root: the player moved first, so nothing precedes the arrival and
    the start position is the anchor. State and boundary land in one UPDATE."""
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="white")

    with _capture(engine) as stmts:
        response = _route_check(
            client,
            auth_headers,
            session_id,
            current_fen=E4_FEN,
            previous_fen=START_FEN,
            played_uci="e2e4",
            current_ply=1,
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "root_reached"
    assert body["drill_root_reached_ply"] == 1

    session = _session(db_session, session_id)
    assert session.drill_state == "root_reached"
    assert session.drill_root_reached_ply == 1

    updates = _gs_updates(stmts)
    assert len(updates) == 1
    # When both are written they are written together: one statement, both columns.
    # The converse does not hold: legacy rows and soft-declined proofs can leave a
    # permanent root_reached-without-boundary shape.
    assert "drill_state" in updates[0]
    assert "drill_root_reached_ply" in updates[0]


def test_player_root_mid_route_is_anchored_to_the_served_decision(
    client, auth_headers, db_session
):
    """The position the player moved FROM is the resulting position of the decision
    this server served two plies earlier — that is what proves ply 3."""
    session_id = _drill(client, auth_headers, target=NF3_FEN, player_color="white")

    # The opponent's 1...e5 is served and recorded at ply_before=1.
    served = _opponent_move(client, auth_headers, session_id, E4_FEN, ["e2e4"])
    assert served.status_code == 200, served.text
    assert served.json()["move"]["uci"] == "e7e5"
    assert _session(db_session, session_id).drill_state == "active"

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=NF3_FEN,
        previous_fen=E4_E5_FEN,
        played_uci="g1f3",
        current_ply=3,
    )

    assert response.status_code == 200, response.text
    assert response.json()["drill_root_reached_ply"] == 3
    session = _session(db_session, session_id)
    assert session.drill_state == "root_reached"
    assert session.drill_root_reached_ply == 3


def test_unanchored_player_root_transitions_without_stamping(
    client, auth_headers, db_session
):
    """No decision anchors ply 3 (a drill in flight across the deploy). The claim is
    well formed but unprovable: the drill still reaches root, the boundary stays NULL."""
    session_id = _drill(client, auth_headers, target=NF3_FEN, player_color="white")

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=NF3_FEN,
        previous_fen=E4_E5_FEN,
        played_uci="g1f3",
        current_ply=3,
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "root_reached"
    assert response.json()["drill_root_reached_ply"] is None
    session = _session(db_session, session_id)
    assert session.drill_state == "root_reached"
    assert session.drill_root_reached_ply is None


def test_understated_ply_is_rejected(client, auth_headers, db_session):
    """The attack the anchor exists to stop: an arrival at ply 3 claimed as ply 1 would
    lower the boundary and readmit the scripted pre-root plies as evidence."""
    session_id = _drill(client, auth_headers, target=NF3_FEN, player_color="white")

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=NF3_FEN,
        previous_fen=E4_E5_FEN,
        played_uci="g1f3",
        current_ply=1,
    )

    assert response.status_code == 422, response.text
    session = _session(db_session, session_id)
    assert session.drill_state == "active"
    assert session.drill_root_reached_ply is None


def test_wrong_parity_is_rejected(client, auth_headers, db_session):
    """White's moves land on odd plies. An even claim contradicts the move order."""
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="white")

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=E4_FEN,
        previous_fen=START_FEN,
        played_uci="e2e4",
        current_ply=2,
    )

    assert response.status_code == 422, response.text
    assert _session(db_session, session_id).drill_root_reached_ply is None


def test_played_move_that_does_not_produce_the_position_is_rejected(
    client, auth_headers, db_session
):
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="white")

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=E4_FEN,
        previous_fen=START_FEN,
        played_uci="d2d4",  # legal, but reaches D4_FEN — not the claimed position
        current_ply=1,
    )

    assert response.status_code == 422, response.text
    session = _session(db_session, session_id)
    assert session.drill_state == "active"
    assert session.drill_root_reached_ply is None


def test_decision_id_is_rejected_on_a_player_reached_root(
    client, auth_headers, db_session
):
    """1.e4 is the player's move, so a decision cannot have produced it. Supplying one
    is the client trying to pick its own proof."""
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="white")
    decision_id = _insert_decision(
        db_session, session_id, ply_before=0, resulting_fen=E4_FEN
    )

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=E4_FEN,
        previous_fen=START_FEN,
        played_uci="e2e4",
        current_ply=1,
        decision_id=str(decision_id),
    )

    assert response.status_code == 422, response.text
    assert _session(db_session, session_id).drill_root_reached_ply is None


# ---------------------------------------------------------------------------
# Opponent-reached root
# ---------------------------------------------------------------------------


def _decision_row(
    session_id,
    *,
    ply_before: int,
    resulting_fen: str,
    history: list[str] | None = None,
    request_fen: str | None = None,
) -> OpponentDecision:
    """Build a decision row.

    ``history`` defaults to a PROVABLE one — the first ``ply_before`` plies of the
    module's route — so a test that is not about provenance gets a row whose recorded ply
    its own history backs. ``request_fen`` defaults to whatever that history replays to;
    overriding it is how a test builds the row a truncated-history serve used to write.
    """
    if history is None:
        history = ROUTE_LINE[:ply_before]
    replayed = request_fen if request_fen is not None else replay_history_fen(history)
    decision_id = uuid.uuid4()
    return OpponentDecision(
        decision_id=decision_id,
        session_id=uuid.UUID(str(session_id)),
        request_fingerprint=f"fp-{decision_id}",
        request_fen_hash=fen_hash(replayed) if replayed else "unreplayable",
        uci_history=json.dumps(history, separators=(",", ":")),
        ply_before=ply_before,
        served_at=datetime.now(timezone.utc),
        response_payload="{}",
        resulting_fen=resulting_fen,
        reaches_drill_root=True,
    )


def _insert_decision(db_session, session_id, **kwargs) -> uuid.UUID:
    row = _decision_row(session_id, **kwargs)
    db_session.add(row)
    db_session.commit()
    return row.decision_id


def test_opponent_root_confirmation_stamps_the_boundary(
    client, auth_headers, db_session
):
    """The two-phase transition, end to end.

    The served route move reaches the root but transitions nothing. Confirmation
    validates the applied position against the RECORDED decision, and state and
    boundary land together.
    """
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="black")

    served = _opponent_move(client, auth_headers, session_id, START_FEN, [])
    assert served.status_code == 200, served.text
    assert served.json()["drill_route"]["status"] == "root_pending"
    decision_id = served.json()["decision_id"]
    assert decision_id is not None
    session = _session(db_session, session_id)
    # Phase A: nothing written but the decision row.
    assert session.drill_state == "active"
    assert session.drill_root_reached_ply is None

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=E4_FEN,
        current_ply=1,
        decision_id=decision_id,
    )

    # Phase B: state and boundary in one transaction.
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "root_reached"
    assert response.json()["drill_root_reached_ply"] == 1
    session = _session(db_session, session_id)
    assert session.drill_state == "root_reached"
    assert session.drill_root_reached_ply == 1


def test_opponent_root_requires_a_decision_id(client, auth_headers, db_session):
    """1.e4 is the OPPONENT's move here, so the player-arrival proof is not on offer."""
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="black")
    _opponent_move(client, auth_headers, session_id, START_FEN, [])

    response = _route_check(
        client, auth_headers, session_id, current_fen=E4_FEN, current_ply=1
    )

    assert response.status_code == 422, response.text
    assert _session(db_session, session_id).drill_root_reached_ply is None


def test_unknown_decision_id_is_rejected(client, auth_headers, db_session):
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="black")
    _opponent_move(client, auth_headers, session_id, START_FEN, [])

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=E4_FEN,
        current_ply=1,
        decision_id=str(uuid.uuid4()),
    )

    assert response.status_code == 422, response.text
    assert _session(db_session, session_id).drill_root_reached_ply is None


def test_decision_from_another_session_is_rejected(client, auth_headers, db_session):
    """decision_id is opaque, but it is still scoped: a valid id from a DIFFERENT
    session cannot confirm this one's root."""
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="black")
    _opponent_move(client, auth_headers, session_id, START_FEN, [])
    other_id = _drill(client, auth_headers, target=E4_FEN, player_color="black")
    other_decision = _insert_decision(
        db_session, other_id, ply_before=0, resulting_fen=E4_FEN
    )

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=E4_FEN,
        current_ply=1,
        decision_id=str(other_decision),
    )

    assert response.status_code == 422, response.text
    assert _session(db_session, session_id).drill_root_reached_ply is None


def test_ply_inconsistent_with_the_decision_is_rejected(
    client, auth_headers, db_session
):
    """current_ply is checked against the recorded ply_before + 1, so the client's
    number is never the one written."""
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="black")
    served = _opponent_move(client, auth_headers, session_id, START_FEN, [])

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=E4_FEN,
        current_ply=5,
        decision_id=served.json()["decision_id"],
    )

    assert response.status_code == 422, response.text
    assert _session(db_session, session_id).drill_root_reached_ply is None


def test_stale_decision_that_does_not_reach_the_root_is_rejected(
    client, auth_headers, db_session
):
    """A decision from an earlier ply (or a reverted branch) resolves to a different
    resulting position, so it cannot confirm this root."""
    session_id = _drill(client, auth_headers, target=NF3_FEN, player_color="black")
    stale = _insert_decision(db_session, session_id, ply_before=1, resulting_fen=E4_E5_FEN)

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=NF3_FEN,
        current_ply=2,
        decision_id=str(stale),
    )

    assert response.status_code == 422, response.text
    assert _session(db_session, session_id).drill_root_reached_ply is None


def test_confirmation_off_route_fails_the_confirmation_not_the_drill(
    client, auth_headers, db_session
):
    """The server SERVED the move being confirmed, so a position that is not the root
    must not be judged off-route: the confirmation fails, the drill stays alive."""
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="black")
    decision_id = _insert_decision(
        db_session, session_id, ply_before=0, resulting_fen=D4_FEN
    )

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=D4_FEN,
        previous_fen=START_FEN,
        played_uci="d2d4",
        current_ply=1,
        decision_id=str(decision_id),
    )

    assert response.status_code == 422, response.text
    session = _session(db_session, session_id)
    assert session.drill_state == "active"  # NOT failed
    assert session.drill_terminal_reason is None


# ---------------------------------------------------------------------------
# ply_before provenance: the recorded ply is only evidence once its history replays
# ---------------------------------------------------------------------------


def test_serving_a_route_move_rejects_a_history_that_does_not_reach_the_fen(
    client, auth_headers, db_session
):
    """The leak this closes at the source. ply_before is len(request.moves), so a
    legitimate on-route FEN paired with a TRUNCATED history would be served the real
    route move and record a ply several plies too low — which confirmation would then
    treat as the evidence boundary, readmitting the scripted prefix it exists to
    exclude. Nothing is served and nothing is recorded."""
    session_id = _drill(client, auth_headers, target=NF3_FEN, player_color="black")

    served = _opponent_move(client, auth_headers, session_id, E4_E5_FEN, [])

    assert served.status_code == 400, served.text
    assert "history" in served.json()["detail"].lower()
    assert (
        db_session.query(OpponentDecision)
        .filter(OpponentDecision.session_id == uuid.UUID(session_id))
        .count()
        == 0
    )
    assert _session(db_session, session_id).drill_state == "active"


def test_serving_a_route_move_accepts_a_transposed_history_that_does_reach_the_fen(
    client, auth_headers, db_session
):
    """The check is a replay, not a route comparison: a longer legal history reaching
    the same position is honest history and is served normally, with its own true ply."""
    session_id = _drill(client, auth_headers, target=NF3_FEN, player_color="black")
    shuffled = SHUFFLED_ROUTE_LINE[:6]  # knight shuffle, then 1.e4 e5 — 6 plies

    served = _opponent_move(client, auth_headers, session_id, E4_E5_FEN, shuffled)

    assert served.status_code == 200, served.text
    assert served.json()["move"]["uci"] == "g1f3"
    row = (
        db_session.query(OpponentDecision)
        .filter(OpponentDecision.session_id == uuid.UUID(session_id))
        .one()
    )
    assert row.ply_before == 6  # the TRUE ply, not the route's shortest distance


def test_normal_sessions_still_forward_an_unreplayable_history(
    client, auth_headers, create_game_session
):
    """The replay is scoped to the pre-root drill branch. The ghost/engine path forwards
    `moves` to Maia verbatim with no per-element validation, and narrowing that contract
    is not this feature's business — the fingerprint tests depend on it."""
    session_id = create_game_session(user_id=123, player_color="white")

    with patch(
        "app.opponent_move_controller.choose_move",
        return_value=_controller_move(),
    ):
        served = _opponent_move(
            client, auth_headers, session_id, E4_FEN, ["e2e4 e7e5"]
        )

    assert served.status_code == 200, served.text


def test_opponent_confirmation_declines_a_decision_with_an_unproven_ply(
    client, auth_headers, db_session
):
    """A row of exactly the shape a truncated-history serve used to write: it claims
    ply_before=0 while the position it was served FROM is four plies in. Its ply is not
    evidence, so the drill reaches root with no boundary rather than a false one."""
    session_id = _drill(client, auth_headers, target=NF3_FEN, player_color="black")
    decision_id = _insert_decision(
        db_session,
        session_id,
        ply_before=0,
        resulting_fen=NF3_FEN,
        history=[],
        request_fen=E4_E5_FEN,  # served from ply 2, recorded as ply 0
    )

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=NF3_FEN,
        current_ply=1,
        decision_id=str(decision_id),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "root_reached"
    assert response.json()["drill_root_reached_ply"] is None
    session = _session(db_session, session_id)
    assert session.drill_state == "root_reached"
    assert session.drill_root_reached_ply is None


def test_post_root_ghost_decision_cannot_stamp_a_low_boundary(
    client, auth_headers, db_session
):
    """The gap the serve-time replay alone does NOT close.

    Once a drill is root-reached the endpoint serves from the ghost/engine path, which
    validates no history, and the ghost can steer back through the route target by
    repetition. Such a decision has resulting_fen == the target, so it reaches the
    confirmation validator carrying a ply nothing checked — here claiming ply_before=0
    while it was served from a position four plies in. Re-proving the row is what stops
    it from stamping a boundary far below the real root."""
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="black")
    served = _opponent_move(client, auth_headers, session_id, START_FEN, [])
    assert served.status_code == 200, served.text  # serve-time transition, no boundary
    assert _session(db_session, session_id).drill_root_reached_ply is None

    ghost_like = _insert_decision(
        db_session,
        session_id,
        ply_before=0,
        resulting_fen=E4_FEN,
        history=[],
        request_fen=E4_E5_FEN,
    )

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=E4_FEN,
        current_ply=1,
        decision_id=str(ghost_like),
    )

    assert response.status_code == 200, response.text
    assert response.json()["drill_root_reached_ply"] is None
    assert _session(db_session, session_id).drill_root_reached_ply is None


def test_player_anchor_declines_a_decision_with_an_unproven_ply(
    client, auth_headers, db_session
):
    """The anchor inherits the same requirement: a candidate whose own ply is unproven
    anchors nothing, so the player arrival declines instead of stamping ply 3."""
    session_id = _drill(client, auth_headers, target=NF3_FEN, player_color="white")
    _insert_decision(
        db_session,
        session_id,
        ply_before=1,
        resulting_fen=E4_E5_FEN,
        history=[],  # length 0 cannot be a ply-1 history
    )

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=NF3_FEN,
        previous_fen=E4_E5_FEN,
        played_uci="g1f3",
        current_ply=3,
    )

    assert response.status_code == 200, response.text
    assert response.json()["drill_root_reached_ply"] is None
    assert _session(db_session, session_id).drill_root_reached_ply is None


def test_reaches_drill_root_agrees_with_geometry(client, auth_headers, db_session):
    """Guards the trap this column is built on.

    Confirmation deliberately validates against ``resulting_fen`` rather than this flag.
    The flag used to be extracted from the response's STATUS string, and the served
    status is now ``root_pending`` — so a status-based extraction would silently write
    FALSE for exactly the decisions confirmation is about, and NO confirmation test
    would fail. This asserts the flag against geometry in both directions, so any future
    re-coupling to a status string has to break here.
    """
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="black")

    served = _opponent_move(client, auth_headers, session_id, START_FEN, [])
    assert served.status_code == 200, served.text
    assert served.json()["drill_route"]["status"] == "root_pending"

    row = (
        db_session.query(OpponentDecision)
        .filter(OpponentDecision.session_id == uuid.UUID(session_id))
        .one()
    )
    assert row.resulting_fen == E4_FEN  # this decision DOES reach the drill root...
    assert row.reaches_drill_root is True  # ...so the recorded flag must say so


def test_reaches_drill_root_is_false_for_an_on_route_serve(
    client, auth_headers, db_session
):
    """The other direction: an on-route move short of the root records FALSE."""
    session_id = _drill(client, auth_headers, target=NF3_FEN, player_color="black")

    served = _opponent_move(client, auth_headers, session_id, START_FEN, [])
    assert served.status_code == 200, served.text
    assert served.json()["drill_route"]["status"] == "on_route"
    assert served.json()["drill_route"]["reaches_root"] is False

    row = (
        db_session.query(OpponentDecision)
        .filter(OpponentDecision.session_id == uuid.UUID(session_id))
        .one()
    )
    assert row.resulting_fen == E4_FEN  # 1.e4 is two plies short of 2.Nf3
    assert row.reaches_drill_root is False


# ---------------------------------------------------------------------------
# Idempotency and request contract
# ---------------------------------------------------------------------------


def test_duplicate_confirmation_returns_the_boundary_without_restamping(
    client, auth_headers, db_session
):
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="white")
    body = dict(
        current_fen=E4_FEN, previous_fen=START_FEN, played_uci="e2e4", current_ply=1
    )
    first = _route_check(client, auth_headers, session_id, **body)
    assert first.status_code == 200, first.text

    with _capture(engine) as stmts:
        second = _route_check(client, auth_headers, session_id, **body)

    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert _gs_updates(stmts) == []  # no second stamp
    assert _session(db_session, session_id).drill_root_reached_ply == 1


def test_route_check_requires_current_ply(
    client, auth_headers, db_session
):
    """The compatibility window is closed: omitted ply fails before any transition."""
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="white")

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=E4_FEN,
        previous_fen=START_FEN,
        played_uci="e2e4",
    )

    assert response.status_code == 422, response.text
    assert ["body", "current_ply"] in [
        error["loc"] for error in response.json()["error"]["details"]
    ]
    session = _session(db_session, session_id)
    assert session.drill_state == "active"
    assert session.drill_root_reached_ply is None


def test_current_ply_on_an_on_route_check_is_not_a_boundary_claim(
    client, auth_headers, db_session
):
    """Under the cutover the per-move call site sends current_ply on EVERY check, so
    away from the root it is ordinary metadata — not a claim, and never a rejection."""
    session_id = _drill(client, auth_headers, target=NF3_FEN, player_color="white")

    response = _route_check(
        client,
        auth_headers,
        session_id,
        current_fen=E4_FEN,
        previous_fen=START_FEN,
        played_uci="e2e4",
        current_ply=1,
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "on_route"
    assert response.json()["drill_root_reached_ply"] is None
    session = _session(db_session, session_id)
    assert session.drill_state == "active"
    assert session.drill_root_reached_ply is None


def test_duplicate_opponent_confirmation_does_not_restamp(
    client, auth_headers, db_session
):
    """The client retries confirmations, so the same body can arrive twice. The second
    must be a pure read: same answer, and no second write."""
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="black")
    served = _opponent_move(client, auth_headers, session_id, START_FEN, [])
    assert served.status_code == 200, served.text
    body = dict(
        current_fen=E4_FEN, current_ply=1, decision_id=served.json()["decision_id"]
    )

    first = _route_check(client, auth_headers, session_id, **body)
    assert first.status_code == 200, first.text
    assert first.json()["drill_root_reached_ply"] == 1

    with _capture(engine) as stmts:
        second = _route_check(client, auth_headers, session_id, **body)

    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert _gs_updates(stmts) == []
    assert _session(db_session, session_id).drill_root_reached_ply == 1


def test_observed_root_fallback_stamps_the_boundary_write_once(
    client, auth_headers, db_session
):
    """The request FEN already IS the root.

    This position is client-OBSERVED, not merely served, so unlike the route branch it
    does transition — and stamps the boundary, which len(moves) proves because the
    history was already required to reproduce this very FEN. Under the client-side
    barrier current clients never take this path; it is the fallback for legacy tabs
    and arrivals whose confirmation was lost.
    """
    session_id = _drill(client, auth_headers, target=E4_FEN, player_color="white")

    first = _opponent_move(client, auth_headers, session_id, E4_FEN, ["e2e4"])

    assert first.status_code == 400, first.text
    assert "already reached" in first.json()["detail"].lower()
    session = _session(db_session, session_id)
    assert session.drill_state == "root_reached"
    assert session.drill_root_reached_ply == 1

    # Write-once. The fallback stamped both fields together, so a confirmation
    # arriving afterwards — here claiming a DIFFERENT ply — must read the committed
    # boundary back rather than move it, and must write nothing at all.
    with _capture(engine) as stmts:
        late = _route_check(
            client,
            auth_headers,
            session_id,
            current_fen=E4_FEN,
            previous_fen=START_FEN,
            played_uci="e2e4",
            current_ply=3,
        )

    assert late.status_code == 200, late.text
    assert late.json()["drill_root_reached_ply"] == 1
    assert _gs_updates(stmts) == []
    assert _session(db_session, session_id).drill_root_reached_ply == 1


# ---------------------------------------------------------------------------
# Postgres: write-once under real row-lock contention
# ---------------------------------------------------------------------------


@pg_required
def test_concurrent_confirmations_converge_on_one_ply(
    pg_client, pg_engine, pg_session_factory, auth_headers
):
    """Two REAL confirmations race for one drill, each carrying its own independently
    valid ply.

    Both are honest: the same target position is genuinely reachable at ply 3 (1.e4 e5
    2.Nf3) and at ply 7 (knight shuffle first), and each claim is backed by its own
    recorded decision whose history replays. So the winner is decided by the row lock and
    nothing else — which is what makes this a write-once proof. Both callers must come
    back with the SAME committed ply, the row must hold it, and exactly one UPDATE may
    apply a boundary: an implementation that restamped would leave the loser's ply in the
    row while the winner's response claimed the other.
    """
    user_id = 7401
    db = pg_session_factory()
    try:
        if db.query(User).filter(User.id == user_id).first() is None:
            db.add(User(id=user_id, username=None, is_anonymous=True))
            db.commit()
        session = GameSession(
            id=uuid.uuid4(),
            user_id=user_id,
            started_at=datetime.now(timezone.utc),
            status="active",
            engine_elo=1500,
            # Black plays the drill, so WHITE moves into the black-to-move target: an
            # opponent arrival, which is the kind a decision_id confirms.
            player_color="black",
            session_mode="drill",
            drill_state="active",
            drill_opening_key=NF3_FEN,
            drill_strictness="standard",
            is_rated=False,
        )
        db.add(session)
        db.commit()
        session_id = session.id
        # Two served decisions reaching the same root from the same position, at
        # different true plies. ROUTE_LINE[:2] and SHUFFLED_ROUTE_LINE[:6] both replay
        # to E4_E5_FEN, so both rows are provable and neither claim is the "wrong" one.
        short = _decision_row(session_id, ply_before=2, resulting_fen=NF3_FEN)
        long = _decision_row(
            session_id,
            ply_before=6,
            resulting_fen=NF3_FEN,
            history=SHUFFLED_ROUTE_LINE[:6],
        )
        db.add_all([short, long])
        db.commit()
        claims = [(str(short.decision_id), 3), (str(long.decision_id), 7)]
    finally:
        db.close()
    _TARGET[str(session_id)] = NF3_FEN

    gate_held = threading.Event()
    both_waiting = threading.Event()
    results: dict[int, object] = {}
    elapsed: dict[int, float] = {}

    def _gate() -> None:
        """Hold the session row so both confirmations pile up on the lock, then let go —
        the overlap is what makes this a race rather than two sequential calls."""
        gate_db = pg_session_factory()
        try:
            gate_db.execute(text("SET LOCAL lock_timeout = '5s'"))
            gate_db.execute(
                text("SELECT id FROM game_sessions WHERE id = :id FOR NO KEY UPDATE"),
                {"id": session_id},
            )
            gate_held.set()
            both_waiting.wait(timeout=5)
            time.sleep(_HOLD)  # both are blocked on the lock by now
            gate_db.rollback()
        finally:
            gate_db.close()

    def _confirm(index: int) -> None:
        decision_id, ply = claims[index]
        started = time.perf_counter()
        results[index] = _route_check(
            pg_client,
            auth_headers,
            str(session_id),
            user_id=user_id,
            current_fen=NF3_FEN,
            current_ply=ply,
            decision_id=decision_id,
        )
        elapsed[index] = time.perf_counter() - started

    with _capture(pg_engine) as stmts:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            gate_future = pool.submit(_gate)
            assert gate_held.wait(timeout=5)
            confirmations = [pool.submit(_confirm, i) for i in range(2)]
            time.sleep(_HOLD)  # let both reach the lock
            both_waiting.set()
            for future in confirmations:
                future.result(timeout=20)
            gate_future.result(timeout=20)

    first, second = results[0], results[1]
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    committed = first.json()["drill_root_reached_ply"]
    assert committed in (3, 7)  # whichever won the lock
    assert second.json()["drill_root_reached_ply"] == committed  # both converge

    db = pg_session_factory()
    try:
        stored = db.execute(
            text("SELECT drill_root_reached_ply FROM game_sessions WHERE id = :id"),
            {"id": session_id},
        ).scalar()
    finally:
        db.close()
    assert stored == committed  # the loser did not overwrite the winner

    boundary_updates = [s for s in _gs_updates(stmts) if "drill_root_reached_ply" in s]
    assert len(boundary_updates) == 1  # write-once, under contention
    # BOTH queued behind the gate — if either had run to completion before the other
    # started, this would be two sequential calls and would prove nothing about a race.
    assert min(elapsed.values()) >= _HOLD
