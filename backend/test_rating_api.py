import uuid
from datetime import datetime, timedelta, timezone

from app.models import RatingHistory
from app.rating import DEFAULT_RATING


def _end_game(client, auth_headers, session_id, user_id=123, result="checkmate_win"):
    response = client.post(
        "/api/game/end",
        json={"session_id": session_id, "result": result, "pgn": "1. e4 e5"},
        headers=auth_headers(user_id=user_id),
    )
    assert response.status_code == 200


def _insert_rating(db_session, *, user_id, game_session_id, rating, is_provisional, games_played, recorded_at=None):
    if recorded_at is None:
        recorded_at = datetime.now(timezone.utc)
    row = RatingHistory(
        user_id=user_id,
        game_session_id=uuid.UUID(game_session_id),
        rating=rating,
        is_provisional=is_provisional,
        games_played=games_played,
        recorded_at=recorded_at,
    )
    db_session.add(row)
    db_session.commit()
    return row


# --- GET /api/stats/current-rating ---


def test_current_rating_empty(client, auth_headers):
    response = client.get("/api/stats/current-rating", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    data = response.json()
    assert data == {
        "current_rating": DEFAULT_RATING,
        "is_provisional": True,
        "games_played": 0,
    }


def test_current_rating_returns_latest(client, auth_headers, create_game_session, db_session):
    now = datetime.now(timezone.utc)
    s1 = create_game_session(user_id=123)
    s2 = create_game_session(user_id=123)

    _insert_rating(
        db_session, user_id=123, game_session_id=s1,
        rating=1220, is_provisional=True, games_played=1,
        recorded_at=now - timedelta(hours=2),
    )
    _insert_rating(
        db_session, user_id=123, game_session_id=s2,
        rating=1245, is_provisional=True, games_played=2,
        recorded_at=now - timedelta(hours=1),
    )

    response = client.get("/api/stats/current-rating", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    data = response.json()
    assert data["current_rating"] == 1245
    assert data["games_played"] == 2
    assert data["is_provisional"] is True


def test_current_rating_user_isolation(client, auth_headers, create_game_session, db_session):
    """User A's rating should not leak to User B."""
    now = datetime.now(timezone.utc)
    s1 = create_game_session(user_id=123)

    _insert_rating(
        db_session, user_id=123, game_session_id=s1,
        rating=1400, is_provisional=False, games_played=25,
        recorded_at=now,
    )

    response = client.get("/api/stats/current-rating", headers=auth_headers(user_id=999))
    assert response.status_code == 200
    data = response.json()
    assert data["current_rating"] == DEFAULT_RATING
    assert data["games_played"] == 0


# --- GET /api/stats/rating-history ---


def test_rating_history_empty(client, auth_headers):
    response = client.get("/api/stats/rating-history", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    data = response.json()
    assert data == {
        "ratings": [],
        "current_rating": DEFAULT_RATING,
        "games_played": 0,
    }


def test_rating_history_response_shape(client, auth_headers, create_game_session, db_session):
    now = datetime.now(timezone.utc)
    s1 = create_game_session(user_id=123)

    _insert_rating(
        db_session, user_id=123, game_session_id=s1,
        rating=1230, is_provisional=True, games_played=1,
        recorded_at=now,
    )

    response = client.get("/api/stats/rating-history", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    data = response.json()

    assert data["current_rating"] == 1230
    assert data["games_played"] == 1
    assert len(data["ratings"]) == 1

    point = data["ratings"][0]
    assert set(point.keys()) == {"timestamp", "rating", "is_provisional", "game_session_id"}
    assert point["rating"] == 1230
    assert point["is_provisional"] is True
    assert point["game_session_id"] == s1


def test_rating_history_ordered_ascending(client, auth_headers, create_game_session, db_session):
    now = datetime.now(timezone.utc)
    s1 = create_game_session(user_id=123)
    s2 = create_game_session(user_id=123)
    s3 = create_game_session(user_id=123)

    for i, (sid, rating) in enumerate([(s1, 1200), (s2, 1220), (s3, 1210)]):
        _insert_rating(
            db_session, user_id=123, game_session_id=sid,
            rating=rating, is_provisional=True, games_played=i + 1,
            recorded_at=now - timedelta(hours=3 - i),
        )

    response = client.get("/api/stats/rating-history", headers=auth_headers(user_id=123))
    data = response.json()

    ratings = [p["rating"] for p in data["ratings"]]
    assert ratings == [1200, 1220, 1210]
    assert data["current_rating"] == 1210  # latest by recorded_at


def test_rating_history_range_7d(client, auth_headers, create_game_session, db_session):
    now = datetime.now(timezone.utc)
    old = create_game_session(user_id=123)
    recent = create_game_session(user_id=123)

    _insert_rating(
        db_session, user_id=123, game_session_id=old,
        rating=1180, is_provisional=True, games_played=1,
        recorded_at=now - timedelta(days=10),
    )
    _insert_rating(
        db_session, user_id=123, game_session_id=recent,
        rating=1250, is_provisional=True, games_played=2,
        recorded_at=now - timedelta(days=2),
    )

    response = client.get("/api/stats/rating-history?range=7d", headers=auth_headers(user_id=123))
    data = response.json()

    assert len(data["ratings"]) == 1
    assert data["ratings"][0]["rating"] == 1250
    # current_rating is always the latest regardless of filter
    assert data["current_rating"] == 1250


def test_rating_history_range_all(client, auth_headers, create_game_session, db_session):
    now = datetime.now(timezone.utc)
    old = create_game_session(user_id=123)
    recent = create_game_session(user_id=123)

    _insert_rating(
        db_session, user_id=123, game_session_id=old,
        rating=1180, is_provisional=True, games_played=1,
        recorded_at=now - timedelta(days=100),
    )
    _insert_rating(
        db_session, user_id=123, game_session_id=recent,
        rating=1250, is_provisional=True, games_played=2,
        recorded_at=now - timedelta(days=2),
    )

    response = client.get("/api/stats/rating-history?range=all", headers=auth_headers(user_id=123))
    data = response.json()
    assert len(data["ratings"]) == 2


def test_rating_history_invalid_range(client, auth_headers):
    response = client.get("/api/stats/rating-history?range=15d", headers=auth_headers(user_id=123))
    assert response.status_code == 422


def test_rating_history_user_isolation(client, auth_headers, create_game_session, db_session):
    now = datetime.now(timezone.utc)
    s1 = create_game_session(user_id=123)

    _insert_rating(
        db_session, user_id=123, game_session_id=s1,
        rating=1300, is_provisional=False, games_played=25,
        recorded_at=now,
    )

    response = client.get("/api/stats/rating-history", headers=auth_headers(user_id=999))
    data = response.json()
    assert data["ratings"] == []
    assert data["current_rating"] == DEFAULT_RATING
