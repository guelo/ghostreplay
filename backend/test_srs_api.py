from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.models import Blunder, Position


def _create_blunder(
    db_session,
    *,
    user_id: int,
    pass_streak: int = 0,
    last_reviewed_at: datetime | None = None,
) -> Blunder:
    position = Position(
        user_id=user_id,
        fen_hash=f"fen-hash-{user_id}-{pass_streak}",
        fen_raw="8/8/8/8/8/8/8/8 w - - 0 1",
        active_color="white",
    )
    db_session.add(position)
    db_session.flush()

    blunder = Blunder(
        user_id=user_id,
        position_id=position.id,
        bad_move_san="Qh5",
        best_move_san="Nf3",
        eval_loss_cp=120,
        pass_streak=pass_streak,
        last_reviewed_at=last_reviewed_at,
    )
    db_session.add(blunder)
    db_session.commit()
    db_session.refresh(blunder)
    return blunder


def test_srs_review_pass_increments_streak_and_logs_review(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123, player_color="white")
    blunder = _create_blunder(
        db_session,
        user_id=123,
        pass_streak=1,
        last_reviewed_at=datetime.now(timezone.utc) - timedelta(hours=6),
    )

    response = client.post(
        "/api/srs/review",
        json={
            "session_id": session_id,
            "blunder_id": blunder.id,
            "passed": True,
            "user_move": "Nf3",
            "eval_delta": 20,
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["blunder_id"] == blunder.id
    assert data["pass_streak"] == 2
    assert data["priority"] == 0.0
    assert "next_expected_review" in data

    db_session.expire_all()
    updated_blunder = db_session.query(Blunder).filter(Blunder.id == blunder.id).first()
    assert updated_blunder is not None
    assert updated_blunder.pass_streak == 2
    assert updated_blunder.last_reviewed_at is not None

    review_row = db_session.execute(
        text("SELECT passed, move_played_san, eval_delta_cp FROM blunder_reviews WHERE blunder_id = :blunder_id"),
        {"blunder_id": blunder.id},
    ).fetchone()
    assert review_row is not None
    assert review_row[0] == 1
    assert review_row[1] == "Nf3"
    assert review_row[2] == 20


def test_srs_review_fail_resets_streak(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123, player_color="white")
    blunder = _create_blunder(
        db_session,
        user_id=123,
        pass_streak=4,
        last_reviewed_at=datetime.now(timezone.utc) - timedelta(days=3),
    )

    response = client.post(
        "/api/srs/review",
        json={
            "session_id": session_id,
            "blunder_id": blunder.id,
            "passed": False,
            "user_move": "Qh5",
            "eval_delta": 170,
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["pass_streak"] == 0
    assert data["priority"] == 0.0

    db_session.expire_all()
    updated_blunder = db_session.query(Blunder).filter(Blunder.id == blunder.id).first()
    assert updated_blunder is not None
    assert updated_blunder.pass_streak == 0


def test_srs_review_session_not_found(client, auth_headers, db_session):
    blunder = _create_blunder(db_session, user_id=123)

    response = client.post(
        "/api/srs/review",
        json={
            "session_id": "00000000-0000-0000-0000-000000000000",
            "blunder_id": blunder.id,
            "passed": True,
            "user_move": "Nf3",
            "eval_delta": 10,
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 404
    assert "session" in response.json()["detail"].lower()


def test_srs_review_forbidden_for_other_users_session(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=999, player_color="white")
    blunder = _create_blunder(db_session, user_id=123)

    response = client.post(
        "/api/srs/review",
        json={
            "session_id": session_id,
            "blunder_id": blunder.id,
            "passed": True,
            "user_move": "Nf3",
            "eval_delta": 10,
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 403
    assert "not authorized" in response.json()["detail"].lower()


def test_srs_review_blunder_not_found(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        "/api/srs/review",
        json={
            "session_id": session_id,
            "blunder_id": 999999,
            "passed": True,
            "user_move": "Nf3",
            "eval_delta": 10,
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 404
    assert "blunder" in response.json()["detail"].lower()
