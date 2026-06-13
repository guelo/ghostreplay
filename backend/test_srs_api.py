from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.models import Blunder, Position
from conftest import pg_required


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


def test_srs_review_succeeds_when_opening_cache_refresh_fails(
    client,
    auth_headers,
    create_game_session,
    db_session,
):
    session_id = create_game_session(user_id=123, player_color="white")
    blunder = _create_blunder(db_session, user_id=123)

    from app.opening_score_scheduler import request_recompute as real_request_recompute

    with patch("app.api.srs.request_recompute", real_request_recompute), patch(
        "app.opening_score_scheduler.OpeningScoreScheduler.request_recompute",
        side_effect=RuntimeError("boom"),
    ):
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
    db_session.expire_all()
    updated_blunder = db_session.query(Blunder).filter(Blunder.id == blunder.id).first()
    assert updated_blunder is not None
    assert updated_blunder.pass_streak == 1
    review_count = db_session.execute(
        text("SELECT COUNT(*) FROM blunder_reviews WHERE blunder_id = :blunder_id"),
        {"blunder_id": blunder.id},
    ).scalar_one()
    assert review_count == 1


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


def _review_count(db_session, blunder_id: int) -> int:
    return db_session.execute(
        text("SELECT COUNT(*) FROM blunder_reviews WHERE blunder_id = :b"),
        {"b": blunder_id},
    ).scalar_one()


def test_srs_review_keyless_duplicates_are_not_deduped(
    client, auth_headers, create_game_session, db_session
):
    """Two keyless reviews of the same blunder produce two rows and increment
    pass_streak twice — the partial unique index excludes NULL keys."""
    session_id = create_game_session(user_id=123, player_color="white")
    blunder = _create_blunder(db_session, user_id=123, pass_streak=0)

    body = {
        "session_id": session_id,
        "blunder_id": blunder.id,
        "passed": True,
        "user_move": "Nf3",
        "eval_delta": 20,
    }
    first = client.post("/api/srs/review", json=body, headers=auth_headers(user_id=123))
    second = client.post("/api/srs/review", json=body, headers=auth_headers(user_id=123))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["pass_streak"] == 1
    assert second.json()["pass_streak"] == 2
    assert _review_count(db_session, blunder.id) == 2


def test_srs_review_idempotency_key_dedupes(
    client, auth_headers, create_game_session, db_session
):
    """Two reviews with the same idempotency key yield one row and a single
    pass_streak increment; the second call echoes the first outcome."""
    session_id = create_game_session(user_id=123, player_color="white")
    blunder = _create_blunder(db_session, user_id=123, pass_streak=0)

    body = {
        "session_id": session_id,
        "blunder_id": blunder.id,
        "passed": True,
        "user_move": "Nf3",
        "eval_delta": 20,
        "idempotency_key": "srs-key-1",
    }
    first = client.post("/api/srs/review", json=body, headers=auth_headers(user_id=123))
    second = client.post("/api/srs/review", json=body, headers=auth_headers(user_id=123))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["pass_streak"] == 1
    assert second.json()["pass_streak"] == 1
    assert first.json()["blunder_id"] == second.json()["blunder_id"]
    assert _review_count(db_session, blunder.id) == 1


def test_srs_review_retry_echoes_original_after_later_review(
    client, auth_headers, create_game_session, db_session
):
    """A retry of key A AFTER an intervening review (key B) echoes A's ORIGINAL
    response — not the current (B-mutated) state — and does not re-apply."""
    session_id = create_game_session(user_id=123, player_color="white")
    blunder = _create_blunder(db_session, user_id=123, pass_streak=0)

    def review(key: str):
        return client.post(
            "/api/srs/review",
            json={
                "session_id": session_id,
                "blunder_id": blunder.id,
                "passed": True,
                "user_move": "Nf3",
                "eval_delta": 20,
                "idempotency_key": key,
            },
            headers=auth_headers(user_id=123),
        )

    first = review("key-A")
    assert first.status_code == 200
    assert first.json()["pass_streak"] == 1
    first_body = first.json()

    second = review("key-B")
    assert second.status_code == 200
    assert second.json()["pass_streak"] == 2

    retry = review("key-A")
    assert retry.status_code == 200
    # Exact original response, NOT the current (streak-2) state.
    assert retry.json() == first_body

    # No extra row, and the blunder is untouched by the retry (still streak 2).
    assert _review_count(db_session, blunder.id) == 2
    db_session.expire_all()
    refreshed = db_session.query(Blunder).filter(Blunder.id == blunder.id).first()
    assert refreshed is not None
    assert refreshed.pass_streak == 2


def _pg_seed_blunder(pg_session_factory, *, user_id: int) -> int:
    db = pg_session_factory()
    try:
        position = Position(
            user_id=user_id,
            fen_hash=f"pg-fen-{user_id}",
            fen_raw="8/8/8/8/8/8/8/8 w - - 0 1",
            active_color="white",
        )
        db.add(position)
        db.flush()
        blunder = Blunder(
            user_id=user_id,
            position_id=position.id,
            bad_move_san="Qh5",
            best_move_san="Nf3",
            eval_loss_cp=120,
            pass_streak=0,
        )
        db.add(blunder)
        db.commit()
        return blunder.id
    finally:
        db.close()


@pg_required
def test_srs_review_concurrent_same_key_single_row(
    pg_client, pg_session_factory, auth_headers
):
    """Under real row locks, two concurrent reviews with the same key serialize:
    exactly one BlunderReview row and a single pass_streak increment."""
    start = pg_client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=123),
    )
    assert start.status_code == 201
    session_id = start.json()["session_id"]
    blunder_id = _pg_seed_blunder(pg_session_factory, user_id=123)

    body = {
        "session_id": session_id,
        "blunder_id": blunder_id,
        "passed": True,
        "user_move": "Nf3",
        "eval_delta": 20,
        "idempotency_key": "concurrent-srs-key",
    }

    def _post():
        return pg_client.post(
            "/api/srs/review", json=body, headers=auth_headers(user_id=123)
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        responses = [f.result() for f in [pool.submit(_post), pool.submit(_post)]]

    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["pass_streak"] == 1 for r in responses)

    verify = pg_session_factory()
    try:
        count = verify.execute(
            text("SELECT COUNT(*) FROM blunder_reviews WHERE blunder_id = :b"),
            {"b": blunder_id},
        ).scalar_one()
        streak = verify.execute(
            text("SELECT pass_streak FROM blunders WHERE id = :b"),
            {"b": blunder_id},
        ).scalar_one()
    finally:
        verify.close()
    assert count == 1
    assert streak == 1
