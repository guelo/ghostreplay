from __future__ import annotations

import uuid
from unittest.mock import patch

from app.models import GameSession, RatingHistory, SessionMove
from app.opening_roots import OpeningRoot, OpeningRoots

ROOT_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"


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


def test_continue_drill_sets_boundary_and_resegments(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    session_uuid = uuid.UUID(session_id)

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
