"""In-process debounced scheduler for /moves graph + SRS evidence side effects.

POST ``/api/session/<id>/moves`` durably commits the ``SessionMove`` rows and
then returns. The EXPENSIVE accounting that used to run inline on the request
path — the per-user advisory lock + ghost-graph upsert + blunder-opportunity
recompute + commit, the analysis-cache write, and the opening-score recompute
enqueue — is deferred here so it no longer blocks the move-upload response or
holds the per-user advisory lock during user-visible latency.

This mirrors :mod:`app.opening_score_scheduler`: a single daemon worker thread,
in-memory coalescing, drain-on-graceful-shutdown. It is NOT a durable DB outbox.

IMPORTANT — single-process assumption (same as the opening-score scheduler):
    Pending state lives in memory and runs on one daemon thread, so coalescing is
    per-process. The deployment configs start ``uvicorn app.main:app`` with one
    worker and one replica, both load-bearing. The single worker also serializes
    ALL evidence runs (even across sessions/users), so in single-process prod the
    per-user advisory lock is effectively uncontended; it still guards the
    ``(user_id, fen_hash)`` unique index against any cross-process writer.

Accepted durability risk:
    A hard kill (SIGKILL/OOM/deploy-kill) between enqueue and worker completion
    drops that session's deferred accounting. Backstops: (1) blunder-opportunity
    events regenerate (a full recompute from all committed ``session_moves``) on
    the next OPPORTUNITY-ENABLED upload — i.e. one with ``run_opportunity=True``;
    after g-y90g mid-game incremental uploads explicitly skip opportunity, and the
    final ``run_opportunity=True`` upload may be the LAST one for the session, so
    this is not guaranteed to fire — falling back to (2) the offline
    ``scripts/recompute_srs_opportunities.py`` recompute; (3) ``drain=True`` on
    graceful shutdown. This is a narrow, explicitly-accepted regression vs the old
    synchronous commit, traded for the latency win.

Coalescing key is ``session_id`` (a session has exactly one user_id +
player_color). Within a coalesced entry, moves are deduped by
``(move_number, color)`` with LAST-WRITE-WINS — matching the
``session_moves`` upsert key, so the worker processes exactly one payload per
committed move slot and the entry stays bounded by SESSION SIZE rather than by
upload count. This collapses the end-of-session burst (the incremental
fire-and-forget uploader plus the final full-history upload re-sending the same
slots) into ONE worker run carrying one payload per slot.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from app.db import SessionLocal

logger = logging.getLogger(__name__)

Key = uuid.UUID


@dataclass
class _Entry:
    user_id: int
    player_color: str
    # move slot -> latest payload. Keyed by (move_number, color) so a re-sent slot
    # (incremental upload followed by the full-history upload) overwrites rather
    # than duplicating — last write wins, matching the session_moves upsert.
    moves: dict[tuple[int, str], object]
    first_seen: float
    deadline: float
    # OR-folded across coalesced enqueues (g-y90g): once ANY upload for this
    # session requests the opportunity recompute (the final, complete upload),
    # the single coalesced worker run computes it. A burst of mid-game
    # incremental uploads (all False) folds to False → zero recomputes.
    run_opportunity: bool = True
    # OR-folded session-finality bit, derived from the client-sent terminal_action
    # (g-mk1d code review). SEPARATE from run_opportunity on purpose: the revert
    # upload also sends recompute_opportunity=True, and any client predating
    # g-y90g defaults it True on every mid-game upload, so run_opportunity marks
    # "this run recomputes opportunity", NOT "this session is over". Only
    # terminal_action's PRESENCE identifies the end-of-session final_full upload
    # (see the SessionMovesRequest.terminal_action note in app/api/session.py).
    is_final: bool = False
    enqueue_count: int = 0


@dataclass
class SessionEvidenceScheduler:
    """Debounced, coalescing scheduler for /moves evidence. DI for testing."""

    session_factory: Callable = SessionLocal
    run_side_effects: Callable = None  # set in __post_init__ to break import cycle
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
        if self.run_side_effects is None:
            self.run_side_effects = _default_run_side_effects

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------
    def _enqueue_locked(
        self,
        session_id: Key,
        user_id: int,
        player_color: str,
        moves: list,
        run_opportunity: bool,
        is_final: bool,
    ) -> None:
        """Coalesce an enqueue for ``session_id``. Caller must hold ``_cond``.

        New key: create a pending entry with the move payload. Existing key: fold
        the moves in last-write-wins per ``(move_number, color)`` slot and refresh
        the debounce deadline (capped by ``max_wait`` from first_seen).

        ``run_opportunity`` OR-folds into the entry (g-y90g): an existing entry
        becomes True if ANY enqueue (this one or a prior) requested it, so the
        incremental(False) + final(True) burst collapses to exactly one recompute.

        ``is_final`` OR-folds the same way and for the same reason: the coalesced
        run covers the whole burst, so it IS the session's final run once the
        final_full upload has joined it.
        """
        now = self.clock()
        entry = self._pending.get(session_id)
        if entry is None:
            entry = _Entry(
                user_id=user_id,
                player_color=player_color,
                moves={(m.move_number, m.color): m for m in moves},
                first_seen=now,
                deadline=min(now + self.quiet_window, now + self.max_wait),
                run_opportunity=run_opportunity,
                is_final=is_final,
            )
            self._pending[session_id] = entry
        else:
            for m in moves:
                entry.moves[(m.move_number, m.color)] = m
            entry.deadline = min(
                now + self.quiet_window, entry.first_seen + self.max_wait
            )
            entry.run_opportunity = entry.run_opportunity or run_opportunity
            entry.is_final = entry.is_final or is_final
        entry.enqueue_count += 1
        self._cond.notify_all()

    def enqueue(
        self,
        session_id: Key,
        user_id: int,
        player_color: str,
        moves: list,
        run_opportunity: bool = True,
        is_final: bool = False,
    ) -> None:
        """Coalesce an evidence run for ``session_id``.

        Best-effort: a thread-start failure is swallowed and logged so it can
        never propagate into the ``/moves`` handler.
        """
        with self._cond:
            if self._shutdown:
                return
            self._enqueue_locked(
                session_id, user_id, player_color, moves, run_opportunity, is_final
            )
        if self.auto_start:
            try:
                self.start()
            except Exception:
                logger.exception(
                    "session evidence scheduler start failed; side effects will not run"
                )

    # ------------------------------------------------------------------
    # Synchronous test surface
    # ------------------------------------------------------------------
    def run_due(self, now: float | None = None) -> None:
        """Run all sessions whose deadline has passed. Runs on the caller's thread."""
        if now is None:
            now = self.clock()
        while True:
            with self._lock:
                due = [
                    session_id
                    for session_id, entry in self._pending.items()
                    if entry.deadline <= now and session_id not in self._inflight
                ]
                if not due:
                    return
                runs: list[tuple[Key, _Entry]] = []
                for session_id in due:
                    entry = self._pending.pop(session_id)
                    self._inflight.add(session_id)
                    runs.append((session_id, entry))
            for session_id, entry in runs:
                self._run_one(session_id, entry)

    def flush_pending(self, timeout: float = 30.0) -> None:
        """Block until both ``_pending`` and ``_inflight`` are empty.

        Requests an immediate worker-thread drain, then waits on the condition.
        Side effects never run on this caller's thread, so ``timeout`` remains
        enforceable even when a run hangs. Raises ``TimeoutError`` if the system
        has not gone quiescent within the bound.
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
                # Make only entries that exist now immediately due. Later enqueues
                # keep their normal debounce deadlines even if this flush times out
                # while a worker run is still in flight.
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
                name="session-evidence-scheduler",
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

        Draining remains on the worker thread so a hung run cannot wedge the
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
                    "session evidence scheduler did not stop before timeout"
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
                    for session_id, entry in self._pending.items()
                    if session_id not in self._inflight
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
    # Run a single session's evidence side effects
    # ------------------------------------------------------------------
    def _run_one(self, session_id: Key, entry: _Entry) -> None:
        db = None
        moves = list(entry.moves.values())
        try:
            db = self.session_factory()
            self.run_side_effects(
                db,
                session_id=session_id,
                user_id=entry.user_id,
                player_color=entry.player_color,
                evidence_moves=moves,
                move_count=len(moves),
                dialect_name=db.bind.dialect.name,
                run_opportunity=entry.run_opportunity,
                is_final=entry.is_final,
            )
        except Exception:
            logger.exception(
                "session evidence side effects failed",
                extra={
                    "session_id": str(session_id),
                    "user_id": entry.user_id,
                    "player_color": entry.player_color,
                    "move_count": len(moves),
                },
            )
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    logger.exception(
                        "session evidence scheduler failed to close session"
                    )
            with self._cond:
                self._inflight.discard(session_id)
                self._cond.notify_all()


# Module-level singleton + thin facade -------------------------------------
def _default_run_side_effects(db, **kwargs) -> None:
    """Run the real /moves evidence side effects on ``db``.

    The function-local import breaks the import cycle: ``app.api.session``
    imports ``enqueue_session_evidence`` from this module at top, so this module
    must NOT import ``app.api.session`` at top.
    """
    from app.api.session import _run_session_move_evidence_side_effects

    _run_session_move_evidence_side_effects(db, **kwargs)


_scheduler = SessionEvidenceScheduler()


def get_evidence_scheduler() -> SessionEvidenceScheduler:
    return _scheduler


def enqueue_session_evidence(
    db,
    *,
    session_id: Key,
    user_id: int,
    player_color: str,
    evidence_moves: list,
    move_count: int,
    recompute_opportunity: bool = True,
    is_final: bool = False,
) -> None:
    """Schedule the deferred /moves evidence side effects (best-effort).

    Fully swallowing: any scheduler enqueue/start failure is logged and never
    propagates into the ``/moves`` handler (an enqueue failure must not regress
    it from 200 to 500 — same contract as ``request_recompute``).

    ``db`` is accepted but NOT used in production (the worker opens its own
    session); it exists so the synchronous test shim can run the side effects on
    the request's session for both the SQLite and Postgres-override test paths.
    ``move_count`` is accepted for logging parity (``len(evidence_moves)`` also
    works). ``recompute_opportunity`` (g-y90g) OR-folds into the coalesced entry:
    False from mid-game incremental uploads, True from the final/complete upload,
    so the burst collapses to exactly one opportunity recompute.

    ``is_final`` is the caller's ``terminal_action is not None`` — the ONLY reliable
    end-of-session signal — and OR-folds independently of ``recompute_opportunity``.
    """
    try:
        _scheduler.enqueue(
            session_id,
            user_id,
            player_color,
            evidence_moves,
            run_opportunity=recompute_opportunity,
            is_final=is_final,
        )
    except Exception:
        logger.exception(
            "session evidence enqueue failed",
            extra={
                "session_id": str(session_id),
                "user_id": user_id,
                "player_color": player_color,
                "move_count": move_count,
            },
        )
