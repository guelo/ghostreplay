from datetime import datetime, timedelta
import json

import pytest
from sqlalchemy import text

from conftest import pg_gate
from app import srs_write_telemetry as telemetry
from app.api.session import _run_graph_evidence_txn
from app.models import BlunderOpportunityEvent
from scripts.census_srs_retention import BASE, census
from test_srs_opportunity import _blunder, _decision, _position, _session
from test_srs_write_telemetry import compute, private_store, seed  # noqa: F401


@pg_gate
def test_completion_bound_uses_post_commit_database_time(private_store, pg_session_factory, monkeypatch):  # noqa: F811
    with pg_session_factory() as db:
        session, _, _ = seed(db)
        compute(db, session)
        transaction_time = db.scalar(text("SELECT transaction_timestamp()"))
        db.execute(text("SELECT pg_sleep(0.03)"))
        immediately_before = db.scalar(text("SELECT clock_timestamp()"))
        db.commit()
        assert not db.in_transaction()
        observed, = private_store.rows()
        completion = datetime.fromisoformat(observed["database_at"])
        assert completion >= immediately_before > transaction_time
        assert observed["outcome"] == "committed" and observed["wrote"]
        assert db.query(BlunderOpportunityEvent).count() == 0
        compute(db, session)
        db.rollback()
        assert {row["outcome"] for row in private_store.rows()} == {"committed", "rolled_back"}

        # Pending and completion spool I/O must both be outside the actual
        # PostgreSQL advisory-lock window, not merely outside a mocked timer.
        lock_checks = []
        original_put = telemetry.PrivateStore.put
        def put(self, **fields):
            with db.get_bind().connect() as probe:
                lock_checks.append((fields["outcome"], probe.scalar(text(
                    "SELECT pg_try_advisory_xact_lock(123)"
                ))))
            original_put(self, **fields)
        monkeypatch.setattr(telemetry.PrivateStore, "put", put)
        _run_graph_evidence_txn(db, session_id=session.id, user_id=123,
                                player_color="white", evidence_moves=[], move_count=0,
                                dialect_name="postgresql")
        assert lock_checks == [("pending", True), ("committed", True)]
        assert not db.in_transaction()


@pg_gate
def test_census_original_pairs_pins_storage_and_aggregate_privacy(pg_session_factory):
    with pg_session_factory() as db:
        engine = db.get_bind()
        now = db.scalar(text("SELECT clock_timestamp()"))
        position = _position(db, user_id=123, active_color="white",
                             fen="8/8/8/8/8/8/K7/4k3 w - - 0 1")
        blunder = _blunder(db, user_id=123, position=position)
        blunder.created_at = now - timedelta(days=100)
        other_position = _position(db, user_id=123, active_color="white",
                                   fen="8/8/8/8/8/K7/8/4k3 w - - 0 1")
        late_blunder = _blunder(db, user_id=123, position=other_position)
        late_blunder.created_at = now - timedelta(days=20)
        sessions = [_session(db, user_id=123, started_at=now - timedelta(days=age))
                    for age in (70, 45, 45, 10, 45, 5, 45, 45)]
        for i, session in enumerate(sessions):
            if i == 5:
                continue
            occurred = (now - timedelta(days=40) if i == 6 else
                        now + timedelta(days=1) if i == 7 else session.started_at)
            db.add(BlunderOpportunityEvent(
                session_id=session.id, blunder_id=late_blunder.id if i == 4 else blunder.id,
                opportunity=True, reached=True, occurred_at=occurred,
            ))
        for index, target, days in [(1, blunder, 29), (1, blunder, 1), (2, blunder, 35),
                                    (4, late_blunder, 1), (5, blunder, 1)]:
            _decision(db, session=sessions[index], blunder=target,
                      served_at=now - timedelta(days=days))
        db.commit()
        identifiers = [str(session.id) for session in sessions]
    result = census(engine, candidate_days=[7])
    diagnostic = result["diagnostics"]
    assert diagnostic["rows"] == 7
    assert diagnostic["older_60d"] == 1
    assert diagnostic["timestamp_audit_required"] == 2
    assert diagnostic["broad_ineligible_targeted_reached"] == 1
    assert diagnostic["current_target_event_pairs"] == 2
    policy = next(row for row in result["policies"] if row["m_days"] == 30)
    assert policy["foldable_union"] == 2
    assert policy["solely_target_pinned_pairs"] == 2
    assert policy["retained_rows"] == 5
    assert result["repeated_decisions"]["repeated_decisions"] == 1
    assert result["fanout"][0]["targets_without_event"] == 1
    assert result["fanout"][0]["ever_targeted_pairs"] == 4
    assert result["fanout"][0]["current_targeted_pairs"] == 3
    assert len(result["projections"]) == 6
    assert all(row["total_bytes"] >= row["index_bytes"] for row in result["storage"])
    assert not result["gate_b_eligible"]
    assert not any(identifier in json.dumps(result, default=float) for identifier in identifiers)
    with engine.connect() as connection:
        # The inclusive cutoff remains pinned, unlike a decision one microsecond older.
        boundary = now + timedelta(days=29)
        pinned = connection.execute(text(BASE + "SELECT count(*) FROM events WHERE pinned"),
                                    {"as_of": boundary}).scalar_one()
        after = connection.execute(text(BASE + "SELECT count(*) FROM events WHERE pinned"),
                                   {"as_of": boundary + timedelta(microseconds=1)}).scalar_one()
        assert (pinned, after) == (2, 0)
        assert connection.scalar(text("SELECT count(*) FROM blunder_opportunity_events")) == 7


@pg_gate
def test_empty_census_and_invalid_candidates(pg_session_factory):
    with pg_session_factory() as db:
        engine = db.get_bind()
    result = census(engine)
    assert result["diagnostics"]["rows"] == 0
    assert result["policies"][0]["foldable_fraction"] is None
    assert result["projections"] == []
    for invalid in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            census(engine, candidate_days=[invalid])
