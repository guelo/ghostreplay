"""
Tests for POST /api/blunder endpoint.

Run with: pytest test_blunder_api.py -v
"""
import uuid

from sqlalchemy import text

from app.fen import fen_hash
from app.models import GameSession


def test_record_blunder_success(client, auth_headers, create_game_session):
    """Test successful blunder recording with simple PGN."""
    session_id = create_game_session(user_id=123, player_color="white")

    # PGN: 1. e4 e5 2. Nf3 Nc6 3. Bb5 a6
    # The blunder is 3...a6 but we're testing white's perspective
    # Let's use a PGN where white makes the last move (blunder)
    # 1. e4 e5 2. Qh5 (blunder)
    pgn = "1. e4 e5 2. Qh5"
    # FEN before Qh5: after 1. e4 e5
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_blunder,
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 201
    data = response.json()

    assert "blunder_id" in data
    assert "position_id" in data
    assert "positions_created" in data
    assert "is_new" in data
    assert data["is_new"] is True
    assert data["positions_created"] == 4  # Starting pos + after e4 + after e5 + after Qh5


def test_record_blunder_links_pre_move_position(client, auth_headers, create_game_session, db_session):
    """Test that blunder.position_id points to the pre-move position."""
    session_id = create_game_session(user_id=123, player_color="white")

    pgn = "1. e4 e5 2. Qh5"
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_blunder,
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 201
    blunder_id = response.json()["blunder_id"]

    position_row = db_session.execute(
        text(
            "SELECT id FROM positions WHERE user_id = :user_id AND fen_hash = :fen_hash"
        ),
        {"user_id": 123, "fen_hash": fen_hash(fen_before_blunder)},
    ).fetchone()
    assert position_row is not None

    blunder_position_id = db_session.execute(
        text("SELECT position_id FROM blunders WHERE id = :id"),
        {"id": blunder_id},
    ).fetchone()[0]

    assert blunder_position_id == position_row[0]


def test_record_blunder_creates_positions_and_moves(client, auth_headers, create_game_session, db_session):
    """Test that all intermediate positions and moves are created."""
    session_id = create_game_session(user_id=123, player_color="white")

    pgn = "1. e4 e5 2. Nf3"
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_blunder,
            "user_move": "Nf3",
            "best_move": "Nc3",
            "eval_before": 50,
            "eval_after": 30,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 201

    # Check positions were created
    positions = db_session.execute(text("SELECT COUNT(*) FROM positions WHERE user_id = 123")).fetchone()[0]
    assert positions == 4  # Starting + after e4 + after e5 + after Nf3

    moves = db_session.execute(text("SELECT COUNT(*) FROM moves")).fetchone()[0]
    assert moves == 3  # e4, e5, Nf3


def test_record_blunder_session_not_found(client, auth_headers):
    """Test 404 when session doesn't exist."""
    fake_id = str(uuid.uuid4())

    response = client.post(
        "/api/blunder",
        json={
            "session_id": fake_id,
            "pgn": "1. e4 e5",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "e5",
            "best_move": "d5",
            "eval_before": 50,
            "eval_after": 30,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_record_blunder_wrong_user(client, auth_headers, create_game_session):
    """Test 403 when session belongs to different user."""
    session_id = create_game_session(user_id=999, player_color="white")

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 403
    assert "not authorized" in response.json()["detail"].lower()


def test_record_blunder_already_recorded(client, auth_headers, create_game_session):
    """Test that second blunder in same session is not recorded."""
    session_id = create_game_session(user_id=123, player_color="white", blunder_recorded=True)

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    # Should return 201 but with is_new=False
    assert response.status_code == 201
    data = response.json()
    assert data["is_new"] is False
    assert data["positions_created"] == 0


def test_record_blunder_invalid_pgn(client, auth_headers, create_game_session):
    """Test 422 when PGN is malformed."""
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "not valid pgn at all!!!",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 422


def test_record_blunder_fen_mismatch(client, auth_headers, create_game_session):
    """Test 422 when pre-move FEN doesn't match PGN."""
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            # Wrong FEN - this is starting position, not after 1. e4 e5
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 422
    assert "mismatch" in response.json()["detail"].lower()


def test_record_blunder_wrong_color(client, auth_headers, create_game_session):
    """Test 400 when blunder position is opponent's move."""
    # Player is white, but PGN ends with black's move
    session_id = create_game_session(user_id=123, player_color="white")

    # 1. e4 e5 - e5 is black's move, so pre-blunder position has black to move
    pgn = "1. e4 e5"
    # FEN before e5 (after 1. e4): black to move
    fen_before_e5 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_e5,
            "user_move": "e5",
            "best_move": "d5",
            "eval_before": 30,
            "eval_after": 50,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 400
    assert "black to move" in response.json()["detail"].lower()


def test_record_blunder_black_player(client, auth_headers, create_game_session):
    """Test recording blunder when player is black."""
    session_id = create_game_session(user_id=123, player_color="black")

    # 1. e4 e5 - e5 is black's move
    pgn = "1. e4 e5"
    # FEN before e5: after 1. e4, black to move
    fen_before_e5 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_e5,
            "user_move": "e5",
            "best_move": "d5",
            "eval_before": 30,
            "eval_after": 50,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 201
    data = response.json()
    assert data["is_new"] is True


def test_record_blunder_duplicate_position(client, auth_headers, create_game_session):
    """Test that same position in different games creates only one blunder."""
    # First game - record a blunder
    session1 = create_game_session(user_id=123, player_color="white")
    pgn = "1. e4 e5 2. Qh5"
    fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    response1 = client.post(
        "/api/blunder",
        json={
            "session_id": session1,
            "pgn": pgn,
            "fen": fen,
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response1.status_code == 201
    data1 = response1.json()
    assert data1["is_new"] is True
    positions_first = data1["positions_created"]

    # Second game - same position blunder
    session2 = create_game_session(user_id=123, player_color="white")

    response2 = client.post(
        "/api/blunder",
        json={
            "session_id": session2,
            "pgn": pgn,
            "fen": fen,
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response2.status_code == 201
    data2 = response2.json()
    # Same position, so blunder is not new
    assert data2["is_new"] is False
    # Positions already exist
    assert data2["positions_created"] == 0
    # Same position_id
    assert data2["position_id"] == data1["position_id"]


def test_record_blunder_missing_auth(client, create_game_session):
    """Test 401 when no auth token provided."""
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
    )

    assert response.status_code == 401


def test_record_blunder_sets_blunder_recorded_flag(client, auth_headers, create_game_session, db_session):
    """Test that blunder_recorded flag is set on session."""
    session_id = create_game_session(user_id=123, player_color="white")

    # Verify flag is false initially
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
    assert session is not None
    assert session.blunder_recorded is False

    # Record blunder
    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 201

    # Verify flag is now true
    db_session.expire_all()
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
    assert session is not None
    assert session.blunder_recorded is True


def test_record_blunder_eval_loss_calculation(client, auth_headers, create_game_session, db_session):
    """Test that eval_loss_cp is calculated correctly."""
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 201
    blunder_id = response.json()["blunder_id"]

    # Check eval_loss_cp in database
    db_session.expire_all()
    result = db_session.execute(
        text("SELECT eval_loss_cp FROM blunders WHERE id = :id"),
        {"id": blunder_id}
    ).fetchone()
    # eval_loss = eval_before - eval_after = 50 - (-100) = 150
    assert result[0] == 150


def test_record_manual_blunder_success(client, auth_headers, create_game_session):
    """Manual endpoint records a selected move into ghost library."""
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        "/api/blunder/manual",
        json={
            "session_id": session_id,
            "pgn": "1. e4",
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "user_move": "e4",
            "best_move": None,
            "eval_before": None,
            "eval_after": None,
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 201
    data = response.json()
    assert data["is_new"] is True
    assert data["positions_created"] >= 1


def test_record_manual_blunder_duplicate_returns_not_new(client, auth_headers, create_game_session):
    """Manual duplicate add returns is_new=false for same pre-move position."""
    session_id = create_game_session(user_id=123, player_color="white")
    payload = {
        "session_id": session_id,
        "pgn": "1. e4",
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "user_move": "e4",
        "best_move": "d4",
        "eval_before": 20,
        "eval_after": 15,
    }

    first = client.post("/api/blunder/manual", json=payload, headers=auth_headers(user_id=123))
    assert first.status_code == 201
    assert first.json()["is_new"] is True

    second = client.post("/api/blunder/manual", json=payload, headers=auth_headers(user_id=123))
    assert second.status_code == 201
    assert second.json()["is_new"] is False


def test_record_manual_blunder_allows_ended_session(client, auth_headers, create_game_session):
    """Manual endpoint works for ended sessions as well as active sessions."""
    session_id = create_game_session(user_id=123, player_color="white")

    end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "draw",
            "pgn": "1. e4",
        },
        headers=auth_headers(user_id=123),
    )
    assert end_response.status_code == 200

    manual_response = client.post(
        "/api/blunder/manual",
        json={
            "session_id": session_id,
            "pgn": "1. e4",
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "user_move": "e4",
            "best_move": None,
            "eval_before": None,
            "eval_after": None,
        },
        headers=auth_headers(user_id=123),
    )

    assert manual_response.status_code == 201


def test_record_manual_blunder_wrong_color(client, auth_headers, create_game_session):
    """Manual capture rejects opponent-side decision points."""
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        "/api/blunder/manual",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5",
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "user_move": "e5",
            "best_move": None,
            "eval_before": None,
            "eval_after": None,
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 400
    assert "black to move" in response.json()["detail"].lower()


def test_record_manual_blunder_does_not_set_session_flag(client, auth_headers, create_game_session, db_session):
    """Manual capture must not toggle first-auto-blunder session flag."""
    session_id = create_game_session(user_id=123, player_color="white")
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
    assert session is not None
    assert session.blunder_recorded is False

    response = client.post(
        "/api/blunder/manual",
        json={
            "session_id": session_id,
            "pgn": "1. e4",
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "user_move": "e4",
            "best_move": None,
            "eval_before": None,
            "eval_after": None,
        },
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 201

    db_session.expire_all()
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
    assert session is not None
    assert session.blunder_recorded is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
