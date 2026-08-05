"""``opening_scores_recomputed`` perf-event capture (Phase 5 of g-g9sq).

The event must fire ONLY when an actual recompute runs (never on the fast
cached-batch return), and its ``reason`` must reflect the dominant trigger in
priority order: cache_miss > registry_drift > stale_branch_keys >
evidence_change > decay_staleness. These tests patch ``opening_cache.capture``
with a recorder and drive ``recompute_opening_scores_if_needed`` directly, reusing
the graph/roots seeding helpers from ``test_opening_cache``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

import app.opening_cache as opening_cache
import app.opening_evidence as opening_evidence
from app.models import SessionMove
from app.opening_cache import (
    OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL,
    RecomputeDisposition,
    bump_evidence_seq,
    recompute_opening_scores_if_needed,
)
from app.opening_score_scheduler import OpeningScoreScheduler, OpeningScoreTrigger
from test_opening_cache import _make_graph, _make_roots, _seed_black_opening_session


@pytest.fixture(autouse=True)
def _mock_singletons():
    with (
        patch("app.opening_cache.get_opening_graph", return_value=_make_graph()),
        patch("app.opening_cache.get_opening_roots", return_value=_make_roots()),
    ):
        yield


@pytest.fixture
def captured(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr("app.opening_cache.capture", lambda *a, **k: calls.append(a))
    return calls


def _events(calls: list[tuple]) -> list[tuple]:
    return [a for a in calls if len(a) >= 2 and a[1] == "opening_scores_recomputed"]


def _only_props(calls: list[tuple]) -> dict:
    events = _events(calls)
    assert len(events) == 1, f"expected exactly one recompute event, got {len(events)}"
    return events[0][2]


def test_cache_miss_emits_with_full_props(db_session, captured):
    _seed_black_opening_session(db_session)
    result = recompute_opening_scores_if_needed(db_session, 123, "black")
    assert result.disposition is RecomputeDisposition.REBUILT

    events = _events(captured)
    assert len(events) == 1
    did, _event, props = events[0]
    assert did == "123"
    assert props["reason"] == "cache_miss"
    assert props["cache_miss"] is True
    assert props["registry_drift"] is False
    assert props["stale_branch_keys"] is False
    assert props["evidence_change"] is False
    assert props["decay_staleness"] is False
    assert props["player_color"] == "black"
    assert props["freshness_capture"] == "operational"
    assert isinstance(props["duration_ms"], (int, float))
    assert props["batch_size"] is not None and props["batch_size"] >= 0
    assert props["replay_cache_builds"] == 1
    assert props["replay_cache_probed_sessions"] == 1
    assert props["replay_cache_l1_hits"] == 0
    assert props["replay_cache_l2_hits"] == 0
    assert props["replay_cache_raw_derivations"] == 1
    assert props["replay_cache_persisted_upserts"] == 1
    assert props["replay_cache_l2_read_failed"] is False
    assert props["replay_cache_l2_write_failed"] is False


def test_persisted_bootstrap_emits_l2_restart_signature(db_session, captured):
    _seed_black_opening_session(db_session)
    opening_evidence.overlay_evidence(db_session, 123, "black", _make_graph())
    db_session.commit()  # Direct SQLite build deliberately owns L2 durability.
    opening_evidence.reset_session_evidence_cache()

    result = recompute_opening_scores_if_needed(db_session, 123, "black")

    assert result.disposition is RecomputeDisposition.REBUILT
    props = _only_props(captured)
    assert props["replay_cache_builds"] == 1
    assert props["replay_cache_probed_sessions"] == 1
    assert props["replay_cache_l1_hits"] == 0
    assert props["replay_cache_l2_hits"] == 1
    assert props["replay_cache_raw_derivations"] == 0
    assert props["replay_cache_persisted_upserts"] == 0


def test_fallback_event_merges_discarded_overlay_cache_work(
    db_session, captured, monkeypatch
):
    _seed_black_opening_session(db_session)
    opening_evidence.overlay_evidence(db_session, 123, "black", _make_graph())
    db_session.commit()
    opening_evidence.reset_session_evidence_cache()

    real_shared_snapshot = opening_cache.shared_scope_snapshot
    calls = {"n": 0}

    def drift_counter(*args, **kwargs):
        snapshot = real_shared_snapshot(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            bump_evidence_seq(db_session, 123, "black")
            db_session.commit()
        return snapshot

    monkeypatch.setattr(
        opening_cache, "shared_scope_snapshot", drift_counter
    )
    result = recompute_opening_scores_if_needed(db_session, 123, "black")

    assert result.disposition is RecomputeDisposition.REBUILT
    props = _only_props(captured)
    assert props["freshness_capture"] == "fallback_counter"
    assert props["replay_cache_builds"] == 2
    assert props["replay_cache_probed_sessions"] == 2
    assert props["replay_cache_l1_hits"] == 1
    assert props["replay_cache_l2_hits"] == 1
    assert props["replay_cache_raw_derivations"] == 0


def test_missing_epoch_emits_and_logs_null_epoch_fallback(
    db_session, captured, caplog
):
    _seed_black_opening_session(db_session)
    db_session.execute(text("DELETE FROM evidence_epoch"))
    db_session.commit()

    with caplog.at_level("INFO", logger="app.opening_cache"):
        result = recompute_opening_scores_if_needed(db_session, 123, "black")

    assert result.disposition is RecomputeDisposition.REBUILT
    props = _only_props(captured)
    assert props["freshness_capture"] == "fallback_null_epoch"
    assert any(
        "freshness_capture=fallback_null_epoch" in record.getMessage()
        for record in caplog.records
    )


def test_no_evidence_does_not_emit(db_session, captured):
    result = recompute_opening_scores_if_needed(db_session, 999, "white")
    assert result.disposition is RecomputeDisposition.NO_EVIDENCE
    assert result.batch is None
    assert result.reason is None
    assert _events(captured) == []


def test_cached_return_does_not_emit(db_session, captured):
    _seed_black_opening_session(db_session)
    recompute_opening_scores_if_needed(db_session, 123, "black")
    captured.clear()

    second = recompute_opening_scores_if_needed(db_session, 123, "black")
    assert second.disposition is RecomputeDisposition.CACHED
    assert second.reason is None
    assert _events(captured) == []


def test_evidence_change_reason(db_session, captured):
    session = _seed_black_opening_session(db_session)
    recompute_opening_scores_if_needed(db_session, 123, "black")
    captured.clear()

    move = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session.id, SessionMove.color == "black")
        .first()
    )
    move.eval_delta = 500
    # Mirror the production upsert_session_moves choke-point bump (g-jact).
    bump_evidence_seq(db_session, 123, "black")
    db_session.commit()

    second = recompute_opening_scores_if_needed(db_session, 123, "black")
    assert second.disposition is RecomputeDisposition.REBUILT
    props = _only_props(captured)
    assert props["reason"] == "evidence_change"
    assert props["evidence_change"] is True
    assert props["cache_miss"] is False
    assert props["registry_drift"] is False
    assert props["stale_branch_keys"] is False


def test_decay_staleness_reason(db_session, captured):
    _seed_black_opening_session(db_session)
    first = recompute_opening_scores_if_needed(db_session, 123, "black").batch
    captured.clear()

    stale_at = datetime.now(timezone.utc) - OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL - timedelta(hours=1)
    first.computed_at = stale_at
    db_session.commit()

    second = recompute_opening_scores_if_needed(db_session, 123, "black")
    assert second.disposition is RecomputeDisposition.REBUILT
    props = _only_props(captured)
    assert props["reason"] == "decay_staleness"
    assert props["decay_staleness"] is True
    assert props["evidence_change"] is False
    assert props["registry_drift"] is False
    assert props["stale_branch_keys"] is False


def test_registry_drift_reason(db_session, captured, monkeypatch):
    _seed_black_opening_session(db_session)
    recompute_opening_scores_if_needed(db_session, 123, "black")
    captured.clear()

    # A model-version bump drifts the registry fingerprint; registry_drift wins
    # the priority order regardless of whether the raw fingerprint also moved.
    monkeypatch.setattr("app.opening_cache.SCORE_MODEL_VERSION", "sm-bumped")
    second = recompute_opening_scores_if_needed(db_session, 123, "black")
    assert second.disposition is RecomputeDisposition.REBUILT
    props = _only_props(captured)
    assert props["reason"] == "registry_drift"
    assert props["registry_drift"] is True
    assert props["cache_miss"] is False


def test_stale_branch_keys_reason(db_session, captured):
    _seed_black_opening_session(db_session)
    recompute_opening_scores_if_needed(db_session, 123, "black")
    captured.clear()

    # Force the stale-branch-keys branch on an otherwise-unchanged batch; it
    # outranks evidence_change/decay in the reason priority order.
    with patch("app.opening_cache._batch_has_stale_branch_keys", return_value=True):
        second = recompute_opening_scores_if_needed(db_session, 123, "black")
    assert second.disposition is RecomputeDisposition.REBUILT
    props = _only_props(captured)
    assert props["reason"] == "stale_branch_keys"
    assert props["stale_branch_keys"] is True
    assert props["registry_drift"] is False
    assert props["evidence_change"] is False


# ---------------------------------------------------------------------------
# Scheduler-timed enrichment (g-score-queue-timing Phase 4)
#
# The queue/worker decomposition rides on the EXISTING event, so these cases drive
# a REAL rebuild through a real scheduler (fake clock, synchronous run_due) and
# assert what a production HogQL query would see.
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def _scheduler_for(db_session, clock):
    """A real scheduler whose worker recomputes on the TEST session, run inline."""
    return OpeningScoreScheduler(
        session_factory=lambda: _NonClosingSession(db_session),
        clock=clock,
        auto_start=False,
    )


class _NonClosingSession:
    """Proxy so the scheduler's ``db.close()`` cannot close the test's session."""

    def __init__(self, db) -> None:
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self) -> None:
        pass


_TIMING_FIELDS = (
    "queue_first_ms",
    "queue_last_ms",
    "coalesce_span_ms",
    "deadline_delay_ms",
    "dispatch_lag_ms",
    "worker_compute_ms",
)


def test_scheduled_rebuild_is_timed_with_finite_fields(db_session, captured):
    _seed_black_opening_session(db_session)
    clock = _FakeClock()
    sched = _scheduler_for(db_session, clock)

    sched.request_recompute(123, "black", source=OpeningScoreTrigger.SESSION_LINEAGE_COLD)
    clock.advance(2.0)
    sched.run_due()

    props = _only_props(captured)
    assert props["scheduler_timed"] is True
    assert props["scheduler_timing_version"] == 1
    assert isinstance(props["scheduler_run_id"], str) and props["scheduler_run_id"]
    for field in _TIMING_FIELDS:
        value = props[field]
        assert isinstance(value, (int, float)), field
        assert value == value and abs(value) != float("inf"), field  # finite
        assert value >= 0, field
    # The existing narrow rebuild span is preserved alongside the new worker span.
    assert isinstance(props["duration_ms"], (int, float))
    assert props["reason"] == "cache_miss"
    assert props["trigger_sources"] == ["session_lineage_cold"]
    assert props["forced_dispatch"] is False
    assert props["immediate"] is False
    assert props["enqueue_count"] == 1
    assert props["quiet_window_ms"] == 1500.0
    assert props["max_wait_ms"] == 10000.0
    # No user-derived payload beyond the existing distinct id / player_color.
    assert "user_id" not in props
    assert "opening_key" not in props


def test_direct_recompute_is_explicitly_untimed(db_session, captured):
    # A direct/offline/test call has no scheduler context: the event still fires but
    # must not invent queue values, and production reports filter it out.
    _seed_black_opening_session(db_session)
    recompute_opening_scores_if_needed(db_session, 123, "black")

    props = _only_props(captured)
    assert props["scheduler_timed"] is False
    for field in (*_TIMING_FIELDS, "scheduler_run_id", "trigger_sources"):
        assert field not in props


def test_timing_lookup_failure_does_not_change_the_durable_result(
    db_session, captured, monkeypatch
):
    # Telemetry is best-effort: a broken timing snapshot must not alter the batch or
    # the explicit rebuilt disposition.
    import app.opening_score_scheduler as scheduler_module

    monkeypatch.setattr(
        scheduler_module,
        "current_run_timing",
        lambda: (_ for _ in ()).throw(RuntimeError("timing exploded")),
    )
    _seed_black_opening_session(db_session)
    clock = _FakeClock()
    sched = _scheduler_for(db_session, clock)

    sched.request_recompute(123, "black", source=OpeningScoreTrigger.SCORE_DELTA)
    clock.advance(2.0)
    sched.run_due()

    props = _only_props(captured)
    assert props["scheduler_timed"] is False  # degraded, not fabricated
    assert props["reason"] == "cache_miss"
    batch, rows = _latest(db_session)
    assert batch is not None and rows


def _latest(db_session):
    from app.opening_cache import list_cached_opening_scores

    return list_cached_opening_scores(db_session, 123, "black")


def test_mixed_source_run_is_selected_by_set_membership_not_by_endpoints(
    db_session, captured
):
    """The behavioral fixture for the runbook's mixed-source HogQL.

    One coalesced run can carry ``session_lineage_cold`` at NEITHER endpoint. Filtering
    the g-a5v3 target cohort by ``trigger_first``/``trigger_last`` equality would drop
    exactly this run, which is why the runbook filters on membership in
    ``trigger_sources``.
    """
    _seed_black_opening_session(db_session)
    clock = _FakeClock()
    sched = _scheduler_for(db_session, clock)

    sched.request_recompute(123, "black", source=OpeningScoreTrigger.TREE_READER_WARM)
    clock.advance(0.2)
    sched.request_recompute(123, "black", source=OpeningScoreTrigger.SESSION_LINEAGE_COLD)
    clock.advance(0.2)
    sched.request_recompute(123, "black", source=OpeningScoreTrigger.SCORE_DELTA)
    clock.advance(2.0)
    sched.run_due()

    props = _only_props(captured)
    assert props["trigger_first"] == "tree_reader_warm"
    assert props["trigger_last"] == "score_delta"
    assert props["trigger_sources"] == [
        "score_delta",
        "session_lineage_cold",
        "tree_reader_warm",
    ]
    # Membership selects the run...
    assert "session_lineage_cold" in props["trigger_sources"]
    # ...while equality against either endpoint would have missed it.
    assert props["trigger_first"] != "session_lineage_cold"
    assert props["trigger_last"] != "session_lineage_cold"
