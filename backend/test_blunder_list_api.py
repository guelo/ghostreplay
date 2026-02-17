from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import Blunder, Position


def _create_blunder(
    db_session,
    *,
    user_id: int,
    fen: str = "8/8/8/8/8/8/8/8 w - - 0 1",
    bad_move: str = "Qh5",
    best_move: str = "Nf3",
    eval_loss_cp: int = 120,
    pass_streak: int = 0,
    last_reviewed_at: datetime | None = None,
    fen_hash_suffix: str = "",
) -> Blunder:
    position = Position(
        user_id=user_id,
        fen_hash=f"hash-{user_id}-{fen_hash_suffix or id(object())}",
        fen_raw=fen,
        active_color="white",
    )
    db_session.add(position)
    db_session.flush()

    blunder = Blunder(
        user_id=user_id,
        position_id=position.id,
        bad_move_san=bad_move,
        best_move_san=best_move,
        eval_loss_cp=eval_loss_cp,
        pass_streak=pass_streak,
        last_reviewed_at=last_reviewed_at,
    )
    db_session.add(blunder)
    db_session.commit()
    db_session.refresh(blunder)
    return blunder


def test_list_blunders_returns_all_for_user(client, auth_headers, db_session):
    _create_blunder(db_session, user_id=123, fen_hash_suffix="a")
    _create_blunder(db_session, user_id=123, fen_hash_suffix="b")
    _create_blunder(db_session, user_id=999, fen_hash_suffix="c")  # other user

    response = client.get("/api/blunder", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2


def test_list_blunders_empty(client, auth_headers):
    response = client.get("/api/blunder", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    assert response.json() == []


def test_list_blunders_includes_expected_fields(client, auth_headers, db_session):
    now = datetime.now(timezone.utc)
    _create_blunder(
        db_session,
        user_id=123,
        fen="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        bad_move="d5",
        best_move="e5",
        eval_loss_cp=200,
        pass_streak=3,
        last_reviewed_at=now - timedelta(hours=1),
        fen_hash_suffix="fields",
    )

    response = client.get("/api/blunder", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    item = data[0]
    assert item["fen"] == "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    assert item["bad_move"] == "d5"
    assert item["best_move"] == "e5"
    assert item["eval_loss_cp"] == 200
    assert item["pass_streak"] == 3
    assert item["last_reviewed_at"] is not None
    assert item["created_at"] is not None
    assert isinstance(item["srs_priority"], float)


def test_list_blunders_due_filter(client, auth_headers, db_session):
    now = datetime.now(timezone.utc)
    # Overdue: pass_streak=0, last reviewed 2 hours ago (interval=1h, priority=2.0)
    _create_blunder(
        db_session,
        user_id=123,
        pass_streak=0,
        last_reviewed_at=now - timedelta(hours=2),
        fen_hash_suffix="due",
    )
    # Not due: pass_streak=5, last reviewed 1 hour ago (interval=32h, priority≈0.03)
    _create_blunder(
        db_session,
        user_id=123,
        pass_streak=5,
        last_reviewed_at=now - timedelta(hours=1),
        fen_hash_suffix="notdue",
    )

    # Without filter: both returned
    response = client.get("/api/blunder", headers=auth_headers(user_id=123))
    assert len(response.json()) == 2

    # With due=true: only the overdue one
    response = client.get("/api/blunder?due=true", headers=auth_headers(user_id=123))
    data = response.json()
    assert len(data) == 1
    assert data[0]["srs_priority"] > 1.0


def test_list_blunders_sorted_by_priority_descending(client, auth_headers, db_session):
    now = datetime.now(timezone.utc)
    # Low priority
    _create_blunder(
        db_session,
        user_id=123,
        pass_streak=3,
        last_reviewed_at=now - timedelta(hours=1),
        fen_hash_suffix="low",
    )
    # High priority
    _create_blunder(
        db_session,
        user_id=123,
        pass_streak=0,
        last_reviewed_at=now - timedelta(hours=10),
        fen_hash_suffix="high",
    )

    response = client.get("/api/blunder", headers=auth_headers(user_id=123))
    data = response.json()
    assert len(data) == 2
    assert data[0]["srs_priority"] >= data[1]["srs_priority"]


def test_list_blunders_requires_auth(client):
    response = client.get("/api/blunder")
    assert response.status_code == 401
