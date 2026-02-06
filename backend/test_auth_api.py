"""
Tests for POST /api/auth/register endpoint.

Run with: pytest test_auth_api.py -v
"""
from app.models import User
from app.security import decode_access_token


def test_register_success(client):
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


def test_register_returns_valid_jwt(client):
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


def test_register_user_is_anonymous(client, db_session):
    """Test that registered users have is_anonymous=True."""
    response = client.post(
        "/api/auth/register",
        json={"username": "anonuser", "password": "password123"}
    )

    assert response.status_code == 201

    user = db_session.query(User).filter(User.username == "anonuser").first()
    assert user is not None
    assert user.is_anonymous is True


def test_register_password_is_hashed(client, db_session):
    """Test that password is stored hashed, not plaintext."""
    response = client.post(
        "/api/auth/register",
        json={"username": "hashuser", "password": "password123"}
    )

    assert response.status_code == 201

    user = db_session.query(User).filter(User.username == "hashuser").first()
    assert user is not None
    assert user.password_hash != "password123"
    assert user.password_hash.startswith("$2b$")  # bcrypt prefix


def test_register_duplicate_username(client):
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


def test_register_username_too_short(client):
    """Test that username under 3 chars is rejected."""
    response = client.post(
        "/api/auth/register",
        json={"username": "ab", "password": "password123"}
    )

    assert response.status_code == 422


def test_register_username_too_long(client):
    """Test that username over 50 chars is rejected."""
    response = client.post(
        "/api/auth/register",
        json={"username": "a" * 51, "password": "password123"}
    )

    assert response.status_code == 422


def test_register_password_too_short(client):
    """Test that password under 8 chars is rejected."""
    response = client.post(
        "/api/auth/register",
        json={"username": "validuser", "password": "short"}
    )

    assert response.status_code == 422


def test_register_missing_username(client):
    """Test that missing username is rejected."""
    response = client.post(
        "/api/auth/register",
        json={"password": "password123"}
    )

    assert response.status_code == 422


def test_register_missing_password(client):
    """Test that missing password is rejected."""
    response = client.post(
        "/api/auth/register",
        json={"username": "validuser"}
    )

    assert response.status_code == 422


def test_login_success(client, create_user):
    """Test successful login returns token and user info."""
    user = create_user("loginuser", "password123", is_anonymous=False)

    response = client.post(
        "/api/auth/login",
        json={"username": "loginuser", "password": "password123"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == user.id
    assert data["username"] == "loginuser"
    assert "token" in data


def test_login_returns_valid_jwt(client, create_user):
    """Test that login returns a valid JWT token."""
    user = create_user("jwtlogin", "password123", is_anonymous=True)

    response = client.post(
        "/api/auth/login",
        json={"username": "jwtlogin", "password": "password123"},
    )

    assert response.status_code == 200
    data = response.json()

    payload = decode_access_token(data["token"])
    assert payload.user_id == user.id
    assert payload.username == "jwtlogin"
    assert payload.is_anonymous is True


def test_login_invalid_password(client, create_user):
    """Test that invalid password returns 401."""
    create_user("badpass", "password123")

    response = client.post(
        "/api/auth/login",
        json={"username": "badpass", "password": "wrongpass"},
    )

    assert response.status_code == 401
    assert "Invalid credentials" in response.json()["detail"]


def test_login_unknown_user(client):
    """Test that unknown user returns 401."""
    response = client.post(
        "/api/auth/login",
        json={"username": "missing", "password": "password123"},
    )

    assert response.status_code == 401
    assert "Invalid credentials" in response.json()["detail"]


def test_register_unicode_username(client):
    """Test that unicode usernames are accepted."""
    response = client.post(
        "/api/auth/register",
        json={"username": "用户名测试", "password": "password123"}
    )

    assert response.status_code == 201
    assert response.json()["username"] == "用户名测试"


def test_register_special_chars_username(client):
    """Test that special characters in username are accepted."""
    response = client.post(
        "/api/auth/register",
        json={"username": "user@test.com", "password": "password123"}
    )

    assert response.status_code == 201
    assert response.json()["username"] == "user@test.com"


# --- POST /api/auth/claim ---


def _register_anonymous(client) -> dict:
    """Helper: register an anonymous user and return the response data."""
    resp = client.post(
        "/api/auth/register",
        json={"username": "anon_temp", "password": "password123"},
    )
    assert resp.status_code == 201
    return resp.json()


def test_claim_success(client, db_session):
    """Test upgrading an anonymous account to a claimed account."""
    reg = _register_anonymous(client)

    response = client.post(
        "/api/auth/claim",
        json={"new_username": "realuser", "new_password": "newpass123"},
        headers={"Authorization": f"Bearer {reg['token']}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "realuser"
    assert data["user_id"] == reg["user_id"]
    assert "token" in data

    # Verify DB was updated
    user = db_session.query(User).filter(User.id == reg["user_id"]).first()
    assert user.username == "realuser"
    assert user.is_anonymous is False


def test_claim_returns_valid_jwt(client):
    """Test that claim returns a JWT with is_anonymous=False."""
    reg = _register_anonymous(client)

    response = client.post(
        "/api/auth/claim",
        json={"new_username": "claimed", "new_password": "newpass123"},
        headers={"Authorization": f"Bearer {reg['token']}"},
    )

    assert response.status_code == 200
    payload = decode_access_token(response.json()["token"])
    assert payload.username == "claimed"
    assert payload.is_anonymous is False


def test_claim_already_claimed(client):
    """Test that claiming an already-claimed account returns 409."""
    reg = _register_anonymous(client)

    # First claim succeeds
    resp1 = client.post(
        "/api/auth/claim",
        json={"new_username": "first", "new_password": "newpass123"},
        headers={"Authorization": f"Bearer {reg['token']}"},
    )
    assert resp1.status_code == 200

    # Second claim with original token fails (user is no longer anonymous)
    resp2 = client.post(
        "/api/auth/claim",
        json={"new_username": "second", "new_password": "newpass123"},
        headers={"Authorization": f"Bearer {reg['token']}"},
    )
    assert resp2.status_code == 409
    assert "already claimed" in resp2.json()["detail"]


def test_claim_username_taken(client):
    """Test that claiming with an existing username returns 409."""
    # Register two anonymous users
    reg1 = _register_anonymous(client)
    client.post(
        "/api/auth/register",
        json={"username": "taken_name", "password": "password123"},
    )

    response = client.post(
        "/api/auth/claim",
        json={"new_username": "taken_name", "new_password": "newpass123"},
        headers={"Authorization": f"Bearer {reg1['token']}"},
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_claim_no_token(client):
    """Test that claim without Authorization header returns 422."""
    response = client.post(
        "/api/auth/claim",
        json={"new_username": "notoken", "new_password": "newpass123"},
    )

    assert response.status_code == 422


def test_claim_invalid_token(client):
    """Test that claim with invalid token returns 401."""
    response = client.post(
        "/api/auth/claim",
        json={"new_username": "badtoken", "new_password": "newpass123"},
        headers={"Authorization": "Bearer invalid.jwt.token"},
    )

    assert response.status_code == 401


def test_claim_username_too_short(client):
    """Test that new_username under 3 chars is rejected."""
    reg = _register_anonymous(client)

    response = client.post(
        "/api/auth/claim",
        json={"new_username": "ab", "new_password": "newpass123"},
        headers={"Authorization": f"Bearer {reg['token']}"},
    )

    assert response.status_code == 422


def test_claim_password_too_short(client):
    """Test that new_password under 8 chars is rejected."""
    reg = _register_anonymous(client)

    response = client.post(
        "/api/auth/claim",
        json={"new_username": "validname", "new_password": "short"},
        headers={"Authorization": f"Bearer {reg['token']}"},
    )

    assert response.status_code == 422


def test_claim_can_login_with_new_credentials(client):
    """Test that after claiming, the user can login with new credentials."""
    reg = _register_anonymous(client)

    client.post(
        "/api/auth/claim",
        json={"new_username": "newlogin", "new_password": "newpass123"},
        headers={"Authorization": f"Bearer {reg['token']}"},
    )

    login_resp = client.post(
        "/api/auth/login",
        json={"username": "newlogin", "password": "newpass123"},
    )

    assert login_resp.status_code == 200
    assert login_resp.json()["user_id"] == reg["user_id"]
