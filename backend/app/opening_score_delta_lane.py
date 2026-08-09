"""Immediate in-process lane for terminal-session opening-score deltas.

Supported terminal game and drill handlers enqueue two independent background
jobs:

* this lane publishes the freshness-bound played-opening result immediately;
* ``opening_score_scheduler`` performs the ordinary debounced whole-graph
  recompute that eventually converges dashboard and tree rows.

The split is load-bearing. This module never calls ``refresh_now``, never runs a
whole-graph recompute, and has no dependency on the whole-graph scheduler's
pending or in-flight state.

The queue is keyed by ``(user_id, player_color)`` so terminal sessions for one
opening overlay share a single Phase-2 publication call. A duplicate session
keeps only its newest request generation. New work is due immediately; only a
failed attempt is delayed by bounded retry backoff.

IMPORTANT — single-process assumption:
    The queue, request generations, and scoped publications all live in process
    memory. The deployment configs start one uvicorn worker and one replica;
    both limits are load-bearing. More workers or replicas require a shared
    queue plus shared generation/publication coordination before scoped
    freshness remains reliable.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from app.db import SessionLocal
from app.opening_score_delta import (
    ScopedDeltaRequest,
    is_scoped_delta_request_current,
    publish_scoped_opening_score_deltas,
    reserve_scoped_delta_generation,
)

logger = logging.getLogger(__name__)

Key = tuple[int, str]

DELTA_LANE_MAX_PENDING_KEYS = 128
DELTA_LANE_MAX_SESSIONS_PER_KEY = 32
DELTA_LANE_RETRY_BACKOFF_SECONDS = (0.25, 1.0, 2.0)


class DeltaLaneEnqueueOutcome(str, Enum):
    ENQUEUED = "enqueued"
    COALESCED = "coalesced"
    PENDING_KEY_OVERFLOW = "pending_key_overflow"
    SESSION_OVERFLOW = "session_overflow"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True, slots=True)
class _QueuedRequest:
    request: ScopedDeltaRequest
    first_seen: float
    last_seen: float
    enqueue_count: int
    retry_count: int = 0


@dataclass(slots=True)
class _Entry:
    deadline: float
    requests: dict[str, _QueuedRequest] = field(default_factory=dict)

    @property
    def first_seen(self) -> float:
        return min(item.first_seen for item in self.requests.values())

    @property
    def enqueue_count(self) -> int:
        return sum(item.enqueue_count for item in self.requests.values())

    @property
    def retry_number(self) -> int:
        return max(item.retry_count for item in self.requests.values())

    @property
    def has_first_attempt(self) -> bool:
        return any(item.retry_count == 0 for item in self.requests.values())


@dataclass
class OpeningScoreDeltaLane:
    """Keyed, immediate, single-worker scoped-delta lane. Dependency-injected."""

    session_factory: Callable = SessionLocal
    publish: Callable = staticmethod(publish_scoped_opening_score_deltas)
    reserve: Callable = staticmethod(reserve_scoped_delta_generation)
    request_is_current: Callable = staticmethod(is_scoped_delta_request_current)
    clock: Callable[[], float] = time.monotonic
    auto_start: bool = True
    max_pending_keys: int = DELTA_LANE_MAX_PENDING_KEYS
    max_sessions_per_key: int = DELTA_LANE_MAX_SESSIONS_PER_KEY
    retry_backoff: tuple[float, ...] = DELTA_LANE_RETRY_BACKOFF_SECONDS

    _pending: dict[Key, _Entry] = field(default_factory=dict, init=False)
    _inflight: set[Key] = field(default_factory=set, init=False)
    _shutdown: bool = field(default=False, init=False)
    _cancel_pending: bool = field(default=False, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.max_pending_keys <= 0:
            raise ValueError("max_pending_keys must be positive")
        if self.max_sessions_per_key <= 0:
            raise ValueError("max_sessions_per_key must be positive")
        if any(delay < 0 for delay in self.retry_backoff):
            raise ValueError("retry_backoff delays must be non-negative")
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

    def enqueue(
        self,
        user_id: int,
        player_color: str,
        session_id,
    ) -> DeltaLaneEnqueueOutcome:
        """Reserve and enqueue one terminal session without a quiet window.

        Generation reservation and pending-entry replacement share ``_cond``.
        Capacity is checked before reservation, but a capacity-rejected enqueue
        still reserves its generation so an older in-flight publication cannot
        become current.
        """
        key: Key = (user_id, player_color)
        with self._cond:
            if self._shutdown:
                return DeltaLaneEnqueueOutcome.SHUTDOWN

            entry = self._pending.get(key)
            session_key = str(session_id)
            key_overflow = entry is None and len(self._pending) >= self.max_pending_keys
            session_overflow = (
                entry is not None
                and session_key not in entry.requests
                and len(entry.requests) >= self.max_sessions_per_key
            )

            # Load-bearing ordering: reserve while the lane lock is held even when
            # capacity will reject the request. The new generation invalidates any
            # older in-flight publication through its compare-and-swap.
            request = self.reserve(session_id)
            session_key = str(request.session_id)

            if key_overflow:
                self._log_overflow(
                    key, outcome=DeltaLaneEnqueueOutcome.PENDING_KEY_OVERFLOW
                )
                return DeltaLaneEnqueueOutcome.PENDING_KEY_OVERFLOW
            if session_overflow:
                self._log_overflow(
                    key, outcome=DeltaLaneEnqueueOutcome.SESSION_OVERFLOW
                )
                return DeltaLaneEnqueueOutcome.SESSION_OVERFLOW

            now = self.clock()
            if entry is None:
                entry = _Entry(deadline=now)
                self._pending[key] = entry
            previous = entry.requests.get(session_key)
            entry.requests[session_key] = _QueuedRequest(
                request=request,
                first_seen=previous.first_seen if previous is not None else now,
                last_seen=now,
                enqueue_count=(
                    previous.enqueue_count + 1 if previous is not None else 1
                ),
                # A new authoritative terminal enqueue gets the full retry budget.
                retry_count=0,
            )
            # A first attempt is always due now. It also pulls this key ahead of a
            # delayed retry already pending for another session.
            entry.deadline = min(entry.deadline, now)
            self._cond.notify_all()
            outcome = (
                DeltaLaneEnqueueOutcome.COALESCED
                if previous is not None
                else DeltaLaneEnqueueOutcome.ENQUEUED
            )

        if self.auto_start:
            try:
                self.start()
            except Exception:
                logger.exception(
                    "opening score delta lane start failed; scoped delta will not run"
                )
        return outcome

    def _log_overflow(
        self,
        key: Key,
        *,
        outcome: DeltaLaneEnqueueOutcome,
    ) -> None:
        user_id, player_color = key
        logger.warning(
            "opening_score_delta_lane_enqueue outcome=%s user_id=%s color=%s "
            "pending_keys=%s max_pending_keys=%s max_sessions_per_key=%s",
            outcome.value,
            user_id,
            player_color,
            len(self._pending),
            self.max_pending_keys,
            self.max_sessions_per_key,
        )

    def is_scheduled(self, user_id: int, player_color: str) -> bool:
        key: Key = (user_id, player_color)
        with self._lock:
            return key in self._pending or key in self._inflight

    def is_inflight(self, user_id: int, player_color: str) -> bool:
        key: Key = (user_id, player_color)
        with self._lock:
            return key in self._inflight

    def run_due(self, now: float | None = None) -> None:
        """Run every due key on the caller's thread, one key at a time."""
        live_clock = now is None
        while True:
            if live_clock:
                now = self.clock()
            assert now is not None
            with self._lock:
                due = [
                    key
                    for key, entry in self._pending.items()
                    if entry.deadline <= now and key not in self._inflight
                ]
                if not due:
                    return
                # Mark only the key that is actually about to execute. The lane
                # has one worker, so marking a whole due batch "in-flight" would
                # make the baseline guard treat keys waiting behind this one as
                # active CPU contention.
                # A fresh terminal attempt wins over an expired retry even when
                # the retry's key entered the dict first. Within the same class,
                # preserve deadline/arrival order for deterministic fairness.
                key = min(
                    due,
                    key=lambda candidate: (
                        not self._pending[candidate].has_first_attempt,
                        self._pending[candidate].deadline,
                        self._pending[candidate].first_seen,
                    ),
                )
                entry = self._pending.pop(key)
                self._inflight.add(key)
            self._run_one(key, entry)

    def flush_pending(self, timeout: float = 30.0) -> None:
        """Force pending work due and wait boundedly for lane quiescence."""
        deadline = self.clock() + timeout
        self.start()
        while True:
            with self._cond:
                if not self._pending and not self._inflight:
                    return
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TimeoutError(
                        "opening score delta lane flush did not reach quiescence"
                    )
                now = self.clock()
                for entry in self._pending.values():
                    entry.deadline = min(entry.deadline, now)
                self._cond.notify_all()
                self._cond.wait(timeout=min(remaining, 0.1))

    def start(self) -> None:
        """Idempotently start, recreating the one worker after shutdown."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._shutdown = False
            self._cancel_pending = False
            thread = threading.Thread(
                target=self._worker_loop,
                name="opening-score-delta-lane",
                daemon=True,
            )
            try:
                thread.start()
            except Exception:
                self._thread = None
                self._shutdown = False
                raise
            self._thread = thread

    def shutdown(self, drain: bool = True, timeout: float = 30.0) -> None:
        """Stop accepting work and boundedly drain or cancel pending attempts."""
        if drain:
            # Start before taking the shutdown latch even when the queue is empty.
            # That closes the small accept-without-worker race for a concurrent
            # enqueue between a pending-state check and setting ``_shutdown``.
            self.start()
        with self._cond:
            self._shutdown = True
            self._cancel_pending = not drain
            cancelled_keys = len(self._pending) if not drain else 0
            cancelled_requests = (
                sum(len(entry.requests) for entry in self._pending.values())
                if not drain
                else 0
            )
            if not drain:
                self._pending.clear()
            self._cond.notify_all()
            thread = self._thread
        if cancelled_requests:
            logger.info(
                "opening_score_delta_lane_shutdown outcome=cancelled "
                "cancelled_keys=%s cancelled_requests=%s",
                cancelled_keys,
                cancelled_requests,
            )
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise TimeoutError(
                    "opening score delta lane did not stop before timeout"
                )
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def _worker_loop(self) -> None:
        while True:
            with self._cond:
                if self._shutdown and not self._pending:
                    return
                now = self.clock()
                due_deadlines = [
                    entry.deadline
                    for key, entry in self._pending.items()
                    if key not in self._inflight
                ]
                wait_for = (
                    None
                    if not due_deadlines
                    else max(0.0, min(due_deadlines) - now)
                )
                if wait_for is None or wait_for > 0:
                    self._cond.wait(timeout=wait_for)
                shutting_down = self._shutdown
            self.run_due(now=float("inf") if shutting_down else None)

    def _run_one(self, key: Key, entry: _Entry) -> None:
        worker_started = self.clock()
        user_id, player_color = key
        db = None
        published_count = 0
        phase: dict[str, object] = {}
        outcome = "failed"
        retry_scheduled = 0
        retry_exhausted = 0
        retry_overflow = 0
        retry_cancelled = 0
        try:
            db = self.session_factory()
            published = self.publish(
                db,
                user_id,
                player_color,
                tuple(item.request for item in entry.requests.values()),
                on_complete=phase.update,
            )
            published_count = int(published or 0)
            outcome = str(phase.get("outcome", "completed"))
        except Exception:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            logger.exception(
                "opening score delta lane publication failed",
                extra={"user_id": user_id, "player_color": player_color},
            )
            with self._cond:
                (
                    retry_scheduled,
                    retry_exhausted,
                    retry_overflow,
                    retry_cancelled,
                ) = self._requeue_failed_locked(key, entry)
            if retry_scheduled:
                outcome = "retry_scheduled"
            elif retry_exhausted:
                outcome = "retry_exhausted"
            elif retry_overflow:
                outcome = "retry_overflow"
            elif retry_cancelled:
                outcome = "retry_cancelled"
            else:
                outcome = "superseded"
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    logger.exception(
                        "opening score delta lane failed to close session"
                    )
            with self._cond:
                self._inflight.discard(key)
                self._cond.notify_all()

        stage_ms = phase.get("stage_ms")
        if not isinstance(stage_ms, dict):
            stage_ms = {}
        logger.info(
            "opening_score_delta_lane_run outcome=%s user_id=%s color=%s "
            "request_count=%s candidate_count=%s enqueue_count=%s "
            "queue_to_dispatch_ms=%.3f "
            "retry_number=%s retry_scheduled=%s retry_exhausted=%s "
            "retry_overflow=%s retry_cancelled=%s phase_outcome=%s "
            "published_count=%s "
            "session_load_ms=%s counter_ms=%s overlay_ms=%s digest_ms=%s "
            "score_ms=%s publish_ms=%s total_ms=%s "
            "replay_cache_builds=%s replay_cache_probed_sessions=%s "
            "replay_cache_l1_hits=%s replay_cache_l2_hits=%s "
            "replay_cache_raw_derivations=%s "
            "replay_cache_persisted_upserts=%s "
            "replay_cache_l2_read_failed=%s "
            "replay_cache_l2_write_failed=%s",
            outcome,
            user_id,
            player_color,
            len(entry.requests),
            phase.get("candidate_count"),
            entry.enqueue_count,
            (worker_started - entry.first_seen) * 1000.0,
            entry.retry_number,
            retry_scheduled,
            retry_exhausted,
            retry_overflow,
            retry_cancelled,
            phase.get("outcome"),
            published_count,
            stage_ms.get("session_load"),
            stage_ms.get("counter"),
            stage_ms.get("overlay"),
            stage_ms.get("digest"),
            stage_ms.get("score"),
            stage_ms.get("publish"),
            phase.get("total_ms"),
            phase.get("replay_cache_builds", 0),
            phase.get("replay_cache_probed_sessions", 0),
            phase.get("replay_cache_l1_hits", 0),
            phase.get("replay_cache_l2_hits", 0),
            phase.get("replay_cache_raw_derivations", 0),
            phase.get("replay_cache_persisted_upserts", 0),
            phase.get("replay_cache_l2_read_failed", False),
            phase.get("replay_cache_l2_write_failed", False),
        )

    def _requeue_failed_locked(
        self,
        key: Key,
        failed: _Entry,
    ) -> tuple[int, int, int, int]:
        """Requeue only current requests. Caller must hold ``_cond``."""
        scheduled = 0
        exhausted = 0
        overflow = 0
        cancelled = 0
        if self._cancel_pending:
            cancelled = len(failed.requests)
            return scheduled, exhausted, overflow, cancelled

        now = self.clock()
        pending = self._pending.get(key)
        for session_key, queued in failed.requests.items():
            try:
                current = self.request_is_current(queued.request)
            except Exception:
                logger.exception(
                    "opening score delta lane generation probe failed"
                )
                current = False
            if not current:
                continue
            if queued.retry_count >= len(self.retry_backoff):
                exhausted += 1
                continue
            if pending is None:
                if len(self._pending) >= self.max_pending_keys:
                    overflow += 1
                    continue
                pending = _Entry(deadline=float("inf"))
                self._pending[key] = pending
            if (
                session_key not in pending.requests
                and len(pending.requests) >= self.max_sessions_per_key
            ):
                overflow += 1
                continue
            retry_count = queued.retry_count + 1
            pending.requests[session_key] = _QueuedRequest(
                request=queued.request,
                first_seen=queued.first_seen,
                last_seen=queued.last_seen,
                enqueue_count=queued.enqueue_count,
                retry_count=retry_count,
            )
            retry_deadline = now + self.retry_backoff[queued.retry_count]
            pending.deadline = min(pending.deadline, retry_deadline)
            scheduled += 1
        if pending is not None and not pending.requests:
            self._pending.pop(key, None)
        if scheduled:
            self._cond.notify_all()
        return scheduled, exhausted, overflow, cancelled


_lane = OpeningScoreDeltaLane()


def get_delta_lane() -> OpeningScoreDeltaLane:
    return _lane


def enqueue_scoped_delta(
    user_id: int,
    player_color: str,
    session_id,
) -> None:
    """Best-effort terminal enqueue; never propagate into an HTTP handler."""
    try:
        _lane.enqueue(user_id, player_color, session_id)
    except Exception:
        logger.exception(
            "opening score delta lane enqueue failed",
            extra={"user_id": user_id, "player_color": player_color},
        )


def is_delta_lane_inflight(user_id: int, player_color: str) -> bool:
    """Best-effort in-flight-only probe used by baseline capture."""
    try:
        return _lane.is_inflight(user_id, player_color)
    except Exception:
        logger.exception(
            "opening score delta lane inflight probe failed",
            extra={"user_id": user_id, "player_color": player_color},
        )
        return False
