"""Serialize rated game-end chains (g-rating-serial).

These pin the behaviour that makes rated ``/api/game/end`` safe now that the
evidence-cursor bump is the transaction sink:

* a rated *scoring* end locks the users row FOR NO KEY UPDATE exactly once,
  reads the durable rating head under that lock, and inserts ``head + 1``;
* unrated and rated *non-scoring* ends issue no users query and write no rating;
* a missing users row fails closed with 500 and persists nothing;
* the rating insert and terminal update flush BEFORE the cursor bump, which is
  the final write before commit;
* ``latest_rating_order`` selects the games-played-first durable head, immune to
  application clock skew;
* under real Postgres concurrency the chain stays monotone, a double-end of one
  session yields exactly one rating row, and the bump-last regime keeps the
  cursor row writable by another transaction while an end is mid-rating.

The SQLite tests run everywhere; the row-lock/interleaving proofs are
``@pg_required`` and skip cleanly without a Postgres URL.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.dialects import postgresql

import app.api.game as game_api
from app.models import GameSession, RatingHistory, User
from app.opening_cache import bump_evidence_seq, current_evidence_seq
from app.rating_scores import latest_rating_order
from app.row_locks import for_no_key_update
from conftest import engine, pg_required
from sql_capture import capture_statements, cursor_last_before_commit


def _count_users_selects(statements: list[str]) -> int:
    """Statements that SELECT from the users table (the sanctioned users lock)."""
    return sum(1 for s in statements if s.lstrip().startswith("select") and "from users" in s)


def _start(client, auth_headers, user_id: int = 123) -> str:
    resp = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=user_id),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


# ---------------------------------------------------------------------------
# SQLite: users-lock accounting, lock shape, fail-closed, ordering, durable head.
# ---------------------------------------------------------------------------
def test_rated_scoring_end_takes_exactly_one_users_lock(client, auth_headers, db_session):
    """A rated scoring end issues exactly one users-row SELECT (the NKU lock) and
    writes exactly one rating row."""
    user_id = 4101
    headers = auth_headers(user_id=user_id)  # seeds the backing users row OUTSIDE the capture
    session_id = _start(client, auth_headers, user_id)

    with capture_statements() as log:
        resp = client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "checkmate_win", "pgn": "1. e4 e5", "is_rated": True},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    assert _count_users_selects(log.statements()) == 1
    assert db_session.query(RatingHistory).filter(RatingHistory.user_id == user_id).count() == 1


def test_unrated_end_takes_no_users_lock(client, auth_headers, db_session):
    """An unrated end never touches the users row and writes no rating."""
    user_id = 4102
    headers = auth_headers(user_id=user_id)
    session_id = _start(client, auth_headers, user_id)

    with capture_statements() as log:
        resp = client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "checkmate_win", "pgn": "1. e4 e5", "is_rated": False},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["rating"] is None
    assert _count_users_selects(log.statements()) == 0
    assert db_session.query(RatingHistory).filter(RatingHistory.user_id == user_id).count() == 0


def test_rated_nonscoring_end_takes_no_users_lock(client, auth_headers, db_session):
    """A rated but non-scoring end (abandon is not a rated outcome) issues no users
    query and writes no rating row — the users lock guards only the rating chain."""
    user_id = 4103
    headers = auth_headers(user_id=user_id)
    session_id = _start(client, auth_headers, user_id)

    with capture_statements() as log:
        resp = client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "abandon", "pgn": "1. e4 e5", "is_rated": True},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["rating"] is None
    assert _count_users_selects(log.statements()) == 0
    assert db_session.query(RatingHistory).filter(RatingHistory.user_id == user_id).count() == 0


def test_users_lock_compiles_as_for_no_key_update(db_session):
    """The users lock renders FOR NO KEY UPDATE on Postgres (never bare FOR UPDATE),
    the same shape ``end_game`` applies via ``for_no_key_update``."""
    query = for_no_key_update(db_session.query(User.id).filter(User.id == 123))
    sql = str(query.statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "FOR NO KEY UPDATE" in sql
    assert not sql.rstrip().endswith("FOR UPDATE")


def test_missing_users_row_fails_closed_with_500_and_no_rating(client, auth_headers, db_session):
    """A rated scoring end whose users row is missing is an invariant violation:
    500, no RatingHistory, and the terminal mutation rolls back (session stays
    active) because the rating work runs before commit."""
    user_id = 4104
    headers = auth_headers(user_id=user_id)  # seeds the row so /start succeeds
    session_id = _start(client, auth_headers, user_id)

    # Delete the users row to simulate the impossible-in-production missing user.
    db_session.query(User).filter(User.id == user_id).delete()
    db_session.commit()

    resp = client.post(
        "/api/game/end",
        json={"session_id": session_id, "result": "checkmate_win", "pgn": "1. e4 e5", "is_rated": True},
        headers=headers,
    )

    assert resp.status_code == 500, resp.text
    assert db_session.query(RatingHistory).filter(RatingHistory.user_id == user_id).count() == 0
    db_session.expire_all()
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.status == "active"  # fail-closed: no partial terminal write survived


def test_rating_and_terminal_writes_flush_before_cursor_which_is_last(client, auth_headers, db_session):
    """Statement ordering: the rating insert and terminal game_sessions update flush
    before the evidence-cursor bump, that bump is the transaction's FINAL statement,
    and it commits."""
    user_id = 4105
    headers = auth_headers(user_id=user_id)
    session_id = _start(client, auth_headers, user_id)
    seq_before = current_evidence_seq(db_session, user_id, "white")

    # A scoring end computes the opening-score delta post-commit, which lazily imports
    # request_recompute from its source module — past the autouse fixture's bound-alias
    # patches — so the real scheduler would start a worker thread against the
    # CONFIGURED (non-test) database.
    with patch("app.opening_score_scheduler.request_recompute"), \
         capture_statements() as log:
        resp = client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "checkmate_win", "pgn": "1. e4 e5", "is_rated": True},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    pre, cursor_idx = cursor_last_before_commit(log)
    rating_idx = next(i for i, s in enumerate(pre) if s.lstrip().startswith("insert into rating_history"))
    terminal_idx = next(i for i, s in enumerate(pre) if s.lstrip().startswith("update game_sessions"))

    assert rating_idx < cursor_idx, pre
    assert terminal_idx < cursor_idx, pre

    db_session.expire_all()
    assert current_evidence_seq(db_session, user_id, "white") == seq_before + 1


def test_clock_skew_durable_head_is_games_played_first(client, auth_headers, db_session):
    """The durable head is chosen games-played-first, so a row with MORE games but an
    EARLIER recorded_at (an application clock that moved backwards) still wins, and a
    new end chains off it. A recorded-at-first ordering would pick the wrong head."""
    user_id = 4106
    headers = auth_headers(user_id=user_id)
    now = datetime.now(timezone.utc)

    # Durable head: highest games_played, but recorded two hours in the PAST.
    db_session.add(RatingHistory(
        user_id=user_id, game_session_id=uuid.uuid4(), rating=1500,
        is_provisional=False, games_played=7, recorded_at=now - timedelta(hours=2),
    ))
    # Decoy: fewer games but the LATEST recorded_at.
    db_session.add(RatingHistory(
        user_id=user_id, game_session_id=uuid.uuid4(), rating=1300,
        is_provisional=False, games_played=4, recorded_at=now,
    ))
    db_session.commit()

    # Regression witness: recorded_at-first ordering picks the games_played=4 decoy.
    recorded_at_head = (
        db_session.query(RatingHistory)
        .filter(RatingHistory.user_id == user_id)
        .order_by(RatingHistory.recorded_at.desc())
        .first()
    )
    assert recorded_at_head.games_played == 4

    # The durable-head helper picks the games_played=7 row despite its older timestamp.
    durable_head = (
        db_session.query(RatingHistory)
        .filter(RatingHistory.user_id == user_id)
        .order_by(*latest_rating_order())
        .first()
    )
    assert durable_head.games_played == 7

    # A new rated end chains off the durable head: games_played 8, rating_before 1500.
    session_id = _start(client, auth_headers, user_id)
    resp = client.post(
        "/api/game/end",
        json={"session_id": session_id, "result": "checkmate_win", "pgn": "1. e4 e5", "is_rated": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rating"]["rating_before"] == 1500

    db_session.expire_all()
    new_head = (
        db_session.query(RatingHistory)
        .filter(RatingHistory.user_id == user_id)
        .order_by(RatingHistory.games_played.desc())
        .first()
    )
    assert new_head.games_played == 8


# ---------------------------------------------------------------------------
# Postgres: real row-lock serialization, double-end, and the cursor barrier.
# ---------------------------------------------------------------------------
_LOCKED_HOLD_SECONDS = 0.5


def _seed_pg_user(pg_session_factory, user_id: int) -> None:
    db = pg_session_factory()
    try:
        if db.get(User, user_id) is None:
            db.add(User(id=user_id, username=None, is_anonymous=True))
            db.commit()
    finally:
        db.close()


def _seed_pg_game_session(pg_session_factory, user_id: int) -> uuid.UUID:
    db = pg_session_factory()
    try:
        gs = GameSession(
            id=uuid.uuid4(), user_id=user_id, started_at=datetime.now(timezone.utc),
            status="ended", engine_elo=1500, player_color="white",
        )
        db.add(gs)
        db.commit()
        return gs.id
    finally:
        db.close()


def _pg_start(pg_client, auth_headers, user_id: int) -> str:
    resp = pg_client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=user_id),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


@pg_required
def test_same_user_distinct_session_ends_chain_cleanly(pg_client, pg_session_factory, auth_headers):
    """Four concurrent rated ends of DISTINCT sessions for one user serialize on the
    users-row lock and produce a clean games_played chain 1..4 — no duplicate and no
    skipped advancement — exercising the real handler under real row contention."""
    user_id = 4646
    _seed_pg_user(pg_session_factory, user_id)
    session_ids = [_pg_start(pg_client, auth_headers, user_id) for _ in range(4)]

    def _run(session_id: str):
        return pg_client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "checkmate_win", "pgn": "1. e4 e5", "is_rated": True},
            headers=auth_headers(user_id=user_id),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        responses = [f.result(timeout=20) for f in [pool.submit(_run, s) for s in session_ids]]

    assert all(r.status_code == 200 for r in responses), [(r.status_code, r.text) for r in responses]
    verify = pg_session_factory()
    try:
        games = verify.execute(
            text("SELECT games_played FROM rating_history WHERE user_id = :u ORDER BY games_played"),
            {"u": user_id},
        ).scalars().all()
    finally:
        verify.close()
    assert games == [1, 2, 3, 4]


@pg_required
def test_users_lock_prevents_lost_games_played_update(pg_engine, pg_session_factory):
    """Deterministic lost-update proof. A leader reads the head and stalls before
    inserting; a follower runs the same chain.

    * WITH the users lock the follower blocks on it until the leader commits, then
      reads the leader's row and chains 1 -> 2.
    * WITHOUT the lock the follower reads the stale (empty) head during the leader's
      stall and both insert games_played=1 — the lost update the lock prevents.
    """
    user_id = 4747
    _seed_pg_user(pg_session_factory, user_id)

    insert_sql = text(
        "INSERT INTO rating_history "
        "(user_id, game_session_id, rating, is_provisional, games_played, recorded_at) "
        "VALUES (:u, :s, 1200, false, :g, now())"
    )
    head_sql = text(
        "SELECT games_played FROM rating_history WHERE user_id = :u "
        "ORDER BY games_played DESC, recorded_at DESC, id DESC LIMIT 1"
    )
    lock_sql = text("SELECT id FROM users WHERE id = :u FOR NO KEY UPDATE")

    def scenario(take_lock: bool) -> tuple[list[int], float]:
        with pg_engine.begin() as conn:
            conn.execute(text("DELETE FROM rating_history WHERE user_id = :u"), {"u": user_id})
        leader_sid = _seed_pg_game_session(pg_session_factory, user_id)
        follower_sid = _seed_pg_game_session(pg_session_factory, user_id)
        leader_has_read = threading.Event()
        follower_wait: dict[str, float] = {}

        def leader() -> None:
            db = pg_session_factory()
            try:
                db.execute(text("SET LOCAL lock_timeout = '5s'"))
                if take_lock:
                    db.execute(lock_sql, {"u": user_id})
                head = db.execute(head_sql, {"u": user_id}).scalar()
                leader_has_read.set()
                time.sleep(_LOCKED_HOLD_SECONDS)  # hold the (optional) lock past the follower's read
                db.execute(insert_sql, {"u": user_id, "s": str(leader_sid), "g": (head or 0) + 1})
                db.commit()
            finally:
                db.close()

        def follower() -> None:
            leader_has_read.wait(timeout=5)
            db = pg_session_factory()
            try:
                db.execute(text("SET LOCAL lock_timeout = '5s'"))
                started = time.perf_counter()
                if take_lock:
                    db.execute(lock_sql, {"u": user_id})  # blocks until the leader commits
                head = db.execute(head_sql, {"u": user_id}).scalar()
                follower_wait["seconds"] = time.perf_counter() - started
                db.execute(insert_sql, {"u": user_id, "s": str(follower_sid), "g": (head or 0) + 1})
                db.commit()
            finally:
                db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(leader), pool.submit(follower)]
            for f in futures:
                f.result(timeout=15)

        verify = pg_session_factory()
        try:
            games = verify.execute(
                text("SELECT games_played FROM rating_history WHERE user_id = :u ORDER BY games_played"),
                {"u": user_id},
            ).scalars().all()
        finally:
            verify.close()
        return games, follower_wait.get("seconds", 0.0)

    locked_games, locked_wait = scenario(take_lock=True)
    assert locked_games == [1, 2]
    assert locked_wait >= _LOCKED_HOLD_SECONDS / 2  # the follower demonstrably waited on the lock

    unlocked_games, _ = scenario(take_lock=False)
    assert unlocked_games == [1, 1]  # regression: duplicate games_played without the lock


@pg_required
def test_concurrent_double_end_one_session_loser_gets_400(pg_client, pg_session_factory, auth_headers):
    """Two concurrent ends of the SAME session: the loser waits on the session NKU
    lock, observes the ended state via populate_existing, and returns the existing
    400 — adding neither a rating row nor a second cursor bump."""
    user_id = 4444
    _seed_pg_user(pg_session_factory, user_id)
    session_id = _pg_start(pg_client, auth_headers, user_id)

    def _run():
        return pg_client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "checkmate_win", "pgn": "1. e4 e5", "is_rated": True},
            headers=auth_headers(user_id=user_id),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        r1, r2 = [f.result(timeout=15) for f in [pool.submit(_run), pool.submit(_run)]]

    assert sorted([r1.status_code, r2.status_code]) == [200, 400], (
        (r1.status_code, r1.text), (r2.status_code, r2.text)
    )
    verify = pg_session_factory()
    try:
        rating_count = verify.execute(
            text("SELECT count(*) FROM rating_history WHERE user_id = :u"), {"u": user_id}
        ).scalar()
        cursor_seq = verify.execute(
            text("SELECT evidence_seq FROM opening_score_cursors WHERE user_id = :u AND player_color = 'white'"),
            {"u": user_id},
        ).scalar()
    finally:
        verify.close()
    assert rating_count == 1
    assert cursor_seq == 1


@pg_required
def test_cursor_writer_completes_while_end_paused_in_rating(
    pg_client, pg_session_factory, auth_headers
):
    """Bump-last barrier: while a rated end is paused INSIDE its rating work (before
    its own cursor bump), it holds only the session and users locks, so another
    writer bumping the SAME (user, color) cursor completes without blocking. A
    bump-FIRST design would hold the cursor and deadlock this writer."""
    user_id = 4545
    _seed_pg_user(pg_session_factory, user_id)
    session_id = _pg_start(pg_client, auth_headers, user_id)

    paused = threading.Event()
    release = threading.Event()
    original = game_api.compute_rating_tracks

    def _slow(*args, **kwargs):
        paused.set()
        release.wait(timeout=10)
        return original(*args, **kwargs)

    end_response: dict[str, object] = {}

    def _do_end() -> None:
        with patch.object(game_api, "compute_rating_tracks", _slow):
            end_response["resp"] = pg_client.post(
                "/api/game/end",
                json={"session_id": session_id, "result": "checkmate_win", "pgn": "1. e4 e5", "is_rated": True},
                headers=auth_headers(user_id=user_id),
            )

    def _external_bump() -> None:
        db = pg_session_factory()
        try:
            db.execute(text("SET LOCAL lock_timeout = '3s'"))
            bump_evidence_seq(db, user_id, "white")
            db.commit()
        finally:
            db.close()

    # pg_client must also suppress the lazy source-module enqueue performed by
    # compute_opening_score_delta after commit. Otherwise it starts the real
    # singleton against DATABASE_URL, which outlives the mocked lifespan getter
    # and can deadlock the next test's TRUNCATE during fixture setup.
    with patch(
        "app.opening_score_scheduler.OpeningScoreScheduler.request_recompute"
    ) as real_scheduler_enqueue, concurrent.futures.ThreadPoolExecutor(
        max_workers=2
    ) as pool:
        end_future = pool.submit(_do_end)
        assert paused.wait(timeout=10), "game end never reached its rating work"
        # The external cursor writer completes while the end is stalled: proof the
        # cursor row is free during rating work (would time out under bump-first).
        pool.submit(_external_bump).result(timeout=8)
        release.set()
        end_future.result(timeout=10)

    real_scheduler_enqueue.assert_not_called()

    assert end_response["resp"].status_code == 200, end_response["resp"].text
    verify = pg_session_factory()
    try:
        seq = verify.execute(
            text("SELECT evidence_seq FROM opening_score_cursors WHERE user_id = :u AND player_color = 'white'"),
            {"u": user_id},
        ).scalar()
    finally:
        verify.close()
    assert seq == 2  # external bump + the game end's own bump-last
