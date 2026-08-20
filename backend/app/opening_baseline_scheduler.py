"""In-process scheduler for async opening-baseline capture (g-mxeo).

``POST /api/game/start`` and ``POST /api/drills/start`` durably INSERT the
``GameSession`` and return. Capturing the opening-score baseline
(``GameSession.opening_score_baseline``) used to run inline on that request path.
The current cached-batch proof is O(1) while the per-user sequence and shared
epoch match; after shared-epoch drift it hashes only the stored scope of that
batch. The historical full raw-evidence digest is no longer part of this path.
This scheduler keeps all baseline proof and persistence work OFF the request
thread: the start handler enqueues a best-effort job and returns 201 immediately;
a background worker fills the baseline shortly after.

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
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from app.db import SessionLocal
from app.opening_score_delta import (
    BASELINE_RETRYABLE_SOURCES,
    BaselineSnapshotSource,
    run_baseline_snapshot_job,
)

logger = logging.getLogger(__name__)

Key = uuid.UUID


class BaselineSchedulerState(str, Enum):
    """Closed terminal-telemetry vocabulary for baseline queue state."""

    PENDING = "pending"
    INFLIGHT = "inflight"
    ABSENT = "absent"
    PROBE_FAILED = "probe_failed"


class TerminalKind(str, Enum):
    """Included terminal routes that own an opening-score delta."""

    GAME_END = "game_end"
    CONVERTED_DRILL_END = "converted_drill_end"
    DRILL_ACCURACY_FAIL = "drill_accuracy_fail"
    DRILL_NATURAL_END = "drill_natural_end"


@dataclass(frozen=True, slots=True)
class BaselineSchedulerProbe:
    state: BaselineSchedulerState
    attempts_bucket: str


def _attempts_bucket(attempts: int) -> str:
    if attempts <= 0:
        return "0"
    if attempts == 1:
        return "1"
    if attempts <= 3:
        return "2_3"
    if attempts <= 7:
        return "4_7"
    return "8_plus"


def _session_age_bucket(started_at: object) -> str:
    """Coarse, closed age bucket without publishing a session timestamp."""

    if not isinstance(started_at, datetime):
        return "unknown"
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    age_s = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
    if age_s < 60:
        return "under_1m"
    if age_s < 5 * 60:
        return "1m_5m"
    if age_s < 30 * 60:
        return "5m_30m"
    if age_s < 2 * 60 * 60:
        return "30m_2h"
    return "2h_plus"


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

    def probe(self, session_id: Key) -> BaselineSchedulerProbe:
        """Snapshot queue state without creating, waking, or changing an entry."""

        with self._lock:
            entry = self._active_entries.get(session_id)
            if entry is not None:
                return BaselineSchedulerProbe(
                    BaselineSchedulerState.INFLIGHT,
                    _attempts_bucket(entry.attempts),
                )
            entry = self._pending.get(session_id)
            if entry is not None:
                return BaselineSchedulerProbe(
                    BaselineSchedulerState.PENDING,
                    _attempts_bucket(entry.attempts),
                )
            return BaselineSchedulerProbe(BaselineSchedulerState.ABSENT, "0")

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
                # A shutdown notification can land while run_due executes outside
                # this lock. Once the latch is visible, never begin another retry
                # wait before draining the remaining entries.
                shutting_down = self._shutdown
                deadlines = [
                    entry.not_before
                    for session_id, entry in self._pending.items()
                    if session_id not in self._inflight
                ]
                if shutting_down:
                    if not deadlines:
                        # Defensive invariant guard: this scheduler keeps pending
                        # and in-flight keys disjoint, so current production and
                        # synchronous paths cannot reach this state. If future
                        # queueing changes do, park instead of spinning.
                        self._cond.wait(timeout=0.1)
                else:
                    now = self.clock()
                    wait_for = None if not deadlines else max(0.0, min(deadlines) - now)
                    if wait_for is None or wait_for > 0:
                        # Bounded fallback also keeps ordinary operation responsive
                        # if a notification is missed.
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
        requeue = False
        retry_budget_exhausted = False
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
                retry_budget_exhausted = not shutting_down and not requeue
                if requeue:
                    entry.not_before = next_not_before

            with self._cond:
                self._inflight.discard(session_id)
                self._active_entries.pop(session_id, None)
                attempt = entry.attempts
                enqueue_age_ms = round(
                    max(0.0, self.clock() - entry.first_enqueued_at) * 1000.0,
                    3,
                )
                if requeue:
                    self._pending[session_id] = entry
                self._cond.notify_all()
            try:
                logger.info(
                    "opening_baseline_scheduler_attempt session_id=%s source=%s "
                    "attempt=%s enqueue_age_ms=%s requeued=%s "
                    "retry_budget_exhausted=%s",
                    session_id,
                    source.value if source is not None else "unknown",
                    attempt,
                    enqueue_age_ms,
                    requeue,
                    retry_budget_exhausted,
                )
            except Exception:
                logger.exception("opening baseline scheduler attempt log failed")


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


def probe_baseline_snapshot(session_id: Key) -> BaselineSchedulerProbe:
    """Best-effort, non-mutating baseline-scheduler probe for terminal telemetry."""

    try:
        return _scheduler.probe(session_id)
    except Exception:
        logger.exception(
            "opening baseline scheduler probe failed",
            extra={"session_id": str(session_id)},
        )
        return BaselineSchedulerProbe(BaselineSchedulerState.PROBE_FAILED, "0")


def terminal_baseline_observation(
    session: object,
    terminal_kind: TerminalKind | str,
) -> dict[str, object]:
    """Build the no-read, no-wait terminal analytics payload.

    The caller snapshots this while its already-loaded ``GameSession`` is still in
    the admitted pre-terminal state, then attaches the returned properties only to
    the successful post-commit event. Instrumentation failures never alter the
    route. Scheduler failures use ``probe_failed``; an unreadable baseline state
    uses the closed ``observation_failed`` sentinel.
    """

    try:
        kind = TerminalKind(terminal_kind)
    except (TypeError, ValueError):
        logger.error("terminal baseline observation dropped: unknown terminal kind")
        return {}

    baseline_state = "observation_failed"
    convergence_probe_id: str | None = None
    try:
        baseline_present = getattr(session, "opening_score_baseline", None) is not None
        watermark_complete = all(
            getattr(session, field_name, None) is not None
            for field_name in (
                "baseline_watermark_seq",
                "baseline_watermark_epoch",
                "baseline_watermark_fingerprint",
            )
        )
        if baseline_present:
            baseline_state = "present"
        elif not watermark_complete:
            baseline_state = "missing_watermark"
        else:
            baseline_state = "missing_with_watermark"

        baseline_probe = probe_baseline_snapshot(getattr(session, "id"))

        # Lazy import avoids the existing baseline/score-scheduler cycle.
        from app.opening_score_scheduler import probe_terminal_recompute

        recompute_probe = probe_terminal_recompute(
            int(getattr(session, "user_id")),
            str(getattr(session, "player_color")),
            register_convergence=baseline_state == "missing_with_watermark",
        )
        convergence_probe_id = recompute_probe.convergence_probe_id
        properties: dict[str, object] = {
            "opening_baseline_state": baseline_state,
            "opening_baseline_scheduler_state": baseline_probe.state.value,
            "opening_baseline_attempts_bucket": baseline_probe.attempts_bucket,
            "opening_recompute_state": recompute_probe.state.value,
            "terminal_kind": kind.value,
            "session_age_bucket": _session_age_bucket(
                getattr(session, "started_at", None)
            ),
            "barrier_cohort": "disabled",
            "barrier_outcome": "disabled",
            "barrier_wait_budget_ms": 0,
            "barrier_wait_ms": 0,
        }
        if convergence_probe_id is not None:
            properties["convergence_probe_id"] = convergence_probe_id
        return properties
    except Exception:
        logger.exception("terminal baseline observation failed")
        properties = {
            "opening_baseline_state": baseline_state,
            "opening_baseline_scheduler_state": "probe_failed",
            "opening_baseline_attempts_bucket": "0",
            "opening_recompute_state": "probe_failed",
            "terminal_kind": kind.value,
            "session_age_bucket": "unknown",
            "barrier_cohort": "disabled",
            "barrier_outcome": "disabled",
            "barrier_wait_budget_ms": 0,
            "barrier_wait_ms": 0,
        }
        if convergence_probe_id is not None:
            properties["convergence_probe_id"] = convergence_probe_id
        return properties


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
