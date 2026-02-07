import uuid

from app.models import SessionMove


def test_session_moves_bulk_insert_success(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123, player_color="white")
    session_uuid = uuid.UUID(session_id)

    response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
                    "eval_cp": 20,
                    "eval_mate": None,
                    "best_move_san": "e4",
                    "best_move_eval_cp": 20,
                    "eval_delta": 0,
                    "classification": "best",
                },
                {
                    "move_number": 1,
                    "color": "black",
                    "move_san": "e5",
                    "fen_after": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
                    "eval_cp": 12,
                    "eval_mate": None,
                    "best_move_san": "e5",
                    "best_move_eval_cp": 12,
                    "eval_delta": 0,
                    "classification": "excellent",
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 200
    assert response.json() == {"moves_inserted": 2}

    rows = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session_uuid)
        .order_by(SessionMove.move_number, SessionMove.color)
        .all()
    )
    assert len(rows) == 2
    assert rows[0].move_number == 1
    assert rows[0].color == "black"
    assert rows[0].move_san == "e5"
    assert rows[0].classification == "excellent"
    assert rows[1].move_number == 1
    assert rows[1].color == "white"
    assert rows[1].move_san == "e4"
    assert rows[1].classification == "best"


def test_session_moves_upsert_idempotent(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123, player_color="white")
    session_uuid = uuid.UUID(session_id)

    first = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "fen-1",
                    "eval_cp": 30,
                    "best_move_san": "e4",
                    "best_move_eval_cp": 30,
                    "eval_delta": 0,
                    "classification": "best",
                }
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert first.status_code == 200
    assert first.json()["moves_inserted"] == 1

    second = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "d4",
                    "fen_after": "fen-2",
                    "eval_cp": -10,
                    "best_move_san": "e4",
                    "best_move_eval_cp": 20,
                    "eval_delta": 30,
                    "classification": "good",
                }
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert second.status_code == 200
    assert second.json()["moves_inserted"] == 1

    count = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session_uuid)
        .count()
    )
    assert count == 1

    row = (
        db_session.query(SessionMove)
        .filter(
            SessionMove.session_id == session_uuid,
            SessionMove.move_number == 1,
            SessionMove.color == "white",
        )
        .first()
    )
    assert row is not None
    assert row.move_san == "d4"
    assert row.fen_after == "fen-2"
    assert row.eval_cp == -10
    assert row.best_move_san == "e4"
    assert row.best_move_eval_cp == 20
    assert row.eval_delta == 30
    assert row.classification == "good"


def test_session_moves_session_not_found(client, auth_headers):
    response = client.post(
        "/api/session/00000000-0000-0000-0000-000000000000/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "fen",
                }
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_session_moves_wrong_user_forbidden(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=999, player_color="white")

    response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "fen",
                }
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 403
    assert "not authorized" in response.json()["detail"].lower()


def test_session_moves_duplicate_payload_rejected(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "fen-a",
                },
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "d4",
                    "fen_after": "fen-b",
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 422
    assert "duplicate move entry" in response.json()["detail"].lower()


def test_session_analysis_success(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=123, player_color="white")

    end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "checkmate_win",
            "pgn": "1. e4 e5 2. Nf3",
        },
        headers=auth_headers(user_id=123),
    )
    assert end_response.status_code == 200

    upload_response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 2,
                    "color": "white",
                    "move_san": "Nf3",
                    "fen_after": "fen-2w",
                    "eval_cp": 30,
                    "eval_mate": None,
                    "best_move_san": "Nf3",
                    "best_move_eval_cp": 30,
                    "eval_delta": 0,
                    "classification": "excellent",
                },
                {
                    "move_number": 1,
                    "color": "black",
                    "move_san": "e5",
                    "fen_after": "fen-1b",
                    "eval_cp": 10,
                    "eval_mate": None,
                    "best_move_san": "c5",
                    "best_move_eval_cp": 25,
                    "eval_delta": 15,
                    "classification": "mistake",
                },
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "fen-1w",
                    "eval_cp": 20,
                    "eval_mate": None,
                    "best_move_san": "e4",
                    "best_move_eval_cp": 20,
                    "eval_delta": 0,
                    "classification": "inaccuracy",
                },
                {
                    "move_number": 2,
                    "color": "black",
                    "move_san": "Nc6",
                    "fen_after": "fen-2b",
                    "eval_cp": 5,
                    "eval_mate": None,
                    "best_move_san": "d6",
                    "best_move_eval_cp": 50,
                    "eval_delta": 45,
                    "classification": "blunder",
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert upload_response.status_code == 200

    response = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 200

    data = response.json()
    assert data["session_id"] == session_id
    assert data["result"] == "checkmate_win"
    assert data["pgn"] == "1. e4 e5 2. Nf3"
    assert data["summary"] == {
        "blunders": 1,
        "mistakes": 1,
        "inaccuracies": 1,
        "average_centipawn_loss": 15,
    }
    assert [move["move_san"] for move in data["moves"]] == ["e4", "e5", "Nf3", "Nc6"]


def test_session_analysis_empty_moves_returns_zero_summary(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 200
    assert response.json()["moves"] == []
    assert response.json()["summary"] == {
        "blunders": 0,
        "mistakes": 0,
        "inaccuracies": 0,
        "average_centipawn_loss": 0,
    }


def test_session_analysis_session_not_found(client, auth_headers):
    response = client.get(
        "/api/session/00000000-0000-0000-0000-000000000000/analysis",
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_session_analysis_wrong_user_forbidden(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=999, player_color="white")

    response = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 403
    assert "not authorized" in response.json()["detail"].lower()
