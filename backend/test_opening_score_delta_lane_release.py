"""Restored-production-dump latency gate for g-delta-priority-lane.

This module is intentionally excluded from pre-push. It connects to a disposable
PostgreSQL 18 database copied from the restored production-dump template, runs
real opening overlays/scoring/publication, and mutates only counters, scoped
publications, and recompute batches inside that disposable copy.

See ``scripts/BENCH_OPENING_SCORE_DELTA_LANE.md`` for the exact setup and command.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

import app.opening_cache as opening_cache
from app.models import EvidenceEpoch, GameSession, SessionMove
from app.opening_cache import bump_evidence_seq, recompute_opening_scores
from app.opening_evidence import (
    SESSION_EVIDENCE_ELIGIBLE_SQL,
    overlay_evidence,
    reset_session_evidence_cache,
)
from app.opening_graph import get_opening_graph
from app.opening_roots import get_opening_roots, played_opening_chain
from app.opening_score_delta import (
    _session_played_fens,
    capture_baseline_watermark,
    publish_scoped_opening_score_deltas,
    read_opening_score_delta,
    reset_scoped_delta_cache,
    run_baseline_snapshot_job,
)
from app.opening_score_delta_lane import OpeningScoreDeltaLane

pytestmark = pytest.mark.release_seal

_DATABASE_ENV = "GHOSTREPLAY_DELTA_BENCH_DATABASE_URL"
_PROTECTED_DATABASES = {"postgres", "railway", "gr_snap_base"}
_P95_LIMIT_MS = 3000.0


@dataclass
class _Attempt:
    dispatch_at: float | None = None
    report: dict[str, object] = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)


class _TimedPublisher:
    """Capture the lane's real queue boundary and Phase-2 timing callback."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempt: _Attempt | None = None

    def begin(self) -> _Attempt:
        attempt = _Attempt()
        with self._lock:
            if self._attempt is not None and not self._attempt.done.is_set():
                raise AssertionError("overlapping benchmark attempts")
            self._attempt = attempt
        return attempt

    def __call__(
        self,
        db,
        user_id,
        player_color,
        requests,
        *,
        on_complete,
    ):
        with self._lock:
            attempt = self._attempt
        if attempt is None:
            raise AssertionError("publisher called without a benchmark attempt")
        attempt.dispatch_at = time.perf_counter()

        def capture(report):
            attempt.report = dict(report)
            on_complete(report)

        try:
            return publish_scoped_opening_score_deltas(
                db,
                user_id,
                player_color,
                requests,
                on_complete=capture,
            )
        finally:
            attempt.done.set()


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _summarize(records: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    keys = sorted({key for record in records for key in record})
    return {
        key: {
            "median_ms": round(statistics.median(record[key] for record in records), 3),
            "p95_ms": round(_p95([record[key] for record in records]), 3),
        }
        for key in keys
    }


def _database_url() -> str:
    raw = os.getenv(_DATABASE_ENV)
    if not raw:
        pytest.skip(f"{_DATABASE_ENV} is required for the restored-dump gate")
    url = make_url(raw)
    if not url.drivername.startswith("postgresql"):
        pytest.fail(f"{_DATABASE_ENV} must use PostgreSQL")
    if url.database in _PROTECTED_DATABASES:
        pytest.fail(
            f"{_DATABASE_ENV} must name a disposable copy, not {url.database!r}"
        )
    return raw


def _heavy_pair(db) -> tuple[int, str]:
    row = db.execute(
        text(
            f"""
            SELECT gs.user_id, gs.player_color
            FROM game_sessions gs
            JOIN session_moves sm ON sm.session_id = gs.id
            WHERE {SESSION_EVIDENCE_ELIGIBLE_SQL}
            GROUP BY gs.user_id, gs.player_color
            HAVING bool_or(
                gs.session_mode = 'normal'
                AND gs.status = 'ended'
                AND gs.result <> 'abandon'
            )
            AND bool_or(
                gs.session_mode = 'drill'
                AND (
                    (
                        gs.status = 'active'
                        AND gs.drill_state = 'failed'
                        AND gs.drill_terminal_reason = 'accuracy'
                    )
                    OR (
                        gs.status = 'ended'
                        AND (
                            gs.drill_state = 'converted'
                            OR gs.drill_terminal_reason = 'natural_end'
                        )
                    )
                )
            )
            ORDER BY count(sm.id) DESC
            LIMIT 1
            """
        )
    ).one_or_none()
    if row is None:
        pytest.fail("restored dump has no heavy pair with normal and drill terminals")
    return int(row[0]), str(row[1])


def _terminal_session_id(db, user_id: int, color: str, mode: str) -> uuid.UUID:
    query = (
        db.query(GameSession, func.count(SessionMove.id).label("move_count"))
        .join(SessionMove, SessionMove.session_id == GameSession.id)
        .filter(
            GameSession.user_id == user_id,
            GameSession.player_color == color,
            GameSession.session_mode == mode,
        )
    )
    if mode == "normal":
        query = query.filter(
            GameSession.status == "ended",
            GameSession.result != "abandon",
        )
    else:
        query = query.filter(
            (
                (GameSession.status == "active")
                & (GameSession.drill_state == "failed")
                & (GameSession.drill_terminal_reason == "accuracy")
            )
            | (
                (GameSession.status == "ended")
                & (
                    (GameSession.drill_state == "converted")
                    | (GameSession.drill_terminal_reason == "natural_end")
                )
            )
        )
    candidates = (
        query.group_by(GameSession.id)
        .order_by(text("move_count DESC"))
        .limit(100)
        .all()
    )
    roots = get_opening_roots()
    for session, _move_count in candidates:
        if played_opening_chain(_session_played_fens(db, session.id), roots):
            return session.id
    pytest.fail(f"restored dump has no {mode} terminal with a played opening")


def _bump_user_sequence(session_factory, user_id: int, color: str) -> None:
    with session_factory() as db:
        bump_evidence_seq(db, user_id, color)
        db.commit()


def _bump_shared_epoch(session_factory) -> None:
    with session_factory() as db:
        db.execute(
            update(EvidenceEpoch)
            .where(EvidenceEpoch.id == 1)
            .values(value=EvidenceEpoch.value + 1)
        )
        db.commit()


def _run_lane_once(
    lane: OpeningScoreDeltaLane,
    publisher: _TimedPublisher,
    session_factory,
    *,
    user_id: int,
    color: str,
    session_id: uuid.UUID,
    timeout: float = 45.0,
) -> dict[str, float]:
    attempt = publisher.begin()
    started = time.perf_counter()
    lane.enqueue(user_id, color, session_id)
    deadline = started + timeout
    final_poll_ms = 0.0
    while True:
        poll_started = time.perf_counter()
        with session_factory() as db:
            session = db.get(GameSession, session_id)
            _items, is_fresh = read_opening_score_delta(db, session)
        final_poll_ms = (time.perf_counter() - poll_started) * 1000.0
        # A pre-existing fresh batch must not end the measurement before the lane
        # publication itself completes (important in idle/baseline cells).
        if is_fresh and attempt.done.is_set():
            finished = time.perf_counter()
            break
        if time.perf_counter() >= deadline:
            raise AssertionError("delta lane did not publish a fresh result before timeout")
        time.sleep(0.01)

    if attempt.dispatch_at is None:
        raise AssertionError("lane completion had no dispatch timestamp")
    report = attempt.report
    assert report.get("outcome") == "published", report
    assert report.get("published_count") == 1, report
    stage_ms = report.get("stage_ms")
    assert isinstance(stage_ms, dict)
    record = {
        "queue_to_dispatch": (attempt.dispatch_at - started) * 1000.0,
        "poll_read": final_poll_ms,
        "end_to_end": (finished - started) * 1000.0,
    }
    for name in ("session_load", "counter", "overlay", "digest", "score", "publish"):
        value = stage_ms.get(name)
        if value is None:
            raise AssertionError(f"missing scoped stage timing {name}: {report}")
        record[name] = float(value)
    return record


def test_restored_dump_terminal_lane_p95_under_whole_graph_contention(monkeypatch):
    engine = create_engine(_database_url(), pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    reset_scoped_delta_cache()
    reset_session_evidence_cache()
    publisher = _TimedPublisher()
    lane = OpeningScoreDeltaLane(
        session_factory=session_factory,
        publish=publisher,
        auto_start=True,
    )

    with session_factory() as db:
        version = db.execute(text("SHOW server_version_num")).scalar_one()
        assert int(version) // 10000 == 18
        user_id, color = _heavy_pair(db)
        session_ids = {
            mode: _terminal_session_id(db, user_id, color, mode)
            for mode in ("normal", "drill")
        }
        # Warm the graph/roots and Phase-1 replay cache before timing warm cells.
        overlay_evidence(db, user_id, color, get_opening_graph())
        db.rollback()

    warm_reps = int(os.getenv("GHOSTREPLAY_DELTA_BENCH_REPS", "10"))
    if warm_reps < 5:
        pytest.fail("GHOSTREPLAY_DELTA_BENCH_REPS must be at least 5")
    cells: dict[str, dict[str, list[dict[str, float]]]] = {
        "idle": {"normal": [], "drill": []},
        "whole_graph": {"normal": [], "drill": []},
        "baseline_digest": {"normal": []},
        "process_cold": {"normal": [], "drill": []},
    }

    try:
        for mode in ("normal", "drill"):
            for _ in range(3):
                _bump_user_sequence(session_factory, user_id, color)
                cells["idle"][mode].append(
                    _run_lane_once(
                        lane,
                        publisher,
                        session_factory,
                        user_id=user_id,
                        color=color,
                        session_id=session_ids[mode],
                    )
                )

        real_build = opening_cache._build_cached_scores
        gate_event: list[threading.Event | None] = [None]

        def signal_full_cpu(*args, **kwargs):
            event = gate_event[0]
            if event is not None:
                event.set()
            return real_build(*args, **kwargs)

        monkeypatch.setattr(opening_cache, "_build_cached_scores", signal_full_cpu)
        for mode in ("normal", "drill"):
            for _ in range(warm_reps):
                _bump_user_sequence(session_factory, user_id, color)
                entered_cpu = threading.Event()
                full_done = threading.Event()
                full_errors: list[BaseException] = []
                gate_event[0] = entered_cpu

                def run_full():
                    try:
                        with session_factory() as db:
                            recompute_opening_scores(db, user_id, color)
                    except BaseException as exc:  # surfaced on the test thread
                        full_errors.append(exc)
                    finally:
                        full_done.set()

                full_thread = threading.Thread(target=run_full)
                full_thread.start()
                assert entered_cpu.wait(timeout=45.0)
                record = _run_lane_once(
                    lane,
                    publisher,
                    session_factory,
                    user_id=user_id,
                    color=color,
                    session_id=session_ids[mode],
                )
                # Freshness arrived from the independent lane while the real full
                # scorer/commit was still in flight, not from its eventual batch.
                assert full_done.is_set() is False
                cells["whole_graph"][mode].append(record)
                full_thread.join(timeout=90.0)
                assert full_thread.is_alive() is False
                if full_errors:
                    raise full_errors[0]
        gate_event[0] = None
        monkeypatch.setattr(opening_cache, "_build_cached_scores", real_build)

        # The current production baseline predicate is cheap-signal based. Force
        # the real epoch-drift scoped digest branch, overlap the real async job,
        # and measure rather than assuming it needs a new defer/retry mechanism.
        with session_factory() as db:
            watermark_seq, watermark_epoch, watermark_fingerprint = (
                capture_baseline_watermark(db, user_id, color)
            )
            baseline_session = GameSession(
                id=uuid.uuid4(),
                user_id=user_id,
                started_at=datetime.now(timezone.utc) + timedelta(days=1),
                status="active",
                engine_elo=1500,
                blunder_recorded=False,
                is_rated=False,
                player_color=color,
                session_mode="normal",
                baseline_watermark_seq=watermark_seq,
                baseline_watermark_epoch=watermark_epoch,
                baseline_watermark_fingerprint=watermark_fingerprint,
            )
            db.add(baseline_session)
            db.commit()
            baseline_session_id = baseline_session.id

        real_shared_digest = opening_cache.shared_scope_digest
        baseline_entered = threading.Event()

        def signal_baseline_digest(*args, **kwargs):
            baseline_entered.set()
            return real_shared_digest(*args, **kwargs)

        monkeypatch.setattr(
            opening_cache, "shared_scope_digest", signal_baseline_digest
        )
        for _ in range(3):
            with session_factory() as db:
                db.execute(
                    update(GameSession)
                    .where(GameSession.id == baseline_session_id)
                    .values(opening_score_baseline=None)
                )
                db.commit()
            _bump_shared_epoch(session_factory)
            baseline_entered.clear()
            baseline_errors: list[BaseException] = []

            def run_baseline():
                try:
                    with session_factory() as db:
                        run_baseline_snapshot_job(
                            db, baseline_session_id, user_id, color
                        )
                except BaseException as exc:
                    baseline_errors.append(exc)

            baseline_thread = threading.Thread(target=run_baseline)
            baseline_thread.start()
            assert baseline_entered.wait(timeout=30.0)
            cells["baseline_digest"]["normal"].append(
                _run_lane_once(
                    lane,
                    publisher,
                    session_factory,
                    user_id=user_id,
                    color=color,
                    session_id=session_ids["normal"],
                )
            )
            baseline_thread.join(timeout=45.0)
            assert baseline_thread.is_alive() is False
            if baseline_errors:
                raise baseline_errors[0]
        monkeypatch.setattr(
            opening_cache, "shared_scope_digest", real_shared_digest
        )

        for mode in ("normal", "drill"):
            reset_session_evidence_cache()
            _bump_user_sequence(session_factory, user_id, color)
            cells["process_cold"][mode].append(
                _run_lane_once(
                    lane,
                    publisher,
                    session_factory,
                    user_id=user_id,
                    color=color,
                    session_id=session_ids[mode],
                )
            )

        summary = {
            cell: {
                mode: _summarize(records)
                for mode, records in modes.items()
            }
            for cell, modes in cells.items()
        }
        print("DELTA_LANE_BENCH_RESULT " + json.dumps(summary, sort_keys=True))

        for mode in ("normal", "drill"):
            p95 = summary["whole_graph"][mode]["end_to_end"]["p95_ms"]
            assert p95 < _P95_LIMIT_MS, (
                f"{mode} whole-graph-contention p95 {p95}ms "
                f"exceeds {_P95_LIMIT_MS}ms"
            )
    finally:
        lane.shutdown(drain=False, timeout=30.0)
        reset_scoped_delta_cache()
        engine.dispose()
