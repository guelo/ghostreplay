"""Tests for the in-process debounced opening-score recompute scheduler.

Unit tests inject a fake clock + session factory and drive the scheduler
synchronously (``run_due`` / ``flush_pending``) so coalescing/debounce logic is
deterministic with no real sleeps. Exactly one test exercises the real worker
thread, signalled via ``threading.Event`` rather than timing guesses.
"""

from __future__ import annotations

import threading
from unittest.mock import Mock

import anyio
import pytest

from app import main
from app.opening_score_scheduler import OpeningScoreScheduler


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class _RecordingRecompute:
    """Records (user_id, player_color) per call and the session it received."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []
        self.sessions: list[object] = []

    def __call__(self, db, user_id, player_color):
        self.calls.append((user_id, player_color))
        self.sessions.append(db)
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


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
        sched.request_recompute(123, "white")
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
        sched._enqueue_locked(key, immediate=True)  # due now

    # Normal enqueues keep arriving shortly after; none may postpone the deadline.
    for _ in range(5):
        sched.request_recompute(7, "white")
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
    sched.request_recompute(123, "white")
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


def test_distinct_keys_each_recompute_once_with_own_session():
    clock = _FakeClock()
    recompute = _RecordingRecompute()
    sched, sessions = _make_scheduler(clock, recompute)

    sched.request_recompute(1, "white")
    sched.request_recompute(2, "black")
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
            sched.request_recompute(user_id, player_color)
        recompute.calls.append((user_id, player_color))
        return None

    recompute.calls = []
    sched, _ = _make_scheduler(clock, recompute)

    sched.request_recompute(5, "white")
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
        return None

    recompute.calls = []
    sched, _ = _make_scheduler(clock, recompute)

    sched.request_recompute(1, "white")
    sched.request_recompute(2, "white")
    clock.advance(2.0)
    sched.run_due()

    assert sorted(recompute.calls) == [(1, "white"), (2, "white")]
    # The scheduler is not wedged: a later key still runs.
    sched.request_recompute(3, "white")
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
        sched.request_recompute(7, "white")
        clock.advance(1.0)
        sched.run_due()

    assert recompute.calls.count((7, "white")) >= 1


def test_initial_enqueue_deadline_is_capped_by_max_wait():
    clock = _FakeClock()
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(
        clock, recompute, quiet_window=20.0, max_wait=3.0
    )

    sched.request_recompute(7, "white")
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
        sched.request_recompute(cycle, "white")
        sched.flush_pending(timeout=5.0)
        sched.shutdown(drain=True, timeout=5.0)
        # Post-shutdown enqueue is a no-op and does not raise.
        sched.request_recompute(999, "white")

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
    sched.request_recompute(1, "white")


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
    sched.request_recompute(1, "white")
    assert sched._thread is None
    assert sched._shutdown is False

    # Second enqueue cleanly starts a worker and processes the key.
    sched.request_recompute(1, "white")
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
        return None

    # Real clock so flush_pending's timeout bound actually elapses.
    sched, _ = _make_scheduler(
        time.monotonic, recompute, quiet_window=0.0, auto_start=True
    )
    sched.start()
    sched.request_recompute(1, "white")
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
    sched.request_recompute(1, "white")

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
    sched.request_recompute(1, "white")
    assert first_started.wait(timeout=5.0)

    with pytest.raises(TimeoutError):
        sched.flush_pending(timeout=0.1)

    # Enqueue while the stale forced-drain snapshot would still be blocked in
    # the first recompute. This later request must retain its quiet window.
    sched.request_recompute(2, "white")
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
        return None

    sched, sessions = _make_scheduler(
        _FakeClock(), recompute, quiet_window=0.0, auto_start=True
    )
    sched.start()
    try:
        sched.request_recompute(42, "black")
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
    sched.request_recompute(1, "white")
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
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    monkeypatch.setattr(main, "get_scheduler", lambda: scheduler)
    monkeypatch.setattr(main.engine, "connect", lambda: connection)
    dispose = Mock()
    monkeypatch.setattr(main.engine, "dispose", dispose)

    async def exercise_lifespan():
        async with main.lifespan(main.app):
            pass

    anyio.run(exercise_lifespan)

    scheduler.start.assert_called_once_with()
    scheduler.shutdown.assert_called_once_with(drain=True)
    dispose.assert_called_once_with()


# ---------------------------------------------------------------------------
# refresh_now (keyed flush/await with outcome tracking) — real worker thread
# ---------------------------------------------------------------------------

import time  # noqa: E402


def test_refresh_now_returns_true_on_successful_run():
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(time.monotonic, recompute)
    try:
        assert sched.refresh_now(1, "white", timeout=5.0) is True
        assert recompute.calls == [(1, "white")]
    finally:
        sched.shutdown()


def test_refresh_now_returns_false_on_recompute_exception():
    def boom(db, user_id, player_color):
        raise RuntimeError("recompute failed")

    sched, _ = _make_scheduler(time.monotonic, boom)
    try:
        # A covering run that fails must not be reported as fresh.
        assert sched.refresh_now(1, "white", timeout=5.0) is False
    finally:
        sched.shutdown()


def test_refresh_now_returns_false_on_worker_start_failure_without_blocking():
    recompute = _RecordingRecompute()
    sched, _ = _make_scheduler(time.monotonic, recompute)
    sched.start = Mock(side_effect=RuntimeError("cannot start worker"))
    started = time.monotonic()
    assert sched.refresh_now(1, "white", timeout=5.0) is False
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
        sched.request_recompute(2, "black")
        assert sched.refresh_now(1, "white", timeout=5.0) is True
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
        return None

    sched, _ = _make_scheduler(time.monotonic, slow)
    try:
        # The in-flight run blocks; refresh_now times out and serves the current batch.
        assert sched.refresh_now(1, "white", timeout=0.3) is False
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
            sched.request_recompute(user_id, player_color)
        return None

    sched, _ = _make_scheduler(time.monotonic, recompute, quiet_window=0.0)
    try:
        assert sched.refresh_now(1, "white", timeout=5.0) is True
        assert calls == [(1, "white"), (1, "white")]
    finally:
        sched.shutdown()
