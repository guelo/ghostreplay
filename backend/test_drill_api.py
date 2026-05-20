from __future__ import annotations

import uuid
from unittest.mock import patch

import chess

from app.models import GameSession, RatingHistory, SessionMove
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opening_roots import OpeningRoot, OpeningRoots

ROOT_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
E4_E5_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"


def _push_fen(board: chess.Board, uci: str) -> str:
    board.push(chess.Move.from_uci(uci))
    return " ".join(board.fen().split()[:4])


def _steering_graph() -> OpeningGraph:
    board = chess.Board()
    start = " ".join(board.fen().split()[:4])
    e4 = _push_fen(board, "e2e4")
    e5 = _push_fen(board, "e7e5")

    nodes = {
        start: OpeningGraphNode(start, "white"),
        e4: OpeningGraphNode(e4, "black"),
        e5: OpeningGraphNode(e5, "white"),
    }
    nodes[start].children["e2e4"] = e4
    nodes[e4].parents.add((start, "e2e4"))
    nodes[e4].children["e7e5"] = e5
    nodes[e5].parents.add((e4, "e7e5"))
    graph = OpeningGraph(nodes, start)
    graph.freeze()
    return graph


def _roots() -> OpeningRoots:
    root = OpeningRoot(
        opening_key=ROOT_FEN,
        opening_name="King's Pawn Game",
        opening_family="King's Pawn Game",
        eco="B00",
        depth=1,
        parent_keys=frozenset(),
        child_keys=frozenset(),
    )
    return OpeningRoots({ROOT_FEN: root}, {ROOT_FEN: frozenset([ROOT_FEN])})


def _roots_for(root_fen: str) -> OpeningRoots:
    root = OpeningRoot(
        opening_key=root_fen,
        opening_name="Test Root",
        opening_family="Test Root",
        eco="T00",
        depth=1,
        parent_keys=frozenset(),
        child_keys=frozenset(),
    )
    return OpeningRoots({root_fen: root}, {root_fen: frozenset([root_fen])})


def _start_drill(client, auth_headers, *, user_id: int = 123):
    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        return client.post(
            "/api/drills/start",
            json={
                "opening_key": ROOT_FEN,
                "player_color": "black",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(user_id=user_id),
        )


def test_start_drill_persists_contract(client, auth_headers, db_session):
    response = _start_drill(client, auth_headers)

    assert response.status_code == 201
    data = response.json()
    assert data["mode"] == "drill"
    assert data["drill_state"] == "active"
    assert data["opening_key"] == ROOT_FEN
    assert data["opening_name"] == "King's Pawn Game"
    assert data["player_color"] == "black"
    assert data["engine_elo"] == 1500
    assert data["strictness"] == "standard"
    assert data["is_rated"] is False
    assert data["rated_start_ply"] is None

    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(data["session_id"])).one()
    assert session.session_mode == "drill"
    assert session.drill_state == "active"
    assert session.drill_opening_key == ROOT_FEN
    assert session.drill_strictness == "standard"
    assert session.is_rated is False


def test_start_drill_unknown_opening_returns_404(client, auth_headers):
    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        response = client.post(
            "/api/drills/start",
            json={
                "opening_key": "missing",
                "player_color": "black",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(),
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown opening root"


def test_route_check_marks_root_reached_by_user_move(client, auth_headers, db_session):
    graph = _steering_graph()
    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(ROOT_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = client.post(
            "/api/drills/start",
            json={
                "opening_key": ROOT_FEN,
                "player_color": "white",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(),
        )
        assert start.status_code == 201
        session_id = start.json()["session_id"]
        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={
                "current_fen": ROOT_FEN,
                "previous_fen": START_FEN,
                "played_uci": "e2e4",
            },
            headers=auth_headers(),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "root_reached"
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.drill_state == "root_reached"


def test_route_check_fails_only_when_no_path_remains(client, auth_headers, db_session):
    graph = _steering_graph()
    d4_fen = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -"
    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(E4_E5_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = client.post(
            "/api/drills/start",
            json={
                "opening_key": E4_E5_FEN,
                "player_color": "white",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(),
        )
        assert start.status_code == 201
        session_id = start.json()["session_id"]
        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={
                "current_fen": d4_fen,
                "previous_fen": START_FEN,
                "played_uci": "d2d4",
            },
            headers=auth_headers(),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "failed"
    assert data["failure"]["correction_fen"] == START_FEN
    assert [suggestion["uci"] for suggestion in data["suggestions"]] == ["e2e4"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.drill_state == "failed"


def test_next_opponent_move_steers_active_drill_and_persists_root_reached(
    client,
    auth_headers,
    db_session,
):
    graph = _steering_graph()
    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(ROOT_FEN)),
        patch("app.api.game.get_opening_graph", return_value=graph),
    ):
        start = client.post(
            "/api/drills/start",
            json={
                "opening_key": ROOT_FEN,
                "player_color": "black",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(),
        )
        assert start.status_code == 201
        session_id = start.json()["session_id"]
        response = client.post(
            "/api/game/next-opponent-move",
            json={"session_id": session_id, "fen": START_FEN, "moves": []},
            headers=auth_headers(),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "ghost"
    assert data["decision_source"] == "ghost_path"
    assert data["move"] == {"uci": "e2e4", "san": "e4"}
    assert data["target_blunder_id"] is None
    assert data["drill_route"]["status"] == "root_reached"
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.drill_state == "root_reached"


def test_continue_drill_sets_boundary_and_resegments(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    session_uuid = uuid.UUID(session_id)
    session = db_session.query(GameSession).filter(GameSession.id == session_uuid).one()
    session.drill_state = "root_reached"
    db_session.commit()

    upload = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "fen-after-e4",
                },
                {
                    "move_number": 1,
                    "color": "black",
                    "move_san": "e5",
                    "fen_after": "fen-after-e5",
                },
            ]
        },
        headers=auth_headers(),
    )
    assert upload.status_code == 200
    assert {row.segment for row in db_session.query(SessionMove).filter(SessionMove.session_id == session_uuid)} == {"drill"}

    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        response = client.post(
            f"/api/drills/{session_id}/continue",
            json={"current_ply": 1},
            headers=auth_headers(),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["drill_state"] == "converted"
    assert data["is_rated"] is True
    assert data["rated_start_ply"] == 1
    assert data["normal_started_at"] is not None
    assert data["converted_at"] is not None

    rows = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session_uuid)
        .order_by(SessionMove.move_number, SessionMove.color)
        .all()
    )
    assert [(row.color, row.segment) for row in rows] == [("black", "normal"), ("white", "drill")]

    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        repeated = client.post(
            f"/api/drills/{session_id}/continue",
            json={"current_ply": 1},
            headers=auth_headers(),
        )
        conflict = client.post(
            f"/api/drills/{session_id}/continue",
            json={"current_ply": 2},
            headers=auth_headers(),
        )
    assert repeated.status_code == 200
    assert conflict.status_code == 409


def test_continue_drill_requires_root_reached(client, auth_headers):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]

    active = client.post(
        f"/api/drills/{session_id}/continue",
        json={"current_ply": 0},
        headers=auth_headers(),
    )

    failed = client.post(
        f"/api/drills/{session_id}/fail",
        headers=auth_headers(),
    )
    assert failed.status_code == 200
    failed_continue = client.post(
        f"/api/drills/{session_id}/continue",
        json={"current_ply": 0},
        headers=auth_headers(),
    )

    assert active.status_code == 400
    assert active.json()["detail"] == "Drill root must be reached before continuing"
    assert failed_continue.status_code == 400
    assert failed_continue.json()["detail"] == "Drill root must be reached before continuing"


def test_unconverted_drill_rejects_game_end_and_stays_unrated(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]

    response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "checkmate_win",
            "pgn": "1. e4 e5",
            "is_rated": True,
        },
        headers=auth_headers(),
    )

    assert response.status_code == 400
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.status == "active"
    assert session.is_rated is False
    assert db_session.query(RatingHistory).filter(RatingHistory.game_session_id == session.id).count() == 0


def test_converted_drill_ignores_request_is_rated_false(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.drill_state = "root_reached"
    db_session.commit()

    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        converted = client.post(
            f"/api/drills/{session_id}/continue",
            json={"current_ply": 0},
            headers=auth_headers(),
        )
    assert converted.status_code == 200

    ended = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "checkmate_win",
            "pgn": "1. e4 e5",
            "is_rated": False,
        },
        headers=auth_headers(),
    )

    assert ended.status_code == 200
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.is_rated is True
    assert db_session.query(RatingHistory).filter(RatingHistory.game_session_id == session.id).count() == 1


def test_converted_drill_abandon_preserves_rated_session_without_rating_history(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.drill_state = "root_reached"
    db_session.commit()

    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        converted = client.post(
            f"/api/drills/{session_id}/continue",
            json={"current_ply": 0},
            headers=auth_headers(),
        )
    assert converted.status_code == 200

    ended = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "abandon",
            "pgn": "1. e4 e5",
            "is_rated": False,
        },
        headers=auth_headers(),
    )

    assert ended.status_code == 200
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.is_rated is True
    assert session.result == "abandon"
    assert db_session.query(RatingHistory).filter(RatingHistory.game_session_id == session.id).count() == 0


def test_abandoned_drill_hidden_from_history_stats_and_analysis(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        abandoned = client.post(f"/api/drills/{session_id}/abandon", headers=auth_headers())
    assert abandoned.status_code == 200

    assert client.get("/api/history", headers=auth_headers()).json()["games"] == []
    assert client.get("/api/stats/summary", headers=auth_headers()).json()["games"]["played"] == 0
    assert client.get(f"/api/session/{session_id}/analysis", headers=auth_headers()).status_code == 404

    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.result == "drill_abandon"
    assert session.is_rated is False
