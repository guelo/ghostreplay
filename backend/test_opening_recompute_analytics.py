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

from app.models import SessionMove
from app.opening_cache import (
    OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL,
    recompute_opening_scores_if_needed,
)
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
    batch = recompute_opening_scores_if_needed(db_session, 123, "black")
    assert batch is not None

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
    assert isinstance(props["duration_ms"], (int, float))
    assert props["batch_size"] is not None and props["batch_size"] >= 0


def test_no_evidence_does_not_emit(db_session, captured):
    result = recompute_opening_scores_if_needed(db_session, 999, "white")
    assert result is None
    assert _events(captured) == []


def test_cached_return_does_not_emit(db_session, captured):
    _seed_black_opening_session(db_session)
    recompute_opening_scores_if_needed(db_session, 123, "black")
    captured.clear()

    second = recompute_opening_scores_if_needed(db_session, 123, "black")
    assert second is not None
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
    db_session.commit()

    second = recompute_opening_scores_if_needed(db_session, 123, "black")
    assert second is not None
    props = _only_props(captured)
    assert props["reason"] == "evidence_change"
    assert props["evidence_change"] is True
    assert props["cache_miss"] is False
    assert props["registry_drift"] is False
    assert props["stale_branch_keys"] is False


def test_decay_staleness_reason(db_session, captured):
    _seed_black_opening_session(db_session)
    first = recompute_opening_scores_if_needed(db_session, 123, "black")
    captured.clear()

    stale_at = datetime.now(timezone.utc) - OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL - timedelta(hours=1)
    first.computed_at = stale_at
    db_session.commit()

    second = recompute_opening_scores_if_needed(db_session, 123, "black")
    assert second is not None
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
    assert second is not None
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
    assert second is not None
    props = _only_props(captured)
    assert props["reason"] == "stale_branch_keys"
    assert props["stale_branch_keys"] is True
    assert props["registry_drift"] is False
    assert props["evidence_change"] is False
