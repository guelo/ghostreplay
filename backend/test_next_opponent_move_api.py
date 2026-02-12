"""
Tests for POST /api/game/next-opponent-move endpoint.

Contract validation for g-29c.2.1.
Full implementation tested in g-29c.2.2 (ghost decision) and g-29c.2.3 (Maia runtime).
Decision branch smoke tests for g-29c.2.6.
"""
import uuid
from unittest.mock import patch

from sqlalchemy import text

from app.fen import fen_hash


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


def test_next_opponent_move_ghost_branch_happy_path(
    client, auth_headers, create_game_session, db_session
):
    """Ghost branch returns mode=ghost when position graph leads to a blunder."""
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")

    # FEN A: black to move (opponent's turn for white player)
    fen_a = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    # FEN B: white to move (player's color → blunder position)
    fen_b = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"

    # Seed position A
    db_session.execute(
        text("""
            INSERT INTO positions (user_id, fen_hash, fen_raw, active_color)
            VALUES (:uid, :hash, :fen, 'black')
        """),
        {"uid": user_id, "hash": fen_hash(fen_a), "fen": fen_a},
    )
    # Seed position B
    db_session.execute(
        text("""
            INSERT INTO positions (user_id, fen_hash, fen_raw, active_color)
            VALUES (:uid, :hash, :fen, 'white')
        """),
        {"uid": user_id, "hash": fen_hash(fen_b), "fen": fen_b},
    )
    db_session.flush()

    # Get position IDs
    pos_a_id = db_session.execute(
        text("SELECT id FROM positions WHERE fen_hash = :h"),
        {"h": fen_hash(fen_a)},
    ).scalar_one()
    pos_b_id = db_session.execute(
        text("SELECT id FROM positions WHERE fen_hash = :h"),
        {"h": fen_hash(fen_b)},
    ).scalar_one()

    # Insert edge A → B via move "e5"
    db_session.execute(
        text("""
            INSERT INTO moves (from_position_id, move_san, to_position_id)
            VALUES (:from_id, 'e5', :to_id)
        """),
        {"from_id": pos_a_id, "to_id": pos_b_id},
    )

    # Insert blunder on position B for this user
    db_session.execute(
        text("""
            INSERT INTO blunders (user_id, position_id, bad_move_san, best_move_san, eval_loss_cp)
            VALUES (:uid, :pid, 'Nf6', 'd5', 150)
        """),
        {"uid": user_id, "pid": pos_b_id},
    )
    db_session.commit()

    response = client.post(
        "/api/game/next-opponent-move",
        json={"session_id": session_id, "fen": fen_a},
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "ghost"
    assert data["decision_source"] == "ghost_path"
    assert data["target_blunder_id"] is not None
    assert data["move"]["san"] == "e5"
    assert data["move"]["uci"] == "e7e5"


def test_next_opponent_move_engine_branch_happy_path(
    client, auth_headers, create_game_session
):
    """Engine branch returns mode=engine when no ghost data exists."""
    from app.maia_engine import MaiaMove

    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")

    fake_move = MaiaMove(uci="e7e5", san="e5", confidence=0.85)

    with patch("app.maia_engine.MaiaEngineService") as mock_maia:
        mock_maia.get_best_move.return_value = fake_move

        response = client.post(
            "/api/game/next-opponent-move",
            json={
                "session_id": session_id,
                "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            },
            headers=auth_headers(user_id=user_id),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "engine"
    assert data["decision_source"] == "backend_engine"
    assert data["target_blunder_id"] is None
    assert data["move"]["uci"] == "e7e5"
    assert data["move"]["san"] == "e5"
