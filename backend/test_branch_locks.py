"""Branch-scoped drill and opponent locks (g-branch-locks).

These pin the contract that the two "unlocked snapshot, sometimes mutates"
handlers refresh and lock only their mutating branches:

* drill ``/route-check`` keeps its entry read, root-reached, and on-route
  responses unlocked (pure snapshots that write nothing); only the target-reached
  and off-route branches acquire the session NKU lock, refresh, and re-derive
  before writing;
* ``/next-opponent-move`` keeps post-root steering and the ghost/engine path unlocked; only the active
  pre-root drill branch locks and refreshes, then routes: failed/abandoned -> the
  existing 400, converted/root-reached -> release the lock and dispatch from the
  refreshed state, still active -> compute/write route state under the lock and return the route
  response.

The SQLite tests (statement capture) run everywhere; the row-lock interleaving
proofs are ``@pg_required`` and skip cleanly without a Postgres URL.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import threading
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import chess
from sqlalchemy import event, text

from app.models import GameSession, User
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opponent_move_controller import ControllerMove
from conftest import engine, pg_required


# Four-field FENs matching the drill route map's normalized keys.
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
ROOT_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"  # after 1.e4
E4_E5_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"  # after 1.e4 e5
D4_FEN = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -"  # off-route (1.d4)

_HOLD = 0.5  # seconds a leader holds the row lock so the follower demonstrably waits


def _push_fen(board: chess.Board, uci: str) -> str:
    board.push(chess.Move.from_uci(uci))
    return " ".join(board.fen().split()[:4])


def _steering_graph() -> OpeningGraph:
    """start -> e4 (ROOT_FEN) -> e5 (E4_E5_FEN). Parents-only route maps make e5 a
    child of e4, so D4_FEN is off every target's route."""
    board = chess.Board()
    start = " ".join(board.fen().split()[:4])
    e4 = _push_fen(board, "e2e4")
    e5 = _push_fen(board, "e7e5")

    nodes = {
        start: OpeningGraphNode(start, "white"),
        e4: OpeningGraphNode(e4, "black"),
        e5: OpeningGraphNode(e5, "white"),
    }
    nodes[start].children["e2e4"] = e4
    nodes[e4].parents.add((start, "e2e4"))
    nodes[e4].children["e7e5"] = e5
    nodes[e5].parents.add((e4, "e7e5"))
    graph = OpeningGraph(nodes, start)
    graph.freeze()
    return graph


def _make_drill(
    db,
    *,
    drill_state: str,
    user_id: int = 123,
    player_color: str = "white",
    opening_key: str = ROOT_FEN,
    drill_root_reached_ply: int | None = None,
) -> uuid.UUID:
    now = datetime.now(timezone.utc)
    kwargs = dict(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=now,
        status="active",
        engine_elo=1500,
        player_color=player_color,
        session_mode="drill",
        drill_state=drill_state,
        drill_opening_key=opening_key,
        drill_root_reached_ply=drill_root_reached_ply,
        drill_strictness="standard",
        is_rated=False,
    )
    if drill_state == "converted":
        # The drill rating-boundary check requires the full converted shape.
        kwargs.update(
            is_rated=True,
            normal_started_at=now,
            converted_at=now,
            rated_start_ply=1,
        )
    gs = GameSession(**kwargs)
    db.add(gs)
    db.commit()
    return gs.id


# ---------------------------------------------------------------------------
# SQLite statement capture: which game_sessions rows were read / written.
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _capture(target_engine):
    stmts: list[str] = []

    def _on(conn, cursor, statement, parameters, context, executemany) -> None:
        stmts.append(statement.lower())

    event.listen(target_engine, "before_cursor_execute", _on)
    try:
        yield stmts
    finally:
        event.remove(target_engine, "before_cursor_execute", _on)


def _gs_updates(stmts: list[str]) -> list[str]:
    return [s for s in stmts if s.lstrip().startswith("update game_sessions")]


def _gs_selects(stmts: list[str]) -> list[str]:
    return [s for s in stmts if s.lstrip().startswith("select") and "from game_sessions" in s]


# ---------------------------------------------------------------------------
# SQLite: snapshot branches write nothing; mutating branches lock (re-read) + write.
# ---------------------------------------------------------------------------
def test_route_check_root_reached_snapshot_writes_nothing(client, auth_headers, db_session):
    """The entry root-reached response is a pure snapshot: one unlocked read, no
    locked re-read, no write."""
    session_id = _make_drill(
        db_session, drill_state="root_reached", drill_root_reached_ply=1
    )
    headers = auth_headers()
    with patch("app.api.drills.get_opening_graph", return_value=_steering_graph()):
        with _capture(engine) as stmts:
            resp = client.post(
                f"/api/drills/{session_id}/route-check",
                json={
                    "current_fen": ROOT_FEN,
                    "previous_fen": START_FEN,
                    "played_uci": "e2e4",
                    "current_ply": 1,
                },
                headers=headers,
            )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "root_reached"
    assert _gs_updates(stmts) == []
    assert len(_gs_selects(stmts)) == 1  # unlocked entry read only — no lock acquired


def test_route_check_on_route_snapshot_writes_nothing(client, auth_headers, db_session):
    """The on-route response is a snapshot too: no locked re-read and no write."""
    session_id = _make_drill(db_session, drill_state="active")
    headers = auth_headers()
    with patch("app.api.drills.get_opening_graph", return_value=_steering_graph()):
        with _capture(engine) as stmts:
            resp = client.post(
                f"/api/drills/{session_id}/route-check",
                # On route to ROOT_FEN, not the target.
                json={"current_fen": START_FEN, "current_ply": 0},
                headers=headers,
            )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "on_route"
    assert _gs_updates(stmts) == []
    assert len(_gs_selects(stmts)) == 1


def test_route_check_target_reached_locks_and_writes(client, auth_headers, db_session):
    """The target-reached branch mutates: it takes the locked re-read (a second
    game_sessions SELECT) and writes root_reached."""
    session_id = _make_drill(db_session, drill_state="active")
    headers = auth_headers()
    with patch("app.api.drills.get_opening_graph", return_value=_steering_graph()):
        with _capture(engine) as stmts:
            resp = client.post(
                f"/api/drills/{session_id}/route-check",
                json={
                    "current_fen": ROOT_FEN,  # the target
                    "previous_fen": START_FEN,
                    "played_uci": "e2e4",
                    "current_ply": 1,
                },
                headers=headers,
            )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "root_reached"
    assert len(_gs_updates(stmts)) == 1
    assert len(_gs_selects(stmts)) == 2  # unlocked entry read + locked re-read
    db_session.expire_all()
    assert db_session.get(GameSession, session_id).drill_state == "root_reached"


def test_route_check_off_route_locks_and_writes(client, auth_headers, db_session):
    """The off-route branch mutates too: locked re-read + a failed/off_route write."""
    session_id = _make_drill(db_session, drill_state="active", opening_key=E4_E5_FEN)
    headers = auth_headers()
    with patch("app.api.drills.get_opening_graph", return_value=_steering_graph()):
        with _capture(engine) as stmts:
            resp = client.post(
                f"/api/drills/{session_id}/route-check",
                json={
                    "current_fen": D4_FEN,
                    "previous_fen": START_FEN,
                    "played_uci": "d2d4",
                    "current_ply": 1,
                },
                headers=headers,
            )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"
    assert len(_gs_updates(stmts)) == 1
    assert len(_gs_selects(stmts)) == 2
    db_session.expire_all()
    session = db_session.get(GameSession, session_id)
    assert session.drill_state == "failed"
    assert session.drill_terminal_reason == "off_route"


def test_next_opponent_converted_snapshot_makes_no_drill_write(client, auth_headers, db_session):
    """A converted drill skips the pre-root branch entirely: the normal ghost/engine
    path runs unlocked and writes no drill state."""
    session_id = _make_drill(db_session, drill_state="converted", player_color="black")
    headers = auth_headers()
    move = ControllerMove(uci="e2e4", san="e4", method="test")
    with patch("app.opponent_move_controller.choose_move", return_value=move):
        with _capture(engine) as stmts:
            resp = client.post(
                "/api/game/next-opponent-move",
                json={"session_id": str(session_id), "fen": START_FEN, "moves": []},
                headers=headers,
            )
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "engine"
    assert _gs_updates(stmts) == []


# ---------------------------------------------------------------------------
# Postgres helpers.
# ---------------------------------------------------------------------------
def _seed_pg_user(pg_session_factory, user_id: int) -> None:
    db = pg_session_factory()
    try:
        if db.get(User, user_id) is None:
            db.add(User(id=user_id, username=None, is_anonymous=True))
            db.commit()
    finally:
        db.close()


def _seed_pg_drill(pg_session_factory, **kwargs) -> uuid.UUID:
    db = pg_session_factory()
    try:
        return _make_drill(db, **kwargs)
    finally:
        db.close()


@contextlib.contextmanager
def _row_lock_leader(pg_session_factory, session_id, sql: str, params: dict, locked: threading.Event):
    """Acquire the session NKU lock, signal, hold, then run ``sql`` and commit.

    Yields the future so the caller can join it. The follower under test starts
    after ``locked`` is set, races into its own locking re-read, and blocks until
    this leader commits the concurrent transition.
    """

    def _leader() -> None:
        db = pg_session_factory()
        try:
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            db.execute(
                text("SELECT id FROM game_sessions WHERE id = :id FOR NO KEY UPDATE"),
                {"id": session_id},
            )
            locked.set()
            time.sleep(_HOLD)  # hold the lock so the follower demonstrably waits
            db.execute(text(sql), {**params, "id": session_id})
            db.commit()
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        yield pool, pool.submit(_leader)


def _drill_state(pg_session_factory, session_id) -> str:
    db = pg_session_factory()
    try:
        return db.execute(
            text("SELECT drill_state FROM game_sessions WHERE id = :id"),
            {"id": session_id},
        ).scalar()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Postgres: route-check mutating branches yield to concurrent transitions.
# ---------------------------------------------------------------------------
@pg_required
def test_route_check_off_route_yields_to_concurrent_root_reached(
    pg_client, pg_session_factory, auth_headers
):
    """An off-route route-check locks and refreshes before writing. If a concurrent
    request reaches root first, the refreshed branch returns the root-reached
    response and writes no failure — the concurrent transition survives."""
    user_id = 7101
    _seed_pg_user(pg_session_factory, user_id)
    session_id = _seed_pg_drill(
        pg_session_factory, user_id=user_id, drill_state="active", opening_key=E4_E5_FEN
    )
    locked = threading.Event()
    result: dict = {}

    def _follower() -> None:
        started = time.perf_counter()
        with patch("app.api.drills.get_opening_graph", return_value=_steering_graph()):
            resp = pg_client.post(
                f"/api/drills/{session_id}/route-check",
                json={
                    "current_fen": D4_FEN,
                    "previous_fen": START_FEN,
                    "played_uci": "d2d4",
                    "current_ply": 1,
                },
                headers=auth_headers(user_id=user_id),
            )
        result["elapsed"] = time.perf_counter() - started
        result["resp"] = resp

    with _row_lock_leader(
        pg_session_factory,
        session_id,
        "UPDATE game_sessions SET drill_state = 'root_reached' WHERE id = :id",
        {},
        locked,
    ) as (pool, leader_future):
        assert locked.wait(timeout=5)
        follower_future = pool.submit(_follower)
        leader_future.result(timeout=10)
        follower_future.result(timeout=10)

    resp = result["resp"]
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "root_reached"  # response derived from refreshed state
    assert result["elapsed"] >= _HOLD / 2  # the follower waited on the NKU lock
    assert _drill_state(pg_session_factory, session_id) == "root_reached"  # no 'failed' overwrite


@pg_required
def test_route_check_target_reached_yields_to_concurrent_failure(
    pg_client, pg_session_factory, auth_headers
):
    """A target-reached route-check refreshes under the lock. If the drill failed
    concurrently, it returns the existing 400 and does not overwrite the failure
    with root_reached."""
    user_id = 7102
    _seed_pg_user(pg_session_factory, user_id)
    session_id = _seed_pg_drill(
        pg_session_factory, user_id=user_id, drill_state="active", opening_key=ROOT_FEN
    )
    locked = threading.Event()
    result: dict = {}

    def _follower() -> None:
        started = time.perf_counter()
        with patch("app.api.drills.get_opening_graph", return_value=_steering_graph()):
            resp = pg_client.post(
                f"/api/drills/{session_id}/route-check",
                json={
                    "current_fen": ROOT_FEN,  # the target
                    "previous_fen": START_FEN,
                    "played_uci": "e2e4",
                    "current_ply": 1,
                },
                headers=auth_headers(user_id=user_id),
            )
        result["elapsed"] = time.perf_counter() - started
        result["resp"] = resp

    with _row_lock_leader(
        pg_session_factory,
        session_id,
        "UPDATE game_sessions SET drill_state = 'failed', drill_terminal_reason = 'off_route' WHERE id = :id",
        {},
        locked,
    ) as (pool, leader_future):
        assert locked.wait(timeout=5)
        follower_future = pool.submit(_follower)
        leader_future.result(timeout=10)
        follower_future.result(timeout=10)

    resp = result["resp"]
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Drill route cannot be checked from its current state"
    assert result["elapsed"] >= _HOLD / 2
    assert _drill_state(pg_session_factory, session_id) == "failed"  # not overwritten


@pg_required
def test_route_check_root_reached_snapshot_preserves_concurrent_failure(
    pg_client, pg_session_factory, auth_headers
):
    """An initially-root-reached drill racing to failed: the unlocked snapshot
    response returns root_reached (it read before the commit) yet writes nothing, so
    the concurrently committed failed transition is the durable state."""
    user_id = 7103
    _seed_pg_user(pg_session_factory, user_id)
    session_id = _seed_pg_drill(
        pg_session_factory,
        user_id=user_id,
        drill_state="root_reached",
        opening_key=ROOT_FEN,
        drill_root_reached_ply=1,
    )
    locked = threading.Event()
    result: dict = {}

    def _follower() -> None:
        # The entry snapshot does not lock, so this read sees the still-committed
        # root_reached while the leader holds an uncommitted failed transition.
        with patch("app.api.drills.get_opening_graph", return_value=_steering_graph()):
            result["resp"] = pg_client.post(
                f"/api/drills/{session_id}/route-check",
                json={
                    "current_fen": ROOT_FEN,
                    "previous_fen": START_FEN,
                    "played_uci": "e2e4",
                    "current_ply": 1,
                },
                headers=auth_headers(user_id=user_id),
            )

    with _row_lock_leader(
        pg_session_factory,
        session_id,
        "UPDATE game_sessions SET drill_state = 'failed', drill_terminal_reason = 'off_route' WHERE id = :id",
        {},
        locked,
    ) as (pool, leader_future):
        assert locked.wait(timeout=5)
        follower_future = pool.submit(_follower)
        follower_future.result(timeout=10)  # finishes fast — never blocks on a lock
        leader_future.result(timeout=10)

    resp = result["resp"]
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "root_reached"  # snapshot, unaffected by the pending failure
    assert _drill_state(pg_session_factory, session_id) == "failed"  # snapshot wrote nothing


# ---------------------------------------------------------------------------
# Postgres: next-opponent stale-state re-derivation.
# ---------------------------------------------------------------------------
@pg_required
def test_next_opponent_stale_failed_returns_400(pg_client, pg_session_factory, auth_headers):
    """The next-opponent active pre-root branch locks and refreshes. A failed
    transition committed concurrently yields the existing 400, not a root_reached
    overwrite."""
    user_id = 7201
    _seed_pg_user(pg_session_factory, user_id)
    session_id = _seed_pg_drill(
        pg_session_factory, user_id=user_id, drill_state="active", player_color="black"
    )
    locked = threading.Event()
    result: dict = {}

    def _follower() -> None:
        started = time.perf_counter()
        resp = pg_client.post(
            "/api/game/next-opponent-move",
            json={"session_id": str(session_id), "fen": START_FEN, "moves": []},
            headers=auth_headers(user_id=user_id),
        )
        result["elapsed"] = time.perf_counter() - started
        result["resp"] = resp

    with _row_lock_leader(
        pg_session_factory,
        session_id,
        "UPDATE game_sessions SET drill_state = 'failed', drill_terminal_reason = 'off_route' WHERE id = :id",
        {},
        locked,
    ) as (pool, leader_future):
        assert locked.wait(timeout=5)
        follower_future = pool.submit(_follower)
        leader_future.result(timeout=10)
        follower_future.result(timeout=10)

    resp = result["resp"]
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Opponent moves are unavailable for this drill state"
    assert result["elapsed"] >= _HOLD / 2
    assert _drill_state(pg_session_factory, session_id) == "failed"


@pg_required
def test_next_opponent_stale_converted_falls_through(pg_client, pg_session_factory, auth_headers):
    """A convert committed concurrently: the branch refreshes, releases the lock,
    and falls through to the normal ghost/engine path — no drill-state overwrite and
    no route response."""
    user_id = 7202
    _seed_pg_user(pg_session_factory, user_id)
    session_id = _seed_pg_drill(
        pg_session_factory, user_id=user_id, drill_state="active", player_color="black"
    )
    locked = threading.Event()
    result: dict = {}
    move = ControllerMove(uci="e2e4", san="e4", method="test")

    def _follower() -> None:
        started = time.perf_counter()
        with patch("app.opponent_move_controller.choose_move", return_value=move):
            resp = pg_client.post(
                "/api/game/next-opponent-move",
                json={"session_id": str(session_id), "fen": START_FEN, "moves": []},
                headers=auth_headers(user_id=user_id),
            )
        result["elapsed"] = time.perf_counter() - started
        result["resp"] = resp

    with _row_lock_leader(
        pg_session_factory,
        session_id,
        "UPDATE game_sessions SET drill_state = 'converted', is_rated = true, "
        "normal_started_at = now(), converted_at = now(), rated_start_ply = 1 WHERE id = :id",
        {},
        locked,
    ) as (pool, leader_future):
        assert locked.wait(timeout=5)
        follower_future = pool.submit(_follower)
        leader_future.result(timeout=10)
        follower_future.result(timeout=10)

    resp = result["resp"]
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "engine"  # fell through to the engine, not the route response
    assert result["elapsed"] >= _HOLD / 2
    assert _drill_state(pg_session_factory, session_id) == "converted"  # not overwritten


@pg_required
def test_next_opponent_releases_lock_before_post_root_steering_so_moves_commits(
    pg_client, pg_session_factory, auth_headers
):
    """Lock-release barrier. The active pre-root branch acquires the NKU lock, sees a
    concurrently committed root_reached, re-derives post-root steering, and rolls back.
    While the follower is paused INSIDE Ghost search, a concurrent ``/moves`` upload —
    which takes its own session NKU lock — completes: proof no row lock survives into
    post-root Ghost/structural computation."""
    user_id = 7301
    _seed_pg_user(pg_session_factory, user_id)
    session_id = _seed_pg_drill(
        pg_session_factory, user_id=user_id, drill_state="active", player_color="black"
    )
    locked = threading.Event()
    ghost_paused = threading.Event()
    ghost_release = threading.Event()
    result: dict = {}
    graph = _steering_graph()

    def _paused_ghost(*args, **kwargs):
        ghost_paused.set()
        ghost_release.wait(timeout=10)
        return (None, None, None, None, None)

    def _follower() -> None:
        with (
            patch("app.api.game.get_opening_graph", return_value=graph),
            patch("app.api.game.find_ghost_move", _paused_ghost),
        ):
            result["opp"] = pg_client.post(
                "/api/game/next-opponent-move",
                json={"session_id": str(session_id), "fen": START_FEN, "moves": []},
                headers=auth_headers(user_id=user_id),
            )

    def _moves_probe():
        return pg_client.post(
            f"/api/session/{session_id}/moves",
            json={"moves": [
                {"move_number": 1, "color": "white", "move_san": "e4", "fen_after": "fen-after-e4"}
            ]},
            headers=auth_headers(user_id=user_id),
        )

    with _row_lock_leader(
        pg_session_factory,
        session_id,
        "UPDATE game_sessions SET drill_state = 'root_reached' WHERE id = :id",
        {},
        locked,
    ) as (pool, leader_future):
        assert locked.wait(timeout=5)
        follower_future = pool.submit(_follower)
        try:
            # The follower blocks on the leader's lock, refreshes to root_reached,
            # rolls back, then stalls inside Ghost work holding no row lock.
            assert ghost_paused.wait(timeout=10), "next-opponent never reached Ghost work"
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as probe_pool:
                moves_resp = probe_pool.submit(_moves_probe).result(timeout=8)
        finally:
            ghost_release.set()
        leader_future.result(timeout=10)
        follower_future.result(timeout=10)

    assert moves_resp.status_code == 200, moves_resp.text  # /moves committed while opp was paused
    assert moves_resp.json()["moves_inserted"] == 1
    assert result["opp"].status_code == 200, result["opp"].text
    assert result["opp"].json()["mode"] == "ghost"
    assert result["opp"].json()["move"] == {"uci": "e2e4", "san": "e4"}
    assert _drill_state(pg_session_factory, session_id) == "root_reached"
