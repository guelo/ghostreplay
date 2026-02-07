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
    assert data["mode"] == "ghost"
    assert data["move"] == "e5"
    assert data["target_blunder_id"] is not None


def test_ghost_move_returns_move_to_manual_library_target(client, auth_headers, create_game_session):
    """Manual /api/blunder/manual targets should be reachable by ghost-move traversal."""
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")

    # Manual add for position after 1.e4 e5 (white to move), selected move is 2.Nf3
    manual_response = client.post(
        "/api/blunder/manual",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Nf3",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Nf3",
            "best_move": "Nf3",
            "eval_before": 30,
            "eval_after": 30,
        },
        headers=auth_headers(user_id=user_id),
    )
    assert manual_response.status_code == 201
    assert manual_response.json()["is_new"] is True

    new_session_id = create_game_session(user_id=user_id, player_color="white")
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    response = client.get(
        "/api/game/ghost-move",
        params={"session_id": new_session_id, "fen": fen_after_e4},
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "ghost"
    assert data["move"] == "e5"
    assert data["target_blunder_id"] is not None


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
    assert response.json()["mode"] == "engine"
    assert response.json()["move"] is None


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
    assert response.json()["mode"] == "engine"
    assert response.json()["move"] is None


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
    assert response.json()["mode"] == "ghost"
    assert response.json()["move"] == "Nf3"
    assert response.json()["target_blunder_id"] is not None


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


def test_ghost_move_finds_blunder_multiple_moves_downstream(client, auth_headers, create_game_session):
    """Test ghost-move finds a blunder 3 moves downstream via recursive CTE."""
    user_id = 123

    # Record a blunder at move 4 (white's second move)
    # PGN: 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nd4 (white blunders with Bc4, a dubious move)
    # Blunder is at position after 1.e4 e5 2.Nf3 Nc6 (white to move)
    session_id = create_game_session(user_id=user_id, player_color="white")
    pgn = "1. e4 e5 2. Nf3 Nc6 3. Bc4"
    # FEN after 1.e4 e5 2.Nf3 Nc6 (white to move) - this is where blunder happens
    fen_before_blunder = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"

    blunder_response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_blunder,
            "user_move": "Bc4",
            "best_move": "Bb5",
            "eval_before": 30,
            "eval_after": -20,
        },
        headers=auth_headers(user_id=user_id),
    )
    assert blunder_response.status_code == 201

    # Now start a NEW game and query ghost-move from earlier in the game
    new_session_id = create_game_session(user_id=user_id, player_color="white")

    # After 1.e4 (black to move) - blunder is 3 half-moves away
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    response = client.get(
        "/api/game/ghost-move",
        params={"session_id": new_session_id, "fen": fen_after_e4},
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 200
    data = response.json()
    # Ghost should suggest "e5" - the first move in the path to the blunder position
    assert data["mode"] == "ghost"
    assert data["move"] == "e5"
    assert data["target_blunder_id"] is not None


def _create_position_chain(db_session, user_id: int, length: int):
    """Helper to create a chain of positions for depth tests."""
    from app.models import Move, Position
    from app.fen import fen_hash

    positions = []
    for i in range(length):
        color_char = "w" if i % 2 == 0 else "b"
        # Place king at different squares to get unique positions
        file_idx = i % 8
        rank_idx = i // 8
        # Build FEN with king at unique square
        ranks = ["8"] * 8
        if file_idx == 0:
            ranks[rank_idx] = f"K{7 - file_idx}" if 7 - file_idx > 0 else "K"
        else:
            ranks[rank_idx] = f"{file_idx}K" + (f"{7 - file_idx}" if 7 - file_idx > 0 else "")
        fen = "/".join(reversed(ranks)) + f" {color_char} - - 0 {i}"

        pos = Position(
            user_id=user_id,
            fen_hash=fen_hash(fen),
            fen_raw=fen,
            active_color="white" if i % 2 == 0 else "black",
        )
        db_session.add(pos)
        db_session.flush()
        positions.append(pos)

    # Create moves connecting them: 0->1->2->...->n
    for i in range(length - 1):
        move = Move(
            from_position_id=positions[i].id,
            move_san=f"m{i}",
            to_position_id=positions[i + 1].id,
        )
        db_session.add(move)

    return positions


def test_ghost_move_finds_blunder_at_max_depth(client, auth_headers, create_game_session, db_session):
    """Test ghost-move finds a blunder exactly at depth 15 (the limit)."""
    from app.models import Blunder

    user_id = 123

    # Create chain of 17 positions (0 through 16)
    # Query from position 1, blunder at position 16 = depth 15
    positions = _create_position_chain(db_session, user_id, 17)

    # Create a blunder at position 16 (depth 15 from position 1)
    # Position 16 has active_color="white" (16 % 2 == 0)
    blunder = Blunder(
        user_id=user_id,
        position_id=positions[16].id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
    )
    db_session.add(blunder)
    db_session.commit()

    # Create a game session as white
    session_id = create_game_session(user_id=user_id, player_color="white")

    # Query from position 1 (black to move = opponent's turn)
    # Blunder is at position 16, which is exactly 15 moves away
    response = client.get(
        "/api/game/ghost-move",
        params={"session_id": session_id, "fen": positions[1].fen_raw},
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 200
    # Should find the blunder at exactly depth 15
    assert response.json()["mode"] == "ghost"
    assert response.json()["move"] == "m1"
    assert response.json()["target_blunder_id"] is not None


def test_ghost_move_respects_depth_limit(client, auth_headers, create_game_session, db_session):
    """Test ghost-move does not find blunders beyond depth 15."""
    from app.models import Blunder

    user_id = 123

    # Create chain of 19 positions (0 through 18)
    # Query from position 1, blunder at position 18 = depth 17 (beyond limit)
    positions = _create_position_chain(db_session, user_id, 19)

    # Create a blunder at position 18 (depth 17 from position 1, beyond limit of 15)
    # Position 18 has active_color="white" (18 % 2 == 0)
    blunder = Blunder(
        user_id=user_id,
        position_id=positions[18].id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
    )
    db_session.add(blunder)
    db_session.commit()

    # Create a game session as white
    session_id = create_game_session(user_id=user_id, player_color="white")

    # Query from position 1 (black to move = opponent's turn for white player)
    # Blunder is at position 18, which is 17 moves away (beyond depth 15)
    response = client.get(
        "/api/game/ghost-move",
        params={"session_id": session_id, "fen": positions[1].fen_raw},
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 200
    # Should NOT find the blunder at depth 17 (positions 1->2->...->18)
    assert response.json()["mode"] == "engine"
    assert response.json()["move"] is None


def test_ghost_move_handles_cycles(client, auth_headers, create_game_session, db_session):
    """Test ghost-move handles cycles in the position graph without infinite loops."""
    from app.models import Blunder, Move, Position
    from app.fen import fen_hash

    user_id = 123

    # Create positions that form a cycle: A -> B -> C -> A
    # Plus a path from B to a blunder position D
    # Use valid FEN format with different piece placements to get unique hashes
    fen_a = "8/8/8/8/8/8/8/K7 b - - 0 1"  # black to move
    fen_b = "8/8/8/8/8/8/8/1K6 w - - 0 2"  # white to move
    fen_c = "8/8/8/8/8/8/8/2K5 b - - 0 3"  # black to move
    fen_d = "8/8/8/8/8/8/8/3K4 w - - 0 4"  # white to move - blunder position

    pos_a = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_a),
        fen_raw=fen_a,
        active_color="black",  # Opponent's turn
    )
    pos_b = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_b),
        fen_raw=fen_b,
        active_color="white",  # User's turn
    )
    pos_c = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_c),
        fen_raw=fen_c,
        active_color="black",  # Opponent's turn
    )
    pos_d = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_d),
        fen_raw=fen_d,
        active_color="white",  # User's turn - blunder here
    )

    db_session.add_all([pos_a, pos_b, pos_c, pos_d])
    db_session.flush()

    # Create cycle: A -> B -> C -> A
    move_ab = Move(from_position_id=pos_a.id, move_san="a2b", to_position_id=pos_b.id)
    move_bc = Move(from_position_id=pos_b.id, move_san="b2c", to_position_id=pos_c.id)
    move_ca = Move(from_position_id=pos_c.id, move_san="c2a", to_position_id=pos_a.id)
    # Also B -> D (path to blunder)
    move_bd = Move(from_position_id=pos_b.id, move_san="b2d", to_position_id=pos_d.id)

    db_session.add_all([move_ab, move_bc, move_ca, move_bd])

    # Create blunder at position D
    blunder = Blunder(
        user_id=user_id,
        position_id=pos_d.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
    )
    db_session.add(blunder)
    db_session.commit()

    # Create game session as white
    session_id = create_game_session(user_id=user_id, player_color="white")

    # Query from position A (black to move = opponent's turn)
    # Should find path A -> B -> D and return "a2b"
    response = client.get(
        "/api/game/ghost-move",
        params={"session_id": session_id, "fen": fen_a},
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 200
    # Should find the blunder despite the cycle, and not hang
    assert response.json()["mode"] == "ghost"
    assert response.json()["move"] == "a2b"
    assert response.json()["target_blunder_id"] is not None


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
