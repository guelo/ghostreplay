"""
Tests for POST /api/auth/register endpoint.

Run with: pytest test_auth_api.py -v
"""
import os
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("JWT_SECRET", "test-secret-32-bytes-minimum-length")

from app.models import Base, User
from app.main import app
from app.db import get_db
from app.security import decode_access_token, hash_password

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
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username VARCHAR(50) UNIQUE,
                password_hash VARCHAR(255),
                is_anonymous BOOLEAN NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
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
    # Set up override for this test
    app.dependency_overrides[get_db] = override_get_db

    # Reset table
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS users"))
        conn.commit()
    create_test_tables()
    yield


client = TestClient(app)


def create_user(username: str, password: str, is_anonymous: bool = True) -> int:
    db = TestingSessionLocal()
    user = User(
        username=username,
        password_hash=hash_password(password),
        is_anonymous=is_anonymous,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()
    return user.id


def test_register_success():
    """Test successful user registration."""
    response = client.post(
        "/api/auth/register",
        json={"username": "testuser", "password": "password123"}
    )

    assert response.status_code == 201
    data = response.json()
    assert "token" in data
    assert "user_id" in data
    assert data["username"] == "testuser"


def test_register_returns_valid_jwt():
    """Test that registration returns a valid JWT token."""
    response = client.post(
        "/api/auth/register",
        json={"username": "jwtuser", "password": "password123"}
    )

    assert response.status_code == 201
    data = response.json()

    payload = decode_access_token(data["token"])
    assert payload.user_id == data["user_id"]
    assert payload.username == "jwtuser"
    assert payload.is_anonymous is True


def test_register_user_is_anonymous():
    """Test that registered users have is_anonymous=True."""
    response = client.post(
        "/api/auth/register",
        json={"username": "anonuser", "password": "password123"}
    )

    assert response.status_code == 201

    db = TestingSessionLocal()
    user = db.query(User).filter(User.username == "anonuser").first()
    assert user is not None
    assert user.is_anonymous is True
    db.close()


def test_register_password_is_hashed():
    """Test that password is stored hashed, not plaintext."""
    response = client.post(
        "/api/auth/register",
        json={"username": "hashuser", "password": "password123"}
    )

    assert response.status_code == 201

    db = TestingSessionLocal()
    user = db.query(User).filter(User.username == "hashuser").first()
    assert user is not None
    assert user.password_hash != "password123"
    assert user.password_hash.startswith("$2b$")  # bcrypt prefix
    db.close()


def test_register_duplicate_username():
    """Test that duplicate username returns 409."""
    client.post(
        "/api/auth/register",
        json={"username": "duplicate", "password": "password123"}
    )

    response = client.post(
        "/api/auth/register",
        json={"username": "duplicate", "password": "differentpass"}
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_register_username_too_short():
    """Test that username under 3 chars is rejected."""
    response = client.post(
        "/api/auth/register",
        json={"username": "ab", "password": "password123"}
    )

    assert response.status_code == 422


def test_register_username_too_long():
    """Test that username over 50 chars is rejected."""
    response = client.post(
        "/api/auth/register",
        json={"username": "a" * 51, "password": "password123"}
    )

    assert response.status_code == 422


def test_register_password_too_short():
    """Test that password under 8 chars is rejected."""
    response = client.post(
        "/api/auth/register",
        json={"username": "validuser", "password": "short"}
    )

    assert response.status_code == 422


def test_register_missing_username():
    """Test that missing username is rejected."""
    response = client.post(
        "/api/auth/register",
        json={"password": "password123"}
    )

    assert response.status_code == 422


def test_register_missing_password():
    """Test that missing password is rejected."""
    response = client.post(
        "/api/auth/register",
        json={"username": "validuser"}
    )

    assert response.status_code == 422


def test_login_success():
    """Test successful login returns token and user info."""
    user_id = create_user("loginuser", "password123", is_anonymous=False)

    response = client.post(
        "/api/auth/login",
        json={"username": "loginuser", "password": "password123"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == user_id
    assert data["username"] == "loginuser"
    assert "token" in data


def test_login_returns_valid_jwt():
    """Test that login returns a valid JWT token."""
    user_id = create_user("jwtlogin", "password123", is_anonymous=True)

    response = client.post(
        "/api/auth/login",
        json={"username": "jwtlogin", "password": "password123"},
    )

    assert response.status_code == 200
    data = response.json()

    payload = decode_access_token(data["token"])
    assert payload.user_id == user_id
    assert payload.username == "jwtlogin"
    assert payload.is_anonymous is True


def test_login_invalid_password():
    """Test that invalid password returns 401."""
    create_user("badpass", "password123")

    response = client.post(
        "/api/auth/login",
        json={"username": "badpass", "password": "wrongpass"},
    )

    assert response.status_code == 401
    assert "Invalid credentials" in response.json()["detail"]


def test_login_unknown_user():
    """Test that unknown user returns 401."""
    response = client.post(
        "/api/auth/login",
        json={"username": "missing", "password": "password123"},
    )

    assert response.status_code == 401
    assert "Invalid credentials" in response.json()["detail"]


def test_register_unicode_username():
    """Test that unicode usernames are accepted."""
    response = client.post(
        "/api/auth/register",
        json={"username": "用户名测试", "password": "password123"}
    )

    assert response.status_code == 201
    assert response.json()["username"] == "用户名测试"


def test_register_special_chars_username():
    """Test that special characters in username are accepted."""
    response = client.post(
        "/api/auth/register",
        json={"username": "user@test.com", "password": "password123"}
    )

    assert response.status_code == 201
    assert response.json()["username"] == "user@test.com"
