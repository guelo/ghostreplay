"""The read-only reader/oracle comparison run before the SRS retention rollout."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import pg_required
from app.models import (
    BlunderOpportunitySummary,
    OpportunityRetentionPolicy,
    User,
    UserOpportunityRetentionState,
)
from app.srs_opportunity import load_opportunity_counters
from scripts import compare_srs_reader_parity as parity
from test_opportunity_compaction_pg import _seed
from test_srs_opportunity import _blunder, _opportunity_event, _position

FEN_A = "8/8/8/8/8/8/8/K6k w - - 0 1"
FEN_B = "8/8/8/8/8/8/8/K5k1 w - - 0 1"


def _owners(db_session):
    """Two owners: one blunder with two sessions, one with one session."""
    now = datetime.now(timezone.utc)
    blunders = []
    for user_id, fen, sessions in ((501, FEN_A, 2), (502, FEN_B, 1)):
        db_session.add(User(id=user_id, username=None, is_anonymous=True))
        db_session.flush()
        blunder = _blunder(db_session, user_id=user_id, position=_position(
            db_session, user_id=user_id, fen=fen, active_color="white"))
        for day in range(sessions):
            _opportunity_event(
                db_session, user_id=user_id, blunder=blunder, opportunity=True,
                reached=day == 0, occurred_at=now - timedelta(hours=day + 1),
                targeted=day == 0,
            )
        blunders.append(blunder)
    db_session.commit()
    return blunders


def _run(db):
    lines = []
    code = parity.run(db, out=lines.append)
    return code, "\n".join(lines)


def test_matching_readers_compare_every_blunder_with_each_exclusion(db_session):
    first, _ = _owners(db_session)
    seeded = load_opportunity_counters(db_session, [first.id], user_id=501)[first.id]
    # Not a vacuous match: the seeded evidence shows up in both streams.
    assert seeded.event_count == 2 and seeded.targeted_30d == 1

    code, out = _run(db_session)

    assert code == 0
    assert "first_fold=never" in out
    # 501: one blunder x (no exclusion + 2 sessions); 502: one x (1 + 1).
    assert "owners=2 blunders=2 session_exclusions=3 comparisons=5" in out
    assert "mismatches=0" in out


def test_a_disagreeing_reader_is_counted_per_counter_and_fails(db_session, monkeypatch):
    first, _ = _owners(db_session)
    real = parity.raw_counters

    def skewed(db, ids, **kwargs):
        result = real(db, ids, **kwargs)
        if first.id in result:
            counters = result[first.id]
            result[first.id] = type(counters)(
                **{**counters.__dict__, "event_count": counters.event_count + 1})
        return result

    monkeypatch.setattr(parity, "raw_counters", skewed)
    code, out = _run(db_session)

    assert code == 1
    # The first owner's blunder disagrees under all three of its comparisons.
    assert "mismatches=3 (" in out
    assert "event_count=3" in out and "targeted_30d=0" in out


@pytest.mark.parametrize("signal", ["summary", "prefix", "anchor", "freeze"])
def test_freezing_or_anything_folded_refuses_before_comparing(
    db_session, monkeypatch, signal,
):
    first, _ = _owners(db_session)
    if signal == "freeze":
        policy = db_session.get(OpportunityRetentionPolicy, 1)
        policy.readiness = True
        policy.freeze_enabled = True
    elif signal == "summary":
        db_session.add(BlunderOpportunitySummary(
            blunder_id=first.id, folded_eligible_count=1))
    elif signal == "prefix":
        db_session.add(UserOpportunityRetentionState(
            user_id=501, folded_through_started_at=datetime.now(timezone.utc)))
    else:
        db_session.get(OpportunityRetentionPolicy, 1).first_fold_committed_at = (
            datetime.now(timezone.utc))
    db_session.commit()
    monkeypatch.setattr(parity, "compare", pytest.fail)

    code, out = _run(db_session)

    assert code == 2
    assert "refused" in out


@pg_required
def test_pg_comparison_runs_in_one_read_only_repeatable_read_snapshot(
    pg_session_factory, monkeypatch, tmp_path,
):
    monkeypatch.setenv("GHOSTREPLAY_SRS_FOLD_EXPORT_DIR", str(tmp_path))
    _seed(pg_session_factory)
    with pg_session_factory() as db:
        # _seed switches folding fully on; this runs before any of that.
        policy = db.get(OpportunityRetentionPolicy, 1)
        policy.cleanup_enabled = False
        db.flush()
        policy.freeze_enabled = False
        db.flush()
        policy.readiness = False
        db.commit()
    real_compare = parity.compare
    seen = {}

    def observed(db, *, now):
        for setting in ("transaction_isolation", "transaction_read_only",
                        "statement_timeout"):
            seen[setting] = db.scalar(parity.text(f"SHOW {setting}"))
        seen["now"] = now
        return real_compare(db, now=now)

    monkeypatch.setattr(parity, "compare", observed)
    with pg_session_factory() as db:
        code, out = _run(db)

    assert code == 0, out
    assert "owners=1 blunders=1" in out and "mismatches=0" in out
    assert seen["transaction_isolation"] == "repeatable read"
    assert seen["transaction_read_only"] == "on"
    assert seen["statement_timeout"] == "30s"
    assert seen["now"].tzinfo is not None
