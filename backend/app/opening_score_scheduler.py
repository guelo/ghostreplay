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
    Reader endpoints go through ``opening_cache``. A WARM reader (a batch already
    exists) calls ``request_recompute(user_id, player_color, source=...)`` to
    schedule a coalesced BACKGROUND recompute and serves the currently-cached
    batch immediately — it never blocks. The remaining COLD BLOCKING readers
    (``load_cached_rows`` and the ``/tree`` bootstrap in ``ensure_tree_cache``)
    call ``refresh_now(user_id, player_color, source=...)`` — a keyed flush/await
    that enqueues an *immediate* recompute for that one key and waits for a
    covering, successful run to reach quiescence (bounded by ``timeout``). It runs
    only the matching key's work through the single serialized worker, so one
    user's read never triggers unrelated users' recomputes. On ``True`` the cold
    reader reloads and serves the freshly-built rows; on ``False`` (timeout /
    failure / shutdown) it serves whatever is cached (possibly empty) and lets the
    worker finish in the background.

    Since g-a5v3 the latency-sensitive live session-lineage reader
    (``load_cached_rows_nonblocking``) never blocks: a cold read WITH evidence
    issues a *guarded* normal (debounced) enqueue and reports scores as pending,
    and a cold read with NO evidence settles with no enqueue at all (the worker
    would write no batch for such a user). Its ~3s reconciliation poll does not
    re-enqueue while the key is already pending/in-flight, so polling cannot
    postpone the very compute it waits on.

    All recompute decisions (cache miss, registry drift, stale branch keys,
    evidence change, decay staleness) are consolidated in
    ``recompute_opening_scores_if_needed`` so the worker is the only reader-driven
    path that writes a batch. That function returns an explicit
    ``OpeningScoreRecomputeResult`` (``rebuilt`` / ``cached`` / ``no_evidence``)
    rather than a bare batch, so this scheduler can label its run outcome without
    inferring anything from batch presence; an exception is the fourth outcome,
    ``failed``.

Terminal delta split (g-delta-priority-lane):
    A terminal handler submits an ordinary ``SCORE_DELTA`` request here so the
    persisted whole graph eventually converges. Its freshness-bound played-chain
    result is owned by ``opening_score_delta_lane`` instead: an immediate,
    independent single-worker lane that does not wait for this scheduler's quiet
    window or in-flight recompute. This scheduler accepts no session payload and
    never publishes scoped deltas.

Timing instrumentation (g-score-queue-timing):
    Every enqueue carries a required ``source`` from the closed
    ``OpeningScoreTrigger`` vocabulary, validated at the enqueue boundary BEFORE any
    queue mutation so an unknown value can never reach a run context, log, or
    analytics event. Each pending entry keeps both ends of its coalescing burst
    (``first_seen`` / ``last_seen``), the first/last/complete-set of triggers folded
    into it, and whether its dispatch was forced (shutdown drain or explicit
    ``flush_pending``).

    ``_run_one`` samples "worker start" as its FIRST instruction — per run, after
    any earlier due key has finished — and publishes the resulting queue/debounce/
    dispatch decomposition in a ContextVar for the duration of the recompute call.
    ``opening_cache._emit_opening_scores_recomputed`` reads it through
    ``current_run_timing()`` and folds it into the existing
    ``opening_scores_recomputed`` event; a separate completion log record covers all
    four run outcomes. No event is emitted per enqueue, and no scheduler
    configuration is changed by the instrumentation.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from app.db import SessionLocal
from app.opening_cache import (
    OpeningScoreRecomputeResult,
    recompute_opening_scores_if_needed,
)

logger = logging.getLogger(__name__)

Key = tuple[int, str]

# Timing-contract version stamped on every scheduler-timed analytics event. Bump
# when the meaning of a timing field changes so a report can exclude older shapes.
SCHEDULER_TIMING_VERSION = 1


class OpeningScoreTrigger(str, Enum):
    """Closed, low-cardinality vocabulary of *who* asked for a recompute.

    Every production enqueue names itself with one of these. The set is closed and
    validated at the enqueue boundary (not merely type-hinted) because these values
    land in PostHog properties: an ad-hoc string would silently create unbounded
    analytics cardinality and break the source segmentation this instrumentation
    exists to provide.
    """

    CACHED_SCORE_READER_WARM = "cached_score_reader_warm"
    CACHED_SCORE_READER_COLD = "cached_score_reader_cold"
    SESSION_LINEAGE_WARM = "session_lineage_warm"
    SESSION_LINEAGE_COLD = "session_lineage_cold"
    TREE_READER_WARM = "tree_reader_warm"
    TREE_READER_BOOTSTRAP = "tree_reader_bootstrap"
    TREE_STATUS_BOOTSTRAP = "tree_status_bootstrap"
    SCORE_DELTA = "score_delta"
    SESSION_EVIDENCE = "session_evidence"
    SRS_REVIEW = "srs_review"
    BASELINE_RECOVERY = "baseline_recovery"


class UnknownOpeningScoreTrigger(ValueError):
    """An enqueue named a source outside the ``OpeningScoreTrigger`` vocabulary.

    Distinct from a bare ``ValueError`` so the module facade can drop the enqueue
    and log a *generic* line: ``OpeningScoreTrigger(bad)``'s own message embeds the
    rejected value, so logging it with a traceback would write the unvalidated
    string into production logs — the same uncontrolled-value leak the closed
    vocabulary exists to prevent, just one sink over.
    """


def _coerce_trigger(source: OpeningScoreTrigger | str) -> OpeningScoreTrigger:
    """Validate an enqueue source, raising without echoing the rejected value."""
    try:
        return OpeningScoreTrigger(source)
    except ValueError:
        pass
    # Raised OUTSIDE the handler so the rejecting ValueError — whose message embeds
    # the raw value — is not even attached as ``__context__``. ``raise ... from None``
    # would only suppress its *display*, leaving it reachable on the object.
    raise UnknownOpeningScoreTrigger(
        "opening score recompute source is not a known OpeningScoreTrigger"
    )


@dataclass
class _Entry:
    first_seen: float
    # Newest enqueue folded into this entry. ``first_seen`` stays immutable, so the
    # pair preserves BOTH ends of a coalesced burst: one run may represent several
    # enqueues, making "the" enqueue time ambiguous unless both are reported.
    last_seen: float
    deadline: float
    # Validated provenance. ``trigger_first``/``trigger_last`` explain burst
    # ordering; ``trigger_sources`` is the authoritative set for cohort membership
    # (a coalesced run can carry a source at neither endpoint).
    trigger_first: OpeningScoreTrigger
    trigger_last: OpeningScoreTrigger
    trigger_sources: set[OpeningScoreTrigger] = field(default_factory=set)
    enqueue_count: int = 0
    # Highest per-key enqueue sequence folded into this pending entry. A run that
    # pops this entry has "run through" this sequence; refresh_now waiters compare
    # against it to know their enqueue has been covered by a completed run.
    max_seq: int = 0
    # Sticky once an immediate (refresh_now) enqueue folds in: the entry stays due
    # now until it is popped. Later normal enqueues must not push the deadline back
    # out — otherwise a sustained burst could starve refresh_now past its timeout.
    immediate: bool = False
    # Set when a shutdown drain or an explicit flush_pending pulls this entry in
    # AHEAD of its configured deadline. Such runs stay visible operationally but are
    # excluded from steady-state queue-time distributions.
    forced_dispatch: bool = False


@dataclass(frozen=True, slots=True)
class _RunContext:
    """Per-run timing/provenance published to the recompute call via ContextVar.

    Holds only the FIXED fields (already resolved at worker start) plus the clock
    and ``worker_started``, so a later snapshot can measure ``worker_compute_ms``
    against the same monotonic source the queue fields were computed from.
    """

    run_id: str
    clock: Callable[[], float]
    worker_started: float
    queue_first_ms: float
    queue_last_ms: float
    coalesce_span_ms: float
    deadline_delay_ms: float
    dispatch_lag_ms: float
    trigger_first: OpeningScoreTrigger
    trigger_last: OpeningScoreTrigger
    trigger_sources: tuple[OpeningScoreTrigger, ...]
    enqueue_count: int
    immediate: bool
    forced_dispatch: bool
    quiet_window_ms: float
    max_wait_ms: float


# Private to this module: readers go through ``current_run_timing()``. Reset in a
# ``finally`` on every disposition and exception so one serialized run can never
# leak its timing/provenance into the next.
_run_context: ContextVar[_RunContext | None] = ContextVar(
    "opening_score_run_context", default=None
)


def current_run_timing() -> dict | None:
    """Serializable snapshot of the current scheduler run's timing, else None.

    ``None`` means "not called from a scheduler run" — a direct/offline/test
    recompute. Callers stamp that as ``scheduler_timed=False`` and MUST NOT
    fabricate queue values; production reports filter on ``scheduler_timed = true``.

    ``worker_compute_ms`` is measured AT THIS CALL, so the caller controls what it
    covers: ``opening_cache._emit_opening_scores_recomputed`` snapshots at its own
    entry, i.e. worker start through a durable batch (commit + post-commit pruning),
    excluding its own analytics query. Rounding happens here — never while the
    scheduler updates timestamps.
    """
    context = _run_context.get()
    if context is None:
        return None
    worker_compute_ms = (context.clock() - context.worker_started) * 1000.0
    return {
        "scheduler_timing_version": SCHEDULER_TIMING_VERSION,
        "scheduler_run_id": context.run_id,
        "queue_first_ms": round(context.queue_first_ms, 3),
        "queue_last_ms": round(context.queue_last_ms, 3),
        "coalesce_span_ms": round(context.coalesce_span_ms, 3),
        "deadline_delay_ms": round(context.deadline_delay_ms, 3),
        "dispatch_lag_ms": round(context.dispatch_lag_ms, 3),
        "worker_compute_ms": round(worker_compute_ms, 3),
        "trigger_first": context.trigger_first.value,
        "trigger_last": context.trigger_last.value,
        "trigger_sources": [source.value for source in context.trigger_sources],
        "enqueue_count": context.enqueue_count,
        "immediate": context.immediate,
        "forced_dispatch": context.forced_dispatch,
        "quiet_window_ms": round(context.quiet_window_ms, 3),
        "max_wait_ms": round(context.max_wait_ms, 3),
    }


@dataclass
class OpeningScoreScheduler:
    """Debounced, coalescing scheduler. Dependency-injected for testing."""

    session_factory: Callable = SessionLocal
    recompute: Callable = staticmethod(recompute_opening_scores_if_needed)
    fill_baselines: Callable | None = None
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
        if self.fill_baselines is None:
            session_factory = self.session_factory

            def fill_baselines(batch_id: int) -> int:
                return _default_fill_opening_baselines_for_batch(
                    batch_id,
                    session_factory=session_factory,
                )

            self.fill_baselines = fill_baselines

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------
    def _enqueue_locked(
        self,
        key: Key,
        *,
        immediate: bool,
        source: OpeningScoreTrigger,
    ) -> int:
        """Coalesce an enqueue for ``key``. Caller must hold ``_cond``.

        Returns the per-key sequence assigned to this enqueue. ``immediate``
        makes the entry due now (used by ``refresh_now``); otherwise the normal
        debounce window applies.

        ``source`` must ALREADY be a validated ``OpeningScoreTrigger`` — conversion
        happens at the public boundary, before any queue state is touched, so a bad
        value cannot leave a half-mutated queue behind.
        """
        now = self.clock()
        seq = self._seq_counter.get(key, 0) + 1
        self._seq_counter[key] = seq
        entry = self._pending.get(key)
        if entry is None:
            deadline = now if immediate else min(now + self.quiet_window, now + self.max_wait)
            entry = _Entry(
                first_seen=now,
                last_seen=now,
                deadline=deadline,
                trigger_first=source,
                trigger_last=source,
                trigger_sources={source},
                max_seq=seq,
                immediate=immediate,
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
            # first_seen is immutable (oldest request folded into the burst);
            # last_seen / trigger_last advance with every enqueue.
            entry.last_seen = now
            entry.trigger_last = source
            entry.trigger_sources.add(source)
        entry.enqueue_count += 1
        self._cond.notify_all()
        return seq

    def request_recompute(
        self,
        user_id: int,
        player_color: str,
        *,
        source: OpeningScoreTrigger | str,
    ) -> None:
        """Coalesce a recompute request for ``(user_id, player_color)``.

        ``source`` names the caller from the closed ``OpeningScoreTrigger``
        vocabulary and is validated FIRST: an unknown value raises with the queue
        untouched (the module-level facade swallows it under its existing
        best-effort boundary), so it can never reach a run context or an event.

        Best-effort: a thread-start failure is swallowed and logged so it can
        never propagate into the ``/moves`` or SRS request handlers.
        """
        trigger = _coerce_trigger(source)
        key: Key = (user_id, player_color)
        with self._cond:
            if self._shutdown:
                return
            self._enqueue_locked(
                key,
                immediate=False,
                source=trigger,
            )
        if self.auto_start:
            try:
                self.start()
            except Exception:
                logger.exception("opening score scheduler start failed; recompute will not run")

    def refresh_now(
        self,
        user_id: int,
        player_color: str,
        timeout: float = 5.0,
        *,
        source: OpeningScoreTrigger | str,
    ) -> bool:
        """Enqueue an immediate keyed recompute and await its quiescent success.

        Returns ``True`` only when a run that covers this enqueue's sequence
        completed **successfully** — any of the three normal dispositions
        (``rebuilt``, ``cached``, ``no_evidence``) counts as covering — and the key
        has no pending or in-flight work remaining. Returns ``False`` on a
        covering-run failure (including a recompute-contract violation),
        worker-start failure, scheduler shutdown, or ``timeout`` — in which case
        the caller should serve the current cached batch and let the worker finish
        in the background.

        ``source`` is validated before the enqueue, as in ``request_recompute``.
        """
        trigger = _coerce_trigger(source)
        key: Key = (user_id, player_color)
        deadline = self.clock() + timeout
        with self._cond:
            if self._shutdown:
                return False
            seq = self._enqueue_locked(key, immediate=True, source=trigger)
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

    def is_inflight(self, user_id: int, player_color: str) -> bool:
        """Non-mutating: is a recompute for this key RUNNING right now?

        Narrower than ``is_scheduled`` (which also counts a pending/debounced
        entry). The one-shot baseline snapshot gates on this: only a RUNNING
        recompute makes the O(evidence) digest serialize against the worker (the
        9.6s GIL-contention case); a merely-pending entry is idle. Guarded by the
        same lock the worker mutates ``_inflight`` under, so it observes a
        consistent set.
        """
        key: Key = (user_id, player_color)
        with self._lock:
            return key in self._inflight

    # ------------------------------------------------------------------
    # Synchronous test surface
    # ------------------------------------------------------------------
    def run_due(self, now: float | None = None) -> None:
        """Run all keys whose deadline has passed. Runs on the caller's thread.

        Pops due entries and marks them in-flight under the lock, then scores them
        OUTSIDE it, one at a time. The whole ``_Entry`` is carried into ``_run_one``
        (not reduced to counts) so each run can time itself against its own
        coalescing window — and so the second and later keys of one due batch record
        the head-of-line wait behind the first, which a pop-time timestamp would
        erase.
        """
        # ``now=inf`` is the shutdown-drain sentinel: run everything regardless of
        # deadline. Mark the pull-ins here rather than once before the drain starts, so
        # a follow-up enqueued BY a recompute during the drain is also attributed
        # correctly instead of looking like a steady-state debounce observation.
        forced_drain = now == float("inf")
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
                if forced_drain:
                    self._mark_forced_dispatch_locked(self.clock())
                runs: list[tuple[Key, _Entry]] = []
                for key in due:
                    entry = self._pending.pop(key)
                    self._inflight.add(key)
                    runs.append((key, entry))
            for key, entry in runs:
                self._run_one(key, entry)

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
                flush_now = self.clock()
                self._mark_forced_dispatch_locked(flush_now)
                for entry in self._pending.values():
                    entry.deadline = min(entry.deadline, flush_now)
                self._cond.notify_all()
                self._cond.wait(timeout=min(remaining, 0.1))

    def _mark_forced_dispatch_locked(self, now: float) -> None:
        """Flag pending entries being pulled in AHEAD of their deadline.

        Caller must hold the scheduler lock. Shared by the shutdown drain
        (``run_due(now=inf)``) and ``flush_pending`` — the two paths that run work
        before it is due. Entries already past their deadline at ``now`` were going to
        run anyway, so they stay unforced and remain valid steady-state queue-time
        observations.
        """
        for entry in self._pending.values():
            if entry.deadline > now:
                entry.forced_dispatch = True

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
                # A shutdown notification can land while run_due executes outside
                # this lock. Once the latch is visible, never begin another deadline
                # wait before draining the remaining entries.
                shutting_down = self._shutdown
                if not shutting_down:
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
            # ``inf`` drains every pending entry regardless of deadline; run_due marks
            # the ones it pulls in early as forced dispatches.
            self.run_due(now=float("inf") if shutting_down else None)

    # ------------------------------------------------------------------
    # Run a single recompute
    # ------------------------------------------------------------------
    def _build_run_context(self, entry: _Entry, worker_started: float) -> _RunContext:
        """Fixed queue/debounce/dispatch decomposition for one run.

        ``entry.deadline`` here is the FINAL deadline the entry carried when it was
        popped — after every debounce extension, max-wait cap, immediate override and
        forced-dispatch pull-in. Splitting queue time into policy delay
        (``deadline_delay_ms``, intentional) and post-eligibility lag
        (``dispatch_lag_ms``, principally single-worker head-of-line blocking) is
        what makes the measurement actionable: shortening the quiet window cannot fix
        dispatch lag, and adding worker capacity cannot fix policy delay.
        """
        return _RunContext(
            run_id=uuid.uuid4().hex,
            clock=self.clock,
            worker_started=worker_started,
            queue_first_ms=(worker_started - entry.first_seen) * 1000.0,
            queue_last_ms=(worker_started - entry.last_seen) * 1000.0,
            coalesce_span_ms=(entry.last_seen - entry.first_seen) * 1000.0,
            deadline_delay_ms=(entry.deadline - entry.first_seen) * 1000.0,
            dispatch_lag_ms=max(0.0, (worker_started - entry.deadline) * 1000.0),
            trigger_first=entry.trigger_first,
            trigger_last=entry.trigger_last,
            trigger_sources=tuple(
                sorted(entry.trigger_sources, key=lambda source: source.value)
            ),
            enqueue_count=entry.enqueue_count,
            immediate=entry.immediate,
            forced_dispatch=entry.forced_dispatch,
            quiet_window_ms=self.quiet_window * 1000.0,
            max_wait_ms=self.max_wait * 1000.0,
        )

    def _log_run_completion(
        self,
        context: _RunContext,
        *,
        run_outcome: str,
        rebuild_reason: str | None,
        worker_run_ms: float | None,
        generation: int | None,
    ) -> None:
        """One completion record per executed run, for ALL four outcomes.

        The semantic fields go in the MESSAGE, not in ``extra``: the root formatter is
        ``%(asctime)s %(levelname)s %(message)s`` (``app.logging_config``), so
        ``extra`` kwargs are silently dropped — the regression this replaces.

        Deliberately carries no user ID, opening key, session ID, position, or score:
        this is an operational/aggregate surface, and those are either
        high-cardinality or user-derived. Best-effort — a logging failure must not
        stop later due keys from running.
        """
        try:
            logger.info(
                "opening_score_recompute_run run_id=%s run_outcome=%s rebuild_reason=%s "
                "queue_first_ms=%s queue_last_ms=%s coalesce_span_ms=%s "
                "deadline_delay_ms=%s dispatch_lag_ms=%s worker_run_ms=%s "
                "trigger_first=%s trigger_last=%s trigger_sources=%s "
                "enqueue_count=%s immediate=%s forced_dispatch=%s generation=%s",
                context.run_id,
                run_outcome,
                rebuild_reason,
                round(context.queue_first_ms, 3),
                round(context.queue_last_ms, 3),
                round(context.coalesce_span_ms, 3),
                round(context.deadline_delay_ms, 3),
                round(context.dispatch_lag_ms, 3),
                None if worker_run_ms is None else round(worker_run_ms, 3),
                context.trigger_first.value,
                context.trigger_last.value,
                ",".join(source.value for source in context.trigger_sources),
                context.enqueue_count,
                context.immediate,
                context.forced_dispatch,
                generation,
            )
        except Exception:
            logger.exception("opening score recompute completion log failed")

    def _run_one(self, key: Key, entry: _Entry) -> None:
        # FIRST instruction: this is the real dispatch boundary. Sampling here — per
        # run, after any earlier due key of the same batch has finished — is what
        # makes dispatch_lag_ms include head-of-line waiting. run_due's pop time
        # would report zero lag for every key.
        worker_started = self.clock()
        context = self._build_run_context(entry, worker_started)
        user_id, player_color = key
        ran_seq = entry.max_seq
        db = None
        ok = False
        run_outcome = "failed"
        rebuild_reason: str | None = None
        generation: int | None = None
        worker_run_ms: float | None = None
        push_fill_batch_id: int | None = None
        token = None
        try:
            token = _run_context.set(context)
            db = self.session_factory()
            result = self.recompute(db, user_id, player_color)
            # Sample the operational duration immediately, before session close and
            # logging, so teardown never inflates it.
            worker_run_ms = (self.clock() - worker_started) * 1000.0
            if isinstance(result, OpeningScoreRecomputeResult):
                run_outcome = result.disposition.value
                rebuild_reason = result.reason
                generation = getattr(result.batch, "generation", None)
                batch_id = getattr(result.batch, "id", None)
                if batch_id is not None:
                    push_fill_batch_id = int(batch_id)
                ok = True
            else:
                # A bare batch / None is the pre-contract shape and cannot be
                # labelled: treat it as a failure rather than re-deriving the outcome
                # from batch presence (the inference this contract removed).
                logger.error(
                    "opening score recompute returned a non-contract result type=%s",
                    type(result).__name__,
                )

            # The recompute's commit is the durability boundary for push-fill.
            # Close its session before opening the independent best-effort fill
            # transaction, and keep the optional side effect out of recompute timing.
            if db is not None:
                try:
                    db.close()
                except Exception:
                    logger.exception(
                        "opening score scheduler failed to close recompute session"
                    )
                db = None
            if token is not None:
                _run_context.reset(token)
                token = None
            if push_fill_batch_id is not None:
                try:
                    assert self.fill_baselines is not None
                    self.fill_baselines(push_fill_batch_id)
                except Exception:
                    logger.exception(
                        "opening baseline push-fill failed after durable recompute",
                        extra={"batch_id": push_fill_batch_id},
                    )
        except Exception:
            worker_run_ms = (self.clock() - worker_started) * 1000.0
            logger.exception(
                "opening score recompute failed",
                extra={"user_id": user_id, "player_color": player_color},
            )
        finally:
            if token is not None:
                # Always reset: one serialized run must never leak its timing or
                # provenance into the next (or into a direct recompute on this thread).
                _run_context.reset(token)
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
        self._log_run_completion(
            context,
            run_outcome=run_outcome,
            rebuild_reason=rebuild_reason,
            worker_run_ms=worker_run_ms,
            generation=generation,
        )


# Module-level singleton + thin facade -------------------------------------
def _default_fill_opening_baselines_for_batch(
    batch_id: int,
    *,
    session_factory: Callable,
) -> int:
    # Function-local import avoids opening_score_delta -> scheduler import cycles.
    from app.opening_score_delta import fill_opening_baselines_for_batch

    return fill_opening_baselines_for_batch(
        batch_id,
        session_factory=session_factory,
    )


_scheduler = OpeningScoreScheduler()


def get_scheduler() -> OpeningScoreScheduler:
    return _scheduler


def request_recompute(
    user_id: int,
    player_color: str,
    *,
    source: OpeningScoreTrigger | str,
) -> None:
    """Schedule a coalesced opening-score recompute (best-effort).

    Fully swallowing: any scheduler enqueue/start failure is logged and never
    propagates into the ``/moves`` or SRS request handlers (an enqueue failure
    must not regress those endpoints from 200 to 500). An invalid ``source`` raises
    inside the class method with the queue untouched, so it is dropped here — the
    enqueue is lost, but no unvalidated value can reach a run context, an event,
    or (via a rendered traceback) a log line.
    """
    try:
        _scheduler.request_recompute(
            user_id,
            player_color,
            source=source,
        )
    except UnknownOpeningScoreTrigger:
        logger.error(
            "opening score recompute enqueue dropped: unknown trigger source "
            "(rejected value withheld)"
        )
    except Exception:
        logger.exception(
            "opening score recompute enqueue failed",
            extra={"user_id": user_id, "player_color": player_color},
        )


def refresh_now(
    user_id: int,
    player_color: str,
    timeout: float = 5.0,
    *,
    source: OpeningScoreTrigger | str,
) -> bool:
    """Reader-facing keyed flush/await (best-effort).

    Returns ``True`` only when a covering recompute completed successfully and the
    key is quiescent; ``False`` on failure/timeout/shutdown (or any scheduler
    error, including an invalid ``source``). The caller serves the current cached
    batch on ``False``.
    """
    try:
        return _scheduler.refresh_now(
            user_id, player_color, timeout=timeout, source=source
        )
    except UnknownOpeningScoreTrigger:
        logger.error(
            "opening score refresh_now dropped: unknown trigger source "
            "(rejected value withheld)"
        )
        return False
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


def is_recompute_inflight(user_id: int, player_color: str) -> bool:
    """Non-mutating probe: is a recompute for this key RUNNING right now?

    Narrower than ``is_recompute_scheduled`` (pending OR in-flight). The one-shot
    baseline snapshot gates the O(evidence) digest on this: only a RUNNING
    recompute causes the GIL-contention pathology. Best-effort (any scheduler
    error is logged and reported as "not in-flight") so the start hot path can
    never 500 on a scheduler fault.
    """
    try:
        return _scheduler.is_inflight(user_id, player_color)
    except Exception:
        logger.exception(
            "opening score is_inflight probe failed",
            extra={"user_id": user_id, "player_color": player_color},
        )
        return False
