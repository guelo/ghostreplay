"""Tests for the in-process debounced opening-score recompute scheduler.

Unit tests inject a fake clock + session factory and drive the scheduler
synchronously (``run_due`` / ``flush_pending``) so coalescing/debounce logic is
deterministic with no real sleeps. Exactly one test exercises the real worker
thread, signalled via ``threading.Event`` rather than timing guesses.

Injected recomputes return contract-shaped ``OpeningScoreRecomputeResult`` values:
the scheduler labels its run outcome from the explicit disposition and treats a
legacy bare batch / ``None`` as a contract failure, so a fake that returns the
pre-contract shape is itself a regression signal (see the contract tests below).
"""

from __future__ import annotations

import logging
import threading
from unittest.mock import Mock

import anyio
import pytest

from app import main
from app.logging_config import SimpleFormatter
from app.opening_cache import OpeningScoreRecomputeResult, RecomputeDisposition
from app.opening_rootcalc import RowIsolationSummary
from app.opening_score_scheduler import (
    OpeningScoreScheduler,
    OpeningScoreTrigger,
    UnknownOpeningScoreTrigger,
    current_run_timing,
)

# conftest's autouse ``_no_op_recompute_scheduler`` patches the module ATTRIBUTE
# ``app.opening_score_scheduler.request_recompute``, so a facade test reaching it as
# ``module.request_recompute`` would exercise a MagicMock and assert nothing. These
# import-time bindings are the real functions; they still resolve ``_scheduler`` from
# module globals at call time, so monkeypatching the singleton works as usual.
from app.opening_score_scheduler import refresh_now as _real_refresh_now
from app.opening_score_scheduler import request_recompute as _real_request_recompute

# Default provenance for tests that are not about provenance itself.
_TRIGGER = OpeningScoreTrigger.CACHED_SCORE_READER_WARM
_CLEAN_ROW_ISOLATION = RowIsolationSummary(
    outcome="clean",
    omitted_root_row_count=0,
    omitted_position_row_count=0,
    opportunity_invariant_count=0,
    report_fold_bounds_count=0,
)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class _FakeBatch:
    """Stand-in for a durable batch; ``id=None`` suppresses push-fill."""

    def __init__(self, generation: int = 7, batch_id: int | None = None) -> None:
        self.generation = generation
        self.id = batch_id


def _rebuilt(
    reason: str = "cache_miss",
    generation: int = 7,
    batch_id: int | None = None,
    row_isolation: RowIsolationSummary = _CLEAN_ROW_ISOLATION,
):
    return OpeningScoreRecomputeResult(
        disposition=RecomputeDisposition.REBUILT,
        batch=_FakeBatch(generation, batch_id),
        reason=reason,
        row_isolation=row_isolation,
    )


def _cached(generation: int = 7, batch_id: int | None = None):
    return OpeningScoreRecomputeResult(
        disposition=RecomputeDisposition.CACHED,
        batch=_FakeBatch(generation, batch_id),
    )


def _no_evidence():
    return OpeningScoreRecomputeResult(
        disposition=RecomputeDisposition.NO_EVIDENCE, batch=None
    )


class _RecordingRecompute:
    """Records (user_id, player_color) per call and the session it received."""

    def __init__(self, result=None) -> None:
        self.calls: list[tuple[int, str]] = []
        self.sessions: list[object] = []
        self._result = result if result is not None else _rebuilt()

    def __call__(self, db, user_id, player_color):
        self.calls.append((user_id, player_color))
        self.sessions.append(db)
        return self._result


class _TimingRecompute(_RecordingRecompute):
    """Also snapshots the scheduler run context visible to each recompute call."""

    def __init__(self, result=None, advance: tuple[_FakeClock, float] | None = None) -> None:
        super().__init__(result)
        self.timings: list[dict | None] = []
        self._advance = advance

    def __call__(self, db, user_id, player_color):
        if self._advance is not None:
            clock, dt = self._advance
            clock.advance(dt)
        self.timings.append(current_run_timing())
        return super().__call__(db, user_id, player_color)


def _completion_records(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.getMessage().startswith("opening_score_recompute_run ")
    ]


def _rendered_completion(caplog) -> str:
    """The completion record as the PRODUCTION formatter renders it.

    ``app.logging_config`` prints ``%(asctime)s %(levelname)s %(message)s`` only, so
    anything the scheduler puts in ``extra`` is invisible in production. Assert
    against this rendering, never against ``record.__dict__``.
    """
    records = _completion_records(caplog)
    assert len(records) == 1, f"expected one completion record, got {len(records)}"
    return SimpleFormatter("%(asctime)s %(levelname)s %(message)s").format(records[0])


def _convergence_records(caplog) -> list[str]:
    return [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("opening_score_terminal_convergence ")
    ]


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self.rollbacks = 0

    def close(self) -> None:
        self.closed = True

    def rollback(self) -> None:
        self.rollbacks += 1


def _make_scheduler(clock, recompute, **kwargs):
    sessions: list[_FakeSession] = []

    def factory():
        s = _FakeSession()
        sessions.append(s)
        return s

    params = {"quiet_window": 1.5, "max_wait": 10.0, "auto_start": False}
    params.update(kwargs)
    sched = OpeningScoreScheduler(
        session_factory=factory,
        recompute=recompute,
        clock=clock,
        **params,
    )
    return sched, sessions


def test_coalesces_burst_into_single_recompute():
    clock = _FakeClock()
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(clock, recompute)

    for _ in range(10):
        sched.request_recompute(123, "white", source=_TRIGGER)
        clock.advance(0.1)  # within the quiet window

    # Nothing is due yet (deadline keeps extending).
    sched.run_due()
    assert recompute.calls == []

    clock.advance(2.0)  # past the quiet window
    sched.run_due()
    assert recompute.calls == [(123, "white")]


def test_immediate_deadline_is_sticky_under_normal_enqueues():
    # An immediate (refresh_now) enqueue is due now. A subsequent burst of normal
    # enqueues for the same key must not push that deadline back into the debounce
    # window — otherwise sustained traffic could starve refresh_now past its
    # timeout while the worker sits idle.
    clock = _FakeClock()
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(clock, recompute)

    key = (7, "white")
    with sched._cond:
        sched._enqueue_locked(key, immediate=True, source=_TRIGGER)  # due now

    # Normal enqueues keep arriving shortly after; none may postpone the deadline.
    for _ in range(5):
        sched.request_recompute(7, "white", source=_TRIGGER)
        clock.advance(0.1)  # still well within one quiet window

    # The immediate enqueue is still due at the current (advanced) time.
    sched.run_due()
    assert recompute.calls == [(7, "white")]


def test_is_scheduled_tracks_pending_inflight_and_is_key_scoped():
    # The read-side /tree/status probe uses is_scheduled to tell a freshly-fired
    # bootstrap ("cold") from one already running ("building") without enqueuing.
    clock = _FakeClock()
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(clock, recompute)

    # Idle: nothing scheduled.
    assert sched.is_scheduled(123, "white") is False

    # A queued recompute is "scheduled" — auto_start is off so the key stays in
    # _pending and the worker never runs it here.
    sched.request_recompute(123, "white", source=_TRIGGER)
    assert sched.is_scheduled(123, "white") is True
    # Key-scoped: an unrelated (user, color) is not reported scheduled.
    assert sched.is_scheduled(999, "black") is False

    # The in-flight set is observed too (a recompute mid-run, before it pops).
    with sched._lock:
        sched._inflight.add((5, "white"))
    assert sched.is_scheduled(5, "white") is True
    with sched._lock:
        sched._inflight.discard((5, "white"))
    assert sched.is_scheduled(5, "white") is False

    # After the queued run completes the key is neither pending nor in-flight.
    clock.advance(2.0)
    sched.run_due()
    assert sched.is_scheduled(123, "white") is False


def test_is_inflight_tracks_only_running_runs_and_is_key_scoped():
    # The one-shot baseline snapshot (g-1iul) gates the O(evidence) digest on
    # is_inflight: narrower than is_scheduled, it reports True ONLY for a RUNNING
    # recompute, never a merely-pending/debounced one.
    clock = _FakeClock()
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(clock, recompute)

    # Idle: nothing in-flight.
    assert sched.is_inflight(123, "white") is False

    # A queued (pending) recompute is NOT in-flight — this is the whole point of
    # the narrower probe vs is_scheduled (which would report True here).
    sched.request_recompute(123, "white", source=_TRIGGER)
    assert sched.is_scheduled(123, "white") is True
    assert sched.is_inflight(123, "white") is False

    # A running recompute (mid-run, before it pops) IS in-flight.
    with sched._lock:
        sched._inflight.add((5, "white"))
    assert sched.is_inflight(5, "white") is True
    # Key-scoped: an unrelated (user, color) is not reported in-flight.
    assert sched.is_inflight(999, "black") is False
    # Removing the key clears it.
    with sched._lock:
        sched._inflight.discard((5, "white"))
    assert sched.is_inflight(5, "white") is False


def test_distinct_keys_each_recompute_once_with_own_session():
    clock = _FakeClock()
    recompute = _RecordingRecompute()
    sched, sessions = _make_scheduler(clock, recompute)

    sched.request_recompute(1, "white", source=_TRIGGER)
    sched.request_recompute(2, "black", source=_TRIGGER)
    clock.advance(2.0)
    sched.run_due()

    assert sorted(recompute.calls) == [(1, "white"), (2, "black")]
    # Each run got its own freshly-created session, and both were closed.
    assert len(recompute.sessions) == 2
    assert recompute.sessions[0] is not recompute.sessions[1]
    assert all(s.closed for s in sessions)


def test_request_during_inflight_triggers_followup_run():
    clock = _FakeClock()

    enqueue_during_run = {"done": False}

    def recompute(db, user_id, player_color):
        # Simulate a request arriving while this key is in-flight.
        if not enqueue_during_run["done"]:
            enqueue_during_run["done"] = True
            sched.request_recompute(user_id, player_color, source=_TRIGGER)
        recompute.calls.append((user_id, player_color))
        return _rebuilt()

    recompute.calls = []
    sched, _ = _make_scheduler(clock, recompute)

    sched.request_recompute(5, "white", source=_TRIGGER)
    clock.advance(2.0)
    sched.run_due()  # first run re-enqueues
    clock.advance(2.0)
    sched.run_due()  # follow-up run

    assert recompute.calls == [(5, "white"), (5, "white")]


def test_recompute_failure_is_swallowed_and_next_key_runs():
    clock = _FakeClock()

    def recompute(db, user_id, player_color):
        recompute.calls.append((user_id, player_color))
        if user_id == 1:
            raise RuntimeError("boom")
        return _rebuilt()

    recompute.calls = []
    sched, _ = _make_scheduler(clock, recompute)

    sched.request_recompute(1, "white", source=_TRIGGER)
    sched.request_recompute(2, "white", source=_TRIGGER)
    clock.advance(2.0)
    sched.run_due()

    assert sorted(recompute.calls) == [(1, "white"), (2, "white")]
    # The scheduler is not wedged: a later key still runs.
    sched.request_recompute(3, "white", source=_TRIGGER)
    clock.advance(2.0)
    sched.run_due()
    assert (3, "white") in recompute.calls


def test_max_wait_cap_fires_under_sustained_enqueues():
    clock = _FakeClock()
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(clock, recompute)

    # Keep enqueueing just under the quiet window so the deadline keeps moving,
    # but the max_wait cap (10s from first_seen) must eventually force a run.
    for _ in range(20):
        sched.request_recompute(7, "white", source=_TRIGGER)
        clock.advance(1.0)
        sched.run_due()

    assert recompute.calls.count((7, "white")) >= 1


def test_initial_enqueue_deadline_is_capped_by_max_wait():
    clock = _FakeClock()
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(
        clock, recompute, quiet_window=20.0, max_wait=3.0
    )

    sched.request_recompute(7, "white", source=_TRIGGER)
    clock.advance(3.0)
    sched.run_due()

    assert recompute.calls == [(7, "white")]


def test_lifecycle_two_start_shutdown_cycles():
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(
        _FakeClock(), recompute, quiet_window=0.0, auto_start=True
    )

    for cycle in range(2):
        sched.start()
        sched.request_recompute(cycle, "white", source=_TRIGGER)
        sched.flush_pending(timeout=5.0)
        sched.shutdown(drain=True, timeout=5.0)
        # Post-shutdown enqueue is a no-op and does not raise.
        sched.request_recompute(999, "white", source=_TRIGGER)

    assert (0, "white") in recompute.calls
    assert (1, "white") in recompute.calls
    assert (999, "white") not in recompute.calls


def test_start_failure_is_swallowed_by_facade(monkeypatch):
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(_FakeClock(), recompute, auto_start=True)

    def boom(self):
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(OpeningScoreScheduler, "start", boom)
    # request_recompute swallows the start failure internally.
    sched.request_recompute(1, "white", source=_TRIGGER)


def test_retry_safe_start_after_thread_start_raises(monkeypatch):
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(
        _FakeClock(), recompute, quiet_window=0.0, auto_start=True
    )

    real_thread_start = threading.Thread.start
    state = {"failed": False}

    def flaky_start(self):
        if not state["failed"] and self.name == "opening-score-scheduler":
            state["failed"] = True
            raise RuntimeError("first start fails")
        return real_thread_start(self)

    monkeypatch.setattr(threading.Thread, "start", flaky_start, raising=True)

    # First enqueue: start() raises, facade swallows; state is reset.
    sched.request_recompute(1, "white", source=_TRIGGER)
    assert sched._thread is None
    assert sched._shutdown is False

    # Second enqueue cleanly starts a worker and processes the key.
    sched.request_recompute(1, "white", source=_TRIGGER)
    sched.flush_pending(timeout=5.0)
    assert (1, "white") in recompute.calls
    sched.shutdown(drain=True, timeout=5.0)


def test_flush_pending_times_out_on_wedged_run():
    import time

    started = threading.Event()
    release = threading.Event()

    def recompute(db, user_id, player_color):
        started.set()
        release.wait(timeout=5.0)
        return _rebuilt()

    # Real clock so flush_pending's timeout bound actually elapses.
    sched, _ = _make_scheduler(
        time.monotonic, recompute, quiet_window=0.0, auto_start=True
    )
    sched.start()
    sched.request_recompute(1, "white", source=_TRIGGER)
    assert started.wait(timeout=5.0)

    # While the worker is wedged in-flight, flush cannot reach quiescence.
    with pytest.raises(TimeoutError):
        sched.flush_pending(timeout=0.3)

    release.set()
    sched.shutdown(drain=True, timeout=5.0)


def test_flush_pending_timeout_bounds_caller_owned_pending_run():
    import time

    started = threading.Event()
    release = threading.Event()

    def recompute(db, user_id, player_color):
        started.set()
        release.wait(timeout=5.0)

    sched, _ = _make_scheduler(time.monotonic, recompute, quiet_window=60.0)
    sched.request_recompute(1, "white", source=_TRIGGER)

    outcome: list[BaseException] = []

    def flush():
        try:
            sched.flush_pending(timeout=0.1)
        except BaseException as exc:
            outcome.append(exc)

    before = time.monotonic()
    caller = threading.Thread(target=flush)
    caller.start()
    assert started.wait(timeout=5.0)
    caller.join(timeout=0.5)

    assert not caller.is_alive()
    assert time.monotonic() - before < 0.5
    assert len(outcome) == 1
    assert isinstance(outcome[0], TimeoutError)

    release.set()
    sched.shutdown(drain=True, timeout=5.0)


def test_timed_out_flush_does_not_sweep_later_request_into_forced_drain():
    import time

    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    calls = 0

    def recompute(db, user_id, player_color):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            release_first.wait(timeout=5.0)
        else:
            second_started.set()

    sched, _ = _make_scheduler(
        time.monotonic, recompute, quiet_window=0.25, auto_start=True
    )
    sched.request_recompute(1, "white", source=_TRIGGER)
    assert first_started.wait(timeout=5.0)

    with pytest.raises(TimeoutError):
        sched.flush_pending(timeout=0.1)

    # Enqueue while the stale forced-drain snapshot would still be blocked in
    # the first recompute. This later request must retain its quiet window.
    sched.request_recompute(2, "white", source=_TRIGGER)
    release_first.set()

    assert not second_started.wait(timeout=0.05)
    assert second_started.wait(timeout=1.0)

    sched.shutdown(drain=True, timeout=5.0)


def test_thread_integration_real_worker_runs_recompute():
    done = threading.Event()
    recompute_calls: list[tuple[int, str]] = []

    def recompute(db, user_id, player_color):
        recompute_calls.append((user_id, player_color))
        done.set()
        return _rebuilt()

    sched, sessions = _make_scheduler(
        _FakeClock(), recompute, quiet_window=0.0, auto_start=True
    )
    sched.start()
    try:
        sched.request_recompute(42, "black", source=_TRIGGER)
        assert done.wait(timeout=5.0)
        assert recompute_calls == [(42, "black")]
    finally:
        sched.shutdown(drain=True, timeout=5.0)
    assert all(s.closed for s in sessions)


def test_concurrent_start_creates_one_worker(monkeypatch):
    sched, _ = _make_scheduler(_FakeClock(), _RecordingRecompute())
    real_start = threading.Thread.start
    start_entered = threading.Event()
    allow_start = threading.Event()
    started_threads: list[threading.Thread] = []

    def blocking_start(thread):
        if thread.name != "opening-score-scheduler":
            return real_start(thread)
        started_threads.append(thread)
        start_entered.set()
        assert allow_start.wait(timeout=5.0)
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", blocking_start)
    callers = [threading.Thread(target=sched.start) for _ in range(2)]
    callers[0].start()
    assert start_entered.wait(timeout=5.0)
    callers[1].start()
    allow_start.set()
    for caller in callers:
        caller.join(timeout=5.0)

    assert len(started_threads) == 1
    sched.shutdown(drain=False, timeout=5.0)


def test_shutdown_timeout_does_not_run_recompute_on_caller():
    import time

    started = threading.Event()
    release = threading.Event()

    def recompute(db, user_id, player_color):
        started.set()
        release.wait(timeout=5.0)

    sched, _ = _make_scheduler(
        time.monotonic, recompute, quiet_window=0.0, auto_start=True
    )
    sched.request_recompute(1, "white", source=_TRIGGER)
    assert started.wait(timeout=5.0)

    before = time.monotonic()
    with pytest.raises(TimeoutError):
        sched.shutdown(drain=True, timeout=0.1)
    assert time.monotonic() - before < 0.5

    release.set()
    sched.shutdown(drain=True, timeout=5.0)


def test_lifespan_swallows_shutdown_timeout_and_disposes_engine(monkeypatch):
    scheduler = Mock()
    scheduler.shutdown.side_effect = TimeoutError("hung recompute")
    delta = Mock()
    evidence = Mock()
    baseline = Mock()
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    monkeypatch.setattr(main, "get_scheduler", lambda: scheduler)
    monkeypatch.setattr(main, "get_delta_lane", lambda: delta)
    monkeypatch.setattr(main, "get_evidence_scheduler", lambda: evidence)
    monkeypatch.setattr(main, "get_baseline_scheduler", lambda: baseline)
    monkeypatch.setattr(main.engine, "connect", lambda: connection)
    dispose = Mock()
    monkeypatch.setattr(main.engine, "dispose", dispose)
    start_prewarm = Mock()
    monkeypatch.setattr(main, "start_prewarm", start_prewarm)

    async def exercise_lifespan():
        async with main.lifespan(main.app):
            pass

    anyio.run(exercise_lifespan)

    scheduler.start.assert_called_once_with()
    scheduler.shutdown.assert_called_once_with(drain=True)
    delta.start.assert_called_once_with()
    delta.shutdown.assert_called_once_with(drain=True)
    start_prewarm.assert_called_once_with()
    dispose.assert_called_once_with()


# ---------------------------------------------------------------------------
# refresh_now (keyed flush/await with outcome tracking) — real worker thread
# ---------------------------------------------------------------------------

import time  # noqa: E402


def test_refresh_now_returns_true_on_successful_run():
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(time.monotonic, recompute)
    try:
        assert sched.refresh_now(1, "white", timeout=5.0, source=_TRIGGER) is True
        assert recompute.calls == [(1, "white")]
    finally:
        sched.shutdown()


def test_refresh_now_returns_false_on_recompute_exception():
    def boom(db, user_id, player_color):
        raise RuntimeError("recompute failed")

    sched, _ = _make_scheduler(time.monotonic, boom)
    try:
        # A covering run that fails must not be reported as fresh.
        assert sched.refresh_now(1, "white", timeout=5.0, source=_TRIGGER) is False
    finally:
        sched.shutdown()


def test_refresh_now_returns_false_on_worker_start_failure_without_blocking():
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(time.monotonic, recompute)
    sched.start = Mock(side_effect=RuntimeError("cannot start worker"))
    started = time.monotonic()
    assert sched.refresh_now(1, "white", timeout=5.0, source=_TRIGGER) is False
    # Must not block for the full timeout waiting on a worker that never runs.
    assert time.monotonic() - started < 1.0


def test_refresh_now_does_not_trigger_unrelated_keys():
    recompute = _RecordingRecompute()
    # Long quiet window so the unrelated normal enqueue stays pending (its debounce
    # deadline is far in the future) for the whole test; only the immediate
    # refresh_now key should ever become due and run.
    sched, _ = _make_scheduler(time.monotonic, recompute, quiet_window=30.0)
    try:
        # Queue an unrelated key as a normal (debounced) recompute. It must remain
        # pending — never run, never in-flight — while we refresh a different key.
        sched.request_recompute(2, "black", source=_TRIGGER)
        assert sched.refresh_now(1, "white", timeout=5.0, source=_TRIGGER) is True
        assert recompute.calls == [(1, "white")]
        # The unrelated key is still pending and was never started: refresh_now
        # isolates its flush/await to its own key.
        with sched._cond:
            assert (2, "black") in sched._pending
            assert (2, "black") not in sched._inflight
        assert (2, "black") not in recompute.calls
    finally:
        sched.shutdown(drain=False)


def test_refresh_now_times_out_returns_false_and_makes_no_duplicate_run():
    release = threading.Event()
    calls: list[tuple[int, str]] = []

    def slow(db, user_id, player_color):
        calls.append((user_id, player_color))
        release.wait(timeout=5.0)
        return _rebuilt()

    sched, _ = _make_scheduler(time.monotonic, slow)
    try:
        # The in-flight run blocks; refresh_now times out and serves the current batch.
        assert sched.refresh_now(1, "white", timeout=0.3, source=_TRIGGER) is False
        # No second/concurrent generation was triggered for the key.
        assert calls == [(1, "white")]
    finally:
        release.set()
        sched.shutdown()


def test_refresh_now_waits_for_followup_enqueued_during_run():
    calls: list[tuple[int, str]] = []

    def recompute(db, user_id, player_color):
        calls.append((user_id, player_color))
        if len(calls) == 1:
            # A normal enqueue arriving during the run creates a follow-up entry
            # with a newer sequence; refresh_now must not return after the first
            # run alone — it must wait for the follow-up and quiescence (TOCTOU).
            sched.request_recompute(user_id, player_color, source=_TRIGGER)
        return _rebuilt()

    sched, _ = _make_scheduler(time.monotonic, recompute, quiet_window=0.0)
    try:
        assert sched.refresh_now(1, "white", timeout=5.0, source=_TRIGGER) is True
        assert calls == [(1, "white"), (1, "white")]
    finally:
        sched.shutdown()


# ---------------------------------------------------------------------------
# Explicit recompute-outcome contract (g-score-queue-timing Phase 1)
#
# The scheduler must label its run from the returned disposition and never infer
# it from batch presence/generation/timing. All three normal dispositions are
# successful COVERING runs for sequence/quiescence; only an exception — or a
# pre-contract return shape — is a failure.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result_factory", [lambda: _rebuilt("evidence_change"), _cached, _no_evidence]
)
def test_every_normal_disposition_is_a_covering_run_for_refresh_now(result_factory):
    # rebuilt / cached / no_evidence are all successful COVERING runs: refresh_now's
    # contract is "a covering run completed and the key is quiescent", not "a batch
    # was written".
    sched, _ = _make_scheduler(time.monotonic, lambda *a: result_factory())
    try:
        assert sched.refresh_now(1, "white", timeout=5.0, source=_TRIGGER) is True
    finally:
        sched.shutdown()


def test_exception_reports_failure_to_refresh_now():
    def boom(db, user_id, player_color):
        raise RuntimeError("recompute failed")

    sched, _ = _make_scheduler(time.monotonic, boom)
    try:
        assert sched.refresh_now(1, "white", timeout=5.0, source=_TRIGGER) is False
    finally:
        sched.shutdown()


@pytest.mark.parametrize(
    "recompute,expected_outcome,expected_reason,expected_generation",
    [
        (lambda *a: _rebuilt("evidence_change", 42), "rebuilt", "evidence_change", "42"),
        (lambda *a: _cached(9), "cached", "None", "9"),
        (lambda *a: _no_evidence(), "no_evidence", "None", "None"),
        (Mock(side_effect=RuntimeError("boom")), "failed", "None", "None"),
    ],
    ids=["rebuilt", "cached", "no_evidence", "failed"],
)
def test_completion_log_reports_the_exact_run_outcome(
    caplog, recompute, expected_outcome, expected_reason, expected_generation
):
    # Driven synchronously (run_due on this thread) so the record is guaranteed to
    # exist when asserted: on the worker thread, refresh_now returns at quiescence,
    # which is BEFORE the completion record is emitted.
    clock = _FakeClock()
    sched, _ = _make_scheduler(clock, recompute)
    sched.request_recompute(1, "white", source=_TRIGGER)
    clock.advance(2.0)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.run_due()

    rendered = _rendered_completion(caplog)
    assert f"run_outcome={expected_outcome}" in rendered
    assert f"rebuild_reason={expected_reason}" in rendered
    assert f"generation={expected_generation}" in rendered
    expected_isolation = "clean" if expected_outcome == "rebuilt" else "not_applicable"
    assert f"row_isolation_outcome={expected_isolation}" in rendered


def test_completion_log_reports_quarantine_counts_without_row_identity(caplog):
    isolation = RowIsolationSummary(
        outcome="quarantined",
        omitted_root_row_count=2,
        omitted_position_row_count=3,
        opportunity_invariant_count=4,
        report_fold_bounds_count=1,
    )
    clock = _FakeClock()
    sched, _ = _make_scheduler(
        clock, lambda *args: _rebuilt(row_isolation=isolation)
    )
    sched.request_recompute(1, "white", source=_TRIGGER)
    clock.advance(2.0)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.run_due()

    rendered = _rendered_completion(caplog)
    assert "row_isolation_outcome=quarantined" in rendered
    assert "omitted_root_row_count=2" in rendered
    assert "omitted_position_row_count=3" in rendered
    assert "opportunity_invariant_count=4" in rendered
    assert "report_fold_bounds_count=1" in rendered


def test_failed_run_does_not_prevent_the_next_due_key():
    clock = _FakeClock()
    calls: list[tuple[int, str]] = []

    def recompute(db, user_id, player_color):
        calls.append((user_id, player_color))
        if user_id == 1:
            raise RuntimeError("boom")
        return _rebuilt()

    sched, _ = _make_scheduler(clock, recompute)
    sched.request_recompute(1, "white", source=_TRIGGER)
    sched.request_recompute(2, "white", source=_TRIGGER)
    clock.advance(2.0)
    sched.run_due()

    assert sorted(calls) == [(1, "white"), (2, "white")]


@pytest.mark.parametrize(
    "result",
    [_rebuilt(batch_id=17), _cached(batch_id=23)],
    ids=["rebuilt", "cached"],
)
def test_durable_batch_result_push_fills_after_recompute_session_close(result):
    clock = _FakeClock()
    fills: list[int] = []
    sched, sessions = _make_scheduler(
        clock,
        lambda *args: result,
        fill_baselines=fills.append,
    )
    sched.request_recompute(1, "white", source=_TRIGGER)
    clock.advance(2.0)

    sched.run_due()

    assert fills == [result.batch.id]
    assert sessions[0].closed is True
    assert sched._last_result[(1, "white")][1] is True


def test_no_evidence_result_has_no_push_candidate():
    clock = _FakeClock()
    fills: list[int] = []
    sched, _ = _make_scheduler(
        clock,
        lambda *args: _no_evidence(),
        fill_baselines=fills.append,
    )
    sched.request_recompute(1, "white", source=_TRIGGER)
    clock.advance(2.0)

    sched.run_due()

    assert fills == []


def test_push_fill_failure_cannot_relabel_durable_recompute(caplog):
    clock = _FakeClock()

    def fail_fill(batch_id):
        raise RuntimeError("optional fill failed")

    sched, _ = _make_scheduler(
        clock,
        lambda *args: _rebuilt(batch_id=31),
        fill_baselines=fail_fill,
    )
    sched.request_recompute(1, "white", source=_TRIGGER)
    seq = sched._seq_counter[(1, "white")]
    clock.advance(2.0)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.run_due()

    assert sched._last_result[(1, "white")] == (seq, True)
    assert "run_outcome=rebuilt" in _rendered_completion(caplog)


@pytest.mark.parametrize("legacy_result", [None, _FakeBatch(3)])
def test_legacy_bare_result_is_a_contract_failure(caplog, legacy_result):
    # A bare OpeningScoreBatch or None is the pre-contract shape. Treating it as
    # success would resurrect presence-based inference — the exact regression the
    # explicit disposition exists to prevent.
    clock = _FakeClock()
    sched, _ = _make_scheduler(clock, lambda *a: legacy_result)
    sched.request_recompute(1, "white", source=_TRIGGER)
    seq = sched._seq_counter[(1, "white")]
    clock.advance(2.0)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.run_due()

    rendered = _rendered_completion(caplog)
    assert "run_outcome=failed" in rendered
    # Even a legacy batch carrying a generation must not be reported as one.
    assert "generation=None" in rendered
    # A covering waiter is told the run failed, not that it succeeded.
    assert sched._last_result[(1, "white")] == (seq, False)


# ---------------------------------------------------------------------------
# Scheduler timing + provenance contracts (g-score-queue-timing Phases 2-3)
# ---------------------------------------------------------------------------


def test_single_enqueue_timing_and_configured_windows():
    clock = _FakeClock()
    recompute = _TimingRecompute()
    sched, _ = _make_scheduler(clock, recompute, quiet_window=1.5, max_wait=10.0)

    sched.request_recompute(1, "white", source=OpeningScoreTrigger.SCORE_DELTA)
    clock.advance(2.0)
    sched.run_due()

    timing = recompute.timings[0]
    assert timing is not None
    assert timing["scheduler_timing_version"] == 1
    assert isinstance(timing["scheduler_run_id"], str) and timing["scheduler_run_id"]
    # One enqueue: both ends of the (empty) burst agree and no time was spent
    # accumulating it.
    assert timing["queue_first_ms"] == pytest.approx(2000.0)
    assert timing["queue_last_ms"] == pytest.approx(2000.0)
    assert timing["coalesce_span_ms"] == pytest.approx(0.0)
    # Policy delay is the quiet window; the remaining wait is post-deadline lag.
    assert timing["deadline_delay_ms"] == pytest.approx(1500.0)
    assert timing["dispatch_lag_ms"] == pytest.approx(500.0)
    assert timing["trigger_first"] == "score_delta"
    assert timing["trigger_last"] == "score_delta"
    assert timing["trigger_sources"] == ["score_delta"]
    assert timing["enqueue_count"] == 1
    assert timing["immediate"] is False
    assert timing["forced_dispatch"] is False
    assert timing["quiet_window_ms"] == pytest.approx(1500.0)
    assert timing["max_wait_ms"] == pytest.approx(10000.0)


def test_burst_keeps_first_seen_fixed_and_advances_last_seen():
    clock = _FakeClock()
    recompute = _TimingRecompute()
    sched, _ = _make_scheduler(clock, recompute)

    sched.request_recompute(1, "white", source=OpeningScoreTrigger.SESSION_EVIDENCE)
    clock.advance(0.5)
    sched.request_recompute(1, "white", source=OpeningScoreTrigger.SCORE_DELTA)
    clock.advance(0.5)
    sched.request_recompute(1, "white", source=OpeningScoreTrigger.SESSION_EVIDENCE)
    clock.advance(2.0)
    sched.run_due()

    timing = recompute.timings[0]
    assert timing["queue_first_ms"] == pytest.approx(3000.0)
    assert timing["queue_last_ms"] == pytest.approx(2000.0)
    assert timing["coalesce_span_ms"] == pytest.approx(1000.0)
    # The decomposition identity that makes both ends reportable from one run.
    assert timing["queue_first_ms"] == pytest.approx(
        timing["coalesce_span_ms"] + timing["queue_last_ms"]
    )
    assert timing["enqueue_count"] == 3
    # Sources fold deterministically (sorted by value) and dedupe.
    assert timing["trigger_sources"] == ["score_delta", "session_evidence"]
    assert timing["trigger_first"] == "session_evidence"
    assert timing["trigger_last"] == "session_evidence"


def test_max_wait_cap_is_the_reported_policy_delay():
    clock = _FakeClock()
    recompute = _TimingRecompute()
    sched, _ = _make_scheduler(clock, recompute, quiet_window=20.0, max_wait=3.0)

    sched.request_recompute(1, "white", source=_TRIGGER)
    clock.advance(1.0)
    sched.request_recompute(1, "white", source=_TRIGGER)
    clock.advance(3.0)
    sched.run_due()

    timing = recompute.timings[0]
    # The second enqueue's quiet window (now+20) loses to the max-wait cap
    # (first_seen+3), so the reported policy delay is the cap, not the window.
    assert timing["deadline_delay_ms"] == pytest.approx(3000.0)
    assert timing["queue_first_ms"] == pytest.approx(4000.0)
    assert timing["dispatch_lag_ms"] == pytest.approx(1000.0)


def test_immediate_stays_sticky_and_provenance_retains_both_callers():
    clock = _FakeClock()
    recompute = _TimingRecompute()
    sched, _ = _make_scheduler(clock, recompute)

    key = (7, "white")
    with sched._cond:
        sched._enqueue_locked(
            key, immediate=True, source=OpeningScoreTrigger.CACHED_SCORE_READER_COLD
        )
    clock.advance(0.2)
    sched.request_recompute(7, "white", source=OpeningScoreTrigger.SESSION_EVIDENCE)
    sched.run_due()

    timing = recompute.timings[0]
    assert timing["immediate"] is True
    # A later normal enqueue did not postpone the immediate entry: it is still due
    # at the current time, so the policy delay stays zero.
    assert timing["deadline_delay_ms"] == pytest.approx(0.0)
    assert timing["dispatch_lag_ms"] == pytest.approx(200.0)
    assert timing["trigger_first"] == "cached_score_reader_cold"
    assert timing["trigger_last"] == "session_evidence"
    assert timing["trigger_sources"] == [
        "cached_score_reader_cold",
        "session_evidence",
    ]


def test_dispatch_lag_includes_head_of_line_wait_behind_an_earlier_due_key():
    # The pickup timestamp must be sampled per _run_one, not when run_due pops the
    # due list: the second key genuinely waited for the first key's whole run.
    clock = _FakeClock()
    timings: list[dict] = []

    def recompute(db, user_id, player_color):
        timings.append(current_run_timing())
        if user_id == 1:
            clock.advance(4.0)  # first run is slow
        return _rebuilt()

    sched, _ = _make_scheduler(clock, recompute)
    sched.request_recompute(1, "white", source=_TRIGGER)
    sched.request_recompute(2, "white", source=_TRIGGER)
    clock.advance(2.0)
    sched.run_due()

    by_lag = sorted(t["dispatch_lag_ms"] for t in timings)
    assert len(by_lag) == 2
    # First key: only the post-deadline wait (2.0s elapsed - 1.5s quiet window).
    assert by_lag[0] == pytest.approx(500.0)
    # Second key: that plus the 4s it spent blocked behind the first run.
    assert by_lag[1] == pytest.approx(4500.0)


def test_reenqueue_during_inflight_run_gets_a_fresh_window_and_run_id():
    clock = _FakeClock()
    timings: list[dict] = []
    followed_up = {"done": False}

    def recompute(db, user_id, player_color):
        timings.append(current_run_timing())
        if not followed_up["done"]:
            followed_up["done"] = True
            clock.advance(1.0)
            sched.request_recompute(user_id, player_color, source=_TRIGGER)
        return _rebuilt()

    sched, _ = _make_scheduler(clock, recompute)
    sched.request_recompute(1, "white", source=_TRIGGER)
    clock.advance(2.0)
    sched.run_due()
    clock.advance(2.0)
    sched.run_due()

    assert len(timings) == 2
    assert timings[0]["scheduler_run_id"] != timings[1]["scheduler_run_id"]
    # The follow-up entry's window starts at its own enqueue, so its queue time is
    # measured from then — not from the original burst.
    assert timings[1]["queue_first_ms"] == pytest.approx(2000.0)
    assert timings[1]["enqueue_count"] == 1


def test_flush_pending_marks_forced_dispatch():
    recompute = _TimingRecompute()
    # Long quiet window: the entry is nowhere near due, so the flush pulls it in
    # ahead of its configured deadline.
    sched, _ = _make_scheduler(time.monotonic, recompute, quiet_window=30.0)
    sched.request_recompute(1, "white", source=_TRIGGER)
    try:
        sched.flush_pending(timeout=5.0)
    finally:
        sched.shutdown()

    assert recompute.timings[0]["forced_dispatch"] is True


def test_forced_dispatch_marking_spares_already_due_entries():
    # The predicate the shutdown drain and flush_pending share. An entry already past
    # its deadline was going to run anyway, so it stays a valid steady-state
    # observation; only a genuine pull-in ahead of the deadline is "forced".
    clock = _FakeClock()
    sched, _ = _make_scheduler(clock, _RecordingRecompute(), quiet_window=1.5)

    sched.request_recompute(1, "white", source=_TRIGGER)  # deadline now+1.5
    clock.advance(2.0)
    sched.request_recompute(2, "white", source=_TRIGGER)  # deadline now+1.5, not due
    with sched._cond:
        sched._mark_forced_dispatch_locked(clock())

    assert sched._pending[(1, "white")].forced_dispatch is False
    assert sched._pending[(2, "white")].forced_dispatch is True


def test_shutdown_drain_does_not_lose_notify_between_worker_iterations(monkeypatch):
    import time

    recompute = _TimingRecompute()
    sched, _ = _make_scheduler(
        time.monotonic,
        recompute,
        quiet_window=0.0,
        max_wait=60.0,
        auto_start=True,
    )

    real_run_due = OpeningScoreScheduler.run_due
    worker_between_iterations = threading.Event()
    release_worker = threading.Event()
    gate_first_run = True

    def gated_run_due(self, now=None):
        nonlocal gate_first_run
        if self is sched and gate_first_run:
            gate_first_run = False
            worker_between_iterations.set()
            assert release_worker.wait(timeout=5.0)
        return real_run_due(self, now)

    # Patch the class, not the scheduler instance: instance monkeypatch undo can
    # leave a bound-method attribute behind on long-lived scheduler objects.
    monkeypatch.setattr(OpeningScoreScheduler, "run_due", gated_run_due)

    # The first key is immediately due and carries the worker out of its condition
    # wait. Gate it before run_due so shutdown's notification is guaranteed to land
    # while the worker is between loop iterations rather than inside Condition.wait.
    sched.request_recompute(1, "white", source=_TRIGGER)
    assert worker_between_iterations.wait(timeout=5.0)

    # Add a second, far-future entry while the worker is gated. The first run_due
    # call will execute key 1 only; the next iteration must observe shutdown and
    # drain key 2 without sleeping for this new quiet window.
    sched.quiet_window = 30.0
    sched.request_recompute(2, "white", source=_TRIGGER)

    shutdown_errors: list[BaseException] = []

    def shut_down():
        try:
            sched.shutdown(drain=True, timeout=0.5)
        except BaseException as exc:
            shutdown_errors.append(exc)

    shutdown_thread = threading.Thread(target=shut_down)
    shutdown_thread.start()

    # Waiting on the same condition proves shutdown has set its latch and sent its
    # notification before the worker is released from the deliberately exposed gap.
    with sched._cond:
        deadline = time.monotonic() + 5.0
        while not sched._shutdown:
            remaining = deadline - time.monotonic()
            assert remaining > 0
            sched._cond.wait(timeout=remaining)

    release_worker.set()
    shutdown_thread.join(timeout=2.0)

    # If the regression returns, wake the daemon so this failing test does not leave
    # a 30-second sleeper behind in the rest of the suite.
    if sched._thread is not None and sched._thread.is_alive():
        with sched._cond:
            for entry in sched._pending.values():
                entry.deadline = sched.clock()
            sched._cond.notify_all()
        sched._thread.join(timeout=2.0)
        sched.shutdown(drain=True, timeout=2.0)

    assert not shutdown_thread.is_alive()
    assert shutdown_errors == []
    assert sched._pending == {}
    assert sched._inflight == set()
    assert sched._thread is None
    assert recompute.calls == [(1, "white"), (2, "white")]
    assert [timing["forced_dispatch"] for timing in recompute.timings] == [False, True]


def test_followup_enqueued_during_a_drain_is_also_marked_forced():
    # Marking at drain start would miss this one: the follow-up entry does not exist
    # yet when the drain begins, and it too runs before its configured deadline.
    clock = _FakeClock()
    timings: list[dict] = []
    followed_up = {"done": False}

    def recompute(db, user_id, player_color):
        timings.append(current_run_timing())
        if not followed_up["done"]:
            followed_up["done"] = True
            sched.request_recompute(user_id, player_color, source=_TRIGGER)
        return _rebuilt()

    sched, _ = _make_scheduler(clock, recompute, quiet_window=30.0)
    sched.request_recompute(1, "white", source=_TRIGGER)
    sched.run_due(now=float("inf"))

    assert len(timings) == 2
    assert all(t["forced_dispatch"] is True for t in timings)


def test_normal_debounced_run_is_not_forced():
    clock = _FakeClock()
    recompute = _TimingRecompute()
    sched, _ = _make_scheduler(clock, recompute)
    sched.request_recompute(1, "white", source=_TRIGGER)
    clock.advance(2.0)
    sched.run_due()

    assert recompute.timings[0]["forced_dispatch"] is False


@pytest.mark.parametrize(
    "result_factory", [_rebuilt, _cached, _no_evidence, None], ids=lambda f: str(f)
)
def test_run_context_is_reset_after_every_disposition_and_exception(result_factory):
    clock = _FakeClock()

    def recompute(db, user_id, player_color):
        assert current_run_timing() is not None  # visible DURING the run
        if result_factory is None:
            raise RuntimeError("boom")
        return result_factory()

    sched, _ = _make_scheduler(clock, recompute)
    sched.request_recompute(1, "white", source=_TRIGGER)
    clock.advance(2.0)
    sched.run_due()

    # Nothing leaks into the next run — or into a direct recompute on this thread.
    assert current_run_timing() is None


def test_unknown_source_raises_before_touching_queue_state():
    clock = _FakeClock()
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(clock, recompute)

    with pytest.raises(UnknownOpeningScoreTrigger) as first:
        sched.request_recompute(1, "white", source="totally_made_up")
    with pytest.raises(UnknownOpeningScoreTrigger) as second:
        sched.refresh_now(1, "white", timeout=5.0, source="totally_made_up")

    # The rejection must not echo the rejected value anywhere reachable by a log:
    # not in the message, and not via a chained exception whose message embeds it.
    for raised in (first, second):
        assert "totally_made_up" not in str(raised.value)
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None

    # Queue state is untouched: no sequence burned, nothing pending, no run.
    assert sched._seq_counter == {}
    assert sched._pending == {}
    assert sched._inflight == set()
    clock.advance(30.0)
    sched.run_due()
    assert recompute.calls == []


def test_module_facade_drops_invalid_source_without_raising(monkeypatch, caplog):
    # Best-effort callers (the /moves and SRS handlers) must not turn a bad source
    # into a 500 — and the invalid value must not reach the queue either.
    from app import opening_score_scheduler as module

    sched, _ = _make_scheduler(_FakeClock(), _RecordingRecompute())
    monkeypatch.setattr(module, "_scheduler", sched)

    with caplog.at_level(logging.DEBUG, logger=module.logger.name):
        _real_request_recompute(1, "white", source="not_a_trigger")
        assert sched._pending == {}
        assert _real_refresh_now(1, "white", source="not_a_trigger") is False
        assert sched._pending == {}

    # Two dropped enqueues, each reported without echoing the rejected value. A
    # bare ``logger.exception`` here would render the traceback — and the enum's
    # own "'not_a_trigger' is not a valid ..." message — into production logs,
    # putting an uncontrolled caller-supplied string in the very sink the closed
    # vocabulary protects.
    formatter = SimpleFormatter("%(asctime)s %(levelname)s %(message)s")
    dropped = [
        formatter.format(record)
        for record in caplog.records
        if "unknown trigger source" in record.getMessage()
    ]
    assert len(dropped) == 2
    for rendered in dropped:
        assert "not_a_trigger" not in rendered
        assert "Traceback" not in rendered


def test_valid_source_string_is_accepted_and_normalized():
    clock = _FakeClock()
    recompute = _TimingRecompute()
    sched, _ = _make_scheduler(clock, recompute)

    sched.request_recompute(1, "white", source="session_lineage_cold")
    clock.advance(2.0)
    sched.run_due()

    assert recompute.timings[0]["trigger_sources"] == ["session_lineage_cold"]


def test_completion_log_failure_is_swallowed_and_next_key_still_runs(monkeypatch):
    from app import opening_score_scheduler as module

    clock = _FakeClock()
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(clock, recompute)

    def boom(*args, **kwargs):
        raise RuntimeError("log handler exploded")

    monkeypatch.setattr(module.logger, "info", boom)
    sched.request_recompute(1, "white", source=_TRIGGER)
    sched.request_recompute(2, "white", source=_TRIGGER)
    clock.advance(2.0)
    # Best-effort: a completion-logging fault must neither raise nor wedge the loop.
    sched.run_due()

    assert sorted(recompute.calls) == [(1, "white"), (2, "white")]


# ---------------------------------------------------------------------------
# Operational completion log — positive contract through the PRODUCTION formatter
#
# The root formatter is "%(asctime)s %(levelname)s %(message)s", so fields placed
# only in `extra` vanish in production. Assert labeled tokens in the RENDERED text,
# independently of order/punctuation/timestamp.
# ---------------------------------------------------------------------------


def test_completion_log_carries_every_semantic_field_after_rendering(caplog):
    clock = _FakeClock()
    recompute = _RecordingRecompute(_rebuilt("evidence_change", generation=42))
    sched, _ = _make_scheduler(clock, recompute)

    sched.request_recompute(1, "white", source=OpeningScoreTrigger.TREE_READER_WARM)
    clock.advance(0.5)
    sched.request_recompute(1, "white", source=OpeningScoreTrigger.SCORE_DELTA)
    clock.advance(2.0)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.run_due()

    rendered = _rendered_completion(caplog)
    assert "run_id=" in rendered
    assert "run_outcome=rebuilt" in rendered
    assert "rebuild_reason=evidence_change" in rendered
    assert "queue_first_ms=2500.0" in rendered
    assert "queue_last_ms=2000.0" in rendered
    assert "coalesce_span_ms=500.0" in rendered
    # The second enqueue re-armed the quiet window from t+0.5, so the final policy
    # deadline sits 2.0s after first_seen and 0.5s of post-deadline lag remains.
    assert "deadline_delay_ms=2000.0" in rendered
    assert "dispatch_lag_ms=500.0" in rendered
    assert "worker_run_ms=" in rendered
    assert "trigger_first=tree_reader_warm" in rendered
    assert "trigger_last=score_delta" in rendered
    assert "trigger_sources=score_delta,tree_reader_warm" in rendered
    assert "enqueue_count=2" in rendered
    assert "immediate=False" in rendered
    assert "forced_dispatch=False" in rendered
    assert "generation=42" in rendered


def test_completion_log_omits_user_derived_identifiers(caplog):
    clock = _FakeClock()
    sched, _ = _make_scheduler(clock, _RecordingRecompute())

    sched.request_recompute(987654, "white", source=_TRIGGER)
    clock.advance(2.0)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.run_due()

    rendered = _rendered_completion(caplog)
    # Operational/aggregate surface only — no user ID, no key, no scores.
    assert "987654" not in rendered
    assert "user_id" not in rendered
    assert "player_color" not in rendered


def test_exactly_one_completion_record_per_executed_run(caplog):
    clock = _FakeClock()
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(clock, recompute)

    sched.request_recompute(1, "white", source=_TRIGGER)
    sched.request_recompute(2, "black", source=_TRIGGER)
    clock.advance(2.0)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.run_due()

    records = _completion_records(caplog)
    assert len(records) == 2
    run_ids = {
        token.split("=", 1)[1]
        for record in records
        for token in record.getMessage().split()
        if token.startswith("run_id=")
    }
    assert len(run_ids) == 2  # each run is independently identifiable


# Phase-0 terminal convergence probes (g-baseline-barrier) -----------------
def test_pending_terminal_probe_is_non_mutating_and_resolves_after_push_fill(caplog):
    clock = _FakeClock()
    fill_finished = []

    def recompute(db, user_id, player_color):
        clock.advance(0.040)
        return _rebuilt(batch_id=17)

    def fill(batch_id):
        assert batch_id == 17
        clock.advance(0.010)
        fill_finished.append(True)

    sched, _ = _make_scheduler(clock, recompute, fill_baselines=fill)
    sched.request_recompute(41, "black", source=_TRIGGER)
    entry = sched._pending[(41, "black")]
    before = vars(entry).copy()

    probe = sched.probe_terminal_recompute(41, "black", register_convergence=True)

    assert probe.state.value == "pending"
    assert probe.convergence_probe_id is not None
    assert vars(entry) == before
    assert sched._thread is None
    clock.advance(1.5)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.run_due()

    assert fill_finished == [True]
    records = _convergence_records(caplog)
    assert len(records) == 1
    rendered = records[0]
    assert f"convergence_probe_id={probe.convergence_probe_id}" in rendered
    assert "state_at_terminal=pending" in rendered
    assert "completion_lag_ms=1550.0" in rendered
    assert "original_deadline_remaining_ms=1500.0" in rendered
    assert "worker_run_ms=40.0" in rendered
    assert "optimistic_lower_bound_ms=1540.0" in rendered
    assert "disposition=rebuilt" in rendered
    assert "forced_dispatch=False" in rendered
    assert sched._convergence_observer_count == 0


def test_inflight_probe_binds_to_active_run_not_later_terminal_enqueue(caplog):
    clock = _FakeClock()
    observed = []
    calls = 0

    def recompute(db, user_id, player_color):
        nonlocal calls
        calls += 1
        if calls == 1:
            probe = sched.probe_terminal_recompute(
                user_id, player_color, register_convergence=True
            )
            observer = sched._convergence_observers[(user_id, player_color)][
                probe.convergence_probe_id
            ]
            observed.append((probe, observer.target_seq))
            sched.request_recompute(user_id, player_color, source=_TRIGGER)
            assert observer.target_seq == 1
            assert sched._pending[(user_id, player_color)].max_seq == 2
        clock.advance(0.020)
        return _rebuilt(batch_id=17)

    sched, _ = _make_scheduler(
        clock, recompute, quiet_window=0.0, fill_baselines=lambda batch_id: None
    )
    sched.request_recompute(7, "white", source=_TRIGGER)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.run_due()

    probe, target_seq = observed[0]
    assert probe.state.value == "inflight"
    assert target_seq == 1
    records = _convergence_records(caplog)
    assert len(records) == 1
    assert f"convergence_probe_id={probe.convergence_probe_id}" in records[0]
    assert "state_at_terminal=inflight" in records[0]
    assert calls == 2


def test_unrelated_run_does_not_resolve_terminal_probe(caplog):
    clock = _FakeClock()
    sched, _ = _make_scheduler(clock, _RecordingRecompute())
    sched.request_recompute(1, "white", source=_TRIGGER)
    sched.request_recompute(2, "black", source=_TRIGGER)
    probe = sched.probe_terminal_recompute(1, "white", register_convergence=True)
    sched._pending[(1, "white")].deadline = clock.now + 10.0
    sched._pending[(2, "black")].deadline = clock.now

    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.run_due()

    assert _convergence_records(caplog) == []
    assert probe.convergence_probe_id in sched._convergence_observers[(1, "white")]
    clock.advance(10.0)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.run_due()
    records = _convergence_records(caplog)
    assert len(records) == 1
    assert f"convergence_probe_id={probe.convergence_probe_id}" in records[0]


def test_terminal_probe_capacity_and_ttl_are_censored_and_bounded(caplog):
    clock = _FakeClock()
    sched, _ = _make_scheduler(
        clock, _RecordingRecompute(),
        convergence_probe_capacity=1, convergence_probe_ttl_s=5.0,
    )
    sched.request_recompute(1, "white", source=_TRIGGER)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        first = sched.probe_terminal_recompute(1, "white", register_convergence=True)
        second = sched.probe_terminal_recompute(1, "white", register_convergence=True)

    assert sched._convergence_observer_count == 1
    records = _convergence_records(caplog)
    assert len(records) == 1
    assert f"convergence_probe_id={second.convergence_probe_id}" in records[0]
    assert "disposition=capacity" in records[0]
    assert "completion_lag_ms=None" in records[0]

    clock.advance(5.0)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.probe_terminal_recompute(9, "black", register_convergence=False)
    records = _convergence_records(caplog)
    assert len(records) == 2
    assert f"convergence_probe_id={first.convergence_probe_id}" in records[1]
    assert "disposition=timeout" in records[1]
    assert sched._convergence_observer_count == 0


def test_terminal_probe_logs_at_most_one_expired_observer_per_request(caplog):
    clock = _FakeClock()
    sched, _ = _make_scheduler(
        clock,
        _RecordingRecompute(),
        convergence_probe_capacity=3,
        convergence_probe_ttl_s=5.0,
    )
    sched.request_recompute(1, "white", source=_TRIGGER)
    for _ in range(3):
        sched.probe_terminal_recompute(1, "white", register_convergence=True)
    assert sched._convergence_observer_count == 3

    clock.advance(5.0)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.probe_terminal_recompute(9, "black", register_convergence=False)

    records = _convergence_records(caplog)
    assert len(records) == 1
    assert "disposition=timeout" in records[0]
    assert sched._convergence_observer_count == 2


@pytest.mark.parametrize(
    ("run_outcome", "expected_disposition"),
    [
        ("failed", "failed"),
        ("no_evidence", "no_evidence"),
        ("no_push_fill", "rebuilt"),
    ],
)
def test_nonconverging_run_has_no_finite_completion_lag(
    run_outcome, expected_disposition, caplog
):
    clock = _FakeClock()

    def recompute(*args):
        clock.advance(0.020)
        if run_outcome == "failed":
            raise RuntimeError("recompute failed")
        if run_outcome == "no_evidence":
            return _no_evidence()
        return _rebuilt(batch_id=None)

    sched, _ = _make_scheduler(clock, recompute)
    sched.request_recompute(1, "white", source=_TRIGGER)
    sched.probe_terminal_recompute(1, "white", register_convergence=True)
    clock.advance(1.5)

    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.run_due()

    record = _convergence_records(caplog)[0]
    assert f"disposition={expected_disposition}" in record
    assert "completion_lag_ms=None" in record
    assert sched._convergence_observer_count == 0


def test_terminal_probe_records_push_fill_failure_without_identity(caplog):
    clock = _FakeClock()

    def fail_fill(batch_id):
        raise RuntimeError("optional fill failed")

    sched, _ = _make_scheduler(
        clock, lambda *args: _rebuilt(batch_id=17), fill_baselines=fail_fill
    )
    sched.request_recompute(987654, "white", source=_TRIGGER)
    probe = sched.probe_terminal_recompute(
        987654, "white", register_convergence=True
    )
    clock.advance(2.0)

    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.run_due()

    record = _convergence_records(caplog)[0]
    assert f"convergence_probe_id={probe.convergence_probe_id}" in record
    assert "disposition=push_fill_failed" in record
    assert "completion_lag_ms=None" in record
    assert "987654" not in record
    assert "user_id" not in record
    assert "player_color" not in record
    assert "session" not in record


def test_shutdown_censors_pending_probe_and_forced_run_has_no_optimistic_bound(caplog):
    clock = _FakeClock()
    sched, _ = _make_scheduler(clock, _RecordingRecompute())
    sched.request_recompute(1, "white", source=_TRIGGER)
    pending = sched.probe_terminal_recompute(1, "white", register_convergence=True)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.shutdown(drain=False)
    records = _convergence_records(caplog)
    assert len(records) == 1
    assert f"convergence_probe_id={pending.convergence_probe_id}" in records[0]
    assert "disposition=shutdown" in records[0]

    caplog.clear()
    sched, _ = _make_scheduler(clock, _RecordingRecompute())
    sched.request_recompute(2, "black", source=_TRIGGER)
    forced = sched.probe_terminal_recompute(2, "black", register_convergence=True)
    with caplog.at_level(logging.INFO, logger="app.opening_score_scheduler"):
        sched.run_due(now=float("inf"))
    records = _convergence_records(caplog)
    assert len(records) == 1
    assert f"convergence_probe_id={forced.convergence_probe_id}" in records[0]
    assert "forced_dispatch=True" in records[0]
    assert "optimistic_lower_bound_ms=None" in records[0]
