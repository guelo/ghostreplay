"""
Tests for POST /api/blunder endpoint.

Run with: pytest test_blunder_api.py -v
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
from app.models import Base, GameSession, Position, Blunder, Move
from app.main import app
from app.db import get_db
from app.security import create_access_token

# Use in-memory SQLite for testing with StaticPool to share connection
SQLALCHEMY_TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
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
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                fen_hash VARCHAR(64) NOT NULL,
                fen_raw TEXT NOT NULL,
                active_color VARCHAR(5) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, fen_hash)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS blunders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                position_id INTEGER NOT NULL,
                bad_move_san VARCHAR(10) NOT NULL,
                best_move_san VARCHAR(10) NOT NULL,
                eval_loss_cp INTEGER NOT NULL,
                pass_streak INTEGER NOT NULL DEFAULT 0,
                last_reviewed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, position_id),
                FOREIGN KEY (position_id) REFERENCES positions(id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS moves (
                from_position_id INTEGER NOT NULL,
                move_san VARCHAR(10) NOT NULL,
                to_position_id INTEGER NOT NULL,
                PRIMARY KEY (from_position_id, move_san),
                FOREIGN KEY (from_position_id) REFERENCES positions(id),
                FOREIGN KEY (to_position_id) REFERENCES positions(id)
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
from datetime import datetime, timezone


@pytest.fixture(autouse=True)
def reset_db():
    """Reset database before each test and configure app override."""
    app.dependency_overrides[get_db] = override_get_db

    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS moves"))
        conn.execute(text("DROP TABLE IF EXISTS blunders"))
        conn.execute(text("DROP TABLE IF EXISTS positions"))
        conn.execute(text("DROP TABLE IF EXISTS game_sessions"))
        conn.commit()
    create_test_tables()
    yield


client = TestClient(app)


def auth_headers(user_id: int = 123, username: str = "ghost_test", is_anonymous: bool = True) -> dict:
    token = create_access_token(user_id=user_id, username=username, is_anonymous=is_anonymous)
    return {"Authorization": f"Bearer {token}"}


def create_game_session(
    user_id: int = 123,
    player_color: str = "white",
    blunder_recorded: bool = False,
) -> str:
    """Helper to create a game session via API."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": player_color},
        headers=auth_headers(user_id=user_id)
    )
    session_id = response.json()["session_id"]

    # If we need to set blunder_recorded=True, update using ORM
    if blunder_recorded:
        db = TestingSessionLocal()
        try:
            session = db.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
            if session:
                session.blunder_recorded = True
                db.commit()
        finally:
            db.close()

    return session_id


def test_record_blunder_success():
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


def test_record_blunder_creates_positions_and_moves():
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
    db = TestingSessionLocal()
    try:
        positions = db.execute(text("SELECT COUNT(*) FROM positions WHERE user_id = 123")).fetchone()[0]
        assert positions == 4  # Starting + after e4 + after e5 + after Nf3

        moves = db.execute(text("SELECT COUNT(*) FROM moves")).fetchone()[0]
        assert moves == 3  # e4, e5, Nf3
    finally:
        db.close()


def test_record_blunder_session_not_found():
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


def test_record_blunder_wrong_user():
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


def test_record_blunder_already_recorded():
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


def test_record_blunder_invalid_pgn():
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


def test_record_blunder_fen_mismatch():
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


def test_record_blunder_wrong_color():
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


def test_record_blunder_black_player():
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


def test_record_blunder_duplicate_position():
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


def test_record_blunder_missing_auth():
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


def test_record_blunder_sets_blunder_recorded_flag():
    """Test that blunder_recorded flag is set on session."""
    session_id = create_game_session(user_id=123, player_color="white")

    # Verify flag is false initially
    db = TestingSessionLocal()
    try:
        session = db.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
        assert session is not None
        assert session.blunder_recorded is False
    finally:
        db.close()

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
    db = TestingSessionLocal()
    try:
        session = db.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
        assert session is not None
        assert session.blunder_recorded is True
    finally:
        db.close()


def test_record_blunder_eval_loss_calculation():
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
    db = TestingSessionLocal()
    try:
        result = db.execute(
            text("SELECT eval_loss_cp FROM blunders WHERE id = :id"),
            {"id": blunder_id}
        ).fetchone()
        # eval_loss = eval_before - eval_after = 50 - (-100) = 150
        assert result[0] == 150
    finally:
        db.close()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
