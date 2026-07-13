"""Cached session-accuracy write hooks (g-accuracy-hooks).

Release A maintains ``game_sessions.player_accuracy`` /
``player_accuracy_algo_version`` inside the serving writers — ``/api/game/end``
and post-end ``/api/session/{id}/moves`` — while stats and history keep computing
accuracy live. These tests pin:

* the bounded ``recompute_session_accuracy`` helper matches frozen v1, stamps a
  legitimate computed ``None``, and only ever touches an ended, VISIBLE session;
* game end after an awaited upload writes the first terminal value, and post-end
  uploads create / change / clear the cache while retaining algorithm version 1;
* active sessions and ended failed/abandoned drills keep both columns NULL,
  including after late uploads (population guard);
* the required flush order — move visibility -> recompute -> accuracy drain ->
  cursor — and the live-vs-ended query / PGN-parse counts;
* statement ordering pins the cache write before the evidence cursor for both
  writers;
* under real Postgres, a game-end-first-then-late-moves sequence heals and a
  moves-first-then-game-end sequence sees committed inputs, and the session
  FOR NO KEY UPDATE lock deterministically SERIALIZES overlapping /moves and
  /game/end transactions (holder/waiter proof with threads + events);
* a continued (converted, still active) drill defers to terminal-only
  computation;
* stats and history still use the live calculation with no read-time backfill.

The SQLite tests run everywhere; the interleaving proofs are ``@pg_required`` and
skip cleanly without a Postgres URL.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import app.accuracy as accuracy_mod
from app.accuracy import (
    ACCURACY_ALGO_VERSION,
    AccuracyMove,
    compute_game_accuracy,
    expected_total_moves_from_pgn,
    recompute_session_accuracy,
)
from app.models import GameSession, SessionMove
from app.opening_cache import current_evidence_seq
from conftest import engine, pg_required
from sql_capture import capture_statements, cursor_last_before_commit


PGN_TWO_PLY = "1. e4 e5"

# White plays a clean improving move (win% up -> 100), black replies. Two plies,
# matching PGN_TWO_PLY, so the game is "complete" and accuracy is computable.
MOVES_HIGH = [
    {"move_number": 1, "color": "white", "move_san": "e4", "fen_after": "f1w", "eval_cp": 20},
    {"move_number": 1, "color": "black", "move_san": "e5", "fen_after": "f1b", "eval_cp": -10},
]
# White's only move drops win% hard, so white's whole-game accuracy is < 100.
MOVES_LOW = [
    {"move_number": 1, "color": "white", "move_san": "e4", "fen_after": "f1w", "eval_cp": -300},
    {"move_number": 1, "color": "black", "move_san": "e5", "fen_after": "f1b", "eval_cp": -10},
]
# White's post-move eval is missing, so the player transition is unscoreable and
# whole-game accuracy is a legitimate None.
MOVES_CLEAR_WHITE = [
    {"move_number": 1, "color": "white", "move_san": "e4", "fen_after": "f1w", "eval_cp": None, "eval_mate": None},
]


# ---------------------------------------------------------------------------
# HTTP + DB helpers.
#
# ``headers=`` lets a caller build the auth headers OUTSIDE a capture block: the
# fixture idempotently seeds the backing users row, whose SQL (and possible
# commit) would otherwise land in the log and make the commit-count assertions
# rest on fixture state.
# ---------------------------------------------------------------------------
def _upload(client, auth_headers, session_id, moves, user_id=123, headers=None):
    return client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": moves},
        headers=headers if headers is not None else auth_headers(user_id=user_id),
    )


def _end(client, auth_headers, session_id, user_id=123, result="checkmate_win", pgn=PGN_TWO_PLY,
         is_rated=False, headers=None):
    return client.post(
        "/api/game/end",
        json={"session_id": str(session_id), "result": result, "pgn": pgn, "is_rated": is_rated},
        headers=headers if headers is not None else auth_headers(user_id=user_id),
    )


def _get(db, session_id) -> GameSession:
    db.expire_all()
    return db.query(GameSession).filter(GameSession.id == uuid.UUID(str(session_id))).one()


def _insert_session(db, **kwargs) -> GameSession:
    """Insert a GameSession row directly, bypassing the serving writers.

    Used to fabricate terminal drill states the /game/end handler refuses, and
    pre-existing unstamped sessions (player_accuracy NULL) for the no-backfill
    read tests.
    """
    defaults = dict(
        id=uuid.uuid4(),
        user_id=123,
        started_at=datetime.now(timezone.utc),
        status="ended",
        engine_elo=1500,
        player_color="white",
        session_mode="normal",
        is_rated=True,
    )
    defaults.update(kwargs)
    session = GameSession(**defaults)
    db.add(session)
    db.commit()
    return session


def _accs(moves) -> list[AccuracyMove]:
    return [AccuracyMove(color=m["color"], eval_cp=m.get("eval_cp"), eval_mate=m.get("eval_mate")) for m in moves]


def _expected_accuracy(moves) -> int | None:
    return compute_game_accuracy(_accs(moves), player_color="white",
                                 expected_total_moves=expected_total_moves_from_pgn(PGN_TWO_PLY))


def _thread_result(results: dict, key: str, fn):
    """Run ``fn`` on a thread, storing its return under ``key`` or its exception
    under ``key + "_exc"`` so the main thread can surface either."""
    def _wrapped() -> None:
        try:
            results[key] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-surfaced by the caller
            results[key + "_exc"] = exc
    return _wrapped


def _timed_waiter(results: dict, key: str, started: threading.Event, thunk):
    """Thread target for the blocked waiter in the serialization tests.

    Records the start time and signals ``started`` BEFORE issuing the (blocking)
    request, so the main thread can wait on ``started`` and guarantee the waiter's
    timer is already running before it begins the hold — otherwise a scheduling
    delay could push the recorded start past the hold window and make the
    ``wait >= hold`` assertion flake. Stores the elapsed wait under
    ``key + "_wait"`` and the response (via :func:`_thread_result`) under ``key``.
    """
    def run():
        t0 = time.perf_counter()
        started.set()
        resp = thunk()
        results[key + "_wait"] = time.perf_counter() - t0
        return resp
    return _thread_result(results, key, run)


def _pg_start(pg_client, auth_headers, user_id: int) -> str:
    resp = pg_client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=user_id),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def _add_moves(db, session_id, moves) -> None:
    for m in moves:
        db.add(
            SessionMove(
                session_id=uuid.UUID(str(session_id)),
                move_number=m["move_number"],
                color=m["color"],
                move_san=m["move_san"],
                fen_after=m["fen_after"],
                eval_cp=m.get("eval_cp"),
                eval_mate=m.get("eval_mate"),
            )
        )
    db.commit()


# ===========================================================================
# 1. Recompute helper: frozen-v1 parity, computed None, and the population guard.
# ===========================================================================
def test_recompute_helper_matches_frozen_v1(db_session):
    """The helper writes exactly what a direct frozen-v1 call produces for the same
    ordered moves, and stamps algorithm version 1."""
    session = _insert_session(db_session, pgn=PGN_TWO_PLY)
    _add_moves(db_session, session.id, MOVES_HIGH)

    expected = compute_game_accuracy(
        [AccuracyMove(color=m["color"], eval_cp=m.get("eval_cp"), eval_mate=m.get("eval_mate")) for m in MOVES_HIGH],
        player_color="white",
        expected_total_moves=expected_total_moves_from_pgn(PGN_TWO_PLY),
    )
    assert expected is not None  # sanity: this fixture is computable

    recompute_session_accuracy(db_session, session)
    assert session.player_accuracy == expected
    assert session.player_accuracy_algo_version == ACCURACY_ALGO_VERSION


def test_recompute_helper_stamps_computed_none(db_session):
    """A legitimate computed None (unscoreable player transition) is still assigned,
    and the algorithm version is stamped — an eligible session is never left
    half-stamped."""
    session = _insert_session(db_session, pgn=PGN_TWO_PLY)
    _add_moves(db_session, session.id, MOVES_CLEAR_WHITE)  # white eval missing -> None

    expected = compute_game_accuracy(
        [AccuracyMove(color="white", eval_cp=None, eval_mate=None)],
        player_color="white",
        expected_total_moves=expected_total_moves_from_pgn(PGN_TWO_PLY),
    )
    assert expected is None

    recompute_session_accuracy(db_session, session)
    assert session.player_accuracy is None
    assert session.player_accuracy_algo_version == ACCURACY_ALGO_VERSION


def test_recompute_helper_leaves_active_session_untouched(db_session):
    """An active session is ineligible: the helper returns before touching either
    cache field (preset sentinels survive)."""
    session = _insert_session(db_session, status="active", pgn=PGN_TWO_PLY,
                              player_accuracy=None, player_accuracy_algo_version=None)
    _add_moves(db_session, session.id, MOVES_HIGH)
    session.player_accuracy = 42
    session.player_accuracy_algo_version = 7

    recompute_session_accuracy(db_session, session)
    assert session.player_accuracy == 42
    assert session.player_accuracy_algo_version == 7


def test_recompute_helper_leaves_ended_failed_drill_untouched(db_session):
    """An ended but non-visible (failed) drill is ineligible: both fields untouched."""
    session = _insert_session(
        db_session, session_mode="drill", drill_state="failed", is_rated=False,
        pgn=PGN_TWO_PLY, player_accuracy=None, player_accuracy_algo_version=None,
    )
    _add_moves(db_session, session.id, MOVES_HIGH)

    recompute_session_accuracy(db_session, session)
    assert session.player_accuracy is None
    assert session.player_accuracy_algo_version is None


def test_recompute_helper_leaves_ended_abandoned_drill_untouched(db_session):
    session = _insert_session(
        db_session, session_mode="drill", drill_state="abandoned", is_rated=False,
        pgn=PGN_TWO_PLY, player_accuracy=None, player_accuracy_algo_version=None,
    )
    _add_moves(db_session, session.id, MOVES_HIGH)

    recompute_session_accuracy(db_session, session)
    assert session.player_accuracy is None
    assert session.player_accuracy_algo_version is None


def test_recompute_helper_never_commits(db_session):
    """The helper leaves the transaction open: its accuracy assignment is a dirty,
    uncommitted change until the caller commits."""
    session = _insert_session(db_session, pgn=PGN_TWO_PLY)
    _add_moves(db_session, session.id, MOVES_HIGH)

    recompute_session_accuracy(db_session, session)
    assert session in db_session.dirty  # pending, not yet flushed/committed
    db_session.rollback()
    assert _get(db_session, session.id).player_accuracy is None  # rollback discarded it


# ===========================================================================
# 2. Game end + post-end uploads: create / change / clear at version 1.
# ===========================================================================
def test_game_end_after_awaited_upload_computes_first_terminal_value(
    client, auth_headers, create_game_session, db_session
):
    """Uploading while active leaves the cache NULL; game end computes the first
    terminal value at version 1."""
    sid = create_game_session(user_id=123)
    assert _upload(client, auth_headers, sid, MOVES_HIGH).status_code == 200

    active = _get(db_session, sid)
    assert active.player_accuracy is None
    assert active.player_accuracy_algo_version is None

    assert _end(client, auth_headers, sid).status_code == 200
    ended = _get(db_session, sid)
    assert ended.player_accuracy is not None
    assert ended.player_accuracy_algo_version == ACCURACY_ALGO_VERSION


def test_post_end_upload_changes_accuracy_and_retains_version(
    client, auth_headers, create_game_session, db_session
):
    """A post-end eval-backfill upload recomputes the cache to a different value and
    keeps algorithm version 1."""
    sid = create_game_session(user_id=123)
    _upload(client, auth_headers, sid, MOVES_HIGH)
    _end(client, auth_headers, sid)
    first = _get(db_session, sid).player_accuracy
    assert first is not None

    assert _upload(client, auth_headers, sid, MOVES_LOW).status_code == 200
    after = _get(db_session, sid)
    assert after.player_accuracy is not None
    assert after.player_accuracy != first  # the blundered white move lowered it
    assert after.player_accuracy_algo_version == ACCURACY_ALGO_VERSION


def test_post_end_upload_can_clear_accuracy(
    client, auth_headers, create_game_session, db_session
):
    """A post-end upload that removes a scored player eval clears the cache to None
    while still stamping version 1."""
    sid = create_game_session(user_id=123)
    _upload(client, auth_headers, sid, MOVES_HIGH)
    _end(client, auth_headers, sid)
    assert _get(db_session, sid).player_accuracy is not None

    assert _upload(client, auth_headers, sid, MOVES_CLEAR_WHITE).status_code == 200
    after = _get(db_session, sid)
    assert after.player_accuracy is None
    assert after.player_accuracy_algo_version == ACCURACY_ALGO_VERSION


# ===========================================================================
# 3. Population guard at the endpoint level.
# ===========================================================================
def test_active_normal_upload_keeps_cache_null(
    client, auth_headers, create_game_session, db_session
):
    sid = create_game_session(user_id=123)
    _upload(client, auth_headers, sid, MOVES_HIGH)
    row = _get(db_session, sid)
    assert row.player_accuracy is None
    assert row.player_accuracy_algo_version is None


def test_post_end_upload_to_failed_drill_keeps_cache_null(
    client, auth_headers, db_session
):
    """Late uploads to an ended failed drill never stamp the cache."""
    session = _insert_session(
        db_session, session_mode="drill", drill_state="failed", is_rated=False, pgn=PGN_TWO_PLY,
    )
    assert _upload(client, auth_headers, session.id, MOVES_HIGH).status_code == 200
    row = _get(db_session, session.id)
    assert row.player_accuracy is None
    assert row.player_accuracy_algo_version is None


def test_post_end_upload_to_abandoned_drill_keeps_cache_null(
    client, auth_headers, db_session
):
    session = _insert_session(
        db_session, session_mode="drill", drill_state="abandoned", is_rated=False, pgn=PGN_TWO_PLY,
    )
    assert _upload(client, auth_headers, session.id, MOVES_HIGH).status_code == 200
    row = _get(db_session, session.id)
    assert row.player_accuracy is None
    assert row.player_accuracy_algo_version is None


def test_converted_drill_continue_then_late_moves_preserves_terminal_only(
    client, auth_headers, db_session
):
    """A continued (converted, still ACTIVE) drill defers to terminal-only
    computation: late uploads leave the cache NULL; only /game/end computes it."""
    now = datetime.now(timezone.utc)
    session = _insert_session(
        db_session,
        status="active",
        session_mode="drill",
        drill_state="converted",
        is_rated=True,
        normal_started_at=now,
        converted_at=now,
        rated_start_ply=0,
        pgn=PGN_TWO_PLY,
    )
    # Late upload while still active -> stays NULL (not terminal yet).
    with patch("app.api.session.enqueue_session_evidence", lambda *a, **k: None):
        assert _upload(client, auth_headers, session.id, MOVES_HIGH).status_code == 200
    assert _get(db_session, session.id).player_accuracy is None

    # /game/end accepts a converted drill and computes the terminal value.
    assert _end(client, auth_headers, session.id).status_code == 200
    ended = _get(db_session, session.id)
    assert ended.player_accuracy is not None
    assert ended.player_accuracy_algo_version == ACCURACY_ALGO_VERSION


# ===========================================================================
# 4. Query contract: live upload does no eval work; ended-visible does one of each.
# ===========================================================================
def test_live_upload_does_no_move_query_or_pgn_parse(
    client, auth_headers, create_game_session
):
    """An upload to an ACTIVE session adds only the one entry lock and performs no
    recompute move query, no PGN parse, and no accuracy computation."""
    sid = create_game_session(user_id=123)
    with patch.object(accuracy_mod, "compute_game_accuracy", wraps=accuracy_mod.compute_game_accuracy) as spy_compute, \
         patch.object(accuracy_mod, "expected_total_moves_from_pgn", wraps=accuracy_mod.expected_total_moves_from_pgn) as spy_pgn, \
         capture_statements() as log:
        assert _upload(client, auth_headers, sid, MOVES_HIGH).status_code == 200

    assert spy_compute.call_count == 0
    assert spy_pgn.call_count == 0
    pre = log.statements_before_first_commit()
    move_selects = [s for s in pre if s.startswith("select") and "from session_moves" in s]
    assert move_selects == []  # recompute issued no move query
    session_locks = [s for s in pre if s.startswith("select") and "from game_sessions" in s]
    assert len(session_locks) == 1  # exactly the one entry lock


def test_ended_visible_upload_does_one_move_query_and_one_pgn_parse(
    client, auth_headers, create_game_session
):
    """A post-end upload performs exactly one entry lock, one ordered move query, and
    one PGN parse."""
    sid = create_game_session(user_id=123)
    _upload(client, auth_headers, sid, MOVES_HIGH)
    _end(client, auth_headers, sid)

    with patch.object(accuracy_mod, "compute_game_accuracy", wraps=accuracy_mod.compute_game_accuracy) as spy_compute, \
         patch.object(accuracy_mod, "expected_total_moves_from_pgn", wraps=accuracy_mod.expected_total_moves_from_pgn) as spy_pgn, \
         capture_statements() as log:
        assert _upload(client, auth_headers, sid, MOVES_LOW).status_code == 200

    assert spy_compute.call_count == 1
    assert spy_pgn.call_count == 1
    pre = log.statements_before_first_commit()
    move_selects = [s for s in pre if s.startswith("select") and "from session_moves" in s]
    assert len(move_selects) == 1  # the single ordered recompute query
    assert "order by" in move_selects[0]
    session_locks = [s for s in pre if s.startswith("select") and "from game_sessions" in s]
    assert len(session_locks) == 1


# ===========================================================================
# 5. Statement ordering: cache write lands before the evidence cursor.
# ===========================================================================
def test_moves_core_path_accuracy_write_precedes_cursor_which_is_last(
    client, auth_headers, create_game_session, db_session
):
    """Core-upsert path: the game_sessions accuracy UPDATE flushes before the
    evidence-cursor bump, which is the transaction's final statement — and the
    bump commits."""
    headers = auth_headers(user_id=123)  # seeds the users row OUTSIDE the capture
    sid = create_game_session(user_id=123)
    _upload(client, auth_headers, sid, MOVES_HIGH, headers=headers)
    _end(client, auth_headers, sid, headers=headers)
    seq_before = current_evidence_seq(db_session, 123, "white")

    # The autouse _sync_session_evidence shim folds the deferred graph work — which
    # commits on its own (session.py ``_run_graph_evidence_txn``) — into the request,
    # adding a SECOND commit that production never has (there it runs on another
    # thread, outside this transaction). No-op it so the capture sees the endpoint's
    # own transaction, exactly as the generic-ORM test below already does.
    with patch("app.api.session.enqueue_session_evidence", lambda *a, **k: None), \
         capture_statements() as log:
        assert _upload(client, auth_headers, sid, MOVES_LOW, headers=headers).status_code == 200

    pre, cursor_idx = cursor_last_before_commit(log)
    accuracy_idx = next(i for i, s in enumerate(pre)
                        if s.startswith("update game_sessions") and "player_accuracy" in s)
    assert accuracy_idx < cursor_idx, pre

    db_session.expire_all()
    assert current_evidence_seq(db_session, 123, "white") == seq_before + 1


def test_game_end_accuracy_write_precedes_cursor_which_is_last(
    client, auth_headers, create_game_session, db_session
):
    """Game end: the terminal+accuracy game_sessions UPDATE flushes before the
    evidence-cursor bump, which is the transaction's final statement — and the
    bump commits."""
    headers = auth_headers(user_id=123)
    sid = create_game_session(user_id=123)
    _upload(client, auth_headers, sid, MOVES_HIGH, headers=headers)
    seq_before = current_evidence_seq(db_session, 123, "white")

    # A non-abandon end computes the opening-score delta post-commit, which lazily
    # imports request_recompute from its source module — past the autouse fixture's
    # bound-alias patches. Unpatched, the real scheduler singleton would start a
    # worker thread against the CONFIGURED (non-test) database.
    with patch("app.opening_score_scheduler.request_recompute"), \
         capture_statements() as log:
        assert _end(client, auth_headers, sid, headers=headers).status_code == 200

    pre, cursor_idx = cursor_last_before_commit(log)
    accuracy_idx = next(i for i, s in enumerate(pre)
                        if s.startswith("update game_sessions") and "player_accuracy" in s)
    assert accuracy_idx < cursor_idx, pre

    db_session.expire_all()
    assert current_evidence_seq(db_session, 123, "white") == seq_before + 1


def test_generic_orm_path_flushes_moves_then_recompute_then_accuracy_then_cursor(
    client, auth_headers, create_game_session, db_session, monkeypatch
):
    """Generic (non-sqlite/postgres) ORM path: the move add/mutate flushes for
    visibility BEFORE the recompute query, and the accuracy UPDATE drains BEFORE the
    cursor, which is the transaction's final statement. Forcing a foreign dialect
    name routes through the generic ``bump_evidence_seq`` branch — the one a new
    dialect would run — while still executing against SQLite."""
    headers = auth_headers(user_id=123)
    sid = create_game_session(user_id=123)
    _upload(client, auth_headers, sid, MOVES_HIGH, headers=headers)
    _end(client, auth_headers, sid, headers=headers)
    seq_before = current_evidence_seq(db_session, 123, "white")

    monkeypatch.setattr(engine.dialect, "name", "genericdb")
    with patch("app.api.session.enqueue_session_evidence", lambda *a, **k: None), \
         capture_statements() as log:
        assert _upload(client, auth_headers, sid, MOVES_LOW, headers=headers).status_code == 200

    pre, cursor_idx = cursor_last_before_commit(log)
    move_write_idxs = [i for i, s in enumerate(pre)
                       if s.startswith(("insert into session_moves", "update session_moves"))]
    recompute_idx = next(i for i, s in enumerate(pre)
                         if s.startswith("select") and "from session_moves" in s and "order by" in s)
    accuracy_idx = next(i for i, s in enumerate(pre)
                        if s.startswith("update game_sessions") and "player_accuracy" in s)

    assert move_write_idxs, pre
    assert max(move_write_idxs) < recompute_idx, pre  # move visibility before recompute
    assert recompute_idx < accuracy_idx, pre          # recompute before accuracy drain
    assert accuracy_idx < cursor_idx, pre             # accuracy drain before cursor

    db_session.expire_all()
    assert current_evidence_seq(db_session, 123, "white") == seq_before + 1


# ===========================================================================
# 6. Reads stay live: no cache-read switch, no read-time backfill.
# ===========================================================================
def test_history_reads_live_accuracy_and_does_not_backfill(
    client, auth_headers, create_game_session, db_session
):
    """History computes accuracy live: a poisoned cache column is ignored on read and
    is not rewritten (no backfill)."""
    sid = create_game_session(user_id=123)
    _upload(client, auth_headers, sid, MOVES_HIGH)
    _end(client, auth_headers, sid)
    live = client.get("/api/history", headers=auth_headers(user_id=123)).json()["games"][0]["summary"]["accuracy"]
    assert live is not None

    # Poison the cache with a value the live calculation cannot produce here.
    poisoned = (live + 1) % 100
    row = _get(db_session, sid)
    row.player_accuracy = poisoned
    db_session.commit()

    again = client.get("/api/history", headers=auth_headers(user_id=123)).json()["games"][0]["summary"]["accuracy"]
    assert again == live  # read ignored the poisoned cache
    assert _get(db_session, sid).player_accuracy == poisoned  # read did not backfill/rewrite


def test_stats_summary_reads_live_accuracy_and_does_not_backfill(
    client, auth_headers, db_session
):
    """Stats summary computes accuracy live and never stamps a pre-existing NULL cache
    on read."""
    session = _insert_session(db_session, pgn=PGN_TWO_PLY, player_accuracy=None,
                              player_accuracy_algo_version=None)
    _add_moves(db_session, session.id, MOVES_HIGH)

    data = client.get("/api/stats/summary", headers=auth_headers(user_id=123)).json()
    assert data["moves"]["accuracy_pct"] is not None  # computed live from the moves

    row = _get(db_session, session.id)
    assert row.player_accuracy is None  # read did not backfill the NULL cache
    assert row.player_accuracy_algo_version is None


# ===========================================================================
# 7. Postgres: committed orderings (sequential) heal / see committed inputs.
# ===========================================================================
@pg_required
def test_pg_game_end_first_then_late_moves_heals(pg_client, pg_session_factory, auth_headers):
    """Ending before the moves arrive computes None (incomplete); a late upload of the
    missing moves heals the cache to a real value — every writer recomputes."""
    user_id = 8801
    sid = _pg_start(pg_client, auth_headers, user_id)

    # Only white's move is present at end time -> 1 move < 2 expected plies -> None.
    _upload(pg_client, auth_headers, sid, [MOVES_HIGH[0]], user_id=user_id)
    assert _end(pg_client, auth_headers, sid, user_id=user_id).status_code == 200

    verify = pg_session_factory()
    try:
        row = verify.query(GameSession).filter(GameSession.id == uuid.UUID(sid)).one()
        assert row.player_accuracy is None
        assert row.player_accuracy_algo_version == ACCURACY_ALGO_VERSION
    finally:
        verify.close()

    # The late upload completes the game; recompute on the upload heals the cache.
    assert _upload(pg_client, auth_headers, sid, [MOVES_HIGH[1]], user_id=user_id).status_code == 200
    verify = pg_session_factory()
    try:
        row = verify.query(GameSession).filter(GameSession.id == uuid.UUID(sid)).one()
        assert row.player_accuracy == _expected_accuracy(MOVES_HIGH)
        assert row.player_accuracy_algo_version == ACCURACY_ALGO_VERSION
    finally:
        verify.close()


@pg_required
def test_pg_moves_first_then_game_end_sees_committed_inputs(pg_client, pg_session_factory, auth_headers):
    """Committing the moves before the end means the game-end recompute reads them and
    computes a real terminal value."""
    user_id = 8802
    sid = _pg_start(pg_client, auth_headers, user_id)

    assert _upload(pg_client, auth_headers, sid, MOVES_HIGH, user_id=user_id).status_code == 200
    assert _end(pg_client, auth_headers, sid, user_id=user_id).status_code == 200

    verify = pg_session_factory()
    try:
        row = verify.query(GameSession).filter(GameSession.id == uuid.UUID(sid)).one()
        assert row.player_accuracy == _expected_accuracy(MOVES_HIGH)
        assert row.player_accuracy_algo_version == ACCURACY_ALGO_VERSION
    finally:
        verify.close()


# ===========================================================================
# 8. Postgres: the session NKU lock SERIALIZES overlapping writers.
#
# The sequential tests above prove both committed orderings produce correct
# results, but not that the entry lock actually serializes CONCURRENT /moves and
# /game/end transactions — a missing or misplaced lock would still pass them.
# These pin serialization deterministically: pause the holder mid-transaction
# (parked at the post-lock recompute seam) while it holds the session's
# FOR NO KEY UPDATE lock, prove a concurrent writer on the same session BLOCKS at
# its own entry lock, then release the holder and prove the waiter ran strictly
# after (its wait spanned the hold; the final cache reflects the later writer).
# ===========================================================================
_HOLD_SECONDS = 0.6


def _lock_gate(holder_reached: threading.Event, release: threading.Event):
    """Wrap the real recompute so the FIRST caller to reach it (the lock holder)
    signals and then parks until released; a later caller (the waiter, which only
    reaches here after acquiring the lock) sails through the already-set gate."""
    real = accuracy_mod.recompute_session_accuracy

    def gate(db, session):
        holder_reached.set()
        release.wait(timeout=30)
        return real(db, session)

    return gate


@pg_required
def test_pg_game_end_lock_serializes_concurrent_late_moves(pg_client, pg_session_factory, auth_headers):
    """While /game/end holds the session lock (parked mid-transaction), a concurrent
    late /moves upload to the same session blocks at its entry lock until /game/end
    commits, then runs — so the final cache reflects the serialized later writer."""
    user_id = 8811
    sid = _pg_start(pg_client, auth_headers, user_id)
    _upload(pg_client, auth_headers, sid, MOVES_HIGH, user_id=user_id)  # committed before either writer

    high, low = _expected_accuracy(MOVES_HIGH), _expected_accuracy(MOVES_LOW)
    assert high is not None and low is not None and high != low  # the two writers are distinguishable

    holder_reached, release = threading.Event(), threading.Event()
    results: dict = {}

    with patch("app.api.game.recompute_session_accuracy", _lock_gate(holder_reached, release)):
        end_thread = threading.Thread(
            target=_thread_result(results, "end", lambda: _end(pg_client, auth_headers, sid, user_id=user_id))
        )
        end_thread.start()
        assert holder_reached.wait(timeout=10), "/game/end never reached the lock-holding seam"

        waiter_started = threading.Event()
        moves_thread = threading.Thread(target=_timed_waiter(
            results, "moves", waiter_started,
            lambda: _upload(pg_client, auth_headers, sid, MOVES_LOW, user_id=user_id),
        ))
        moves_thread.start()
        assert waiter_started.wait(timeout=10)  # the waiter's timer is running before the hold begins
        try:
            time.sleep(_HOLD_SECONDS)
            blocked_during_hold = "moves" not in results  # cannot complete while /game/end holds the lock
        finally:
            release.set()
            end_thread.join(timeout=20)
            moves_thread.join(timeout=20)

    assert "end_exc" not in results, results.get("end_exc")
    assert "moves_exc" not in results, results.get("moves_exc")
    assert results["end"].status_code == 200, results["end"].text
    assert results["moves"].status_code == 200, results["moves"].text
    assert blocked_during_hold, "concurrent /moves completed while /game/end held the session lock"
    assert results["moves_wait"] >= _HOLD_SECONDS  # the waiter blocked on the lock the holder held

    verify = pg_session_factory()
    try:
        row = verify.query(GameSession).filter(GameSession.id == uuid.UUID(sid)).one()
        assert row.player_accuracy == low  # the serialized later writer (/moves) won
        assert row.player_accuracy_algo_version == ACCURACY_ALGO_VERSION
    finally:
        verify.close()


@pg_required
def test_pg_moves_lock_serializes_concurrent_game_end(pg_client, pg_session_factory, auth_headers):
    """Symmetric proof: while a /moves upload holds the session lock (parked
    mid-transaction) on a still-active session, a concurrent /game/end blocks at its
    entry lock until /moves commits, then ends the game reading the committed moves —
    so the terminal cache is computed from the serialized earlier writer's inputs."""
    user_id = 8812
    sid = _pg_start(pg_client, auth_headers, user_id)
    _upload(pg_client, auth_headers, sid, MOVES_HIGH, user_id=user_id)  # active; accuracy stays NULL

    high, low = _expected_accuracy(MOVES_HIGH), _expected_accuracy(MOVES_LOW)
    assert high is not None and low is not None and high != low

    holder_reached, release = threading.Event(), threading.Event()
    results: dict = {}

    with patch("app.api.session.recompute_session_accuracy", _lock_gate(holder_reached, release)):
        # Holder: /moves rewrites the moves to MOVES_LOW and parks (recompute is a
        # no-op while active, but the gate still holds the lock open).
        moves_thread = threading.Thread(
            target=_thread_result(results, "moves", lambda: _upload(pg_client, auth_headers, sid, MOVES_LOW, user_id=user_id))
        )
        moves_thread.start()
        assert holder_reached.wait(timeout=10), "/moves never reached the lock-holding seam"

        waiter_started = threading.Event()
        end_thread = threading.Thread(target=_timed_waiter(
            results, "end", waiter_started,
            lambda: _end(pg_client, auth_headers, sid, user_id=user_id),
        ))
        end_thread.start()
        assert waiter_started.wait(timeout=10)  # the waiter's timer is running before the hold begins
        try:
            time.sleep(_HOLD_SECONDS)
            blocked_during_hold = "end" not in results
        finally:
            release.set()
            moves_thread.join(timeout=20)
            end_thread.join(timeout=20)

    assert "moves_exc" not in results, results.get("moves_exc")
    assert "end_exc" not in results, results.get("end_exc")
    assert results["moves"].status_code == 200, results["moves"].text
    assert results["end"].status_code == 200, results["end"].text
    assert blocked_during_hold, "concurrent /game/end completed while /moves held the session lock"
    assert results["end_wait"] >= _HOLD_SECONDS

    verify = pg_session_factory()
    try:
        row = verify.query(GameSession).filter(GameSession.id == uuid.UUID(sid)).one()
        # /game/end read the moves /moves committed (MOVES_LOW) -> low; a concurrent
        # read of the pre-holder state (MOVES_HIGH) would have produced `high`.
        assert row.player_accuracy == low
        assert row.player_accuracy_algo_version == ACCURACY_ALGO_VERSION
    finally:
        verify.close()
