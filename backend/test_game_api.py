"""
Simple test for POST /api/game/start endpoint.

Run with: pytest test_game_api.py -v
"""
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Import models FIRST to register them with Base.metadata
from app.models import Base, GameSession, Position, Blunder
from app.main import app
from app.db import get_db

# Use in-memory SQLite for testing with StaticPool to share connection
SQLALCHEMY_TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,  # Share the same connection across all threads
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create tables - models must be imported before this line
Base.metadata.create_all(bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


def test_start_game_success():
    """Test successful game creation with standard ELO."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers={"X-User-Id": "123"}
    )

    assert response.status_code == 201
    data = response.json()

    # Verify response structure
    assert "session_id" in data
    assert "engine_elo" in data

    # Verify values
    assert data["engine_elo"] == 1500

    # Verify session_id is a valid UUID
    try:
        uuid.UUID(data["session_id"])
    except ValueError:
        assert False, "session_id is not a valid UUID"


def test_start_game_low_elo():
    """Test that low ELO values are accepted (no validation)."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 400},
        headers={"X-User-Id": "123"}
    )

    assert response.status_code == 201
    data = response.json()
    assert data["engine_elo"] == 400


def test_start_game_high_elo():
    """Test that high ELO values are accepted (no validation)."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 3000},
        headers={"X-User-Id": "123"}
    )

    assert response.status_code == 201
    data = response.json()
    assert data["engine_elo"] == 3000


def test_start_game_missing_auth():
    """Test that missing X-User-Id header is rejected."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500}
    )

    assert response.status_code == 422  # Missing required header


def test_start_game_invalid_user_id():
    """Test that invalid user_id in header is rejected."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers={"X-User-Id": "not-a-number"}
    )

    assert response.status_code == 401  # Invalid user ID


def test_start_game_missing_elo():
    """Test that missing engine_elo is rejected."""
    response = client.post(
        "/api/game/start",
        json={},
        headers={"X-User-Id": "123"}
    )

    assert response.status_code == 422  # Validation error


def test_end_game_success():
    """Test successfully ending a game with checkmate_win."""
    # Start a game first
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers={"X-User-Id": "123"}
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
        headers={"X-User-Id": "123"}
    )

    assert end_response.status_code == 200
    data = end_response.json()

    assert data["session_id"] == session_id
    assert data["result"] == "checkmate_win"
    assert "ended_at" in data


def test_end_game_with_pgn():
    """Test ending a game with PGN."""
    # Start a game
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers={"X-User-Id": "123"}
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
        headers={"X-User-Id": "123"}
    )

    assert end_response.status_code == 200
    assert end_response.json()["result"] == "draw"


def test_end_game_all_result_types():
    """Test all valid result types."""
    results = ["checkmate_win", "checkmate_loss", "resign", "draw", "abandon"]

    for result in results:
        # Start a new game for each result type
        start_response = client.post(
            "/api/game/start",
            json={"engine_elo": 1500},
            headers={"X-User-Id": "123"}
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
            headers={"X-User-Id": "123"}
        )

        assert end_response.status_code == 200
        assert end_response.json()["result"] == result


def test_end_game_not_found():
    """Test ending a non-existent game."""
    fake_uuid = "00000000-0000-0000-0000-000000000000"
    response = client.post(
        "/api/game/end",
        json={
            "session_id": fake_uuid,
            "result": "resign",
            "pgn": "1. e4 e5"
        },
        headers={"X-User-Id": "123"}
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_end_game_wrong_user():
    """Test that users cannot end other users' games."""
    # User 123 starts a game
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers={"X-User-Id": "123"}
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
        headers={"X-User-Id": "456"}
    )

    assert end_response.status_code == 403
    assert "not authorized" in end_response.json()["detail"].lower()


def test_end_game_already_ended():
    """Test that ending an already-ended game fails."""
    # Start a game
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers={"X-User-Id": "123"}
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
        headers={"X-User-Id": "123"}
    )

    # Try to end it again
    second_end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "resign",
            "pgn": "1. e4 e5"
        },
        headers={"X-User-Id": "123"}
    )

    assert second_end_response.status_code == 400
    assert "already ended" in second_end_response.json()["detail"].lower()


def test_end_game_invalid_result():
    """Test that invalid result values are rejected."""
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers={"X-User-Id": "123"}
    )
    session_id = start_response.json()["session_id"]

    response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "invalid_result",
            "pgn": "1. e4 e5"
        },
        headers={"X-User-Id": "123"}
    )

    assert response.status_code == 422  # Validation error


def test_end_game_missing_auth():
    """Test that missing X-User-Id header is rejected."""
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers={"X-User-Id": "123"}
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

    assert response.status_code == 422  # Missing required header


def test_end_game_missing_pgn():
    """Test that missing PGN is rejected."""
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers={"X-User-Id": "123"}
    )
    session_id = start_response.json()["session_id"]

    response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "resign"
        },
        headers={"X-User-Id": "123"}
    )

    assert response.status_code == 422  # Validation error


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
