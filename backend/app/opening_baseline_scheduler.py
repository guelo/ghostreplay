"""In-process scheduler for async opening-baseline capture (g-mxeo).

``POST /api/game/start`` and ``POST /api/drills/start`` durably INSERT the
``GameSession`` and return. Capturing the opening-score baseline
(``GameSession.opening_score_baseline``) used to run inline on that request path,
but proving the cached batch fresh costs an O(all-evidence) digest (~1.3s best
case, up to ~9.6s GIL-serialized behind a running recompute). This scheduler
moves that capture OFF the request thread: the start handler enqueues a
best-effort job and returns 201 immediately; a background worker fills the
baseline shortly after.

Correctness: the worker persists a baseline only when a current-state batch proof
and the session's durable start-watermark proof both hold. Retryable cold/stale
outcomes use bounded backoff; terminal watermark mismatches remain NULL rather
than inventing a before-score.

IMPORTANT — single-process assumption (same as the other two schedulers):
    Pending state lives in memory and runs on one daemon thread, so coalescing is
    per-process. The deployment configs start ``uvicorn app.main:app`` with one
    worker and one replica, both load-bearing.

This is a dedicated scheduler rather than a rider on the opening-score recompute
scheduler: the keys differ (``session_id`` here vs ``(user_id, color)`` there),
and the baseline worker must not sit behind a long recompute. The accepted
tradeoff is occasional concurrent digest work with the recompute worker.

Coalescing key is ``session_id`` (a session has exactly one user_id +
player_color). There is NO debounce — the job is due immediately on enqueue; a
duplicate enqueue for the same ``session_id`` coalesces to one invocation, and
the job itself is idempotent (it no-ops once the baseline is set).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from app.db import SessionLocal
from app.opening_score_delta import (
    BASELINE_RETRYABLE_SOURCES,
    BaselineSnapshotSource,
    run_baseline_snapshot_job,
)

logger = logging.getLogger(__name__)

Key = uuid.UUID


@dataclass
class _Entry:
    # Untrusted routing hints, folded into the coalesced entry. The job re-reads
    # the authoritative user_id/player_color from the GameSession row and captures
    # for THAT identity; these are used only for logging and a cheap sanity check.
    user_id: int
    player_color: str
    not_before: float
    first_enqueued_at: float
    attempts: int = 0
    enqueue_count: int = 0


@dataclass
class OpeningBaselineScheduler:
    """Session-keyed, no-debounce, best-effort async baseline capture. DI for tests."""

    session_factory: Callable = SessionLocal
    run_job: Callable = None  # set in __post_init__ to break import cycle
    clock: Callable[[], float] = time.monotonic
    auto_start: bool = True

    _pending: dict[Key, _Entry] = field(default_factory=dict, init=False)
    _inflight: set[Key] = field(default_factory=set, init=False)
    _active_entries: dict[Key, _Entry] = field(default_factory=dict, init=False)
    _shutdown: bool = field(default=False, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        if self.run_job is None:
            self.run_job = _default_run_baseline_snapshot_job

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------
    def enqueue(self, session_id: Key, user_id: int, player_color: str) -> None:
        """Coalesce a baseline-capture job for ``session_id`` (due immediately).

        Best-effort: a thread-start failure is swallowed and logged so it can
        never propagate into the ``/start`` handler.
        """
        with self._cond:
            if self._shutdown:
                return
            active = self._active_entries.get(session_id)
            if active is not None:
                # The running job re-reads the authoritative row. A duplicate is
                # already covered; if its outcome is retryable, that same entry is
                # requeued without resetting its original budget.
                active.enqueue_count += 1
                return
            entry = self._pending.get(session_id)
            if entry is None:
                now = self.clock()
                entry = _Entry(
                    user_id=user_id,
                    player_color=player_color,
                    not_before=now,
                    first_enqueued_at=now,
                )
                self._pending[session_id] = entry
            entry.enqueue_count += 1
            self._cond.notify_all()
        if self.auto_start:
            try:
                self.start()
            except Exception:
                logger.exception(
                    "opening baseline scheduler start failed; baseline will not be captured"
                )

    # ------------------------------------------------------------------
    # Synchronous test surface
    # ------------------------------------------------------------------
    def run_due(self, now: float | None = None) -> None:
        """Run entries whose retry deadline has passed on the caller's thread."""
        if now is None:
            now = self.clock()
        while True:
            with self._lock:
                due = [
                    sid
                    for sid, entry in self._pending.items()
                    if entry.not_before <= now and sid not in self._inflight
                ]
                if not due:
                    return
                runs: list[tuple[Key, _Entry]] = []
                for session_id in due:
                    entry = self._pending.pop(session_id)
                    entry.attempts += 1
                    self._inflight.add(session_id)
                    self._active_entries[session_id] = entry
                    runs.append((session_id, entry))
            for session_id, entry in runs:
                self._run_one(session_id, entry)

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
                name="opening-baseline-scheduler",
                daemon=True,
            )
            try:
                # Keep publication and start atomic. The worker may begin and block
                # on this lock, but no concurrent caller can replace it.
                thread.start()
            except Exception:
                self._thread = None
                self._shutdown = False
                raise
            self._thread = thread

    def shutdown(self, drain: bool = True, timeout: float = 30.0) -> None:
        """Stop accepting work and wait boundedly for the worker to exit.

        Draining remains on the worker thread so a hung job cannot wedge the
        caller performing application teardown.
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
                raise TimeoutError(
                    "opening baseline scheduler did not stop before timeout"
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
                deadlines = [
                    entry.not_before
                    for session_id, entry in self._pending.items()
                    if session_id not in self._inflight
                ]
                wait_for = None if not deadlines else max(0.0, min(deadlines) - now)
                if wait_for is None or wait_for > 0:
                    # Bounded fallback keeps shutdown responsive even if a notify
                    # is missed; ordinary retries wake at their earliest deadline.
                    self._cond.wait(
                        timeout=1.0 if wait_for is None else min(wait_for, 1.0)
                    )
                shutting_down = self._shutdown
            self.run_due(now=float("inf") if shutting_down else None)

    # ------------------------------------------------------------------
    # Run a single session's baseline job
    # ------------------------------------------------------------------
    def _run_one(self, session_id: Key, entry: _Entry) -> None:
        db = None
        raw_source: object = BaselineSnapshotSource.FAILED.value
        try:
            db = self.session_factory()
            raw_source = self.run_job(
                db,
                session_id=session_id,
                user_id=entry.user_id,
                player_color=entry.player_color,
            )
        except Exception:
            # run_baseline_snapshot_job never raises, but guard anyway so the
            # worker thread is never killed by an unexpected fault.
            logger.exception(
                "opening baseline snapshot job failed",
                extra={
                    "session_id": str(session_id),
                    "user_id": entry.user_id,
                    "player_color": entry.player_color,
                },
            )
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    logger.exception(
                        "opening baseline scheduler failed to close session"
                    )

            try:
                source = BaselineSnapshotSource(raw_source)
            except (TypeError, ValueError):
                source = None
                logger.error(
                    "opening baseline job returned an unknown source type=%s; stopping",
                    type(raw_source).__name__,
                )

            should_requeue = source in BASELINE_RETRYABLE_SOURCES
            if source in {
                BaselineSnapshotSource.SKIPPED_COLD,
                BaselineSnapshotSource.SKIPPED_STALE,
                BaselineSnapshotSource.SKIPPED_RECOMPUTE_INFLIGHT,
            }:
                with self._lock:
                    shutting_down = self._shutdown
                if not shutting_down:
                    try:
                        from app.opening_score_scheduler import (
                            OpeningScoreTrigger,
                            is_recompute_scheduled,
                            request_recompute,
                        )

                        if not is_recompute_scheduled(
                            entry.user_id, entry.player_color
                        ):
                            request_recompute(
                                entry.user_id,
                                entry.player_color,
                                source=OpeningScoreTrigger.BASELINE_RECOVERY,
                            )
                    except Exception:
                        logger.exception(
                            "opening baseline recovery recompute request failed"
                        )

            requeue = False
            if should_requeue:
                now = self.clock()
                delay = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)[
                    min(entry.attempts - 1, 5)
                ]
                next_not_before = now + delay
                with self._lock:
                    shutting_down = self._shutdown
                requeue = (
                    not shutting_down
                    and entry.attempts < 8
                    and next_not_before - entry.first_enqueued_at <= 120.0
                )
                if requeue:
                    entry.not_before = next_not_before

            with self._cond:
                self._inflight.discard(session_id)
                self._active_entries.pop(session_id, None)
                if requeue:
                    self._pending[session_id] = entry
                self._cond.notify_all()


# Module-level singleton + thin facade -------------------------------------
def _default_run_baseline_snapshot_job(db, **kwargs) -> str:
    """Run the real baseline-capture job on ``db``.

    Kept as a seam so scheduler tests can inject a deterministic job.
    """
    return run_baseline_snapshot_job(
        db,
        kwargs["session_id"],
        kwargs["user_id"],
        kwargs["player_color"],
    )


_scheduler = OpeningBaselineScheduler()


def get_baseline_scheduler() -> OpeningBaselineScheduler:
    return _scheduler


def enqueue_baseline_snapshot(
    session_id: Key, user_id: int, player_color: str
) -> None:
    """Schedule the async opening-baseline capture (best-effort).

    Fully swallowing: any scheduler enqueue/start failure is logged and never
    propagates into the ``/start`` handler (an enqueue failure must not regress it
    from 201 to 500 — same contract as ``request_recompute`` /
    ``enqueue_session_evidence``).
    """
    try:
        _scheduler.enqueue(session_id, user_id, player_color)
    except Exception:
        logger.exception(
            "opening baseline enqueue failed",
            extra={
                "session_id": str(session_id),
                "user_id": user_id,
                "player_color": player_color,
            },
        )
