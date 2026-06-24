"""Postgres-backed tests for the per-user graph-write advisory lock + timeout
degrade path (g-q0aw).

These exercise behaviour SQLite cannot: ``pg_advisory_xact_lock(user_id)``
serialization of concurrent graph writes and the ``lock_timeout`` /
``statement_timeout`` guardrails on the graph-dependent evidence transaction.
Decorated with ``@pg_required`` so they skip cleanly when no Postgres URL is set.

The pure narrow-catch propagation test at the bottom needs no Postgres and runs
everywhere.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import app.api.session as session_api
from app.fen import fen_hash
from app.models import (
    AnalysisCache,
    Blunder,
    BlunderOpportunityEvent,
    Move,
    Position,
)
from conftest import pg_required

STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
AFTER_E4E5_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

# The e4 e5 opening line, posted as a /moves upload. Two valid edges teach the
# graph (start->e4, after_e4->e5) and three distinct positions.
_OPENING_MOVES = [
    {
        "move_number": 1,
        "color": "white",
        "move_san": "e4",
        "fen_before": STARTING_FEN,
        "fen_after": AFTER_E4_FEN,
        "move_uci": "e2e4",
        "eval_cp": 20,
        "best_move_san": "e4",
        "best_move_uci": "e2e4",
        "best_move_eval_cp": 20,
        "eval_delta": 0,
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
        "best_move_san": "e5",
        "best_move_uci": "e7e5",
        "best_move_eval_cp": 12,
        "eval_delta": 0,
        "classification": "best",
    },
]


def _start_game(pg_client, auth_headers, user_id: int) -> str:
    start = pg_client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=user_id),
    )
    assert start.status_code == 201
    return start.json()["session_id"]


def _post_opening(pg_client, auth_headers, session_id: str, user_id: int):
    return pg_client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": _OPENING_MOVES},
        headers=auth_headers(user_id=user_id),
    )


def _graph_counts(pg_session_factory, user_id: int) -> tuple[int, int]:
    """Return (position_count, move_count) for the user's ghost graph."""
    verify = pg_session_factory()
    try:
        positions = (
            verify.query(Position).filter(Position.user_id == user_id).all()
        )
        position_ids = [p.id for p in positions]
        move_count = (
            verify.query(Move)
            .filter(Move.from_position_id.in_(position_ids))
            .count()
            if position_ids
            else 0
        )
        return len(positions), move_count
    finally:
        verify.close()


def _seed_reachable_blunder(pg_session_factory, user_id: int) -> int:
    """Seed a Position the e4 e5 line lands on plus a Blunder at it, so the upload's
    opportunity-event computation has a row to write: the blunder is ``reached``
    (its position_id is one of the session positions). Returns the blunder id."""
    db = pg_session_factory()
    try:
        position = Position(
            user_id=user_id,
            fen_hash=fen_hash(AFTER_E4E5_FEN),
            fen_raw=AFTER_E4E5_FEN,
            active_color="white",
        )
        db.add(position)
        db.flush()
        blunder = Blunder(
            user_id=user_id,
            position_id=position.id,
            bad_move_san="bad",
            best_move_san="good",
            eval_loss_cp=200,
        )
        db.add(blunder)
        db.commit()
        return blunder.id
    finally:
        db.close()


def _opportunity_event_count(pg_session_factory, session_id: str) -> int:
    verify = pg_session_factory()
    try:
        return (
            verify.query(BlunderOpportunityEvent)
            .filter(BlunderOpportunityEvent.session_id == session_id)
            .count()
        )
    finally:
        verify.close()


@pg_required
def test_moves_concurrent_same_opening_serialize(
    pg_client, pg_session_factory, auth_headers
):
    """N concurrent same-user /moves replays of the same opening line serialize on
    the per-user advisory lock: all return 200 (no duplicate-key 500s) and the graph
    converges to exactly the deduped edge set — one Move per (from_position_id,
    move_san), three positions for the e4 e5 line."""
    user_id = 123
    session_ids = [
        _start_game(pg_client, auth_headers, user_id) for _ in range(5)
    ]

    def _post(session_id: str):
        return _post_opening(pg_client, auth_headers, session_id, user_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(session_ids)) as pool:
        responses = [r.result() for r in [pool.submit(_post, s) for s in session_ids]]

    assert all(r.status_code == 200 for r in responses), [
        (r.status_code, r.text) for r in responses
    ]
    position_count, move_count = _graph_counts(pg_session_factory, user_id)
    # start, after_e4, after_e5 deduped across all replays; two deduped edges.
    assert position_count == 3
    assert move_count == 2


@pg_required
def test_moves_graph_lock_timeout_degrades(
    pg_client, pg_engine, pg_session_factory, auth_headers, caplog
):
    """When the per-user advisory lock is held elsewhere for the whole request, the
    graph txn times out, retries once, times out again, and degrades: the request
    still returns 200, a WARNING names the skipped opportunity accounting, the graph
    write is rolled back, yet the analysis-cache write and recompute enqueue (outside
    the graph failure boundary) still run."""
    user_id = 321
    session_id = _start_game(pg_client, auth_headers, user_id)
    # A reachable blunder so a successful upload WOULD write an opportunity event;
    # under the degrade we assert that row is absent (accounting genuinely skipped),
    # not merely missing because nothing was reachable. The seed pre-creates the
    # blunder's position, so the rolled-back graph still leaves exactly 1 position.
    _seed_reachable_blunder(pg_session_factory, user_id)

    # Hold a session-level advisory lock on the same key on a DEDICATED raw
    # connection (kept checked out, never returned to the pool) so the request's
    # pg_advisory_xact_lock(user_id) — on a different pooled backend — can never be
    # acquired and blocks until lock_timeout fires.
    holder = pg_engine.connect()
    recompute_mock = MagicMock()
    try:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": user_id})

        start = time.perf_counter()
        with patch.object(session_api, "GRAPH_LOCK_TIMEOUT", "300ms"), patch.object(
            session_api, "request_recompute", recompute_mock
        ), caplog.at_level(logging.WARNING, logger="app.api.session"):
            response = _post_opening(pg_client, auth_headers, session_id, user_id)
        elapsed = time.perf_counter() - start
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": user_id})
        holder.close()

    assert response.status_code == 200
    assert response.json()["moves_inserted"] == len(_OPENING_MOVES)
    # Two lock waits (initial + retry), each bounded by lock_timeout=300ms; allow
    # generous headroom but assert it didn't hang near the 5s production default.
    assert elapsed < 3.0

    # The degrade WARNING names the skipped, non-self-healing opportunity accounting.
    assert any(
        rec.levelno == logging.WARNING and "not self-healing" in rec.getMessage()
        for rec in caplog.records
    ), [r.getMessage() for r in caplog.records]

    # Graph write rolled back: only the pre-seeded blunder position survives, and
    # no edges from the (rolled-back) upload were persisted.
    position_count, move_count = _graph_counts(pg_session_factory, user_id)
    assert position_count == 1
    assert move_count == 0
    # The skipped accounting is real: no opportunity event was written for the session
    # despite the reachable blunder.
    assert _opportunity_event_count(pg_session_factory, session_id) == 0

    # Continuation past the graph failure boundary: analysis-cache rows were written
    # and a recompute was enqueued despite the rollback.
    verify = pg_session_factory()
    try:
        cache_rows = verify.query(AnalysisCache).count()
    finally:
        verify.close()
    assert cache_rows == len(_OPENING_MOVES)
    recompute_mock.assert_called_once_with(user_id, "white")


@pg_required
def test_moves_graph_lock_retry_succeeds(
    pg_client, pg_engine, pg_session_factory, auth_headers, caplog
):
    """When the FIRST attempt times out on the held lock and the lock is released the
    instant that timeout is observed, the orchestrator's retry acquires the lock and
    succeeds: 200, the graph edges ARE written, the reachable blunder's opportunity
    event IS recorded, and no degrade WARNING is emitted.

    Release is driven off the observed first-attempt timeout (not a fixed sleep), so
    attempt 1 is guaranteed to hit the held lock and the test cannot pass by letting
    attempt 1 acquire the lock directly."""
    user_id = 654
    session_id = _start_game(pg_client, auth_headers, user_id)
    blunder_id = _seed_reachable_blunder(pg_session_factory, user_id)

    holder = pg_engine.connect()
    holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": user_id})
    released = {"done": False}

    real_txn = session_api._run_graph_evidence_txn
    attempts = {"n": 0}

    def _release_lock_after_first_timeout(db, **kwargs):
        attempts["n"] += 1
        try:
            return real_txn(db, **kwargs)
        except OperationalError:
            # Attempt 1 blocked on the held lock and hit lock_timeout. Release the
            # lock now (deterministically, right after the timeout) so the retry's
            # fresh acquisition finds it free. Re-raise so the orchestrator rolls
            # back and retries.
            if attempts["n"] == 1 and not released["done"]:
                holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": user_id})
                released["done"] = True
            raise

    try:
        with patch.object(session_api, "GRAPH_LOCK_TIMEOUT", "300ms"), patch.object(
            session_api,
            "_run_graph_evidence_txn",
            side_effect=_release_lock_after_first_timeout,
        ), caplog.at_level(logging.WARNING, logger="app.api.session"):
            response = _post_opening(pg_client, auth_headers, session_id, user_id)
    finally:
        if not released["done"]:
            holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": user_id})
        holder.close()

    assert response.status_code == 200
    # The first attempt timed out and the retry ran: exactly two attempts.
    assert attempts["n"] == 2
    # Retry committed the graph: the pre-seeded position plus the two upload edges.
    position_count, move_count = _graph_counts(pg_session_factory, user_id)
    assert position_count == 3
    assert move_count == 2
    # Opportunity accounting recovered: the reachable blunder's event is recorded.
    verify = pg_session_factory()
    try:
        event_count = (
            verify.query(BlunderOpportunityEvent)
            .filter(
                BlunderOpportunityEvent.session_id == session_id,
                BlunderOpportunityEvent.blunder_id == blunder_id,
            )
            .count()
        )
    finally:
        verify.close()
    assert event_count == 1
    # Recovery path, not the degrade path: no "skipped accounting" warning.
    assert not any(
        "not self-healing" in rec.getMessage() for rec in caplog.records
    ), [r.getMessage() for r in caplog.records]


def _operational_error(sqlstate: str) -> OperationalError:
    orig = Exception("boom")
    orig.sqlstate = sqlstate
    return OperationalError("SELECT 1", {}, orig)


def test_non_timeout_operational_error_propagates():
    """A non-timeout OperationalError (SQLSTATE outside {55P03, 57014}) from the graph
    stage is NOT swallowed by the narrow catch — it propagates so the request fails
    instead of silently dropping evidence. Guards against the catch widening."""
    db = MagicMock()
    with patch.object(
        session_api,
        "_run_graph_evidence_txn",
        side_effect=_operational_error("42P01"),  # undefined_table, not a timeout
    ):
        with pytest.raises(OperationalError):
            session_api._run_session_move_evidence_side_effects(
                db,
                session_id=None,
                user_id=1,
                player_color="white",
                evidence_moves=[],
                move_count=0,
                dialect_name="postgresql",
            )
    # Narrow catch did not run the post-boundary continuation.
    db.rollback.assert_not_called()


def test_timeout_twice_degrades_without_raising():
    """Two consecutive timeout SQLSTATEs degrade: the orchestrator rolls back, runs
    the post-boundary continuation (analysis cache + recompute), and does NOT raise."""
    db = MagicMock()
    with patch.object(
        session_api,
        "_run_graph_evidence_txn",
        side_effect=[_operational_error("55P03"), _operational_error("57014")],
    ), patch.object(session_api, "_upsert_analysis_cache") as cache_mock, patch.object(
        session_api, "request_recompute"
    ) as recompute_mock:
        session_api._run_session_move_evidence_side_effects(
            db,
            session_id=None,
            user_id=7,
            player_color="black",
            evidence_moves=[],
            move_count=0,
            dialect_name="postgresql",
        )
    assert db.rollback.call_count == 2  # one per failed attempt
    cache_mock.assert_called_once()
    recompute_mock.assert_called_once_with(7, "black")
