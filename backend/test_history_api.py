import uuid
from datetime import datetime, timezone

from app.models import GameSession


def _end_game(client, auth_headers, session_id, user_id=123, result="checkmate_win"):
    return client.post(
        "/api/game/end",
        json={"session_id": session_id, "result": result, "pgn": "1. e4 e5"},
        headers=auth_headers(user_id=user_id),
    )


def _upload_moves(client, auth_headers, session_id, moves, user_id=123):
    return client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": moves},
        headers=auth_headers(user_id=user_id),
    )


def test_history_returns_ended_games_newest_first(client, auth_headers, create_game_session):
    s1 = create_game_session(user_id=123)
    s2 = create_game_session(user_id=123)
    _end_game(client, auth_headers, s1)
    _end_game(client, auth_headers, s2)

    response = client.get("/api/history", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    data = response.json()
    assert len(data["games"]) == 2
    # s2 ended after s1 so it should be first
    assert data["games"][0]["session_id"] == s2
    assert data["games"][1]["session_id"] == s1


def test_history_excludes_active_games(client, auth_headers, create_game_session):
    active = create_game_session(user_id=123)
    ended = create_game_session(user_id=123)
    _end_game(client, auth_headers, ended)

    response = client.get("/api/history", headers=auth_headers(user_id=123))
    data = response.json()
    ids = [g["session_id"] for g in data["games"]]
    assert ended in ids
    assert active not in ids


def test_history_includes_summary_stats(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=123)
    _end_game(client, auth_headers, session_id)

    _upload_moves(client, auth_headers, session_id, [
        {
            "move_number": 1, "color": "white", "move_san": "e4",
            "fen_after": "fen-1w", "eval_delta": 0, "classification": "best",
        },
        {
            "move_number": 1, "color": "black", "move_san": "e5",
            "fen_after": "fen-1b", "eval_delta": 10, "classification": "good",
        },
        {
            "move_number": 2, "color": "white", "move_san": "Nf3",
            "fen_after": "fen-2w", "eval_delta": 50, "classification": "blunder",
        },
        {
            "move_number": 2, "color": "black", "move_san": "Nc6",
            "fen_after": "fen-2b", "eval_delta": 20, "classification": "mistake",
        },
    ])

    response = client.get("/api/history", headers=auth_headers(user_id=123))
    game = response.json()["games"][0]
    assert game["summary"]["total_moves"] == 4
    assert game["summary"]["blunders"] == 1
    assert game["summary"]["mistakes"] == 1
    assert game["summary"]["inaccuracies"] == 0
    assert game["summary"]["average_centipawn_loss"] == 20  # (0+10+50+20)/4


def test_history_empty_when_no_ended_games(client, auth_headers):
    response = client.get("/api/history", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    assert response.json() == {"games": []}


def test_history_respects_limit(client, auth_headers, create_game_session):
    for _ in range(5):
        sid = create_game_session(user_id=123)
        _end_game(client, auth_headers, sid)

    response = client.get("/api/history?limit=2", headers=auth_headers(user_id=123))
    assert len(response.json()["games"]) == 2


def test_history_scoped_to_user(client, auth_headers, create_game_session):
    s_other = create_game_session(user_id=999)
    _end_game(client, auth_headers, s_other, user_id=999)

    s_mine = create_game_session(user_id=123)
    _end_game(client, auth_headers, s_mine, user_id=123)

    response = client.get("/api/history", headers=auth_headers(user_id=123))
    ids = [g["session_id"] for g in response.json()["games"]]
    assert s_mine in ids
    assert s_other not in ids


def test_history_game_without_moves_has_zero_summary(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=123)
    _end_game(client, auth_headers, session_id)

    response = client.get("/api/history", headers=auth_headers(user_id=123))
    game = response.json()["games"][0]
    assert game["summary"] == {
        "total_moves": 0,
        "blunders": 0,
        "mistakes": 0,
        "inaccuracies": 0,
        "average_centipawn_loss": 0,
    }


def test_history_limit_validation(client, auth_headers):
    response = client.get("/api/history?limit=0", headers=auth_headers(user_id=123))
    assert response.status_code == 422

    response = client.get("/api/history?limit=101", headers=auth_headers(user_id=123))
    assert response.status_code == 422
