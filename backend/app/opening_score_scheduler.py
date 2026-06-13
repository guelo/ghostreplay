"""In-process debounced scheduler for opening-score recomputes.

Rapid same-opening drill replays each flip the evidence fingerprint and would
otherwise trigger one full ``recompute_opening_scores`` per replay. This module
coalesces those into **one pending recompute per ``(user_id, player_color)``**,
executed once after a burst of enqueues settles (a quiet window, capped by a
maximum wait).

IMPORTANT — single-process assumption:
    The scheduler coalesces only within **one process of one replica**. It holds
    all pending state in memory and runs recomputes on a single daemon thread.
    The deployment configs (railway.toml, render.yaml, nixpacks.toml) start
    ``uvicorn app.main:app`` with no ``--workers`` flag (1 worker) and set no
    replica/scaling counts, so a single instance is the current default. Both
    **one worker AND one replica** are load-bearing here. Adding workers or
    horizontal replicas reverts coalescing to per-process and needs shared-state
    (advisory-lock / external queue) coordination instead.

Accepted staleness window:
    While a recompute is pending in the debounce window, reader endpoints serve
    the previously cached batch (``ensure_opening_scores`` returns cached batches
    unconditionally, and ``_refresh_cached_scores_if_stale`` only compares the
    registry fingerprint, not the evidence ``inputs_fingerprint`` that drilling
    flips). If the process dies with pending work, scores stay stale until the
    next evidence change re-enqueues a recompute. This is accepted for v1: during
    a drill burst the user is drilling, not reading their opening report, and the
    window is seconds. A read-side flush/await belongs with g-score-cache-api.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from app.db import SessionLocal
from app.opening_cache import recompute_opening_scores_if_needed

logger = logging.getLogger(__name__)

Key = tuple[int, str]


@dataclass
class _Entry:
    first_seen: float
    deadline: float
    enqueue_count: int = 0


@dataclass
class OpeningScoreScheduler:
    """Debounced, coalescing scheduler. Dependency-injected for testing."""

    session_factory: Callable = SessionLocal
    recompute: Callable = staticmethod(recompute_opening_scores_if_needed)
    clock: Callable[[], float] = time.monotonic
    quiet_window: float = 1.5
    max_wait: float = 10.0
    auto_start: bool = True

    _pending: dict[Key, _Entry] = field(default_factory=dict, init=False)
    _inflight: set[Key] = field(default_factory=set, init=False)
    _shutdown: bool = field(default=False, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------
    def request_recompute(self, user_id: int, player_color: str) -> None:
        """Coalesce a recompute request for ``(user_id, player_color)``.

        Best-effort: a thread-start failure is swallowed and logged so it can
        never propagate into the ``/moves`` or SRS request handlers.
        """
        key: Key = (user_id, player_color)
        with self._cond:
            if self._shutdown:
                return
            now = self.clock()
            entry = self._pending.get(key)
            if entry is None:
                entry = _Entry(
                    first_seen=now,
                    deadline=min(now + self.quiet_window, now + self.max_wait),
                )
                self._pending[key] = entry
            else:
                entry.deadline = min(now + self.quiet_window, entry.first_seen + self.max_wait)
            entry.enqueue_count += 1
            self._cond.notify()
        if self.auto_start:
            try:
                self.start()
            except Exception:
                logger.exception("opening score scheduler start failed; recompute will not run")

    # ------------------------------------------------------------------
    # Synchronous test surface
    # ------------------------------------------------------------------
    def run_due(self, now: float | None = None) -> None:
        """Run all keys whose deadline has passed. Runs on the caller's thread."""
        if now is None:
            now = self.clock()
        while True:
            with self._lock:
                due = [
                    key
                    for key, entry in self._pending.items()
                    if entry.deadline <= now and key not in self._inflight
                ]
                if not due:
                    return
                runs: list[tuple[Key, int]] = []
                for key in due:
                    entry = self._pending.pop(key)
                    self._inflight.add(key)
                    runs.append((key, entry.enqueue_count))
            for key, enqueue_count in runs:
                self._run_one(key, enqueue_count)

    def flush_pending(self, timeout: float = 30.0) -> None:
        """Block until both ``_pending`` and ``_inflight`` are empty.

        Requests an immediate worker-thread drain, then waits on the condition.
        Recompute code never runs on this caller's thread, so ``timeout`` remains
        enforceable even when a recompute hangs. Raises ``TimeoutError`` if the
        system has not gone quiescent within the bound.
        """
        deadline = self.clock() + timeout
        self.start()
        while True:
            with self._cond:
                if not self._pending and not self._inflight:
                    return
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TimeoutError("flush_pending did not reach quiescence")
                # Make only entries that exist now immediately due. Later
                # enqueues keep their normal debounce deadlines even if this
                # flush times out while a worker run is still in flight.
                for entry in self._pending.values():
                    entry.deadline = min(entry.deadline, self.clock())
                self._cond.notify_all()
                self._cond.wait(timeout=min(remaining, 0.1))

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Idempotent start. Re-creates a worker thread after a prior shutdown."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._shutdown = False
            thread = threading.Thread(
                target=self._worker_loop,
                name="opening-score-scheduler",
                daemon=True,
            )
            try:
                # Keep publication and start atomic. The worker may begin and
                # block on this lock, but no concurrent caller can replace it.
                thread.start()
            except Exception:
                self._thread = None
                self._shutdown = False
                raise
            self._thread = thread

    def shutdown(self, drain: bool = True, timeout: float = 30.0) -> None:
        """Stop accepting work and wait boundedly for the worker to exit.

        Draining remains on the worker thread so a hung recompute cannot wedge
        the caller performing application teardown.
        """
        with self._cond:
            self._shutdown = True
            if not drain:
                self._pending.clear()
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise TimeoutError("opening score scheduler did not stop before timeout")
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
                if due_deadlines:
                    wait_for = max(0.0, min(due_deadlines) - now)
                else:
                    wait_for = None
                if wait_for is None or wait_for > 0:
                    self._cond.wait(timeout=wait_for)
                shutting_down = self._shutdown
            self.run_due(now=float("inf") if shutting_down else None)

    # ------------------------------------------------------------------
    # Run a single recompute
    # ------------------------------------------------------------------
    def _run_one(self, key: Key, enqueue_count: int) -> None:
        user_id, player_color = key
        db = None
        before_gen: int | None = None
        result = None
        try:
            db = self.session_factory()
            result = self.recompute(db, user_id, player_color)
        except Exception:
            logger.exception(
                "opening score recompute failed",
                extra={"user_id": user_id, "player_color": player_color},
            )
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    logger.exception("opening score scheduler failed to close session")
            with self._cond:
                self._inflight.discard(key)
                self._cond.notify_all()
        # NOTE: recompute_opening_scores_if_needed returns the EXISTING batch
        # unchanged when the fingerprint matches, so a non-None result does not
        # prove a new batch was written. Report generation as a "had a batch"
        # signal only.
        generation = getattr(result, "generation", None)
        logger.info(
            "opening score recompute run",
            extra={
                "user_id": user_id,
                "player_color": player_color,
                "enqueue_count": enqueue_count,
                "result_present": result is not None,
                "batch_generation": generation,
            },
        )


# Module-level singleton + thin facade -------------------------------------
_scheduler = OpeningScoreScheduler()


def get_scheduler() -> OpeningScoreScheduler:
    return _scheduler


def request_recompute(user_id: int, player_color: str) -> None:
    """Schedule a coalesced opening-score recompute (best-effort).

    Fully swallowing: any scheduler enqueue/start failure is logged and never
    propagates into the ``/moves`` or SRS request handlers (an enqueue failure
    must not regress those endpoints from 200 to 500).
    """
    try:
        _scheduler.request_recompute(user_id, player_color)
    except Exception:
        logger.exception(
            "opening score recompute enqueue failed",
            extra={"user_id": user_id, "player_color": player_color},
        )


def get_scheduler() -> OpeningScoreScheduler:
    return _scheduler
