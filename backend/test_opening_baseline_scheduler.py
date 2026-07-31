"""Tests for async opening-baseline capture (g-mxeo).

Two layers:

- ``run_baseline_snapshot_job`` (``app.opening_score_delta``): the DB-backed worker
  job that proves the pre-session cached batch fresh AND strictly pre-session before
  persisting the baseline, with a defense-in-depth conditional UPDATE. Driven
  directly against the ``db_session`` fixture (TestingSessionLocal).
- ``OpeningBaselineScheduler`` mechanics (coalescing, run_due, shutdown, best-effort
  facade): driven with fake sessions + a recording job, mirroring
  ``test_session_evidence_scheduler``.
"""

from __future__ import annotations

import threading
import uuid
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import app.opening_baseline_scheduler as baseline_mod
from app.models import (
    Blunder,
    BlunderReview,
    GameSession,
    OpeningScoreBatch,
    Position,
    SessionMove,
    UserOpeningScore,
)
from app.opening_baseline_scheduler import (
    OpeningBaselineScheduler,
    enqueue_baseline_snapshot,
)
from app.opening_cache import (
    SCORE_MODEL_VERSION,
    capture_freshness_snapshot,
    opening_score_inputs_fingerprint,
)
from app.opening_graph import get_opening_graph
from app.opening_roots import OpeningRoot, OpeningRoots, get_opening_roots
from app.opening_rootcalc import root_calc_config_fingerprint
from app.opening_score_delta import run_baseline_snapshot_job

# Fixed timeline: a batch dated at T_BEFORE is provably pre-session for a session
# started at T_START; a batch at T_AFTER is post-session (rejected by the date guard).
T_BEFORE = datetime(2026, 6, 1, tzinfo=timezone.utc)
T_START = datetime(2026, 6, 15, tzinfo=timezone.utc)
T_AFTER = datetime(2026, 6, 20, tzinfo=timezone.utc)


def _baseline_envelope(scores):
    return {
        "schema_version": 1,
        "model_version": SCORE_MODEL_VERSION,
        "root_calc_config_fingerprint": root_calc_config_fingerprint(),
        "scores": scores,
    }


# ---------------------------------------------------------------------------
# DB seeders
# ---------------------------------------------------------------------------
def _make_session(
    db, *, user_id=123, player_color="white", status="active",
    started_at=T_START, baseline=None,
) -> uuid.UUID:
    sid = uuid.uuid4()
    db.add(GameSession(
        id=sid, user_id=user_id, started_at=started_at, status=status,
        result="checkmate_win" if status == "ended" else None, engine_elo=1500,
        player_color=player_color, session_mode="normal",
        opening_score_baseline=baseline,
    ))
    db.commit()
    return sid


def _seed_batch(
    db, *, user_id=123, player_color="white", computed_at, fresh=True, scores,
) -> int:
    """Seed a batch + score rows. ``fresh=True`` stamps the registry fingerprint
    AND the full g-jact freshness bundle ``_is_batch_fresh`` checks (a no-evidence
    user has a deterministic empty bundle); ``fresh=False`` leaves them mismatched
    so the batch is provably stale."""
    if fresh:
        registry_fp = opening_score_inputs_fingerprint(
            get_opening_graph(), get_opening_roots()
        )
        snap = capture_freshness_snapshot(db, user_id, player_color)
        batch = OpeningScoreBatch(
            user_id=user_id, player_color=player_color, generation=1,
            registry_fingerprint=registry_fp,
            inputs_fingerprint=snap.inputs_fingerprint,
            evidence_seq=snap.evidence_seq,
            cache_epoch=snap.cache_epoch,
            scoped_shared_digest=snap.scoped_shared_digest,
            computed_at=computed_at,
        )
    else:
        batch = OpeningScoreBatch(
            user_id=user_id, player_color=player_color, generation=1,
            registry_fingerprint="stale-registry-fp", inputs_fingerprint=None,
            computed_at=computed_at,
        )
    db.add(batch)
    db.flush()
    for key, score in scores.items():
        db.add(UserOpeningScore(
            batch_id=batch.id, user_id=user_id, player_color=player_color,
            opening_key=key, opening_name="x", opening_family="x",
            opening_score=score, confidence=0.5, coverage=0.5, weighted_depth=1.0,
            sample_size=5, computed_at=computed_at,
        ))
    db.commit()
    return batch.id


def _seed_position(db, *, user_id=123) -> int:
    pos = Position(
        user_id=user_id, fen_hash=f"h{uuid.uuid4().hex[:12]}", fen_raw="fen",
        active_color="white",
    )
    db.add(pos)
    db.flush()
    return pos.id


def _seed_blunder(db, *, user_id=123, source_session_id=None) -> int:
    pos_id = _seed_position(db, user_id=user_id)
    blunder = Blunder(
        user_id=user_id, position_id=pos_id, bad_move_san="a", best_move_san="b",
        eval_loss_cp=100, source_session_id=source_session_id,
    )
    db.add(blunder)
    db.flush()
    return blunder.id


def _seed_review(db, *, session_id, user_id=123) -> None:
    blunder_id = _seed_blunder(db, user_id=user_id)
    db.add(BlunderReview(
        blunder_id=blunder_id, session_id=session_id, passed=True,
        move_played_san="a", eval_delta_cp=0,
    ))
    db.commit()


def _seed_move(db, *, session_id, with_fen_before=True) -> None:
    db.add(SessionMove(
        session_id=session_id, move_number=1, color="white", move_san="e4",
        fen_before=(
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
            if with_fen_before else None
        ),
        fen_after="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        segment="normal",
    ))
    db.commit()


def _baseline(db, sid) -> str | None:
    db.expire_all()
    return db.get(GameSession, sid).opening_score_baseline


# ---------------------------------------------------------------------------
# run_baseline_snapshot_job — capture logic
# ---------------------------------------------------------------------------
def test_fresh_pre_session_batch_is_persisted(db_session):
    # (1) Fresh batch dated strictly before started_at, no session evidence ->
    # the score map is persisted with source=cached_fresh.
    sid = _make_session(db_session)
    _seed_batch(db_session, computed_at=T_BEFORE, fresh=True, scores={"k": 42.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "cached_fresh"
    assert json.loads(_baseline(db_session, sid)) == _baseline_envelope({"k": 42.0})


def test_review_race_rejected_by_date_guard(db_session):
    # (2) A session-scoped review plus a newer post-session batch -> NULL,
    # skipped_post_session_batch (the date guard fires before any digest).
    sid = _make_session(db_session)
    _seed_review(db_session, session_id=sid)
    _seed_batch(db_session, computed_at=T_AFTER, fresh=True, scores={"k": 42.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "skipped_post_session_batch"
    assert _baseline(db_session, sid) is None


def test_blunder_target_race_rejected_by_date_guard(db_session):
    # (3) A session-scoped ghost-target blunder plus a newer post-session batch ->
    # NULL, skipped_post_session_batch.
    sid = _make_session(db_session)
    _seed_blunder(db_session, source_session_id=sid)
    db_session.commit()
    _seed_batch(db_session, computed_at=T_AFTER, fresh=True, scores={"k": 42.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "skipped_post_session_batch"
    assert _baseline(db_session, sid) is None


def test_move_race_rejected_by_date_guard(db_session):
    # (4) A session move plus a newer post-session batch -> NULL,
    # skipped_post_session_batch.
    sid = _make_session(db_session)
    _seed_move(db_session, session_id=sid)
    _seed_batch(db_session, computed_at=T_AFTER, fresh=True, scores={"k": 42.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "skipped_post_session_batch"
    assert _baseline(db_session, sid) is None


def test_brand_new_user_persists_empty_baseline(db_session):
    # (5) No batch, no evidence -> a valid empty envelope (empty_no_evidence),
    # so the session's first openings later read as new.
    sid = _make_session(db_session)

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "empty_no_evidence"
    assert json.loads(_baseline(db_session, sid)) == _baseline_envelope({})


def test_already_set_baseline_is_a_noop(db_session):
    # (6) An already-set baseline is idempotent -> already_set, value untouched.
    import json
    sid = _make_session(db_session, baseline=json.dumps({"x": 1.0}))
    _seed_batch(db_session, computed_at=T_BEFORE, fresh=True, scores={"k": 42.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "already_set"
    assert json.loads(_baseline(db_session, sid)) == {"x": 1.0}


@pytest.mark.parametrize("evidence", ["move", "review", "blunder"])
def test_persist_race_leaves_baseline_null(db_session, evidence):
    # (7) Defense-in-depth: capture returns JSON, but session-scoped evidence
    # inserted before the UPDATE makes rowcount 0 -> NULL,
    # raced_evidence_or_already_set. Force a JSON capture and insert real evidence
    # so the conditional UPDATE's NOT EXISTS clause vetoes the write.
    sid = _make_session(db_session)
    if evidence == "move":
        _seed_move(db_session, session_id=sid)
    elif evidence == "review":
        _seed_review(db_session, session_id=sid)
    else:
        _seed_blunder(db_session, source_session_id=sid)
        db_session.commit()

    with patch(
        "app.opening_score_delta._capture_baseline_json",
        return_value=('{"k": 1.0}', "cached_fresh"),
    ):
        source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "raced_evidence_or_already_set"
    assert _baseline(db_session, sid) is None


def test_cold_cache_with_evidence_skipped(db_session):
    # (8a) No batch but the user has evidence (cold, e.g. post-restart) -> NULL,
    # skipped_cold. Seed evidence via a prior ended session's move.
    prior = _make_session(db_session, status="ended", started_at=T_BEFORE)
    _seed_move(db_session, session_id=prior)
    sid = _make_session(db_session)

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "skipped_cold"
    assert _baseline(db_session, sid) is None


def test_stale_pre_session_batch_skipped(db_session):
    # (8b) A pre-session batch whose fingerprints don't match -> NULL,
    # skipped_stale (the date guard passes; freshness fails).
    sid = _make_session(db_session)
    _seed_batch(db_session, computed_at=T_BEFORE, fresh=False, scores={"k": 42.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "skipped_stale"
    assert _baseline(db_session, sid) is None


def test_naive_computed_at_strictly_before_is_accepted(db_session):
    # (9a) A naive computed_at strictly before started_at is accepted without an
    # aware/naive comparison crash.
    sid = _make_session(db_session)
    _seed_batch(
        db_session, computed_at=datetime(2026, 6, 1), fresh=True, scores={"k": 7.0}
    )

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "cached_fresh"
    assert json.loads(_baseline(db_session, sid)) == _baseline_envelope({"k": 7.0})


def test_naive_computed_at_equal_to_started_at_is_rejected(db_session):
    # (9b) computed_at tying started_at cannot be proven pre-session -> rejected
    # (strict guard), and no naive/aware crash. Use the exact started_at the row
    # round-tripped to as the batch's computed_at so equality is guaranteed.
    sid = _make_session(db_session)
    started = db_session.get(GameSession, sid).started_at
    _seed_batch(db_session, computed_at=started, fresh=True, scores={"k": 7.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "skipped_post_session_batch"
    assert _baseline(db_session, sid) is None


def test_missing_session_returns_missing(db_session):
    source = run_baseline_snapshot_job(db_session, uuid.uuid4(), 123, "white")
    assert source == "missing_session"


def test_not_active_session_skipped(db_session):
    sid = _make_session(db_session, status="ended")
    source = run_baseline_snapshot_job(db_session, sid, 123, "white")
    assert source == "not_active"


def test_untrusted_queued_identity_captures_from_the_row(db_session):
    # (11) The queued (user_id, player_color) is an untrusted routing hint. Seed the
    # session owner (123/white) with their own fresh pre-session batch, and a
    # DIFFERENT user/color (999/black) with theirs. A job carrying the WRONG pair
    # must NOT capture the other user's scores; a correct-pair job captures the
    # session owner's batch — proving capture keys off the ROW, not the payload.
    sid = _make_session(db_session, user_id=123, player_color="white")
    _seed_batch(
        db_session, user_id=123, player_color="white", computed_at=T_BEFORE,
        fresh=True, scores={"owner": 42.0},
    )
    _seed_batch(
        db_session, user_id=999, player_color="black", computed_at=T_BEFORE,
        fresh=True, scores={"other": 99.0},
    )

    mismatch = run_baseline_snapshot_job(db_session, sid, 999, "black")
    assert mismatch == "session_mismatch"
    assert _baseline(db_session, sid) is None

    ok = run_baseline_snapshot_job(db_session, sid, 123, "white")
    assert ok == "cached_fresh"
    assert json.loads(_baseline(db_session, sid)) == _baseline_envelope(
        {"owner": 42.0}
    )


# ---------------------------------------------------------------------------
# OpeningBaselineScheduler mechanics (fake sessions + recording job)
# ---------------------------------------------------------------------------
class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _RecordingJob:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.sessions: list[object] = []

    def __call__(self, db, **kwargs):
        self.sessions.append(db)
        self.calls.append(kwargs)


def _make_scheduler(run_job=None, **kwargs):
    sessions: list[_FakeSession] = []

    def factory():
        s = _FakeSession()
        sessions.append(s)
        return s

    params = {"auto_start": False}
    params.update(kwargs)
    sched = OpeningBaselineScheduler(
        session_factory=factory, run_job=run_job or _RecordingJob(), **params
    )
    return sched, sessions


def test_run_due_runs_job_once_with_fresh_session():
    job = _RecordingJob()
    sched, sessions = _make_scheduler(run_job=job)
    sid = uuid.uuid4()

    sched.enqueue(sid, 7, "white")
    sched.run_due()

    assert job.calls == [{"session_id": sid, "user_id": 7, "player_color": "white"}]
    assert len(sessions) == 1
    assert sessions[0].closed is True


def test_duplicate_enqueues_for_same_session_coalesce():
    job = _RecordingJob()
    sched, _ = _make_scheduler(run_job=job)
    sid = uuid.uuid4()

    sched.enqueue(sid, 7, "white")
    sched.enqueue(sid, 7, "white")
    sched.enqueue(sid, 7, "white")
    sched.run_due()

    assert len(job.calls) == 1


def test_distinct_sessions_each_run_with_own_session():
    job = _RecordingJob()
    sched, sessions = _make_scheduler(run_job=job)
    sid1, sid2 = uuid.uuid4(), uuid.uuid4()

    sched.enqueue(sid1, 1, "white")
    sched.enqueue(sid2, 2, "black")
    sched.run_due()

    assert sorted(c["session_id"] for c in job.calls) == sorted([sid1, sid2])
    assert job.sessions[0] is not job.sessions[1]
    assert all(s.closed for s in sessions)


def test_session_closed_when_job_raises_and_later_enqueue_runs():
    def boom(db, **kwargs):
        raise RuntimeError("job failed")

    sched, sessions = _make_scheduler(run_job=boom)
    sched.enqueue(uuid.uuid4(), 7, "white")
    # The failure is swallowed and the session still closed.
    sched.run_due()
    assert len(sessions) == 1
    assert sessions[0].closed is True

    # Not wedged: a later session still runs.
    sched.enqueue(uuid.uuid4(), 8, "black")
    sched.run_due()
    assert len(sessions) == 2
    assert sessions[1].closed is True


def test_enqueue_swallows_auto_start_failure(monkeypatch):
    sched, _ = _make_scheduler(auto_start=True)

    def boom(self):
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(OpeningBaselineScheduler, "start", boom)
    # enqueue swallows the start failure internally (no propagation).
    sched.enqueue(uuid.uuid4(), 1, "white")


def test_daemon_containment_leaves_the_singleton_reusable():
    """``conftest.stop_scheduler_daemon`` must stop the worker AND unlatch it.

    Stopping is what keeps a leaked daemon from writing the shared database for the
    rest of the run; unlatching is what keeps that containment from silently breaking
    every later enqueue. ``shutdown()`` alone fails the second half: ``enqueue``
    returns on ``_shutdown`` before it ever reaches ``start()`` — proven on a throwaway
    instance here rather than by restarting the real singleton, which would run real
    jobs against the configured database (g-rating-serialize-flake).
    """
    from conftest import stop_scheduler_daemon

    def _reusable(sched, ran: threading.Event) -> None:
        """Enqueue and require the job to actually RUN.

        Deliberately NOT an assertion on ``_pending``: unlatching restores
        auto-start, so the worker legitimately races the test to drain the entry
        and a non-empty ``_pending`` is not something this test may rely on. The
        job having run is the outcome that is both stable and the one we mean.
        """
        sched.enqueue(uuid.uuid4(), 1, "white")
        try:
            assert ran.wait(timeout=5.0), "a contained scheduler dropped a later enqueue"
        finally:
            sched.shutdown(drain=False, timeout=5.0)

    # The half that is easy to get wrong: a latched scheduler accepts nothing.
    # auto_start=False so no worker can exist at all — the empty ``_pending`` is
    # then unambiguously the early return on ``_shutdown``, never a drain.
    latched, _ = _make_scheduler(auto_start=False)
    latched.shutdown(drain=False, timeout=5.0)
    latched.enqueue(uuid.uuid4(), 1, "white")
    with latched._cond:
        assert latched._pending == {}, "a latched scheduler silently dropped the enqueue"

    # Case 1: a LIVE daemon is stopped, unlatched, and still usable afterwards.
    live_ran = threading.Event()
    live, _ = _make_scheduler(run_job=lambda db, **kw: live_ran.set(), auto_start=True)
    live.start()
    assert live._thread is not None and live._thread.is_alive()

    assert stop_scheduler_daemon(live) is True
    assert live._thread is None          # the worker is gone...
    assert live._shutdown is False       # ...and the singleton is not latched
    _reusable(live, live_ran)

    # Case 2: ALREADY LATCHED with no live thread — the state a test leaves behind
    # when it runs app.main.lifespan for real (that start()s and shutdown()s the
    # baseline and evidence singletons). There is no daemon to join, so the helper
    # reports False, but it must STILL unlatch: otherwise every later facade call
    # silently no-ops for the rest of the run.
    dormant_ran = threading.Event()
    dormant, _ = _make_scheduler(
        run_job=lambda db, **kw: dormant_ran.set(), auto_start=True
    )
    dormant.shutdown(drain=False, timeout=5.0)
    assert dormant._thread is None and dormant._shutdown is True

    assert stop_scheduler_daemon(dormant) is False  # nothing was running...
    assert dormant._shutdown is False               # ...but it is no longer latched
    _reusable(dormant, dormant_ran)


def test_facade_swallows_enqueue_failure(monkeypatch):
    called: list[int] = []

    def boom(self, *args, **kwargs):
        called.append(1)
        raise RuntimeError("enqueue blew up")

    # Patch the CLASS, never the singleton INSTANCE. `monkeypatch.setattr(instance,
    # "enqueue", ...)` records the inherited BOUND METHOD as the old value (getattr
    # finds it on the class) and undo restores it as an INSTANCE attribute — which
    # then permanently shadows every later class-level patch, including the two
    # endpoint tests below. That is how the real baseline daemon escaped into the
    # rest of the run and started writing the shared database
    # (g-rating-serialize-flake).
    monkeypatch.setattr(OpeningBaselineScheduler, "enqueue", boom)
    # The module facade is best-effort: never propagates into /start.
    enqueue_baseline_snapshot(uuid.uuid4(), 1, "white")
    assert called, "the facade never reached the faulting enqueue"
    assert "enqueue" not in baseline_mod._scheduler.__dict__, (
        "the singleton kept an instance-level `enqueue`, which shadows class patches"
    )


def test_shutdown_drain_true_runs_pending():
    job = _RecordingJob()
    sched, sessions = _make_scheduler(run_job=job, auto_start=True)
    sched.start()
    sid = uuid.uuid4()
    sched.enqueue(sid, 1, "white")
    sched.shutdown(drain=True, timeout=5.0)

    assert len(job.calls) == 1
    assert job.calls[0]["session_id"] == sid
    assert all(s.closed for s in sessions)


def test_shutdown_drain_false_clears_pending():
    job = _RecordingJob()
    sched, _ = _make_scheduler(run_job=job, auto_start=False)
    sid = uuid.uuid4()
    sched.enqueue(sid, 1, "white")
    with sched._cond:
        assert sid in sched._pending

    sched.shutdown(drain=False, timeout=5.0)

    with sched._cond:
        assert not sched._pending
    assert job.calls == []


def test_thread_integration_real_worker_runs_job():
    done = threading.Event()
    seen: list[dict] = []

    def run_job(db, **kwargs):
        seen.append(kwargs)
        done.set()

    sched, sessions = _make_scheduler(run_job=run_job, auto_start=True)
    sched.start()
    sid = uuid.uuid4()
    try:
        sched.enqueue(sid, 42, "black")
        assert done.wait(timeout=5.0)
        assert len(seen) == 1
        assert seen[0]["session_id"] == sid
        assert seen[0]["user_id"] == 42
    finally:
        sched.shutdown(drain=True, timeout=5.0)
    assert all(s.closed for s in sessions)


# ---------------------------------------------------------------------------
# Endpoint best-effort: /start returns 201 even when the enqueue machinery faults
# ---------------------------------------------------------------------------
def test_game_start_returns_201_when_enqueue_faults(client, auth_headers, monkeypatch):
    # Bind the REAL facade into the endpoint (overriding the autouse no-op) and make
    # the underlying scheduler enqueue fault: the facade must swallow it so /start
    # still returns 201 (best-effort contract).
    called: list[int] = []

    def boom(self, *args, **kwargs):
        called.append(1)
        raise RuntimeError("scheduler blew up")

    monkeypatch.setattr(OpeningBaselineScheduler, "enqueue", boom)
    with patch(
        "app.api.game.enqueue_baseline_snapshot",
        baseline_mod.enqueue_baseline_snapshot,
    ):
        resp = client.post(
            "/api/game/start",
            json={"engine_elo": 1500, "player_color": "white"},
            headers=auth_headers(user_id=123),
        )
    assert resp.status_code == 201
    # Without this the test passes on a 201 that never faulted at all — which is
    # exactly what happened while an instance-level `enqueue` shadowed this patch,
    # letting /start spin up the REAL daemon instead (g-rating-serialize-flake).
    assert called, "the endpoint never reached the faulting enqueue"


DRILL_ROOT_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"


def _drill_roots() -> OpeningRoots:
    root = OpeningRoot(
        opening_key=DRILL_ROOT_FEN, opening_name="King's Pawn Game",
        opening_family="King's Pawn Game", eco="B00", depth=1,
        parent_keys=frozenset(), child_keys=frozenset(),
    )
    return OpeningRoots({DRILL_ROOT_FEN: root}, {DRILL_ROOT_FEN: frozenset([DRILL_ROOT_FEN])})


def test_drill_start_returns_201_when_enqueue_faults(client, auth_headers, monkeypatch):
    called: list[int] = []

    def boom(self, *args, **kwargs):
        called.append(1)
        raise RuntimeError("scheduler blew up")

    monkeypatch.setattr(OpeningBaselineScheduler, "enqueue", boom)
    with (
        patch(
            "app.api.drills.enqueue_baseline_snapshot",
            baseline_mod.enqueue_baseline_snapshot,
        ),
        patch("app.api.drills.get_opening_roots", return_value=_drill_roots()),
    ):
        resp = client.post(
            "/api/drills/start",
            json={
                "opening_key": DRILL_ROOT_FEN, "player_color": "white",
                "engine_elo": 1500, "strictness": "standard",
            },
            headers=auth_headers(user_id=123),
        )
    assert resp.status_code == 201
    assert called, "the endpoint never reached the faulting enqueue"
