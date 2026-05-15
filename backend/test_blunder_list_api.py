from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from conftest import engine

from app.models import Blunder, BlunderReview, GameSession, Position


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
    created_at: datetime | None = None,
    source_session_id: uuid.UUID | None = None,
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

    blunder_kwargs = {
        "user_id": user_id,
        "position_id": position.id,
        "bad_move_san": bad_move,
        "best_move_san": best_move,
        "eval_loss_cp": eval_loss_cp,
        "pass_streak": pass_streak,
        "last_reviewed_at": last_reviewed_at,
        "source_session_id": source_session_id,
    }
    if created_at is not None:
        blunder_kwargs["created_at"] = created_at
    blunder = Blunder(**blunder_kwargs)
    db_session.add(blunder)
    db_session.commit()
    db_session.refresh(blunder)
    return blunder


def _create_session(
    db_session,
    *,
    user_id: int = 123,
    ended_at: datetime | None = None,
) -> GameSession:
    session = GameSession(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=(ended_at or datetime.now(timezone.utc)) - timedelta(minutes=30),
        ended_at=ended_at,
        status="completed",
        result="draw",
        engine_elo=1500,
        player_color="white",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    return session


def test_list_blunders_returns_all_for_user(client, auth_headers, db_session):
    _create_blunder(db_session, user_id=123, fen_hash_suffix="a")
    _create_blunder(db_session, user_id=123, fen_hash_suffix="b")
    _create_blunder(db_session, user_id=999, fen_hash_suffix="c")  # other user

    response = client.get("/api/blunder", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 2
    assert data["total"] == 2
    assert data["due_total"] is None
    assert data["limit"] == 50
    assert data["offset"] == 0
    assert data["due"] is False


def test_list_blunders_empty(client, auth_headers):
    response = client.get("/api/blunder", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    assert response.json()["items"] == []


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
    assert len(data["items"]) == 1
    item = data["items"][0]
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
    # Overdue: pass_streak=0, last reviewed 8 hours ago (interval=4h, priority=2.0)
    _create_blunder(
        db_session,
        user_id=123,
        pass_streak=0,
        last_reviewed_at=now - timedelta(hours=8),
        fen_hash_suffix="due",
    )
    # Not due: pass_streak=5, last reviewed 1 hour ago (interval=128h, priority≈0.008)
    _create_blunder(
        db_session,
        user_id=123,
        pass_streak=5,
        last_reviewed_at=now - timedelta(hours=1),
        fen_hash_suffix="notdue",
    )

    # Without filter: both returned
    response = client.get("/api/blunder", headers=auth_headers(user_id=123))
    assert len(response.json()["items"]) == 2

    # With due=true: only the overdue one
    response = client.get("/api/blunder?due=true", headers=auth_headers(user_id=123))
    data = response.json()
    assert len(data["items"]) == 1
    assert data["total"] == 1
    assert data["due_total"] == 1
    assert data["items"][0]["srs_priority"] > 1.0


def test_list_blunders_paginates_with_stable_last_played_order(client, auth_headers, db_session):
    now = datetime.now(timezone.utc)
    newest = _create_session(db_session, ended_at=now - timedelta(hours=1))
    tied_a = _create_session(db_session, ended_at=now - timedelta(hours=2))
    tied_b = _create_session(db_session, ended_at=now - timedelta(hours=2))

    old = _create_blunder(
        db_session,
        user_id=123,
        created_at=now - timedelta(days=4),
        source_session_id=tied_a.id,
        fen_hash_suffix="old",
    )
    newer_tie = _create_blunder(
        db_session,
        user_id=123,
        created_at=now - timedelta(days=1),
        source_session_id=tied_b.id,
        fen_hash_suffix="newer-tie",
    )
    latest_played = _create_blunder(
        db_session,
        user_id=123,
        created_at=now - timedelta(days=3),
        source_session_id=newest.id,
        fen_hash_suffix="latest-played",
    )
    never_played = _create_blunder(
        db_session,
        user_id=123,
        created_at=now,
        fen_hash_suffix="never-played",
    )

    response = client.get("/api/blunder?limit=2&offset=0", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 4
    assert [item["id"] for item in data["items"]] == [latest_played.id, newer_tie.id]

    response = client.get("/api/blunder?limit=2&offset=2", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    data = response.json()
    assert [item["id"] for item in data["items"]] == [old.id, never_played.id]


def test_list_blunders_latest_review_tie_uses_highest_review_id(client, auth_headers, db_session):
    now = datetime.now(timezone.utc)
    blunder = _create_blunder(db_session, user_id=123, fen_hash_suffix="tie-review")
    older_session = _create_session(db_session, ended_at=now - timedelta(days=2))
    newer_session = _create_session(db_session, ended_at=now - timedelta(days=1))
    reviewed_at = now - timedelta(hours=1)
    db_session.add_all(
        [
            BlunderReview(
                blunder_id=blunder.id,
                session_id=older_session.id,
                reviewed_at=reviewed_at,
                passed=True,
                move_played_san="Nf3",
                eval_delta_cp=0,
            ),
            BlunderReview(
                blunder_id=blunder.id,
                session_id=newer_session.id,
                reviewed_at=reviewed_at,
                passed=True,
                move_played_san="Nf3",
                eval_delta_cp=0,
            ),
        ]
    )
    db_session.commit()

    response = client.get("/api/blunder", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["last_session_id"] == str(newer_session.id)
    assert item["last_played_at"].startswith(newer_session.ended_at.isoformat()[:19])


def test_list_blunders_due_filter_paginates_due_total(client, auth_headers, db_session):
    now = datetime.now(timezone.utc)
    due_ids = [
        _create_blunder(
            db_session,
            user_id=123,
            pass_streak=0,
            last_reviewed_at=now - timedelta(hours=8 + idx),
            created_at=now - timedelta(minutes=idx),
            fen_hash_suffix=f"due-{idx}",
        ).id
        for idx in range(3)
    ]
    _create_blunder(
        db_session,
        user_id=123,
        pass_streak=5,
        last_reviewed_at=now - timedelta(hours=1),
        fen_hash_suffix="not-due-page",
    )

    response = client.get(
        "/api/blunder?due=true&limit=2&offset=1",
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert data["due_total"] == 3
    assert len(data["items"]) == 2
    assert all(item["srs_priority"] > 1.0 for item in data["items"])
    assert {item["id"] for item in data["items"]}.issubset(set(due_ids))


def test_list_blunders_due_false_query_count_does_not_grow_per_blunder(
    client,
    auth_headers,
    db_session,
):
    def count_queries(count: int) -> int:
        for idx in range(count):
            _create_blunder(
                db_session,
                user_id=123,
                fen_hash_suffix=f"count-{count}-{idx}",
            )

        statements: list[str] = []

        def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _capture)
        try:
            response = client.get(
                "/api/blunder?limit=50",
                headers=auth_headers(user_id=123),
            )
            assert response.status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", _capture)

        return len(statements)

    five_count = count_queries(5)

    for table in [BlunderReview, Blunder, Position, GameSession]:
        db_session.query(table).delete()
    db_session.commit()

    twenty_five_count = count_queries(25)
    assert twenty_five_count <= five_count + 3


def test_list_blunders_requires_auth(client):
    response = client.get("/api/blunder")
    assert response.status_code == 401
