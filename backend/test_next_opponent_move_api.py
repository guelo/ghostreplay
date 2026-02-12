"""
Tests for POST /api/game/next-opponent-move endpoint.

Contract validation for g-29c.2.1.
Full implementation tested in g-29c.2.2 (ghost decision) and g-29c.2.3 (Maia runtime).
"""
import uuid


def test_next_opponent_move_validates_session_exists(client, auth_headers):
    """Endpoint returns 404 when session does not exist."""
    fake_session_id = uuid.uuid4()
    response = client.post(
        "/api/game/next-opponent-move",
        json={
            "session_id": str(fake_session_id),
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Game session not found"


def test_next_opponent_move_validates_ownership(
    client, auth_headers, create_game_session
):
    """Endpoint returns 403 when session belongs to different user."""
    session_id = create_game_session(user_id=456, player_color="white")

    response = client.post(
        "/api/game/next-opponent-move",
        json={
            "session_id": session_id,
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        },
        headers=auth_headers(user_id=123),  # Different user
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Not authorized to access this game"


def test_next_opponent_move_validates_opponent_turn(
    client, auth_headers, create_game_session
):
    """Endpoint returns 400 when it's the player's turn (not opponent's turn)."""
    # Create white session
    session_id = create_game_session(user_id=123, player_color="white")

    # FEN shows white to move (player's turn, not opponent's)
    response = client.post(
        "/api/game/next-opponent-move",
        json={
            "session_id": session_id,
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1",
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 400
    assert "player's turn" in response.json()["detail"]


def test_next_opponent_move_validates_invalid_fen(
    client, auth_headers, create_game_session
):
    """Endpoint returns 400 when FEN is malformed."""
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        "/api/game/next-opponent-move",
        json={
            "session_id": session_id,
            "fen": "invalid-fen-string",
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 400
    assert "Invalid FEN" in response.json()["detail"]


def test_next_opponent_move_happy_path_returns_valid_contract(
    client, auth_headers, create_game_session
):
    """
    Endpoint returns valid response matching SPEC contract.

    Validates:
    - Response includes mode (ghost | engine)
    - Response includes move with both uci and san formats
    - Response includes decision_source
    - Response includes target_blunder_id (null for engine mode)
    """
    # Create white session
    session_id = create_game_session(user_id=123, player_color="white")

    # FEN shows black to move (opponent's turn)
    response = client.post(
        "/api/game/next-opponent-move",
        json={
            "session_id": session_id,
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 200
    data = response.json()

    # Validate contract structure
    assert "mode" in data
    assert data["mode"] in ["ghost", "engine"]

    assert "move" in data
    assert "uci" in data["move"]
    assert "san" in data["move"]

    assert "decision_source" in data
    assert data["decision_source"] in ["ghost_path", "backend_engine"]

    assert "target_blunder_id" in data
    # In engine mode, target_blunder_id should be null
    if data["mode"] == "engine":
        assert data["target_blunder_id"] is None


def test_next_opponent_move_returns_legal_move(
    client, auth_headers, create_game_session
):
    """Endpoint returns a legal chess move in valid notation."""
    session_id = create_game_session(user_id=123, player_color="white")

    # Starting position, black to move
    response = client.post(
        "/api/game/next-opponent-move",
        json={
            "session_id": session_id,
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 200
    data = response.json()

    # Move should be in valid format
    assert isinstance(data["move"]["uci"], str)
    assert isinstance(data["move"]["san"], str)
    assert len(data["move"]["uci"]) >= 4  # e.g., "e7e5"
    assert len(data["move"]["san"]) >= 2  # e.g., "e5"


def test_next_opponent_move_black_player_validates_turn(
    client, auth_headers, create_game_session
):
    """Endpoint validates opponent turn correctly when player is black."""
    # Create black session
    session_id = create_game_session(user_id=123, player_color="black")

    # FEN shows white to move (opponent's turn for black player)
    response = client.post(
        "/api/game/next-opponent-move",
        json={
            "session_id": session_id,
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1",
        },
        headers=auth_headers(user_id=123),
    )

    # Should succeed (opponent's turn)
    assert response.status_code == 200

    # Now test player's turn (black to move)
    response = client.post(
        "/api/game/next-opponent-move",
        json={
            "session_id": session_id,
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        },
        headers=auth_headers(user_id=123),
    )

    # Should fail (player's turn)
    assert response.status_code == 400
    assert "player's turn" in response.json()["detail"]
