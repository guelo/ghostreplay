import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("JWT_SECRET", "test-secret-32-bytes-minimum-length")

from app.db import get_db
from app.main import app
from app.models import GameSession, User
from app.security import create_access_token, hash_password

SQLALCHEMY_TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _create_test_schema(conn) -> None:
    conn.execute(text("PRAGMA foreign_keys=ON"))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(50) UNIQUE,
            password_hash VARCHAR(255),
            is_anonymous BOOLEAN NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
    """))
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


def _reset_test_schema(conn) -> None:
    conn.execute(text("DROP TABLE IF EXISTS moves"))
    conn.execute(text("DROP TABLE IF EXISTS blunders"))
    conn.execute(text("DROP TABLE IF EXISTS positions"))
    conn.execute(text("DROP TABLE IF EXISTS game_sessions"))
    conn.execute(text("DROP TABLE IF EXISTS users"))
    conn.commit()
    _create_test_schema(conn)


def _override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _db_override():
    app.dependency_overrides[get_db] = _override_get_db
    with engine.connect() as conn:
        _reset_test_schema(conn)
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client(_db_override):
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def db_session(_db_override):
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def auth_headers():
    def _auth_headers(user_id: int = 123, username: str = "ghost_test", is_anonymous: bool = True) -> dict:
        token = create_access_token(user_id=user_id, username=username, is_anonymous=is_anonymous)
        return {"Authorization": f"Bearer {token}"}

    return _auth_headers


@pytest.fixture
def create_user(db_session):
    def _create_user(username: str, password: str, is_anonymous: bool = True) -> User:
        user = User(
            username=username,
            password_hash=hash_password(password),
            is_anonymous=is_anonymous,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        return user

    return _create_user


@pytest.fixture
def create_game_session(client, auth_headers, db_session):
    def _create_game_session(
        user_id: int = 123,
        player_color: str = "white",
        blunder_recorded: bool = False,
    ) -> str:
        response = client.post(
            "/api/game/start",
            json={"engine_elo": 1500, "player_color": player_color},
            headers=auth_headers(user_id=user_id),
        )
        assert response.status_code == 201
        session_id = response.json()["session_id"]

        if blunder_recorded:
            session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
            if session:
                session.blunder_recorded = True
                db_session.commit()

        return session_id

    return _create_game_session
