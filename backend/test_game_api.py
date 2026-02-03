"""
Simple test for POST /api/game/start endpoint.

Run with: pytest test_game_api.py -v
"""
import os
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Ensure JWT secret is set before app import
os.environ.setdefault("JWT_SECRET", "test-secret-32-bytes-minimum-length")

# Import models FIRST to register them with Base.metadata
from app.models import Base, GameSession, Position, Blunder
from app.main import app
from app.db import get_db
from app.security import create_access_token

# Use in-memory SQLite for testing with StaticPool to share connection
SQLALCHEMY_TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,  # Share the same connection across all threads
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def create_test_tables():
    """Create tables with SQLite-compatible schema."""
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS game_sessions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                started_at TIMESTAMP NOT NULL,
                ended_at TIMESTAMP,
                status VARCHAR(20) NOT NULL,
                result VARCHAR(20),
                engine_elo INTEGER NOT NULL,
                blunder_recorded BOOLEAN NOT NULL DEFAULT 0,
                player_color VARCHAR(5) NOT NULL DEFAULT 'white',
                pgn TEXT
            )
        """))
        conn.commit()


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


import pytest


@pytest.fixture(autouse=True)
def reset_db():
    """Reset database before each test and configure app override."""
    app.dependency_overrides[get_db] = override_get_db

    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS game_sessions"))
        conn.commit()
    create_test_tables()
    yield


client = TestClient(app)


def auth_headers(user_id: int = 123, username: str = "ghost_test", is_anonymous: bool = True) -> dict:
    token = create_access_token(user_id=user_id, username=username, is_anonymous=is_anonymous)
    return {"Authorization": f"Bearer {token}"}


def test_start_game_success():
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


def test_start_game_defaults_player_color_white():
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
    db = TestingSessionLocal()
    try:
        session = db.query(GameSession).filter(GameSession.id == session_uuid).first()
        assert session is not None
        assert session.player_color == "white"
    finally:
        db.close()


def test_start_game_with_player_color_black():
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
    db = TestingSessionLocal()
    try:
        session = db.query(GameSession).filter(GameSession.id == session_uuid).first()
        assert session is not None
        assert session.player_color == "black"
    finally:
        db.close()


def test_start_game_low_elo():
    """Test that low ELO values are accepted (no validation)."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 400},
        headers=auth_headers()
    )

    assert response.status_code == 201
    data = response.json()
    assert data["engine_elo"] == 400


def test_start_game_high_elo():
    """Test that high ELO values are accepted (no validation)."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 3000},
        headers=auth_headers()
    )

    assert response.status_code == 201
    data = response.json()
    assert data["engine_elo"] == 3000


def test_start_game_missing_auth():
    """Test that missing Authorization header is rejected."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500}
    )

    assert response.status_code == 401  # Missing auth token


def test_start_game_invalid_user_id():
    """Test that invalid bearer token is rejected."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers={"Authorization": "Bearer not-a-token"}
    )

    assert response.status_code == 401  # Invalid token


def test_start_game_missing_elo():
    """Test that missing engine_elo is rejected."""
    response = client.post(
        "/api/game/start",
        json={},
        headers=auth_headers()
    )

    assert response.status_code == 422  # Validation error


def test_end_game_success():
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


def test_end_game_with_pgn():
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


def test_end_game_all_result_types():
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
        headers=auth_headers()
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_end_game_wrong_user():
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


def test_end_game_already_ended():
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


def test_end_game_invalid_result():
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


def test_end_game_missing_auth():
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


def test_end_game_missing_pgn():
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


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
