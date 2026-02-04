"""
Simple test for POST /api/game/start endpoint.

Run with: pytest test_game_api.py -v
"""
import uuid

from app.models import GameSession


def test_start_game_success(client, auth_headers):
    """Test successful game creation with standard ELO."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )

    assert response.status_code == 201
    data = response.json()

    # Verify response structure
    assert "session_id" in data
    assert "engine_elo" in data
    assert "player_color" in data

    # Verify values
    assert data["engine_elo"] == 1500
    assert data["player_color"] == "white"  # Default

    # Verify session_id is a valid UUID
    try:
        uuid.UUID(data["session_id"])
    except ValueError:
        assert False, "session_id is not a valid UUID"


def test_start_game_defaults_player_color_white(client, auth_headers, db_session):
    """Test that player_color defaults to white when omitted."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )

    assert response.status_code == 201
    data = response.json()
    session_uuid = uuid.UUID(data["session_id"])

    # Verify response includes player_color
    assert data["player_color"] == "white"

    # Verify database persistence
    session = db_session.query(GameSession).filter(GameSession.id == session_uuid).first()
    assert session is not None
    assert session.player_color == "white"


def test_start_game_with_player_color_black(client, auth_headers, db_session):
    """Test that player_color is persisted when provided."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "black"},
        headers=auth_headers()
    )

    assert response.status_code == 201
    data = response.json()
    session_uuid = uuid.UUID(data["session_id"])

    # Verify response includes player_color
    assert data["player_color"] == "black"

    # Verify database persistence
    session = db_session.query(GameSession).filter(GameSession.id == session_uuid).first()
    assert session is not None
    assert session.player_color == "black"


def test_start_game_low_elo(client, auth_headers):
    """Test that low ELO values are accepted (no validation)."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 400},
        headers=auth_headers()
    )

    assert response.status_code == 201
    data = response.json()
    assert data["engine_elo"] == 400


def test_start_game_high_elo(client, auth_headers):
    """Test that high ELO values are accepted (no validation)."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 3000},
        headers=auth_headers()
    )

    assert response.status_code == 201
    data = response.json()
    assert data["engine_elo"] == 3000


def test_start_game_missing_auth(client):
    """Test that missing Authorization header is rejected."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500}
    )

    assert response.status_code == 401  # Missing auth token


def test_start_game_invalid_user_id(client):
    """Test that invalid bearer token is rejected."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers={"Authorization": "Bearer not-a-token"}
    )

    assert response.status_code == 401  # Invalid token


def test_start_game_missing_elo(client, auth_headers):
    """Test that missing engine_elo is rejected."""
    response = client.post(
        "/api/game/start",
        json={},
        headers=auth_headers()
    )

    assert response.status_code == 422  # Validation error


def test_end_game_success(client, auth_headers):
    """Test successfully ending a game with checkmate_win."""
    # Start a game first
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )
    session_id = start_response.json()["session_id"]

    # End the game
    end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "checkmate_win",
            "pgn": "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#"
        },
        headers=auth_headers()
    )

    assert end_response.status_code == 200
    data = end_response.json()

    assert data["session_id"] == session_id
    assert data["result"] == "checkmate_win"
    assert "ended_at" in data


def test_end_game_with_pgn(client, auth_headers):
    """Test ending a game with PGN."""
    # Start a game
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )
    session_id = start_response.json()["session_id"]

    # End with PGN
    pgn = "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6"
    end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "draw",
            "pgn": pgn
        },
        headers=auth_headers()
    )

    assert end_response.status_code == 200
    assert end_response.json()["result"] == "draw"


def test_end_game_all_result_types(client, auth_headers):
    """Test all valid result types."""
    results = ["checkmate_win", "checkmate_loss", "resign", "draw", "abandon"]

    for result in results:
        # Start a new game for each result type
        start_response = client.post(
            "/api/game/start",
            json={"engine_elo": 1500},
            headers=auth_headers()
        )
        session_id = start_response.json()["session_id"]

        # End with specific result
        end_response = client.post(
            "/api/game/end",
            json={
                "session_id": session_id,
                "result": result,
                "pgn": "1. e4 e5"
            },
            headers=auth_headers()
        )

        assert end_response.status_code == 200
        assert end_response.json()["result"] == result


def test_end_game_not_found(client, auth_headers):
    """Test ending a non-existent game."""
    fake_uuid = "00000000-0000-0000-0000-000000000000"
    response = client.post(
        "/api/game/end",
        json={
            "session_id": fake_uuid,
            "result": "resign",
            "pgn": "1. e4 e5"
        },
        headers=auth_headers()
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_end_game_wrong_user(client, auth_headers):
    """Test that users cannot end other users' games."""
    # User 123 starts a game
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers(user_id=123, username="ghost_123")
    )
    session_id = start_response.json()["session_id"]

    # User 456 tries to end it
    end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "resign",
            "pgn": "1. e4 e5"
        },
        headers=auth_headers(user_id=456, username="ghost_456")
    )

    assert end_response.status_code == 403
    assert "not authorized" in end_response.json()["detail"].lower()


def test_end_game_already_ended(client, auth_headers):
    """Test that ending an already-ended game fails."""
    # Start a game
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )
    session_id = start_response.json()["session_id"]

    # End it once
    client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "checkmate_win",
            "pgn": "1. e4 e5"
        },
        headers=auth_headers()
    )

    # Try to end it again
    second_end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "resign",
            "pgn": "1. e4 e5"
        },
        headers=auth_headers()
    )

    assert second_end_response.status_code == 400
    assert "already ended" in second_end_response.json()["detail"].lower()


def test_end_game_invalid_result(client, auth_headers):
    """Test that invalid result values are rejected."""
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )
    session_id = start_response.json()["session_id"]

    response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "invalid_result",
            "pgn": "1. e4 e5"
        },
        headers=auth_headers()
    )

    assert response.status_code == 422  # Validation error


def test_end_game_missing_auth(client, auth_headers):
    """Test that missing Authorization header is rejected."""
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )
    session_id = start_response.json()["session_id"]

    response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "resign",
            "pgn": "1. e4 e5"
        }
    )

    assert response.status_code == 401  # Missing auth token


def test_end_game_missing_pgn(client, auth_headers):
    """Test that missing PGN is rejected."""
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )
    session_id = start_response.json()["session_id"]

    response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "resign"
        },
        headers=auth_headers()
    )

    assert response.status_code == 422  # Validation error


# === Ghost Move Endpoint Tests ===


def test_ghost_move_returns_opponent_move_to_blunder(client, auth_headers, create_game_session):
    """Test ghost-move returns opponent's move leading to a blunder position."""
    user_id = 123

    # First, record a blunder via /api/blunder
    # PGN: 1. e4 e5 2. Qh5 (white blunders with Qh5)
    # Blunder is at position after 1.e4 e5 (white to move)
    session_id = create_game_session(user_id=user_id, player_color="white")
    pgn = "1. e4 e5 2. Qh5"
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    blunder_response = client.post(
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
        headers=auth_headers(user_id=user_id),
    )
    assert blunder_response.status_code == 201

    # Now start a NEW game and query ghost-move
    new_session_id = create_game_session(user_id=user_id, player_color="white")

    # After 1.e4 (black to move), ghost should suggest e5 to reach blunder position
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    response = client.get(
        "/api/game/ghost-move",
        params={"session_id": new_session_id, "fen": fen_after_e4},
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["fen"] == fen_after_e4
    assert data["ghost_move"] == "e5"


def test_ghost_move_no_blunder_in_path(client, auth_headers, create_game_session):
    """Test ghost-move returns null when no blunder exists in the game graph."""
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")

    # Query from starting position - no blunders recorded
    starting_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    response = client.get(
        "/api/game/ghost-move",
        params={"session_id": session_id, "fen": starting_fen},
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 200
    assert response.json()["ghost_move"] is None


def test_ghost_move_users_turn_returns_null(client, auth_headers, create_game_session):
    """Test ghost-move returns null when it's the user's turn."""
    user_id = 123

    # Record a blunder
    session_id = create_game_session(user_id=user_id, player_color="white")
    pgn = "1. e4 e5 2. Qh5"
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    client.post(
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
        headers=auth_headers(user_id=user_id),
    )

    # Start new game and query at the blunder position (white to move)
    new_session_id = create_game_session(user_id=user_id, player_color="white")

    response = client.get(
        "/api/game/ghost-move",
        params={"session_id": new_session_id, "fen": fen_before_blunder},
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 200
    # It's white's turn, ghost doesn't suggest - user decides
    assert response.json()["ghost_move"] is None


def test_ghost_move_black_player(client, auth_headers, create_game_session):
    """Test ghost-move works for black player."""
    user_id = 123

    # Record a blunder as black
    # PGN: 1. e4 e5 2. Nf3 Qh4 (black blunders with Qh4)
    session_id = create_game_session(user_id=user_id, player_color="black")
    pgn = "1. e4 e5 2. Nf3 Qh4"
    # FEN after 1.e4 e5 2.Nf3 (black to move)
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"

    blunder_response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_blunder,
            "user_move": "Qh4",
            "best_move": "Nc6",
            "eval_before": -50,
            "eval_after": 100,
        },
        headers=auth_headers(user_id=user_id),
    )
    assert blunder_response.status_code == 201

    # Start new game as black
    new_session_id = create_game_session(user_id=user_id, player_color="black")

    # After 1.e4 e5 (white to move), ghost should suggest Nf3 to reach blunder position
    fen_after_e5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    response = client.get(
        "/api/game/ghost-move",
        params={"session_id": new_session_id, "fen": fen_after_e5},
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 200
    assert response.json()["ghost_move"] == "Nf3"


def test_ghost_move_session_not_found(client, auth_headers):
    """Test ghost-move returns 404 for non-existent session."""
    fake_uuid = "00000000-0000-0000-0000-000000000000"
    starting_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    response = client.get(
        "/api/game/ghost-move",
        params={"session_id": fake_uuid, "fen": starting_fen},
        headers=auth_headers(),
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_ghost_move_wrong_user(client, auth_headers, create_game_session):
    """Test ghost-move returns 403 when user doesn't own the session."""
    # User 123 starts a game
    session_id = create_game_session(user_id=123, player_color="white")
    starting_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    # User 456 tries to query ghost-move
    response = client.get(
        "/api/game/ghost-move",
        params={"session_id": session_id, "fen": starting_fen},
        headers=auth_headers(user_id=456),
    )

    assert response.status_code == 403
    assert "not authorized" in response.json()["detail"].lower()


def test_ghost_move_missing_auth(client):
    """Test ghost-move returns 401 when auth is missing."""
    starting_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    response = client.get(
        "/api/game/ghost-move",
        params={
            "session_id": "00000000-0000-0000-0000-000000000000",
            "fen": starting_fen,
        },
    )

    assert response.status_code == 401


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
