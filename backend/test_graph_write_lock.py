"""Tests for the shared per-user graph-write advisory lock (g-graph-lock).

``acquire_graph_write_lock`` funnels every same-user writer of the shared ghost
graph — the deferred /moves evidence worker and both blunder-recording paths —
through ``pg_advisory_xact_lock(user_id)`` so they serialize instead of racing (and
deadlocking on) the ``(user_id, fen_hash)`` / ``(from_position_id, move_san)`` unique
indexes.

Layers under test:

* Unit/default-suite (run everywhere): the helper is a no-op off Postgres and emits
  no advisory SQL; a repo-wide source scan proves every shared-graph upsert entry
  point calls the helper first (with the one sanctioned lock-free admin backfill
  explicitly allowlisted); a 40P01 deadlock injected into the worker is attempted
  once (no timeout retry) and drops through the scheduler's log-and-drop.
* ``@pg_required`` (skip without a Postgres URL): two REAL ``_record_target``
  transactions, and the worker vs a real recording, do not overlap under the lock;
  a held lock makes a real recording time out and roll back with nothing persisted;
  and a *reverted*-lock control reproduces the exact opposite-order 40P01 deadlock
  the lock exists to prevent.
"""

from __future__ import annotations

import ast
import concurrent.futures
import inspect
import logging
import pathlib
import textwrap
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import chess
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

import app.api.blunder as blunder_api
import app.api.session as session_api
import app.graph_write_lock as graph_write_lock
from app.api.session import SessionMoveInput
from app.fen import active_color, fen_hash
from app.graph_write_lock import acquire_graph_write_lock
from app.models import Blunder, GameSession, Move, Position
from app.opening_cache import current_evidence_seq
from app.security import TokenPayload
from app.session_evidence_scheduler import SessionEvidenceScheduler
from conftest import pg_required

STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
AFTER_E4E5_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
# Two distinct positions the reverted-lock control inserts in OPPOSITE order.
AFTER_D4_FEN = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1"
AFTER_C4_FEN = "rnbqkbnr/pppppppp/8/8/2P5/8/PP1PPPPP/RNBQKBNR b KQkq - 0 1"

# The e4 e5 opening line as worker evidence moves (two edges, three positions).
_OPENING_MOVES = [
    {
        "move_number": 1,
        "color": "white",
        "move_san": "e4",
        "fen_before": STARTING_FEN,
        "fen_after": AFTER_E4_FEN,
        "move_uci": "e2e4",
        "eval_cp": 20,
        "classification": "best",
    },
    {
        "move_number": 1,
        "color": "black",
        "move_san": "e5",
        "fen_before": AFTER_E4_FEN,
        "fen_after": AFTER_E4E5_FEN,
        "move_uci": "e7e5",
        "eval_cp": 12,
        "classification": "best",
    },
]


def _fen_before_final_ply(sans: list[str]) -> str:
    """Return board.fen() after all but the last SAN — the position ``_replay_pgn``
    treats as the pre-move (blunder) position, so it matches the recording's sanity
    check exactly (both are python-chess ``board.fen()``)."""
    board = chess.Board()
    for san in sans[:-1]:
        board.push_san(san)
    return board.fen()


# Two disjoint real recording lines (distinct pre-move positions → distinct targets).
PGN_HOLDER = "1. e4 e5 2. Qh5"
PGN_WAITER = "1. d4 d5 2. Nf3"
FEN_BEFORE_HOLDER = _fen_before_final_ply(["e4", "e5", "Qh5"])
FEN_BEFORE_WAITER = _fen_before_final_ply(["d4", "d5", "Nf3"])


def _position_spec(fen_raw: str) -> tuple[str, str, str]:
    """Build the (fen_raw, fen_hash, active_color) tuple ``_upsert_positions`` takes."""
    return (fen_raw, fen_hash(fen_raw), active_color(fen_raw))


def _operational_error(sqlstate: str) -> OperationalError:
    orig = Exception("boom")
    orig.sqlstate = sqlstate
    return OperationalError("SELECT 1", {}, orig)


# ---------------------------------------------------------------------------
# Unit / default-suite (no Postgres)
# ---------------------------------------------------------------------------


def test_helper_is_noop_off_postgres():
    """SQLite and unknown dialects skip the advisory lock + SET LOCALs entirely: the
    helper issues NO SQL, so those backends never emit or wait on the advisory lock."""
    for dialect in ("sqlite", "", "mysql"):
        db = MagicMock()
        acquire_graph_write_lock(db, user_id=42, dialect_name=dialect)
        db.execute.assert_not_called()


def test_helper_emits_timeouts_then_advisory_on_postgres():
    """On Postgres the helper sets lock_timeout then statement_timeout then takes
    pg_advisory_xact_lock(user_id) — in that order, with the user id bound in."""
    db = MagicMock()
    acquire_graph_write_lock(db, user_id=777, dialect_name="postgresql")

    statements = [str(c.args[0]) for c in db.execute.call_args_list]
    assert len(statements) == 3
    assert "lock_timeout" in statements[0]
    assert "statement_timeout" in statements[1]
    assert "pg_advisory_xact_lock" in statements[2]
    # The advisory key is the user id.
    advisory_params = db.execute.call_args_list[2].args[0].compile().params
    assert advisory_params == {"uid": 777}


def test_sqlite_recording_exercises_helper_and_emits_no_advisory_sql(
    client, auth_headers, create_game_session
):
    """The SQLite blunder-recording path DOES call the shared helper (proving the wire
    is real, not Postgres-gated away) yet emits no advisory/timeout SQL — the helper's
    dialect no-op. A global cursor listener captures every statement the request runs."""
    session_id = create_game_session(user_id=555, player_color="white")

    captured: list[str] = []

    @event.listens_for(Engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):
        captured.append(statement)

    calls: list[str] = []
    real = graph_write_lock.acquire_graph_write_lock

    def _spy(db, *, user_id, dialect_name):
        calls.append(dialect_name)
        return real(db, user_id=user_id, dialect_name=dialect_name)

    try:
        with patch.object(blunder_api, "acquire_graph_write_lock", _spy):
            response = client.post(
                "/api/blunder",
                json={
                    "session_id": session_id,
                    "pgn": "1. e4 e5 2. Qh5",
                    "fen": AFTER_E4E5_FEN,
                    "user_move": "Qh5",
                    "best_move": "Nf3",
                    "eval_before": 50,
                    "eval_after": -100,
                },
                headers=auth_headers(user_id=555),
            )
    finally:
        event.remove(Engine, "before_cursor_execute", _capture)

    assert response.status_code == 201
    # The helper was invoked exactly once, on the SQLite dialect.
    assert calls == ["sqlite"]
    # ...and it stayed a no-op: nothing advisory/timeout-shaped reached the DB.
    lowered = " ".join(captured).lower()
    assert "advisory" not in lowered
    assert "set_config" not in lowered
    assert "lock_timeout" not in lowered
    assert "statement_timeout" not in lowered


def _callsites(files, callee: str, backend_root: pathlib.Path) -> set[str]:
    """Map every call to ``callee`` (by final name, ``Name`` or ``Attribute``) across
    ``files`` to ``"<relpath>::<nearest-enclosing-def>"``."""
    sites: set[str] = set()
    for path in files:
        tree = ast.parse(path.read_text())
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        rel = path.relative_to(backend_root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else fn.attr
                if isinstance(fn, ast.Attribute)
                else None
            )
            if name != callee:
                continue
            enclosing = None
            cur = parents.get(node)
            while cur is not None:
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    enclosing = cur.name
                    break
                cur = parents.get(cur)
            sites.add(f"{rel}::{enclosing}")
    return sites


def test_source_scan_shared_graph_writers_acquire_lock_first():
    """Guard: every entry point that upserts shared Position/Move rows takes the
    advisory helper BEFORE the upsert, and — scanning the WHOLE production tree, not
    just the defining module — no other function calls those upsert helpers except the
    one sanctioned lock-free admin backfill. A new unlocked graph writer breaks this
    test rather than shipping a silent deadlock regression."""

    def _acquire_precedes_upsert(func, upsert_name: str) -> None:
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        acquire_line = upsert_line = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "acquire_graph_write_lock" and acquire_line is None:
                    acquire_line = node.lineno
                elif node.func.id == upsert_name and upsert_line is None:
                    upsert_line = node.lineno
        assert acquire_line is not None, f"{func.__name__} never acquires the lock"
        assert upsert_line is not None, f"{func.__name__} never calls {upsert_name}"
        assert acquire_line < upsert_line, (
            f"{func.__name__} calls {upsert_name} before acquiring the lock"
        )

    # The two PRODUCTION writers acquire the lock before the shared upsert.
    _acquire_precedes_upsert(blunder_api._record_target, "_upsert_positions")
    _acquire_precedes_upsert(
        session_api._run_graph_evidence_txn, "_upsert_session_position_graph"
    )

    # Repo-wide caller allowlist: scan every production .py under app/ and scripts/.
    backend_root = pathlib.Path(inspect.getsourcefile(session_api)).parents[2]
    files = [
        p
        for sub in ("app", "scripts")
        for p in sorted((backend_root / sub).rglob("*.py"))
        if not p.name.startswith("test_")
    ]

    assert _callsites(files, "_upsert_positions", backend_root) == {
        "app/api/blunder.py::_record_target",
    }
    assert _callsites(files, "_upsert_session_position_graph", backend_root) == {
        "app/api/session.py::_run_graph_evidence_txn",
        # Sanctioned lock-free admin migration: single-threaded, quiet-window only,
        # MUST NOT run concurrently with live uploads (see the backfill module
        # docstring). Explicitly allowlisted so a NEW *production* caller — which
        # would race live writes — still fails this guard until it takes the lock.
        "scripts/backfill_ghost_graph.py::backfill_ghost_graph",
    }


class _FakePgSession:
    """Minimal Session stand-in reporting a Postgres dialect and tracking close()."""

    def __init__(self) -> None:
        self.closed = False
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def close(self) -> None:
        self.closed = True


def test_worker_40p01_is_attempted_once_and_dropped_by_scheduler(caplog):
    """A 40P01 deadlock from the worker's graph txn is NOT a timeout SQLSTATE: it is
    attempted once (no retry), propagates out of the orchestrator without running the
    post-boundary continuation (no analysis-cache/recompute self-heal), and the
    scheduler log-and-drops it — no crash, session still closed, no opportunity work."""
    clock = SimpleNamespace(now=1000.0)
    sessions: list[_FakePgSession] = []

    def factory():
        s = _FakePgSession()
        sessions.append(s)
        return s

    sched = SessionEvidenceScheduler(
        session_factory=factory,
        run_side_effects=session_api._run_session_move_evidence_side_effects,
        clock=lambda: clock.now,
        quiet_window=1.5,
        max_wait=10.0,
        auto_start=False,
    )

    attempts = {"n": 0}

    def _deadlock(db, **kwargs):
        attempts["n"] += 1
        raise _operational_error("40P01")

    sched.enqueue(
        uuid.uuid4(),
        7,
        "white",
        [SimpleNamespace(move_number=1, color="white")],
    )
    clock.now += 2.0

    with patch.object(
        session_api, "_run_graph_evidence_txn", side_effect=_deadlock
    ), patch.object(session_api, "_upsert_analysis_cache") as cache_mock, patch.object(
        session_api, "request_recompute"
    ) as recompute_mock, caplog.at_level(
        logging.ERROR, logger="app.session_evidence_scheduler"
    ):
        # The scheduler must swallow the 40P01 (log-and-drop), not propagate it.
        sched.run_due()

    assert attempts["n"] == 1  # attempted once — the timeout retry did not engage
    cache_mock.assert_not_called()  # non-timeout error → no post-boundary continuation
    recompute_mock.assert_not_called()
    assert sessions and sessions[0].closed is True
    assert any(
        rec.levelno >= logging.ERROR for rec in caplog.records
    ), [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# Postgres serialization + timeout rollback + reverted-lock deadlock control
# ---------------------------------------------------------------------------


def _start_game(pg_client, auth_headers, user_id: int) -> uuid.UUID:
    resp = pg_client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=user_id),
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["session_id"])


def _record_target_cs(
    session_factory, user_id, session_uuid, pgn, fen, user_move, *, mark_first
):
    """Closure that runs the REAL ``_record_target`` transaction on its own session:
    PGN replay, Position/Move upserts, target insert, (optional) first-blunder
    bookkeeping, evidence-cursor bump, and commit. Returns the post-commit timestamp."""
    user = TokenPayload(user_id=user_id, username="tester", is_anonymous=False)

    def _cs():
        db = session_factory()
        try:
            session_row = db.get(GameSession, session_uuid)
            blunder_api._record_target(
                db=db,
                session=session_row,
                user=user,
                pgn=pgn,
                fen=fen,
                user_move=user_move,
                best_move=user_move,
                eval_before=0,
                eval_after=0,
                mark_first_blunder_recorded=mark_first,
                bump_new_target_unconditionally=True,
            )
            return time.perf_counter()
        finally:
            db.close()

    return _cs


def _evidence_seq(session_factory, user_id: int) -> int:
    db = session_factory()
    try:
        return current_evidence_seq(db, user_id, "white")
    finally:
        db.close()


def _advisory_waiters(monitor, user_id: int) -> int:
    return (
        monitor.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND objid = :uid AND NOT granted"
            ),
            {"uid": user_id},
        ).scalar()
        or 0
    )


def _await_advisory_waiter(monitor, user_id: int, timeout: float = 20.0) -> None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if _advisory_waiters(monitor, user_id) >= 1:
            return
        time.sleep(0.05)
    raise AssertionError("the second writer never blocked on the advisory lock")


def _run_serialization_case(pg_engine, user_id, holder_cs, waiter_cs):
    """Drive two same-user graph writers through the real advisory lock and prove their
    critical sections do not overlap.

    ``acquire_graph_write_lock`` is wrapped (on BOTH the session and blunder module
    bindings) so the FIRST acquirer pauses *holding the lock* until the test releases
    it. While it is paused the waiter's acquisition provably blocks: ``pg_locks`` shows
    an ungranted advisory waiter AND ``acquired_at`` has no entry for it — captured
    before the holder is released, so the holder deterministically still holds the lock.
    That is the whole serialization proof; both futures then commit successfully.

    We deliberately do NOT compare the waiter's acquire timestamp against the holder's
    post-commit timestamp: PostgreSQL releases the advisory lock DURING the holder's
    COMMIT (waking the waiter) before ``db.commit()`` returns on the holder's backend,
    so on two separate connections the waiter can legitimately record its acquisition
    before the holder records its commit — a false failure under correct serialization.

    Returns ``(holder_committed_at, waiter_committed_at)`` — both present ⇒ both
    critical sections ran to a clean commit.
    """
    real = graph_write_lock.acquire_graph_write_lock
    order_lock = threading.Lock()
    state = {"count": 0}
    first_acquired = threading.Event()
    may_release = threading.Event()
    acquired_at: dict[int, float] = {}

    def _wrap(db, *, user_id, dialect_name):
        with order_lock:
            state["count"] += 1
            idx = state["count"]
        real(db, user_id=user_id, dialect_name=dialect_name)  # the waiter blocks here
        acquired_at[idx] = time.perf_counter()
        if idx == 1:
            first_acquired.set()
            assert may_release.wait(timeout=20), "holder was never released"

    monitor = pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        with patch.object(session_api, "acquire_graph_write_lock", _wrap), patch.object(
            blunder_api, "acquire_graph_write_lock", _wrap
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f_holder = pool.submit(holder_cs)
                assert first_acquired.wait(timeout=20), "holder never acquired the lock"
                f_waiter = pool.submit(waiter_cs)
                # The waiter is genuinely blocked AT acquisition (not merely slow).
                _await_advisory_waiter(monitor, user_id)
                assert 2 not in acquired_at
                may_release.set()
                holder_committed_at = f_holder.result(timeout=20)
                waiter_committed_at = f_waiter.result(timeout=20)
    finally:
        monitor.close()

    return holder_committed_at, waiter_committed_at


@pg_required
def test_recording_vs_recording_serialize(
    pg_engine, pg_session_factory, pg_client, auth_headers
):
    """Two REAL same-user blunder recordings serialize on the per-user advisory lock.
    Each runs the full ``_record_target`` transaction (replay, Position/Move upserts,
    target insert, bookkeeping, evidence bump, commit) at a DIFFERENT position — so no
    row-level contention is possible and ONLY the advisory lock can make the second
    block (confirmed via ``pg_locks``) until the first commits. Both targets persist,
    the holder's first-blunder bookkeeping runs, and the evidence cursor advances."""
    user_id = 111
    sid_holder = _start_game(pg_client, auth_headers, user_id)
    sid_waiter = _start_game(pg_client, auth_headers, user_id)
    baseline_seq = _evidence_seq(pg_session_factory, user_id)

    holder = _record_target_cs(
        pg_session_factory, user_id, sid_holder,
        PGN_HOLDER, FEN_BEFORE_HOLDER, "Qh5", mark_first=True,
    )
    waiter = _record_target_cs(
        pg_session_factory, user_id, sid_waiter,
        PGN_WAITER, FEN_BEFORE_WAITER, "Nf3", mark_first=False,
    )

    holder_at, waiter_at = _run_serialization_case(
        pg_engine, user_id, holder, waiter
    )

    # Serialization is proven inside _run_serialization_case (the waiter blocked at
    # acquisition — pg_locks + no acquire record — while the holder held the lock).
    # Here we confirm both real transactions then committed cleanly.
    assert isinstance(holder_at, float) and isinstance(waiter_at, float)

    verify = pg_session_factory()
    try:
        # Both real recordings persisted their target...
        assert verify.query(Blunder).filter(Blunder.user_id == user_id).count() == 2
        # ...the holder's first-blunder bookkeeping ran (session flag set)...
        assert verify.get(GameSession, sid_holder).blunder_recorded is True
        # ...and the evidence cursor advanced (cursor bump under the lock).
        assert _evidence_seq(pg_session_factory, user_id) > baseline_seq
    finally:
        verify.close()


@pg_required
def test_worker_vs_recording_serialize(
    pg_engine, pg_session_factory, pg_client, auth_headers
):
    """The deferred evidence worker (real ``_run_graph_evidence_txn``) and a REAL blunder
    recording (``_record_target``) for the same user do not overlap and both commit with
    no 40P01: the worker holds the advisory lock through its graph upsert; the recording
    blocks at acquisition until the worker commits, then runs its own full transaction.
    Exercises BOTH module bindings of the helper (session + blunder)."""
    user_id = 222
    moves = [SessionMoveInput(**m) for m in _OPENING_MOVES]
    sid = _start_game(pg_client, auth_headers, user_id)

    def _worker_cs():
        db = pg_session_factory()
        try:
            session_api._run_graph_evidence_txn(
                db,
                session_id=uuid.uuid4(),
                user_id=user_id,
                player_color="white",
                evidence_moves=moves,
                move_count=len(moves),
                dialect_name="postgresql",
                run_opportunity=False,
            )
            return time.perf_counter()
        finally:
            db.close()

    recording = _record_target_cs(
        pg_session_factory, user_id, sid,
        PGN_WAITER, FEN_BEFORE_WAITER, "Nf3", mark_first=False,
    )

    worker_at, recording_at = _run_serialization_case(
        pg_engine, user_id, _worker_cs, recording
    )

    # Serialization is proven inside _run_serialization_case; both worker and recording
    # then committed cleanly (no exception, no 40P01).
    assert isinstance(worker_at, float) and isinstance(recording_at, float)

    verify = pg_session_factory()
    try:
        # Worker taught the e4 e5 line; the recording added its own target + positions.
        assert verify.query(Blunder).filter(Blunder.user_id == user_id).count() == 1
        assert verify.query(Position).filter(Position.user_id == user_id).count() >= 4
    finally:
        verify.close()


@pg_required
def test_recording_times_out_and_persists_nothing_when_lock_held(
    pg_engine, pg_session_factory, pg_client, auth_headers
):
    """When the per-user advisory lock is held elsewhere, a REAL blunder recording times
    out AT acquisition (SQLSTATE 55P03) BEFORE any entity write. The recording path does
    NOT retry or degrade (that is a worker-only policy): the error propagates and the
    transaction rolls back clean — no Position/Move graph, no Blunder target, no session
    flag, and no evidence-cursor bump persists."""
    user_id = 999
    sid = _start_game(pg_client, auth_headers, user_id)
    baseline_seq = _evidence_seq(pg_session_factory, user_id)

    # Hold the per-user advisory lock on a dedicated raw connection for the whole call.
    holder = pg_engine.connect()
    holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": user_id})
    db = pg_session_factory()
    try:
        session_row = db.get(GameSession, sid)
        user = TokenPayload(user_id=user_id, username="tester", is_anonymous=False)
        with patch.object(graph_write_lock, "GRAPH_LOCK_TIMEOUT", "300ms"):
            with pytest.raises(OperationalError) as excinfo:
                blunder_api._record_target(
                    db=db,
                    session=session_row,
                    user=user,
                    pgn=PGN_HOLDER,
                    fen=FEN_BEFORE_HOLDER,
                    user_move="Qh5",
                    best_move="Nf3",
                    eval_before=50,
                    eval_after=-100,
                    mark_first_blunder_recorded=True,
                    max_full_moves=10,
                )
        # Timed out on lock_not_available — not some other failure masquerading as one.
        assert getattr(excinfo.value.orig, "sqlstate", None) == "55P03"
        db.rollback()
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": user_id})
        holder.close()
        db.close()

    # Clean rollback: nothing from the recording survived.
    verify = pg_session_factory()
    try:
        assert verify.query(Position).filter(Position.user_id == user_id).count() == 0
        assert verify.query(Move).count() == 0
        assert verify.query(Blunder).filter(Blunder.user_id == user_id).count() == 0
        refetched = verify.get(GameSession, sid)
        assert refetched.blunder_recorded is False
        assert refetched.recorded_blunder_id is None
        assert _evidence_seq(pg_session_factory, user_id) == baseline_seq
    finally:
        verify.close()


@pg_required
def test_reverted_lock_reproduces_opposite_order_deadlock(
    pg_engine, pg_session_factory, pg_client
):
    """Reverted-lock control: with the advisory lock removed, two same-user writers that
    insert the SAME two positions in OPPOSITE order deadlock on the unique index —
    exactly the 40P01 the lock exists to prevent.

    This drives the shared writer ``_upsert_positions`` directly (rather than the full
    ``_record_target``) precisely so the test can pause each writer *after its first
    per-position flush*: that deterministic seam is what forces each to hold one of the
    two conflicting rows before contending for the other. Releasing them makes each
    block on the other's row → Postgres aborts exactly one victim (40P01) and the other
    commits. We assert one-victim/one-commit WITHOUT asserting which writer loses."""
    user_id = 333
    p = _position_spec(AFTER_D4_FEN)
    q = _position_spec(AFTER_C4_FEN)
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def _reverted_recording(role: str, first, second):
        def _cs():
            db = pg_session_factory()
            try:
                # Lock reverted (not acquired) — the pre-g-graph-lock behavior.
                blunder_api._upsert_positions(
                    db, user_id=user_id, positions_data=[first]
                )  # holds `first` uncommitted
                barrier.wait(timeout=20)  # both hold their first, opposite row
                blunder_api._upsert_positions(
                    db, user_id=user_id, positions_data=[second]
                )  # blocks on the other's row → deadlock
                db.commit()
                results[role] = "committed"
            except OperationalError as err:
                db.rollback()
                results[role] = getattr(getattr(err, "orig", None), "sqlstate", "?")
            finally:
                db.close()

        return _cs

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_reverted_recording("A", p, q)),
            pool.submit(_reverted_recording("B", q, p)),
        ]
        for f in futures:
            f.result(timeout=30)

    outcomes = sorted(results.values())
    # Exactly one 40P01 victim and exactly one commit; victim identity unasserted.
    assert outcomes == ["40P01", "committed"], results
