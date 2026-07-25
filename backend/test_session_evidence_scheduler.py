"""Tests for the in-process debounced /moves evidence side-effect scheduler.

Unit tests inject a fake clock + session factory and a recording
``run_side_effects``, driving the scheduler synchronously (``run_due`` /
``flush_pending``) so coalescing/dedup is deterministic with no real sleeps. One
test exercises the real worker thread, signalled via ``threading.Event``.
"""

from __future__ import annotations

import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, call

import anyio
import pytest

from app import main
from app import session_evidence_scheduler as evidence_mod
from app.session_evidence_scheduler import (
    SessionEvidenceScheduler,
    enqueue_session_evidence,
)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False
        # The scheduler reads db.bind.dialect.name to pass dialect_name through.
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def close(self) -> None:
        self.closed = True


class _RecordingSideEffects:
    """Records each call's kwargs and the session it received."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.sessions: list[object] = []

    def __call__(self, db, **kwargs):
        self.sessions.append(db)
        self.calls.append(kwargs)
        return None


def _move(move_number: int, color: str, **extra):
    return SimpleNamespace(move_number=move_number, color=color, **extra)


def _make_scheduler(clock, run_side_effects, **kwargs):
    sessions: list[_FakeSession] = []

    def factory():
        s = _FakeSession()
        sessions.append(s)
        return s

    params = {"quiet_window": 1.5, "max_wait": 10.0, "auto_start": False}
    params.update(kwargs)
    sched = SessionEvidenceScheduler(
        session_factory=factory,
        run_side_effects=run_side_effects,
        clock=clock,
        **params,
    )
    return sched, sessions


def test_single_enqueue_runs_side_effects_once():
    clock = _FakeClock()
    run = _RecordingSideEffects()
    sched, sessions = _make_scheduler(clock, run)
    sid = uuid.uuid4()

    sched.enqueue(sid, 7, "white", [_move(1, "white"), _move(1, "black")])

    # Not due yet.
    sched.run_due()
    assert run.calls == []

    clock.advance(2.0)
    sched.run_due()

    assert len(run.calls) == 1
    call = run.calls[0]
    assert call["session_id"] == sid
    assert call["user_id"] == 7
    assert call["player_color"] == "white"
    assert call["move_count"] == 2
    assert call["dialect_name"] == "sqlite"
    assert {(m.move_number, m.color) for m in call["evidence_moves"]} == {
        (1, "white"),
        (1, "black"),
    }
    # The run got a freshly-created session and it was closed.
    assert len(sessions) == 1
    assert sessions[0].closed is True


def test_coalesces_two_enqueues_into_single_run():
    clock = _FakeClock()
    run = _RecordingSideEffects()
    sched, _ = _make_scheduler(clock, run)
    sid = uuid.uuid4()

    sched.enqueue(sid, 7, "white", [_move(1, "white")])
    clock.advance(0.1)  # within the quiet window
    sched.enqueue(sid, 7, "white", [_move(2, "white")])

    clock.advance(2.0)
    sched.run_due()

    # One coalesced run carrying both distinct slots.
    assert len(run.calls) == 1
    slots = {(m.move_number, m.color) for m in run.calls[0]["evidence_moves"]}
    assert slots == {(1, "white"), (2, "white")}


def test_dedup_last_write_wins_per_slot():
    # The end-of-session burst re-sends the same move slot from the incremental
    # uploader and then the full-history upload. The entry must carry the slot
    # exactly once, with the LATER payload.
    clock = _FakeClock()
    run = _RecordingSideEffects()
    sched, _ = _make_scheduler(clock, run)
    sid = uuid.uuid4()

    # "incremental" upload of slot (5, white).
    sched.enqueue(sid, 7, "white", [_move(5, "white", eval_cp=10)])
    clock.advance(0.1)
    # "full-history" upload re-including (5, white) with different eval data,
    # plus another slot.
    sched.enqueue(
        sid,
        7,
        "white",
        [_move(5, "white", eval_cp=20), _move(6, "white", eval_cp=30)],
    )

    clock.advance(2.0)
    sched.run_due()

    assert len(run.calls) == 1
    moves = run.calls[0]["evidence_moves"]
    # Entry bounded by distinct slot count, not enqueue count.
    assert len(moves) == 2
    by_slot = {(m.move_number, m.color): m for m in moves}
    assert set(by_slot) == {(5, "white"), (6, "white")}
    # Last write wins for the overlapping slot.
    assert by_slot[(5, "white")].eval_cp == 20


def test_final_synthetic_provenance_survives_last_write_wins_coalescing():
    clock = _FakeClock()
    run = _RecordingSideEffects()
    sched, _ = _make_scheduler(clock, run)
    sid = uuid.uuid4()

    sched.enqueue(
        sid,
        7,
        "black",
        [_move(2, "black", eval_cp=None, synthetic_terminal_eval=False)],
    )
    clock.advance(0.1)
    sched.enqueue(
        sid,
        7,
        "black",
        [_move(2, "black", eval_cp=10000, synthetic_terminal_eval=True)],
    )

    clock.advance(2.0)
    sched.run_due()

    assert len(run.calls) == 1
    moves = run.calls[0]["evidence_moves"]
    assert len(moves) == 1
    assert moves[0].eval_cp == 10000
    assert moves[0].synthetic_terminal_eval is True


def test_per_row_provenance_rides_the_slot_through_coalescing():
    # Provenance is a field of the move payload, NOT of the request, precisely so
    # last-write-wins per slot keeps every surviving slot bound to its OWN
    # provenance — with no new coalescing semantics and no scheduler change
    # (g-mk1d §2/§4). A request-level value would have no defined merge rule and
    # could stamp one upload's claim onto another upload's numbers.
    clock = _FakeClock()
    run = _RecordingSideEffects()
    sched, _ = _make_scheduler(clock, run)
    sid = uuid.uuid4()

    sched.enqueue(
        sid, 7, "white", [_move(5, "white", eval_cp=10, provenance={"depth": 17})]
    )
    clock.advance(0.1)
    sched.enqueue(
        sid,
        7,
        "white",
        [
            _move(5, "white", eval_cp=20, provenance={"depth": 20}),
            _move(6, "white", eval_cp=30, provenance=None),
        ],
    )

    clock.advance(2.0)
    sched.run_due()

    moves = run.calls[0]["evidence_moves"]
    by_slot = {(m.move_number, m.color): m for m in moves}
    # The surviving payload carries the LATER upload's provenance, matched to the
    # later upload's eval — the two can never come from different requests.
    assert by_slot[(5, "white")].eval_cp == 20
    assert by_slot[(5, "white")].provenance == {"depth": 20}
    # A provenance-less slot in the same burst stays provenance-less.
    assert by_slot[(6, "white")].provenance is None


def test_run_opportunity_default_true_forwarded():
    # A single enqueue with no flag defaults to True and forwards it into the run.
    clock = _FakeClock()
    run = _RecordingSideEffects()
    sched, _ = _make_scheduler(clock, run)
    sid = uuid.uuid4()

    sched.enqueue(sid, 7, "white", [_move(1, "white")])
    clock.advance(2.0)
    sched.run_due()

    assert len(run.calls) == 1
    assert run.calls[0]["run_opportunity"] is True


def test_run_opportunity_or_folds_false_then_true():
    # The burst-collapse case: mid-game incremental (False) followed by the final
    # full upload (True) coalesces into ONE run that DOES recompute opportunity.
    clock = _FakeClock()
    run = _RecordingSideEffects()
    sched, _ = _make_scheduler(clock, run)
    sid = uuid.uuid4()

    sched.enqueue(sid, 7, "white", [_move(1, "white")], run_opportunity=False)
    clock.advance(0.1)
    sched.enqueue(sid, 7, "white", [_move(2, "white")], run_opportunity=True)

    clock.advance(2.0)
    sched.run_due()

    assert len(run.calls) == 1
    assert run.calls[0]["run_opportunity"] is True


def test_run_opportunity_or_folds_true_then_false():
    # OR-fold is order-independent: once any enqueue requested it, a later False
    # cannot clear it (a stray in-flight incremental landing after the final one).
    clock = _FakeClock()
    run = _RecordingSideEffects()
    sched, _ = _make_scheduler(clock, run)
    sid = uuid.uuid4()

    sched.enqueue(sid, 7, "white", [_move(1, "white")], run_opportunity=True)
    clock.advance(0.1)
    sched.enqueue(sid, 7, "white", [_move(2, "white")], run_opportunity=False)

    clock.advance(2.0)
    sched.run_due()

    assert len(run.calls) == 1
    assert run.calls[0]["run_opportunity"] is True


def test_run_opportunity_all_false_burst_stays_false():
    # A pure mid-game burst (no final upload yet) never recomputes opportunity.
    clock = _FakeClock()
    run = _RecordingSideEffects()
    sched, _ = _make_scheduler(clock, run)
    sid = uuid.uuid4()

    sched.enqueue(sid, 7, "white", [_move(1, "white")], run_opportunity=False)
    clock.advance(0.1)
    sched.enqueue(sid, 7, "white", [_move(2, "white")], run_opportunity=False)

    clock.advance(2.0)
    sched.run_due()

    assert len(run.calls) == 1
    assert run.calls[0]["run_opportunity"] is False


def test_facade_forwards_recompute_opportunity_false(monkeypatch):
    # enqueue_session_evidence maps recompute_opportunity onto the scheduler's
    # run_opportunity kwarg.
    captured: dict = {}

    def fake_enqueue(
        session_id, user_id, player_color, moves, run_opportunity=True, is_final=False
    ):
        captured["run_opportunity"] = run_opportunity
        captured["is_final"] = is_final

    monkeypatch.setattr(evidence_mod._scheduler, "enqueue", fake_enqueue)
    enqueue_session_evidence(
        object(),
        session_id=uuid.uuid4(),
        user_id=1,
        player_color="white",
        evidence_moves=[_move(1, "white")],
        move_count=1,
        recompute_opportunity=False,
    )
    assert captured["run_opportunity"] is False


def test_payload_cleared_after_run():
    clock = _FakeClock()
    run = _RecordingSideEffects()
    sched, _ = _make_scheduler(clock, run)
    sid = uuid.uuid4()

    sched.enqueue(sid, 7, "white", [_move(1, "white", eval_cp=1)])
    clock.advance(2.0)
    sched.run_due()

    # Re-enqueue starts a fresh entry — the prior payload is gone.
    sched.enqueue(sid, 7, "white", [_move(9, "white", eval_cp=9)])
    clock.advance(2.0)
    sched.run_due()

    assert len(run.calls) == 2
    second = {(m.move_number, m.color) for m in run.calls[1]["evidence_moves"]}
    assert second == {(9, "white")}


def test_session_closed_even_when_side_effects_raise():
    clock = _FakeClock()

    def boom(db, **kwargs):
        raise RuntimeError("side effects failed")

    sched, sessions = _make_scheduler(clock, boom)
    sid = uuid.uuid4()

    sched.enqueue(sid, 7, "white", [_move(1, "white")])
    clock.advance(2.0)
    # The failure is swallowed (no raise) and the session is still closed.
    sched.run_due()

    assert len(sessions) == 1
    assert sessions[0].closed is True
    # The scheduler is not wedged: a later session still runs.
    sid2 = uuid.uuid4()
    sched.enqueue(sid2, 8, "black", [_move(1, "white")])
    clock.advance(2.0)
    sched.run_due()
    assert len(sessions) == 2


def test_distinct_sessions_each_run_with_own_session():
    clock = _FakeClock()
    run = _RecordingSideEffects()
    sched, sessions = _make_scheduler(clock, run)
    sid1, sid2 = uuid.uuid4(), uuid.uuid4()

    sched.enqueue(sid1, 1, "white", [_move(1, "white")])
    sched.enqueue(sid2, 2, "black", [_move(1, "white")])
    clock.advance(2.0)
    sched.run_due()

    assert sorted(c["session_id"] for c in run.calls) == sorted([sid1, sid2])
    assert run.sessions[0] is not run.sessions[1]
    assert all(s.closed for s in sessions)


def test_enqueue_swallows_start_failure(monkeypatch):
    run = _RecordingSideEffects()
    sched, _ = _make_scheduler(_FakeClock(), run, auto_start=True)

    def boom(self):
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(SessionEvidenceScheduler, "start", boom)
    sid = uuid.uuid4()
    # enqueue swallows the start failure internally (no propagation).
    sched.enqueue(sid, 1, "white", [_move(1, "white")])


def test_facade_swallows_enqueue_failure(monkeypatch):
    # The module facade is best-effort: any scheduler fault is logged and never
    # propagates into /moves (same contract as request_recompute).
    def boom(*args, **kwargs):
        raise RuntimeError("enqueue blew up")

    monkeypatch.setattr(evidence_mod._scheduler, "enqueue", boom)
    enqueue_session_evidence(
        object(),
        session_id=uuid.uuid4(),
        user_id=1,
        player_color="white",
        evidence_moves=[_move(1, "white")],
        move_count=1,
    )


def test_shutdown_drain_true_runs_pending():
    run = _RecordingSideEffects()
    sched, sessions = _make_scheduler(
        _FakeClock(), run, quiet_window=0.0, auto_start=True
    )
    sched.start()
    sid = uuid.uuid4()
    sched.enqueue(sid, 1, "white", [_move(1, "white")])
    sched.shutdown(drain=True, timeout=5.0)

    assert len(run.calls) == 1
    assert run.calls[0]["session_id"] == sid
    assert all(s.closed for s in sessions)


def test_shutdown_drain_false_clears_pending():
    run = _RecordingSideEffects()
    # Long quiet window so the entry never becomes due before shutdown clears it.
    sched, _ = _make_scheduler(
        _FakeClock(), run, quiet_window=60.0, auto_start=False
    )
    sid = uuid.uuid4()
    sched.enqueue(sid, 1, "white", [_move(1, "white")])
    with sched._cond:
        assert sid in sched._pending

    sched.shutdown(drain=False, timeout=5.0)

    with sched._cond:
        assert not sched._pending
    assert run.calls == []


def test_thread_integration_real_worker_runs_side_effects():
    done = threading.Event()
    seen: list[dict] = []

    def run_side_effects(db, **kwargs):
        seen.append(kwargs)
        done.set()
        return None

    sched, sessions = _make_scheduler(
        _FakeClock(), run_side_effects, quiet_window=0.0, auto_start=True
    )
    sched.start()
    sid = uuid.uuid4()
    try:
        sched.enqueue(sid, 42, "black", [_move(1, "white")])
        assert done.wait(timeout=5.0)
        assert len(seen) == 1
        assert seen[0]["session_id"] == sid
        assert seen[0]["user_id"] == 42
    finally:
        sched.shutdown(drain=True, timeout=5.0)
    assert all(s.closed for s in sessions)


def test_flush_pending_times_out_on_wedged_run():
    started = threading.Event()
    release = threading.Event()

    def run_side_effects(db, **kwargs):
        started.set()
        release.wait(timeout=5.0)
        return None

    # Real clock so flush_pending's timeout bound actually elapses.
    sched, _ = _make_scheduler(
        time.monotonic, run_side_effects, quiet_window=0.0, auto_start=True
    )
    sched.start()
    sched.enqueue(uuid.uuid4(), 1, "white", [_move(1, "white")])
    assert started.wait(timeout=5.0)

    with pytest.raises(TimeoutError):
        sched.flush_pending(timeout=0.3)

    release.set()
    sched.shutdown(drain=True, timeout=5.0)


def test_lifespan_starts_all_and_drains_baseline_then_evidence_before_opening(monkeypatch):
    # The lifespan starts all three schedulers, and on teardown drains them in a
    # load-bearing order: the baseline scheduler (a leaf worker) FIRST, then the
    # evidence scheduler BEFORE the opening scheduler — the evidence drain enqueues
    # opening-score recomputes via request_recompute, which silently early-returns
    # once the opening scheduler's _shutdown is set.
    parent = Mock()
    opening = Mock()
    evidence = Mock()
    baseline = Mock()
    parent.attach_mock(opening, "opening")
    parent.attach_mock(evidence, "evidence")
    parent.attach_mock(baseline, "baseline")

    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    monkeypatch.setattr(main, "get_scheduler", lambda: opening)
    monkeypatch.setattr(main, "get_evidence_scheduler", lambda: evidence)
    monkeypatch.setattr(main, "get_baseline_scheduler", lambda: baseline)
    monkeypatch.setattr(main.engine, "connect", lambda: connection)
    monkeypatch.setattr(main.engine, "dispose", Mock())
    start_prewarm = Mock()
    monkeypatch.setattr(main, "start_prewarm", start_prewarm)

    async def exercise_lifespan():
        async with main.lifespan(main.app):
            pass

    anyio.run(exercise_lifespan)

    opening.start.assert_called_once_with()
    evidence.start.assert_called_once_with()
    baseline.start.assert_called_once_with()
    start_prewarm.assert_called_once_with()
    opening.shutdown.assert_called_once_with(drain=True)
    evidence.shutdown.assert_called_once_with(drain=True)
    baseline.shutdown.assert_called_once_with(drain=True)

    shutdown_calls = [c for c in parent.mock_calls if c[0].endswith(".shutdown")]
    assert shutdown_calls == [
        call.baseline.shutdown(drain=True),
        call.evidence.shutdown(drain=True),
        call.opening.shutdown(drain=True),
    ]


def test_is_final_or_folds_across_the_coalesced_burst():
    # The end-of-session burst is incremental(not final) ... incremental ... then
    # the final_full upload. The ONE coalesced run that covers the whole burst is
    # the session's final run, so the bit must survive the fold.
    clock = _FakeClock()
    run = _RecordingSideEffects()
    sched, _ = _make_scheduler(clock, run)
    sid = uuid.uuid4()

    sched.enqueue(sid, 7, "white", [_move(1, "white")], is_final=False)
    sched.enqueue(sid, 7, "white", [_move(2, "black")], is_final=False)
    sched.enqueue(sid, 7, "white", [_move(3, "white")], is_final=True)
    clock.advance(2.0)
    sched.run_due()

    assert run.calls[0]["is_final"] is True


def test_is_final_stays_false_for_a_burst_with_no_terminal_upload():
    clock = _FakeClock()
    run = _RecordingSideEffects()
    sched, _ = _make_scheduler(clock, run)
    sid = uuid.uuid4()

    sched.enqueue(sid, 7, "white", [_move(1, "white")], is_final=False)
    sched.enqueue(sid, 7, "white", [_move(2, "black")], is_final=False)
    clock.advance(2.0)
    sched.run_due()

    assert run.calls[0]["is_final"] is False


def test_is_final_is_independent_of_run_opportunity():
    # The whole point of the separate bit: the revert upload sets
    # recompute_opportunity=True without ending the session, and a pre-g-y90g
    # client does so on EVERY mid-game upload. Folding finality onto
    # run_opportunity would score both as complete sessions.
    clock = _FakeClock()
    run = _RecordingSideEffects()
    sched, _ = _make_scheduler(clock, run)
    sid = uuid.uuid4()

    sched.enqueue(
        sid, 7, "white", [_move(1, "white")], run_opportunity=True, is_final=False
    )
    clock.advance(2.0)
    sched.run_due()

    assert run.calls[0]["run_opportunity"] is True
    assert run.calls[0]["is_final"] is False
