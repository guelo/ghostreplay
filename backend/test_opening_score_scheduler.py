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
