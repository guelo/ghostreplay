from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from conftest import engine

from app.models import (
    Blunder,
    BlunderOpportunityEvent,
    BlunderReview,
    GameSession,
    OpponentDecision,
    Position,
)


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
    opening_family: str | None = None,
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
        "opening_family": opening_family,
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


def _add_review(
    db_session,
    *,
    blunder_id: int,
    session_id: uuid.UUID,
    passed: bool,
    reviewed_at: datetime,
    move_played_san: str = "Nf3",
    eval_delta_cp: int = 0,
) -> BlunderReview:
    review = BlunderReview(
        blunder_id=blunder_id,
        session_id=session_id,
        reviewed_at=reviewed_at,
        passed=passed,
        move_played_san=move_played_san,
        eval_delta_cp=eval_delta_cp,
    )
    db_session.add(review)
    db_session.commit()
    db_session.refresh(review)
    return review


def test_list_blunders_review_counters_latest_passed(client, auth_headers, db_session):
    now = datetime.now(timezone.utc)
    session = _create_session(db_session, ended_at=now - timedelta(days=1))
    blunder = _create_blunder(db_session, user_id=123, fen_hash_suffix="rc-pass")
    _add_review(db_session, blunder_id=blunder.id, session_id=session.id, passed=True, reviewed_at=now - timedelta(hours=3))
    _add_review(db_session, blunder_id=blunder.id, session_id=session.id, passed=False, reviewed_at=now - timedelta(hours=2))
    _add_review(db_session, blunder_id=blunder.id, session_id=session.id, passed=True, reviewed_at=now - timedelta(hours=1))

    item = client.get("/api/blunder", headers=auth_headers(user_id=123)).json()["items"][0]
    assert item["review_count"] == 3
    assert item["pass_count"] == 2
    assert item["fail_count"] == 1
    assert item["last_result"] is True


def test_list_blunders_review_counters_latest_failed(client, auth_headers, db_session):
    now = datetime.now(timezone.utc)
    session = _create_session(db_session, ended_at=now - timedelta(days=1))
    blunder = _create_blunder(db_session, user_id=123, fen_hash_suffix="rc-fail")
    _add_review(db_session, blunder_id=blunder.id, session_id=session.id, passed=True, reviewed_at=now - timedelta(hours=2))
    _add_review(db_session, blunder_id=blunder.id, session_id=session.id, passed=False, reviewed_at=now - timedelta(hours=1))

    item = client.get("/api/blunder", headers=auth_headers(user_id=123)).json()["items"][0]
    assert item["review_count"] == 2
    assert item["pass_count"] == 1
    assert item["fail_count"] == 1
    assert item["last_result"] is False


def test_list_blunders_review_counters_never_reviewed(client, auth_headers, db_session):
    _create_blunder(db_session, user_id=123, fen_hash_suffix="rc-none")

    item = client.get("/api/blunder", headers=auth_headers(user_id=123)).json()["items"][0]
    assert item["review_count"] == 0
    assert item["pass_count"] == 0
    assert item["fail_count"] == 0
    assert item["last_result"] is None


def test_list_blunders_review_counters_tie_uses_highest_review_id(client, auth_headers, db_session):
    now = datetime.now(timezone.utc)
    older_session = _create_session(db_session, ended_at=now - timedelta(days=2))
    newer_session = _create_session(db_session, ended_at=now - timedelta(days=1))
    blunder = _create_blunder(db_session, user_id=123, fen_hash_suffix="rc-tie")
    reviewed_at = now - timedelta(hours=1)
    # Same timestamp, differing passed: higher review id (inserted second) wins.
    _add_review(db_session, blunder_id=blunder.id, session_id=older_session.id, passed=True, reviewed_at=reviewed_at)
    _add_review(db_session, blunder_id=blunder.id, session_id=newer_session.id, passed=False, reviewed_at=reviewed_at)

    item = client.get("/api/blunder", headers=auth_headers(user_id=123)).json()["items"][0]
    assert item["review_count"] == 2
    assert item["last_result"] is False


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
    assert data["practice_ready"] is False


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
        opening_family="Italian Game",
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
    assert item["opening_family"] == "Italian Game"
    assert item["pass_streak"] == 3
    assert item["last_reviewed_at"] is not None
    assert item["created_at"] is not None
    assert isinstance(item["srs_priority"], float)


def test_list_blunders_normalizes_eval_loss_cp_at_response_time(client, auth_headers, db_session):
    """Displayed eval_loss_cp is clamped to 0..1000 in the response projection while
    the DB row keeps the RAW value (response-time normalization only, no migration)."""
    over_cap = _create_blunder(
        db_session,
        user_id=123,
        fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        bad_move="Qh5",
        eval_loss_cp=10000,
        fen_hash_suffix="raw-over-cap",
    )
    negative = _create_blunder(
        db_session,
        user_id=123,
        fen="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        bad_move="d5",
        eval_loss_cp=-40,
        fen_hash_suffix="raw-negative",
    )

    response = client.get("/api/blunder", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()["items"]}
    assert set(items) == {over_cap.id, negative.id}

    # Displayed value normalized to 0..1000 at response time.
    assert items[over_cap.id]["eval_loss_cp"] == 1000
    assert items[negative.id]["eval_loss_cp"] == 0
    # Match by fen/bad_move as well to confirm the projection is per-row.
    assert items[over_cap.id]["fen"] == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    assert items[over_cap.id]["bad_move"] == "Qh5"
    assert items[negative.id]["bad_move"] == "d5"

    # Direct DB read still shows the RAW values — no migration, projection only.
    db_session.expire_all()
    assert db_session.get(Blunder, over_cap.id).eval_loss_cp == 10000
    assert db_session.get(Blunder, negative.id).eval_loss_cp == -40


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


def test_list_blunders_due_includes_high_opportunity_zero_reach(client, auth_headers, db_session):
    """A blunder with many ancestor opportunities but zero reaches must still appear
    in /api/blunder?due=true so explicit review remains possible, even though normal
    ghost steering suppresses it."""
    now = datetime.now(timezone.utc)
    blunder = _create_blunder(
        db_session,
        user_id=123,
        pass_streak=0,
        last_reviewed_at=None,
        created_at=now - timedelta(days=5),
        fen_hash_suffix="low-reach",
    )
    # 183 ancestor-only opportunity events with reached=False
    for idx in range(183):
        session = GameSession(
            id=uuid.uuid4(),
            user_id=123,
            started_at=now - timedelta(minutes=idx + 1),
            status="ended",
            engine_elo=1500,
            player_color="white",
        )
        db_session.add(session)
        db_session.flush()
        db_session.add(
            BlunderOpportunityEvent(
                blunder_id=blunder.id,
                session_id=session.id,
                occurred_at=now - timedelta(minutes=idx + 1),
                opportunity=True,
                reached=False,
            )
        )
    db_session.commit()

    response = client.get("/api/blunder?due=true", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    data = response.json()
    ids = {item["id"] for item in data["items"]}
    assert blunder.id in ids


def test_list_blunders_practice_ready_differs_from_srs_due(client, auth_headers, db_session):
    """A heavily-steered, zero-reach target is SRS due but not ghost-eligible,
    so it appears under due=true but not under practice_ready=true.

    Dueness is a property of BROAD evidence and eligibility a property of the
    TARGETED reach rate, which is exactly why one surface can keep a blunder the
    other drops (g-targeted-reach-rate).
    """
    now = datetime.now(timezone.utc)
    blunder = _create_blunder(
        db_session,
        user_id=123,
        pass_streak=0,
        last_reviewed_at=None,
        created_at=now - timedelta(days=5),
        fen_hash_suffix="due-not-ready",
    )
    for idx in range(183):
        session = GameSession(
            id=uuid.uuid4(),
            user_id=123,
            started_at=now - timedelta(minutes=idx + 1),
            status="ended",
            engine_elo=1500,
            player_color="white",
        )
        db_session.add(session)
        db_session.flush()
        db_session.add(
            BlunderOpportunityEvent(
                blunder_id=blunder.id,
                session_id=session.id,
                occurred_at=now - timedelta(minutes=idx + 1),
                opportunity=True,
                reached=False,
            )
        )
        db_session.add(
            OpponentDecision(
                decision_id=uuid.uuid4(),
                session_id=session.id,
                request_fingerprint=uuid.uuid4().hex,
                request_fen_hash="fen-hash",
                uci_history="[]",
                ply_before=0,
                served_at=now - timedelta(minutes=idx + 1),
                response_payload="{}",
                target_blunder_id=blunder.id,
            )
        )
    db_session.commit()

    due = client.get("/api/blunder?due=true", headers=auth_headers(user_id=123)).json()
    assert {item["id"] for item in due["items"]} == {blunder.id}
    assert due["due_total"] == 1
    assert due["items"][0]["srs_due"] is True
    assert due["items"][0]["ghost_eligible"] is False
    assert due["items"][0]["targeted_30d"] == 183
    assert due["items"][0]["targeted_reached_30d"] == 0

    ready = client.get(
        "/api/blunder?practice_ready=true", headers=auth_headers(user_id=123)
    ).json()
    assert ready["practice_ready"] is True
    assert ready["due_total"] is None
    assert blunder.id not in {item["id"] for item in ready["items"]}
    assert ready["practice_ready_total"] == 0


def test_list_blunders_exposes_practice_fields(client, auth_headers, db_session):
    now = datetime.now(timezone.utc)
    _create_blunder(
        db_session,
        user_id=123,
        pass_streak=0,
        last_reviewed_at=now - timedelta(hours=8),
        fen_hash_suffix="practice-fields",
    )
    data = client.get("/api/blunder", headers=auth_headers(user_id=123)).json()
    item = data["items"][0]
    assert isinstance(item["practice_priority_score"], float)
    assert isinstance(item["srs_due"], bool)
    assert isinstance(item["ghost_eligible"], bool)
    assert "reached_since_review" in item
    assert data["practice_ready_total"] is not None


def test_list_blunders_default_orders_by_practice_priority(client, auth_headers, db_session):
    """Default ordering follows practice_priority_score, not recency or srs_priority."""
    now = datetime.now(timezone.utc)
    # Identical age and no reviews, so urgency is equal; severity (eval loss) drives
    # the practice score. Higher eval loss must sort first regardless of recency.
    recent_session = _create_session(db_session, ended_at=now - timedelta(hours=1))
    low_severity = _create_blunder(
        db_session,
        user_id=123,
        created_at=now - timedelta(days=2),
        eval_loss_cp=60,
        source_session_id=recent_session.id,
        fen_hash_suffix="low-sev",
    )
    high_severity = _create_blunder(
        db_session,
        user_id=123,
        created_at=now - timedelta(days=2),
        eval_loss_cp=900,
        fen_hash_suffix="high-sev",
    )

    response = client.get("/api/blunder", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    ids = [item["id"] for item in data["items"]]
    # high_severity has a higher practice_priority_score even though low_severity
    # was played more recently.
    assert ids == [high_severity.id, low_severity.id]
    assert (
        data["items"][0]["practice_priority_score"]
        > data["items"][1]["practice_priority_score"]
    )


def test_list_blunders_recency_is_tiebreaker_only(client, auth_headers, db_session):
    """When practice scores tie, more recent last_played sorts first."""
    now = datetime.now(timezone.utc)
    newest = _create_session(db_session, ended_at=now - timedelta(hours=1))
    older = _create_session(db_session, ended_at=now - timedelta(hours=5))

    older_played = _create_blunder(
        db_session,
        user_id=123,
        created_at=now - timedelta(days=2),
        source_session_id=older.id,
        fen_hash_suffix="older-played",
    )
    latest_played = _create_blunder(
        db_session,
        user_id=123,
        created_at=now - timedelta(days=2),
        source_session_id=newest.id,
        fen_hash_suffix="latest-played",
    )

    response = client.get("/api/blunder", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    data = response.json()
    # Equal practice score → recency tiebreak puts latest_played first.
    assert [item["id"] for item in data["items"]] == [latest_played.id, older_played.id]


def test_list_blunders_latest_review_tie_uses_highest_review_id(client, auth_headers, db_session):
    now = datetime.now(timezone.utc)
    source_session = _create_session(db_session, ended_at=now - timedelta(days=3))
    blunder = _create_blunder(
        db_session,
        user_id=123,
        source_session_id=source_session.id,
        fen_hash_suffix="tie-review",
    )
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
    assert item["source_session_id"] == str(source_session.id)
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
