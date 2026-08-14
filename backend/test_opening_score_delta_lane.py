"""Deterministic mechanics for the immediate terminal-session delta lane."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from unittest.mock import Mock, patch

import pytest

from app.opening_score_delta import (
    ScopedDeltaRequest,
    is_scoped_delta_request_current,
    reserve_scoped_delta_generation,
)
from app.opening_score_delta_lane import (
    DELTA_LANE_RETRY_BACKOFF_SECONDS,
    DeltaLaneEnqueueOutcome,
    OpeningScoreDeltaLane,
)

# conftest patches the source facade for API isolation. Keep an import-time
# reference for the one test that exercises the real facade.
from app.opening_score_delta_lane import enqueue_scoped_delta as _real_enqueue


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeSession:
    def __init__(self) -> None:
        self.rollbacks = 0
        self.closed = False

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _RecordingPublish:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, tuple[ScopedDeltaRequest, ...]]] = []

    def __call__(
        self,
        db,
        user_id,
        player_color,
        requests,
        *,
        on_complete,
    ):
        self.calls.append((user_id, player_color, requests))
        on_complete(
            {
                "outcome": "published",
                "candidate_count": len(requests),
                "published_count": len(requests),
                "stage_ms": {
                    "session_load": 1.0,
                    "counter": 2.0,
                    "overlay": 3.0,
                    "digest": 4.0,
                    "score": 5.0,
                    "publish": 6.0,
                },
                "total_ms": 21.0,
                "replay_cache_builds": 1,
                "replay_cache_probed_sessions": 4,
                "replay_cache_l1_hits": 1,
                "replay_cache_l2_hits": 2,
                "replay_cache_raw_derivations": 1,
                "replay_cache_persisted_upserts": 1,
                "replay_cache_l2_read_failed": False,
                "replay_cache_l2_write_failed": True,
            }
        )
        return len(requests)


def _make_lane(clock, publish, **kwargs):
    sessions: list[_FakeSession] = []

    def session_factory():
        session = _FakeSession()
        sessions.append(session)
        return session

    params = {
        "clock": clock,
        "publish": publish,
        "session_factory": session_factory,
        "auto_start": False,
    }
    params.update(kwargs)
    return OpeningScoreDeltaLane(**params), sessions


def test_first_attempt_dispatches_immediately_without_clock_advance():
    clock = _FakeClock()
    publish = _RecordingPublish()
    lane, sessions = _make_lane(clock, publish)

    lane.enqueue(123, "white", uuid.uuid4())
    lane.run_due()

    assert len(publish.calls) == 1
    assert publish.calls[0][:2] == (123, "white")
    assert sessions[0].closed is True


def test_same_key_coalesces_sessions_and_duplicate_keeps_newest_generation():
    clock = _FakeClock()
    publish = _RecordingPublish()
    lane, _ = _make_lane(clock, publish)
    first = uuid.uuid4()
    second = uuid.uuid4()

    assert lane.enqueue(123, "white", first) is DeltaLaneEnqueueOutcome.ENQUEUED
    old_request = lane._pending[(123, "white")].requests[str(first)].request
    assert lane.enqueue(123, "white", first) is DeltaLaneEnqueueOutcome.COALESCED
    assert lane.enqueue(123, "white", second) is DeltaLaneEnqueueOutcome.ENQUEUED
    lane.enqueue(456, "black", uuid.uuid4())
    lane.run_due()

    assert len(publish.calls) == 2
    shared = next(call for call in publish.calls if call[:2] == (123, "white"))
    requests = shared[2]
    assert {request.session_id for request in requests} == {first, second}
    assert len(requests) == 2
    assert old_request.generation < next(
        request.generation for request in requests if request.session_id == first
    )
    assert is_scoped_delta_request_current(old_request) is False


def test_enqueue_during_inflight_invalidates_old_and_runs_one_followup():
    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[ScopedDeltaRequest, ...]] = []
    active = 0
    max_active = 0
    state_lock = threading.Lock()

    def publish(db, user_id, player_color, requests, *, on_complete):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        calls.append(requests)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=5.0)
        with state_lock:
            active -= 1
        on_complete(
            {
                "outcome": "published",
                "published_count": len(requests),
                "stage_ms": {},
                "total_ms": 0.0,
            }
        )
        return len(requests)

    lane, _ = _make_lane(time.monotonic, publish)
    session_id = uuid.uuid4()
    lane.enqueue(123, "white", session_id)
    worker = threading.Thread(target=lane.run_due)
    worker.start()
    assert entered.wait(timeout=5.0)
    assert lane.is_inflight(123, "white") is True

    lane.enqueue(123, "white", session_id)
    release.set()
    worker.join(timeout=5.0)

    assert worker.is_alive() is False
    assert len(calls) == 2
    assert calls[0][0].generation < calls[1][0].generation
    assert is_scoped_delta_request_current(calls[0][0]) is False
    assert is_scoped_delta_request_current(calls[1][0]) is True
    assert max_active == 1


def test_reservation_and_pending_replacement_are_atomic_across_enqueuers():
    first_inside_reserve = threading.Event()
    second_inside_reserve = threading.Event()
    release_first = threading.Event()
    counter = 0
    counter_lock = threading.Lock()

    def reserve(session_id):
        nonlocal counter
        with counter_lock:
            counter += 1
            generation = counter
        if generation == 1:
            first_inside_reserve.set()
            assert release_first.wait(timeout=5.0)
        else:
            second_inside_reserve.set()
        return ScopedDeltaRequest(session_id=session_id, generation=generation)

    lane, _ = _make_lane(
        time.monotonic,
        _RecordingPublish(),
        reserve=reserve,
        request_is_current=lambda request: True,
    )
    session_id = uuid.uuid4()
    first = threading.Thread(target=lane.enqueue, args=(123, "white", session_id))
    second = threading.Thread(target=lane.enqueue, args=(123, "white", session_id))
    first.start()
    assert first_inside_reserve.wait(timeout=5.0)
    second.start()
    # The second reservation cannot begin while the first enqueue still owns the
    # lane condition; this closes reserve-B / overwrite-by-A ordering.
    assert second_inside_reserve.wait(timeout=0.1) is False
    release_first.set()
    first.join(timeout=5.0)
    second.join(timeout=5.0)

    assert second_inside_reserve.is_set()
    queued = lane._pending[(123, "white")].requests[str(session_id)].request
    assert queued.generation == 2


def test_ready_first_attempt_precedes_already_due_retry():
    clock = _FakeClock()
    calls: list[int] = []

    def publish(db, user_id, player_color, requests, *, on_complete):
        calls.append(user_id)
        if user_id == 1 and calls.count(1) == 1:
            raise RuntimeError("retry me")
        on_complete(
            {
                "outcome": "published",
                "published_count": 1,
                "stage_ms": {},
                "total_ms": 0.0,
            }
        )
        return 1

    lane, _ = _make_lane(clock, publish, retry_backoff=(0.5,))
    lane.enqueue(1, "white", uuid.uuid4())
    lane.run_due()
    assert calls == [1]

    clock.advance(0.5)
    # Key 1's retry is already due and precedes key 2 in insertion order.
    # The fresh first attempt still dispatches first.
    lane.enqueue(2, "black", uuid.uuid4())
    lane.run_due()
    assert calls == [1, 2, 1]


def test_new_generation_supersedes_a_delayed_retry_and_runs_now():
    clock = _FakeClock()
    calls: list[ScopedDeltaRequest] = []

    def publish(db, user_id, player_color, requests, *, on_complete):
        calls.append(requests[0])
        if len(calls) == 1:
            raise RuntimeError("first attempt")
        on_complete(
            {
                "outcome": "published",
                "published_count": 1,
                "stage_ms": {},
                "total_ms": 0.0,
            }
        )
        return 1

    lane, _ = _make_lane(clock, publish, retry_backoff=(10.0,))
    session_id = uuid.uuid4()
    lane.enqueue(1, "white", session_id)
    lane.run_due()

    lane.enqueue(1, "white", session_id)
    lane.run_due()

    assert len(calls) == 2
    assert calls[1].generation > calls[0].generation
    assert lane._pending == {}


def test_failure_rolls_back_closes_and_exhausts_bounded_retries(caplog):
    clock = _FakeClock()

    def publish(db, user_id, player_color, requests, *, on_complete):
        raise RuntimeError("always broken")

    lane, sessions = _make_lane(clock, publish, retry_backoff=(0.25,))
    lane.enqueue(1, "white", uuid.uuid4())
    with caplog.at_level(logging.INFO):
        lane.run_due()
        clock.advance(0.25)
        lane.run_due()

    assert len(sessions) == 2
    assert all(session.rollbacks == 1 for session in sessions)
    assert all(session.closed for session in sessions)
    assert lane._pending == {}
    assert "outcome=retry_exhausted" in caplog.text


def test_production_backoff_makes_exactly_four_fail_closed_attempts(caplog):
    clock = _FakeClock()
    calls = 0

    def publish(db, user_id, player_color, requests, *, on_complete):
        nonlocal calls
        calls += 1
        raise RuntimeError("corrupt played-root closure")

    lane, _ = _make_lane(
        clock,
        publish,
        retry_backoff=DELTA_LANE_RETRY_BACKOFF_SECONDS,
    )
    lane.enqueue(1, "white", uuid.uuid4())
    with caplog.at_level(logging.INFO, logger="app.opening_score_delta_lane"):
        lane.run_due()
        for delay in DELTA_LANE_RETRY_BACKOFF_SECONDS:
            clock.advance(delay)
            lane.run_due()

    completions = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("opening_score_delta_lane_run ")
    ]
    assert calls == 4
    assert sum("outcome=retry_scheduled" in message for message in completions) == 3
    assert sum("outcome=retry_exhausted" in message for message in completions) == 1
    assert lane._pending == {}


def test_cancelled_inflight_failure_is_not_reported_as_overflow(caplog):
    entered = threading.Event()
    release = threading.Event()

    def publish(db, user_id, player_color, requests, *, on_complete):
        entered.set()
        assert release.wait(timeout=5.0)
        raise RuntimeError("failed during cancellation")

    lane, _ = _make_lane(time.monotonic, publish)
    lane.enqueue(1, "white", uuid.uuid4())
    worker = threading.Thread(target=lane.run_due)
    worker.start()
    assert entered.wait(timeout=5.0)

    lane.shutdown(drain=False, timeout=5.0)
    with caplog.at_level(logging.INFO, logger="app.opening_score_delta_lane"):
        release.set()
        worker.join(timeout=5.0)

    assert worker.is_alive() is False
    assert lane._pending == {}
    assert "outcome=retry_cancelled" in caplog.text
    assert "retry_cancelled=1" in caplog.text
    assert "outcome=retry_overflow" not in caplog.text


def test_capacity_rejection_still_invalidates_an_older_generation():
    clock = _FakeClock()
    lane, _ = _make_lane(
        clock,
        _RecordingPublish(),
        max_pending_keys=1,
        max_sessions_per_key=1,
    )
    lane.enqueue(1, "white", uuid.uuid4())

    rejected_key_session = uuid.uuid4()
    older_key = reserve_scoped_delta_generation(rejected_key_session)
    assert (
        lane.enqueue(2, "black", rejected_key_session)
        is DeltaLaneEnqueueOutcome.PENDING_KEY_OVERFLOW
    )
    assert is_scoped_delta_request_current(older_key) is False

    existing_key_session = uuid.uuid4()
    older_session = reserve_scoped_delta_generation(existing_key_session)
    assert (
        lane.enqueue(1, "white", existing_key_session)
        is DeltaLaneEnqueueOutcome.SESSION_OVERFLOW
    )
    assert is_scoped_delta_request_current(older_session) is False


def test_inflight_probe_counts_running_not_merely_pending():
    entered = threading.Event()
    release = threading.Event()

    def publish(db, user_id, player_color, requests, *, on_complete):
        entered.set()
        assert release.wait(timeout=5.0)
        return 0

    lane, _ = _make_lane(time.monotonic, publish)
    lane.enqueue(1, "white", uuid.uuid4())
    lane.enqueue(2, "black", uuid.uuid4())
    assert lane.is_scheduled(1, "white") is True
    assert lane.is_inflight(1, "white") is False

    worker = threading.Thread(target=lane.run_due)
    worker.start()
    assert entered.wait(timeout=5.0)
    assert lane.is_inflight(1, "white") is True
    # The second due key is still pending behind the one worker, not falsely
    # marked as active CPU merely because run_due observed both deadlines.
    assert lane.is_scheduled(2, "black") is True
    assert lane.is_inflight(2, "black") is False
    release.set()
    worker.join(timeout=5.0)
    assert lane.is_scheduled(1, "white") is False
    assert lane.is_scheduled(2, "black") is False


def test_start_failure_is_swallowed_and_pending_work_remains(caplog):
    lane, _ = _make_lane(time.monotonic, _RecordingPublish(), auto_start=True)
    with (
        caplog.at_level(logging.ERROR),
        patch("app.opening_score_delta_lane.threading.Thread.start", side_effect=RuntimeError("boom")),
    ):
        outcome = lane.enqueue(1, "white", uuid.uuid4())

    assert outcome is DeltaLaneEnqueueOutcome.ENQUEUED
    assert lane.is_scheduled(1, "white") is True
    assert "delta lane start failed" in caplog.text


def test_generation_probe_failure_drops_retry_without_killing_run_due(caplog):
    def publish(db, user_id, player_color, requests, *, on_complete):
        raise RuntimeError("publish failed")

    def broken_probe(request):
        raise RuntimeError("probe failed")

    lane, _ = _make_lane(
        time.monotonic,
        publish,
        request_is_current=broken_probe,
    )
    lane.enqueue(1, "white", uuid.uuid4())

    with caplog.at_level(logging.ERROR):
        lane.run_due()

    assert lane._pending == {}
    assert lane._inflight == set()
    assert "generation probe failed" in caplog.text


def test_flush_shutdown_cancel_and_restart_are_bounded_and_reusable():
    publish = _RecordingPublish()
    lane, _ = _make_lane(time.monotonic, publish)
    lane.enqueue(1, "white", uuid.uuid4())
    lane.flush_pending(timeout=5.0)
    assert len(publish.calls) == 1
    lane.shutdown(drain=True, timeout=5.0)

    cancel_lane, _ = _make_lane(time.monotonic, publish)
    cancel_lane.enqueue(2, "black", uuid.uuid4())
    cancel_lane.shutdown(drain=False, timeout=5.0)
    assert cancel_lane._pending == {}
    assert cancel_lane._thread is None

    cancel_lane.start()
    cancel_lane.enqueue(3, "white", uuid.uuid4())
    cancel_lane.flush_pending(timeout=5.0)
    cancel_lane.shutdown(drain=True, timeout=5.0)
    assert [call[0] for call in publish.calls] == [1, 3]


def test_shutdown_timeout_does_not_run_publication_on_caller():
    entered = threading.Event()
    release = threading.Event()

    def publish(db, user_id, player_color, requests, *, on_complete):
        entered.set()
        release.wait(timeout=5.0)
        return 0

    lane, _ = _make_lane(time.monotonic, publish, auto_start=True)
    lane.enqueue(1, "white", uuid.uuid4())
    assert entered.wait(timeout=5.0)

    before = time.monotonic()
    with pytest.raises(TimeoutError):
        lane.shutdown(drain=True, timeout=0.1)
    assert time.monotonic() - before < 0.5

    release.set()
    lane.shutdown(drain=True, timeout=5.0)


def test_shutdown_drain_starts_worker_for_pending_non_autostart_lane():
    publish = _RecordingPublish()
    lane, _ = _make_lane(time.monotonic, publish)
    lane.enqueue(1, "white", uuid.uuid4())

    lane.shutdown(drain=True, timeout=5.0)

    assert len(publish.calls) == 1
    assert lane._pending == {}
    assert lane._thread is None


def test_shutdown_drain_does_not_lose_notify_between_worker_iterations(monkeypatch):
    publish = _RecordingPublish()
    lane, _ = _make_lane(time.monotonic, publish, auto_start=True)

    real_run_due = OpeningScoreDeltaLane.run_due
    worker_between_iterations = threading.Event()
    release_worker = threading.Event()
    gate_first_run = True

    def gated_run_due(self, now=None):
        nonlocal gate_first_run
        if self is lane and gate_first_run:
            gate_first_run = False
            worker_between_iterations.set()
            assert release_worker.wait(timeout=5.0)
        return real_run_due(self, now)

    monkeypatch.setattr(OpeningScoreDeltaLane, "run_due", gated_run_due)

    lane.enqueue(1, "white", uuid.uuid4())
    assert worker_between_iterations.wait(timeout=5.0)

    lane.enqueue(2, "black", uuid.uuid4())
    with lane._cond:
        lane._pending[(2, "black")].deadline = lane.clock() + 30.0

    shutdown_errors: list[BaseException] = []

    def shut_down():
        try:
            lane.shutdown(drain=True, timeout=0.5)
        except BaseException as exc:
            shutdown_errors.append(exc)

    shutdown_thread = threading.Thread(target=shut_down)
    shutdown_thread.start()

    with lane._cond:
        deadline = time.monotonic() + 5.0
        while not lane._shutdown:
            remaining = deadline - time.monotonic()
            assert remaining > 0
            lane._cond.wait(timeout=remaining)

    release_worker.set()
    shutdown_thread.join(timeout=2.0)

    if lane._thread is not None and lane._thread.is_alive():
        with lane._cond:
            for entry in lane._pending.values():
                entry.deadline = lane.clock()
            lane._cond.notify_all()
        lane._thread.join(timeout=2.0)
        lane.shutdown(drain=True, timeout=2.0)

    assert not shutdown_thread.is_alive()
    assert shutdown_errors == []
    assert lane._pending == {}
    assert lane._inflight == set()
    assert lane._thread is None
    assert [call[:2] for call in publish.calls] == [(1, "white"), (2, "black")]


def test_shutdown_drain_preserves_retry_backoff(monkeypatch):
    clock = _FakeClock()
    attempts: list[float] = []

    def fail_once(db, user_id, player_color, requests, *, on_complete):
        attempts.append(clock())
        if len(attempts) == 1:
            raise RuntimeError("transient publication failure")
        on_complete({"outcome": "published", "published_count": len(requests)})
        return len(requests)

    lane, _ = _make_lane(clock, fail_once, retry_backoff=(0.25,))
    lane.enqueue(1, "white", uuid.uuid4())
    with lane._cond:
        lane._shutdown = True

    waits: list[float] = []
    real_wait = threading.Condition.wait

    def advancing_wait(condition, timeout=None):
        if condition is lane._cond:
            assert timeout is not None
            waits.append(timeout)
            clock.advance(timeout)
            return True
        return real_wait(condition, timeout)

    monkeypatch.setattr(threading.Condition, "wait", advancing_wait)
    lane._worker_loop()

    assert attempts == pytest.approx([1000.0, 1000.25])
    assert sum(waits) == pytest.approx(0.25)
    assert all(wait <= 0.1 for wait in waits)


def test_shutdown_drain_parks_when_pending_key_is_already_inflight(monkeypatch):
    publish = _RecordingPublish()
    lane, _ = _make_lane(_FakeClock(), publish)
    key = (1, "white")
    lane.enqueue(*key, uuid.uuid4())
    with lane._cond:
        lane._shutdown = True
        lane._inflight.add(key)

    parked = threading.Event()
    real_wait = threading.Condition.wait

    def observed_wait(condition, timeout=None):
        if condition is lane._cond:
            assert timeout == 0.1
            parked.set()
        return real_wait(condition, timeout)

    monkeypatch.setattr(threading.Condition, "wait", observed_wait)
    worker = threading.Thread(target=lane._worker_loop)
    worker.start()
    assert parked.wait(timeout=5.0)
    assert publish.calls == []

    with lane._cond:
        lane._inflight.discard(key)
        lane._cond.notify_all()
    worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert [call[:2] for call in publish.calls] == [key]


def test_completion_log_carries_queue_phase_and_publication_timings(caplog):
    clock = _FakeClock()
    lane, _ = _make_lane(clock, _RecordingPublish())
    lane.enqueue(1, "white", uuid.uuid4())
    clock.advance(0.05)

    with caplog.at_level(logging.INFO, logger="app.opening_score_delta_lane"):
        lane.run_due()

    assert "queue_to_dispatch_ms=50.000" in caplog.text
    assert "phase_outcome=published" in caplog.text
    assert "candidate_count=1" in caplog.text
    assert "overlay_ms=3.0" in caplog.text
    assert "published_count=1" in caplog.text
    assert "replay_cache_builds=1" in caplog.text
    assert "replay_cache_probed_sessions=4" in caplog.text
    assert "replay_cache_l1_hits=1" in caplog.text
    assert "replay_cache_l2_hits=2" in caplog.text
    assert "replay_cache_raw_derivations=1" in caplog.text
    assert "replay_cache_persisted_upserts=1" in caplog.text
    assert "replay_cache_l2_read_failed=False" in caplog.text
    assert "replay_cache_l2_write_failed=True" in caplog.text


def test_facade_swallows_unexpected_enqueue_failure(monkeypatch, caplog):
    broken = Mock()
    broken.enqueue.side_effect = RuntimeError("boom")
    monkeypatch.setattr("app.opening_score_delta_lane._lane", broken)

    with caplog.at_level(logging.ERROR):
        assert _real_enqueue(1, "white", uuid.uuid4()) is None

    assert "delta lane enqueue failed" in caplog.text
