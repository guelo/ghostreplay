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

Read side (stale-while-revalidate):
    Reader endpoints go through ``opening_cache.load_cached_rows``. A WARM reader
    (a batch already exists) calls ``request_recompute(user_id, player_color)`` to
    schedule a coalesced BACKGROUND recompute and serves the currently-cached
    batch immediately — it never blocks. Only a COLD reader (no batch yet) calls
    ``refresh_now(user_id, player_color)`` — a keyed flush/await that enqueues an
    immediate recompute for that one key and waits for a covering, successful run
    to reach quiescence (bounded by ``timeout``). It runs only the matching key's
    work through the single serialized worker, so one user's read never triggers
    unrelated users' recomputes. On ``True`` the cold reader reloads and serves the
    freshly-built rows; on ``False`` (timeout/failure/shutdown) it serves whatever
    is cached (possibly empty) and lets the worker finish in the background. All
    recompute decisions (cache miss, registry drift, stale branch keys, evidence
    change) are consolidated in ``recompute_opening_scores_if_needed`` so the
    worker is the only reader-driven path that writes a batch.
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
    # Highest per-key enqueue sequence folded into this pending entry. A run that
    # pops this entry has "run through" this sequence; refresh_now waiters compare
    # against it to know their enqueue has been covered by a completed run.
    max_seq: int = 0
    # Sticky once an immediate (refresh_now) enqueue folds in: the entry stays due
    # now until it is popped. Later normal enqueues must not push the deadline back
    # out — otherwise a sustained burst could starve refresh_now past its timeout.
    immediate: bool = False


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
    # Per-key monotonic enqueue counter and the latest completed run outcome
    # (ran_through_seq, ok). Both guarded by ``_cond``.
    _seq_counter: dict[Key, int] = field(default_factory=dict, init=False)
    _last_result: dict[Key, tuple[int, bool]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------
    def _enqueue_locked(self, key: Key, *, immediate: bool) -> int:
        """Coalesce an enqueue for ``key``. Caller must hold ``_cond``.

        Returns the per-key sequence assigned to this enqueue. ``immediate``
        makes the entry due now (used by ``refresh_now``); otherwise the normal
        debounce window applies.
        """
        now = self.clock()
        seq = self._seq_counter.get(key, 0) + 1
        self._seq_counter[key] = seq
        entry = self._pending.get(key)
        if entry is None:
            deadline = now if immediate else min(now + self.quiet_window, now + self.max_wait)
            entry = _Entry(
                first_seen=now, deadline=deadline, max_seq=seq, immediate=immediate
            )
            self._pending[key] = entry
        else:
            if immediate:
                entry.immediate = True
                entry.deadline = min(entry.deadline, now)
            elif entry.immediate:
                # An immediate refresh is already pending; keep it due now. Fold
                # this enqueue's sequence in but never postpone the deadline.
                pass
            else:
                entry.deadline = min(now + self.quiet_window, entry.first_seen + self.max_wait)
            entry.max_seq = max(entry.max_seq, seq)
        entry.enqueue_count += 1
        self._cond.notify_all()
        return seq

    def request_recompute(self, user_id: int, player_color: str) -> None:
        """Coalesce a recompute request for ``(user_id, player_color)``.

        Best-effort: a thread-start failure is swallowed and logged so it can
        never propagate into the ``/moves`` or SRS request handlers.
        """
        key: Key = (user_id, player_color)
        with self._cond:
            if self._shutdown:
                return
            self._enqueue_locked(key, immediate=False)
        if self.auto_start:
            try:
                self.start()
            except Exception:
                logger.exception("opening score scheduler start failed; recompute will not run")

    def refresh_now(
        self, user_id: int, player_color: str, timeout: float = 5.0
    ) -> bool:
        """Enqueue an immediate keyed recompute and await its quiescent success.

        Returns ``True`` only when a run that covers this enqueue's sequence
        completed **successfully** and the key has no pending or in-flight work
        remaining. Returns ``False`` on a covering-run failure, worker-start
        failure, scheduler shutdown, or ``timeout`` — in which case the caller
        should serve the current cached batch and let the worker finish in the
        background.
        """
        key: Key = (user_id, player_color)
        deadline = self.clock() + timeout
        with self._cond:
            if self._shutdown:
                return False
            seq = self._enqueue_locked(key, immediate=True)
        try:
            self.start()
        except Exception:
            logger.exception("opening score scheduler start failed; refresh_now aborting")
            return False
        with self._cond:
            while True:
                if self._shutdown:
                    return False
                result = self._last_result.get(key)
                covered = result is not None and result[0] >= seq
                quiescent = key not in self._pending and key not in self._inflight
                if covered and quiescent:
                    return result[1]
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=min(remaining, 0.1))

    def is_scheduled(self, user_id: int, player_color: str) -> bool:
        """Non-mutating: is a recompute for this key pending or in-flight?

        The cheap read-side probe behind ``/api/openings/tree/status`` uses this to
        tell a freshly-fired bootstrap ("cold") from one already running
        ("building") WITHOUT enqueuing anything. Guarded by the same lock the worker
        mutates ``_pending``/``_inflight`` under, so it observes a consistent set.
        """
        key: Key = (user_id, player_color)
        with self._lock:
            return key in self._pending or key in self._inflight

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
                runs: list[tuple[Key, int, int]] = []
                for key in due:
                    entry = self._pending.pop(key)
                    self._inflight.add(key)
                    runs.append((key, entry.enqueue_count, entry.max_seq))
            for key, enqueue_count, ran_seq in runs:
                self._run_one(key, enqueue_count, ran_seq)

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
    def _run_one(self, key: Key, enqueue_count: int, ran_seq: int) -> None:
        user_id, player_color = key
        db = None
        result = None
        ok = False
        try:
            db = self.session_factory()
            result = self.recompute(db, user_id, player_color)
            ok = True
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
                # Record the highest sequence this key has run through and whether
                # that run succeeded. Per-key runs are serialized (single worker,
                # one in-flight per key), so ran_seq is monotonic for the key.
                prev = self._last_result.get(key)
                if prev is None or ran_seq >= prev[0]:
                    self._last_result[key] = (ran_seq, ok)
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


def refresh_now(user_id: int, player_color: str, timeout: float = 5.0) -> bool:
    """Reader-facing keyed flush/await (best-effort).

    Returns ``True`` only when a covering recompute completed successfully and the
    key is quiescent; ``False`` on failure/timeout/shutdown (or any scheduler
    error). The caller serves the current cached batch on ``False``.
    """
    try:
        return _scheduler.refresh_now(user_id, player_color, timeout=timeout)
    except Exception:
        logger.exception(
            "opening score refresh_now failed",
            extra={"user_id": user_id, "player_color": player_color},
        )
        return False


def is_recompute_scheduled(user_id: int, player_color: str) -> bool:
    """Non-mutating probe: is a recompute for this key pending or in-flight?

    Best-effort (any scheduler error is logged and reported as "not scheduled")
    so the read-side ``/tree/status`` probe can never 500 on a scheduler fault.
    """
    try:
        return _scheduler.is_scheduled(user_id, player_color)
    except Exception:
        logger.exception(
            "opening score is_scheduled probe failed",
            extra={"user_id": user_id, "player_color": player_color},
        )
        return False
