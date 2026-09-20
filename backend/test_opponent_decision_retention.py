"""Bounded cleanup of expired envelopes and stale facts (g-decision-cleanup).

The sweep is driven by the rows that are still there, never by the history of
expired sessions, and it is default-off. These cases pin that queue shape, the
exact retention margins, the finite budgets and the restart behaviour; the
PostgreSQL cases pin what only a real database can show — SKIP LOCKED, the
statement clock advancing inside a transaction, and that no parent session is
ever locked. The plan cases EXPLAIN the sweep's own statement builders, never a
hand-written lookalike: a plan recorded for a query that is not the one running
is worse evidence than no plan at all.

Envelope-deletion races against live replay/proof consumers already have a home
in test_opponent_session_expiry.py and are reused here rather than rebuilt.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import threading
import time
import uuid

import pytest
from sqlalchemy import DateTime, create_engine, insert, literal, select, text
from sqlalchemy.orm import Session

from app import opponent_cleanup as cleanup
from app.models import GameSession, OpponentDecision, OpponentTargetFact, User
from app.srs_math import as_utc
from conftest import TestingSessionLocal, pg_required
from scripts.retain_opponent_decisions import REFUSED, report_json, run

NOW = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
# The two instants the sweep actually compares against. Pinning them is what
# makes "retain at exact equality" a testable statement at all: with a live clock
# the margin edge has moved by the time the batch runs.
ENVELOPE_CUTOFF = NOW - cleanup.DELETION_MARGIN
FACT_CUTOFF = NOW - cleanup.FACT_WINDOW - cleanup.DELETION_MARGIN
TICK = timedelta(microseconds=1)


@pytest.fixture
def activated(monkeypatch):
    """The full deletion authorization the rollout records before first pruning."""
    monkeypatch.setenv("OPPONENT_DECISION_RETENTION_ENABLED", "1")
    monkeypatch.setenv("OPPONENT_DECISION_RETENTION_SECONDS", "604800")
    monkeypatch.setenv("OPPONENT_TARGET_SOURCE", "facts")
    monkeypatch.setenv(cleanup.CLEANUP_ENABLED_ENV, "1")
    monkeypatch.setenv(cleanup.CLEANUP_NOT_BEFORE_ENV, (NOW - timedelta(days=1)).isoformat())


@pytest.fixture
def frozen(monkeypatch):
    """Pin the cutoffs; the clock arithmetic behind them is pinned separately."""
    at = {"envelope": ENVELOPE_CUTOFF, "fact": FACT_CUTOFF, "now": NOW}

    def stamp(key):
        return lambda db: literal(at[key], type_=DateTime(timezone=True))

    monkeypatch.setattr(cleanup, "envelope_cutoff", stamp("envelope"))
    monkeypatch.setattr(cleanup, "fact_cutoff", stamp("fact"))
    monkeypatch.setattr(cleanup, "database_now", stamp("now"))
    return at


@pytest.fixture
def factory(db_session):
    """Hand the sweep its own sessions; it owns a transaction per batch."""
    db_session.commit()
    return TestingSessionLocal


def _session(db, *, deadline, user_id=123, session_id=None) -> GameSession:
    if db.get(User, user_id) is None:
        db.add(User(id=user_id))
        db.flush()
    session = GameSession(
        id=session_id or uuid.uuid4(), user_id=user_id, started_at=NOW - timedelta(days=30),
        status="active", engine_elo=1500, player_color="white",
        opponent_decisions_expires_at=deadline,
    )
    db.add(session)
    db.flush()
    return session


def _envelope(db, session, *, payload="{}", decision_id=None) -> OpponentDecision:
    decision = OpponentDecision(
        decision_id=decision_id or uuid.uuid4(), session_id=session.id,
        request_fingerprint=uuid.uuid4().hex, request_fen_hash="hash", uci_history="[]",
        ply_before=0, served_at=NOW - timedelta(days=20), response_payload=payload,
    )
    db.add(decision)
    db.flush()
    return decision


def _fact(db, session, *, blunder_id, last_served_at) -> OpponentTargetFact:
    fact = OpponentTargetFact(
        session_id=session.id, blunder_id=blunder_id, last_served_at=last_served_at,
    )
    db.add(fact)
    db.flush()
    return fact


def _blunder_id(db, *, user_id=123) -> int:
    from test_srs_opportunity import _blunder, _position

    position = _position(db, user_id=user_id, fen="8/8/8/8/8/8/K7/4k3 w - - 0 1", active_color="white")
    return _blunder(db, user_id=user_id, position=position).id


# ---------------------------------------------------------------------------
# Default-off: the switches, not a zero-row report, are what keep rows alive.
# ---------------------------------------------------------------------------


def test_the_default_sweep_reports_without_deleting(db_session, factory, frozen):
    session = _session(db_session, deadline=ENVELOPE_CUTOFF - timedelta(days=1))
    _envelope(db_session, session, payload="x" * 64)
    _fact(db_session, session, blunder_id=_blunder_id(db_session), last_served_at=FACT_CUTOFF - TICK)
    db_session.commit()

    report = cleanup.sweep(factory)

    assert not report.applied
    # A dry run reports the rows a real run would have taken, not an estimate.
    assert (report.envelopes_deleted, report.facts_deleted) == (1, 1)
    assert report.envelope_bytes_deleted >= 64
    assert db_session.query(OpponentDecision).count() == 1
    assert db_session.query(OpponentTargetFact).count() == 1
    # And the backlog it leaves behind is the same work, still waiting.
    assert (report.eligible_envelopes, report.eligible_facts) == (1, 1)


@pytest.mark.parametrize("missing,match", [
    ("OPPONENT_DECISION_RETENTION_ENABLED", "retention policy is disabled"),
    # Deleting envelopes while the counters still read them silently removes
    # attempts from the targeted_30d denominator, so the reader switch is an
    # activation control like the others, not just a runbook step.
    ("OPPONENT_TARGET_SOURCE", "must be facts before pruning"),
    (cleanup.CLEANUP_ENABLED_ENV, f"{cleanup.CLEANUP_ENABLED_ENV} is not 1"),
    (cleanup.CLEANUP_NOT_BEFORE_ENV, "activation\\+7d"),
])
def test_deleting_is_refused_without_every_activation_control(
    db_session, factory, frozen, activated, monkeypatch, missing, match,
):
    session = _session(db_session, deadline=ENVELOPE_CUTOFF - timedelta(days=1))
    _envelope(db_session, session)
    db_session.commit()
    monkeypatch.delenv(missing)

    with pytest.raises(cleanup.CleanupRefused, match=match):
        cleanup.sweep(factory, apply=True)
    assert db_session.query(OpponentDecision).count() == 1


def test_deleting_is_refused_until_the_recorded_not_before_instant(
    db_session, factory, frozen, activated, monkeypatch,
):
    session = _session(db_session, deadline=ENVELOPE_CUTOFF - timedelta(days=1))
    _envelope(db_session, session)
    db_session.commit()
    monkeypatch.setenv(cleanup.CLEANUP_NOT_BEFORE_ENV, (NOW + TICK).isoformat())

    with pytest.raises(cleanup.CleanupRefused, match="has not been reached"):
        cleanup.sweep(factory, apply=True)
    assert db_session.query(OpponentDecision).count() == 1

    # Exactly at the recorded instant the hold is over; it is not a deadline to
    # exceed, it is the moment the seven-day interval ends.
    monkeypatch.setenv(cleanup.CLEANUP_NOT_BEFORE_ENV, NOW.isoformat())
    assert cleanup.sweep(factory, apply=True).envelopes_deleted == 1


def test_a_naive_not_before_is_rejected_rather_than_guessed(monkeypatch):
    monkeypatch.setenv(cleanup.CLEANUP_NOT_BEFORE_ENV, "2030-06-01T12:00:00")
    with pytest.raises(ValueError, match="must carry a UTC offset"):
        cleanup.cleanup_not_before()


@pytest.mark.parametrize("raw", ["", "true", "2", "yes"])
def test_an_unreadable_cleanup_switch_is_not_treated_as_on(monkeypatch, raw):
    """Same refusal shape as the retention switch: unreadable is never "off"."""
    monkeypatch.setenv(cleanup.CLEANUP_ENABLED_ENV, raw)
    with pytest.raises(ValueError, match=cleanup.CLEANUP_ENABLED_ENV):
        cleanup.cleanup_enabled()
    monkeypatch.delenv(cleanup.CLEANUP_ENABLED_ENV)
    assert cleanup.cleanup_enabled() is False


@pytest.mark.parametrize("variable,value", [
    ("OPPONENT_DECISION_RETENTION_ENABLED", "yes"),
    ("OPPONENT_TARGET_SOURCE", "factz"),
    (cleanup.CLEANUP_ENABLED_ENV, "true"),
    (cleanup.CLEANUP_NOT_BEFORE_ENV, "next tuesday"),
])
@pytest.mark.parametrize("apply", [False, True])
def test_an_unreadable_maintenance_variable_refuses_instead_of_crashing(
    factory, frozen, activated, monkeypatch, variable, value, apply,
):
    """A typo must reach the job's "refused" status, not its "alerting" one.

    Both are non-zero, and monitoring has to tell "no sweep happened, fix the
    configuration" apart from "a sweep ran and found a backlog". A traceback
    would land on the alerting status and read as the second.
    """
    monkeypatch.setenv(variable, value)
    with pytest.raises(cleanup.CleanupRefused, match=r"(ValueError|RuntimeError)"):
        cleanup.sweep(factory, apply=apply)


def test_the_sizing_harness_refuses_a_database_holding_data_before_it_drops_it(tmp_path):
    """``--reset`` drops the schema, so the refusal has to come first.

    Checked after the drop instead, a mistyped URL would be inspected only once
    it had already been emptied, and would pass. The harness seeds exactly one
    user, so "empty, or holding only that user" is the whole description of a
    database it may destroy.
    """
    from scripts.size_opponent_decision_retention import SIZING_USERNAME, _scratch_guard
    from app.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'scratch.db'}")
    Base.metadata.create_all(engine)
    _scratch_guard(engine)  # Empty: this harness may have it.
    with Session(engine) as db:
        db.add(User(id=1, username=SIZING_USERNAME))
        db.commit()
    _scratch_guard(engine)  # Only a previous run of this harness: still allowed.
    with Session(engine) as db:
        db.add(User(id=2, username="somebody-real"))
        db.commit()
    with pytest.raises(SystemExit, match="holds application data"):
        _scratch_guard(engine)


# ---------------------------------------------------------------------------
# The work queue is the remaining rows.
# ---------------------------------------------------------------------------


def test_an_emptied_expired_session_costs_nothing_on_later_runs(
    db_session, factory, frozen, activated,
):
    kept = _session(db_session, deadline=ENVELOPE_CUTOFF + TICK)
    _envelope(db_session, kept)
    expired = _session(db_session, deadline=ENVELOPE_CUTOFF - TICK)
    _envelope(db_session, expired)
    # An expired session whose envelopes are already gone: it must never appear
    # in the candidate source again, which is the whole point of sweeping rows.
    _session(db_session, deadline=ENVELOPE_CUTOFF - timedelta(days=365))
    db_session.commit()

    first = cleanup.sweep(factory, apply=True)
    assert first.sessions_scanned == 2 and first.sessions_expired == 1
    assert first.envelopes_deleted == 1

    second = cleanup.sweep(factory, apply=True)
    # Only the live session is left to look at; both expired ones are invisible.
    assert second.sessions_scanned == 1 and second.envelopes_deleted == 0
    assert db_session.query(OpponentDecision).count() == 1


def test_an_enabled_policy_with_no_deadline_alerts_instead_of_deleting(
    db_session, factory, frozen, activated, monkeypatch,
):
    session = _session(db_session, deadline=None)
    _envelope(db_session, session)
    db_session.commit()

    report = cleanup.sweep(factory, apply=True)

    assert report.missing_deadline_sessions == 1
    assert report.envelopes_deleted == 0
    assert not report.healthy
    assert report.alerts == ["sessions missing a deadline; retention policy is enabled"]
    assert db_session.query(OpponentDecision).count() == 1

    # The same rows before the rollout initializes deadlines are not an invariant
    # violation, and the alert has to read the policy rather than assert it.
    monkeypatch.setenv("OPPONENT_DECISION_RETENTION_ENABLED", "0")
    dry = cleanup.sweep(factory)
    assert dry.alerts == ["sessions missing a deadline; retention policy is disabled"]


@pytest.mark.parametrize("offset,survives", [(TICK, True), (timedelta(0), True), (-TICK, False)])
def test_envelopes_survive_the_exact_r_plus_margin_edge(
    db_session, factory, frozen, activated, offset, survives,
):
    session = _session(db_session, deadline=ENVELOPE_CUTOFF + offset)
    _envelope(db_session, session)
    db_session.commit()

    cleanup.sweep(factory, apply=True)

    assert bool(db_session.query(OpponentDecision).count()) is survives


@pytest.mark.parametrize("offset,survives", [(TICK, True), (timedelta(0), True), (-TICK, False)])
def test_facts_survive_the_exact_thirty_day_plus_margin_edge(
    db_session, factory, frozen, activated, offset, survives,
):
    session = _session(db_session, deadline=ENVELOPE_CUTOFF - timedelta(days=1))
    _fact(db_session, session, blunder_id=_blunder_id(db_session), last_served_at=FACT_CUTOFF + offset)
    db_session.commit()

    cleanup.sweep(factory, apply=True)

    assert bool(db_session.query(OpponentTargetFact).count()) is survives


def test_a_fact_outlives_its_own_envelopes_by_the_counter_window(
    db_session, factory, frozen, activated,
):
    session = _session(db_session, deadline=ENVELOPE_CUTOFF - timedelta(days=1))
    _envelope(db_session, session)
    _fact(db_session, session, blunder_id=_blunder_id(db_session), last_served_at=NOW - timedelta(days=1))
    db_session.commit()

    report = cleanup.sweep(factory, apply=True)

    assert report.envelopes_deleted == 1 and report.facts_deleted == 0
    assert db_session.query(OpponentTargetFact).count() == 1


def test_cleanup_never_resurrects_a_fact_from_the_envelope_it_deletes(
    db_session, factory, frozen, activated,
):
    """Restoring facts from old envelopes would defeat the fact TTL entirely."""
    session = _session(db_session, deadline=ENVELOPE_CUTOFF - timedelta(days=1))
    blunder_id = _blunder_id(db_session)
    envelope = _envelope(db_session, session)
    envelope.target_blunder_id = blunder_id
    db_session.commit()

    cleanup.sweep(factory, apply=True)

    assert db_session.query(OpponentDecision).count() == 0
    assert db_session.query(OpponentTargetFact).count() == 0


def test_a_fact_the_live_counter_still_needs_is_never_deleted(db_session, factory, frozen, activated):
    """The margin covers the app/database clock difference at the counter edge.

    ``load_opportunity_counters`` subtracts thirty days from the APPLICATION's
    clock. With that clock trailing the database's by any supported skew S < D,
    the oldest pair it still counts is younger than the fact cutoff, so cleanup
    cannot remove a row the counter is about to read.
    """
    from app.srs_opportunity import targeted_counters_query

    skew = cleanup.DELETION_MARGIN - timedelta(minutes=1)
    app_now = NOW - skew
    counter_cutoff = app_now - cleanup.FACT_WINDOW
    session = _session(db_session, deadline=ENVELOPE_CUTOFF - timedelta(days=1))
    blunder_id = _blunder_id(db_session)
    _fact(db_session, session, blunder_id=blunder_id, last_served_at=counter_cutoff)
    db_session.commit()

    counted = targeted_counters_query(
        db_session, cutoff=counter_cutoff, source="facts", blunder_ids=[blunder_id], user_id=123,
    ).all()
    assert [row.targeted_30d for row in counted] == [1]

    cleanup.sweep(factory, apply=True)
    assert db_session.query(OpponentTargetFact).count() == 1


# ---------------------------------------------------------------------------
# Finite budgets, keysets and restarts.
# ---------------------------------------------------------------------------


def test_a_run_stops_at_its_row_budget_and_the_next_run_resumes(
    db_session, factory, frozen, activated,
):
    session = _session(db_session, deadline=ENVELOPE_CUTOFF - TICK)
    for _ in range(5):
        _envelope(db_session, session)
    db_session.commit()

    first = cleanup.sweep(factory, apply=True, batch_rows=2, run_rows=4)
    assert first.envelopes_deleted == 4 and first.budget_exhausted
    assert db_session.query(OpponentDecision).count() == 1

    # No durable cursor: the remaining row is found again because it is still there.
    second = cleanup.sweep(factory, apply=True, batch_rows=2, run_rows=4)
    assert second.envelopes_deleted == 1 and not second.budget_exhausted
    assert db_session.query(OpponentDecision).count() == 0


def test_a_run_stops_at_its_time_budget_without_losing_finished_batches(
    db_session, factory, frozen, activated,
):
    session = _session(db_session, deadline=ENVELOPE_CUTOFF - TICK)
    for _ in range(6):
        _envelope(db_session, session)
    db_session.commit()
    ticks = iter([0.0, 0.0, 0.0, 0.0, 5.0, 5.0, 5.0, 5.0])

    report = cleanup.sweep(
        factory, apply=True, batch_rows=2, run_seconds=1.0, clock=lambda: next(ticks, 9.0),
    )

    assert report.budget_exhausted
    assert 0 < report.envelopes_deleted < 6
    # Whatever the clock did, committed batches stay committed.
    assert db_session.query(OpponentDecision).count() == 6 - report.envelopes_deleted


def test_a_batch_is_bounded_by_payload_bytes_as_well_as_rows(
    db_session, factory, frozen, activated,
):
    session = _session(db_session, deadline=ENVELOPE_CUTOFF - TICK)
    for _ in range(4):
        _envelope(db_session, session, payload="x" * 100)
    db_session.commit()

    report = cleanup.sweep(factory, apply=True, batch_rows=4, batch_bytes=250)

    assert report.envelopes_deleted == 4
    # 250 bytes admits two 100-byte payloads, so four rows took two batches even
    # though the row limit on its own would have taken them in one.
    assert report.batches == 2
    assert db_session.query(OpponentDecision).count() == 0


def test_one_oversized_payload_cannot_stall_the_sweep(db_session, factory, frozen, activated):
    session = _session(db_session, deadline=ENVELOPE_CUTOFF - TICK)
    _envelope(db_session, session, payload="x" * 5000)
    _envelope(db_session, session, payload="x" * 5000)
    db_session.commit()

    report = cleanup.sweep(factory, apply=True, batch_rows=10, batch_bytes=10)

    assert report.envelopes_deleted == 2
    assert db_session.query(OpponentDecision).count() == 0


def test_rows_inserted_behind_the_cursor_are_left_for_the_next_run(
    db_session, factory, frozen, activated, monkeypatch,
):
    """decision_id is a random UUID, so a new row can land behind the cursor."""
    session = _session(db_session, deadline=ENVELOPE_CUTOFF - TICK)
    low = uuid.UUID(int=1)
    _envelope(db_session, session, decision_id=uuid.UUID(int=2))
    _envelope(db_session, session, decision_id=uuid.UUID(int=3))
    db_session.commit()
    inserted = []
    original = cleanup._envelope_batch

    def insert_behind_the_cursor(db, session_id, cursor, **kwargs):
        result = original(db, session_id, cursor, **kwargs)
        if not inserted:
            inserted.append(low)
            db.add(OpponentDecision(
                decision_id=low, session_id=session_id, request_fingerprint=uuid.uuid4().hex,
                request_fen_hash="hash", uci_history="[]", ply_before=0,
                served_at=NOW, response_payload="{}",
            ))
        return result

    monkeypatch.setattr(cleanup, "_envelope_batch", insert_behind_the_cursor)
    first = cleanup.sweep(factory, apply=True, batch_rows=1)

    assert first.envelopes_deleted == 2
    assert [row.decision_id for row in db_session.query(OpponentDecision).all()] == [low]
    monkeypatch.setattr(cleanup, "_envelope_batch", original)
    assert cleanup.sweep(factory, apply=True).envelopes_deleted == 1


def test_sessions_are_paged_by_keyset_not_offset(db_session, factory, frozen, activated, monkeypatch):
    for index in range(5):
        session = _session(db_session, deadline=ENVELOPE_CUTOFF - TICK, session_id=uuid.UUID(int=index + 1))
        _envelope(db_session, session)
    db_session.commit()
    pages = []
    original = cleanup._candidate_sessions

    def record(db, cursor, page):
        rows = original(db, cursor, page)
        pages.append((cursor, [row[0] for row in rows]))
        return rows

    monkeypatch.setattr(cleanup, "_candidate_sessions", record)
    report = cleanup.sweep(factory, apply=True, session_page=2)

    assert report.envelopes_deleted == 5
    assert [cursor for cursor, _ in pages] == [
        None, uuid.UUID(int=2), uuid.UUID(int=4), uuid.UUID(int=5),
    ]


def test_the_backlog_report_alerts_when_the_oldest_overdue_row_outlives_the_hour(
    db_session, factory, frozen, activated,
):
    session = _session(db_session, deadline=NOW - timedelta(hours=40))
    _envelope(db_session, session)
    db_session.commit()

    # A dry run is exactly how the operator reads the backlog before enabling.
    report = cleanup.sweep(factory)

    assert report.oldest_overdue_expiry == NOW - timedelta(hours=40)
    assert report.lag_seconds == pytest.approx(40 * 3600)
    assert report.alerts == ["backlog lag exceeds the healthy hourly window"]
    assert not report.healthy


def test_the_report_carries_aggregates_only(db_session, factory, frozen, activated):
    session = _session(db_session, deadline=ENVELOPE_CUTOFF - TICK)
    _envelope(db_session, session, payload='{"secret": "payload"}')
    _fact(db_session, session, blunder_id=_blunder_id(db_session), last_served_at=FACT_CUTOFF - TICK)
    db_session.commit()

    serialized = report_json(cleanup.sweep(factory))

    assert "secret" not in serialized and str(session.id) not in serialized
    assert json.loads(serialized)["envelopes_deleted"] == 1


# ---------------------------------------------------------------------------
# The margin arithmetic behind the pinned cutoffs, and the operator entrypoint.
# ---------------------------------------------------------------------------


def test_the_cutoffs_trail_the_database_clock_by_their_margins(db_session):
    now, envelope, fact = db_session.execute(select(
        cleanup.database_now(db_session), cleanup.envelope_cutoff(db_session),
        cleanup.fact_cutoff(db_session),
    )).one()
    now, envelope, fact = (as_utc(value) for value in (now, envelope, fact))

    assert abs((now - envelope) - cleanup.DELETION_MARGIN) < timedelta(seconds=2)
    assert abs((now - fact) - (cleanup.FACT_WINDOW + cleanup.DELETION_MARGIN)) < timedelta(seconds=2)


def test_the_operator_command_requires_postgresql():
    from sqlalchemy import create_engine

    with pytest.raises(cleanup.CleanupRefused, match="requires PostgreSQL"):
        run(create_engine("sqlite://"))


def test_the_operator_command_reports_refusal_on_stderr(monkeypatch, capsys):
    from scripts import retain_opponent_decisions as command

    monkeypatch.setattr(command.sys, "argv", ["retain_opponent_decisions.py", "--apply"])
    monkeypatch.setattr(command, "run", lambda *a, **k: (_ for _ in ()).throw(
        cleanup.CleanupRefused("switch is off")))
    assert command.main() == REFUSED
    captured = capsys.readouterr()
    assert json.loads(captured.err)["refused"] == "switch is off"
    assert captured.out == ""


# ---------------------------------------------------------------------------
# PostgreSQL: the locking, the statement clock and the plans. SQLite compiles
# FOR UPDATE / SKIP LOCKED away, so none of this is observable there.
# ---------------------------------------------------------------------------


def _pg_seed(factory, *, deadline, envelopes=1, payload="{}"):
    with factory() as db:
        session = _session(db, deadline=deadline)
        for _ in range(envelopes):
            _envelope(db, session, payload=payload)
        db.commit()
        return session.id


@pg_required
def test_pg_a_locked_envelope_is_skipped_and_taken_on_the_next_run(
    pg_session_factory, frozen, activated,
):
    session_id = _pg_seed(pg_session_factory, deadline=ENVELOPE_CUTOFF - TICK, envelopes=3)
    with pg_session_factory() as holder:
        held = holder.execute(
            select(OpponentDecision.decision_id)
            .where(OpponentDecision.session_id == session_id)
            .order_by(OpponentDecision.decision_id).limit(1).with_for_update()
        ).scalar_one()
        # A live request holding one envelope must never make the job wait.
        first = cleanup.sweep(pg_session_factory, apply=True, run_seconds=10)
        holder.rollback()

    assert first.envelopes_deleted == 2
    with pg_session_factory() as db:
        assert db.scalars(select(OpponentDecision.decision_id)).all() == [held]
    assert cleanup.sweep(pg_session_factory, apply=True).envelopes_deleted == 1


@pg_required
def test_pg_cleanup_never_locks_the_parent_session(pg_session_factory, pg_engine, frozen, activated):
    """A maintenance job that locked game_sessions would queue live traffic."""
    session_id = _pg_seed(pg_session_factory, deadline=ENVELOPE_CUTOFF - TICK, envelopes=2)
    entered, release = threading.Event(), threading.Event()
    original = cleanup._envelope_batch

    def paused(db, *args, **kwargs):
        result = original(db, *args, **kwargs)
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        import unittest.mock

        with unittest.mock.patch.object(cleanup, "_envelope_batch", paused):
            sweeping = pool.submit(cleanup.sweep, pg_session_factory, apply=True, batch_rows=1)
            try:
                assert entered.wait(10)
                # Mid-batch, with envelope rows locked, the parent row is free.
                with pg_session_factory() as rival:
                    rival.execute(text("SET LOCAL lock_timeout = '3s'"))
                    assert rival.execute(
                        select(GameSession.id).where(GameSession.id == session_id).with_for_update()
                    ).scalar_one() == session_id
                    rival.rollback()
            finally:
                release.set()
            report = sweeping.result(timeout=20)
    assert report.envelopes_deleted == 2


@pg_required
def test_pg_deletion_authorizes_on_the_statement_clock_not_transaction_start(
    pg_session_factory, activated,
):
    """A row that becomes eligible DURING the transaction must still be taken.

    The deadline is placed just past the cutoff that transaction-start ``now()``
    implies, so the two clocks disagree for the whole life of this transaction:
    ``now()`` never reaches it and retains the row forever, ``clock_timestamp()``
    reaches it once the margin elapses. Nothing else here differs between them,
    which is what makes the assertions below discriminating rather than
    decorative — swap the clock and the second one fails.
    """
    session_id = _pg_seed(pg_session_factory, deadline=NOW + timedelta(days=3650))
    grace = timedelta(milliseconds=400)
    with pg_session_factory() as db:
        transaction_start = as_utc(db.scalar(text("SELECT now()")))
        db.execute(
            text("UPDATE game_sessions SET opponent_decisions_expires_at = :deadline "
                 "WHERE id = :sid"),
            {"deadline": transaction_start - cleanup.DELETION_MARGIN + grace,
             "sid": session_id},
        )
        # Not yet eligible under either clock.
        _, examined, removed, _ = cleanup._envelope_batch(
            db, session_id, None, rows=10, max_bytes=1 << 20, apply=True,
        )
        assert (examined, removed) == (0, 0)

        time.sleep(grace.total_seconds() * 2)
        # Transaction start has not moved, and is the reading a now() batch would
        # still be using; only the statement clock has passed the deadline.
        assert as_utc(db.scalar(text("SELECT now()"))) == transaction_start
        _, examined, removed, _ = cleanup._envelope_batch(
            db, session_id, None, rows=10, max_bytes=1 << 20, apply=True,
        )
        assert (examined, removed) == (1, 1)
        db.commit()
    with pg_session_factory() as db:
        assert db.query(OpponentDecision).count() == 0


@pg_required
@pytest.mark.parametrize(
    "order", ["committed_upsert_first", "uncommitted_upsert_during", "delete_first"],
)
def test_pg_an_advancing_fact_upsert_survives_the_deleting_batch(
    pg_session_factory, pg_engine, frozen, activated, order,
):
    """Three interleavings, under real row locks rather than a mocked sequence.

    ``uncommitted_upsert_during`` is the one that needs both connections open at
    once: the winning decision holds the fact row lock and has NOT committed when
    the batch arrives. SKIP LOCKED has to turn that into a skip and never a wait
    — a maintenance job blocking there would be holding up a live request — and
    the advanced fact has to be intact once the writer commits.
    """
    from app.opponent_target_facts import publish_target_fact

    with pg_session_factory() as db:
        session = _session(db, deadline=ENVELOPE_CUTOFF - TICK)
        blunder_id = _blunder_id(db)
        _fact(db, session, blunder_id=blunder_id, last_served_at=FACT_CUTOFF - timedelta(days=1))
        db.commit()
        session_id = session.id

    def advance(holding=None, release=None):
        with pg_session_factory() as writer:
            writer.execute(text("SET LOCAL lock_timeout = '10s'"))
            publish_target_fact(writer, session_id=session_id, blunder_id=blunder_id, served_at=NOW)
            if holding is not None:
                holding.set()
                assert release.wait(10)
            writer.commit()

    def advanced_to_now(db):
        return as_utc(db.get(OpponentTargetFact, (session_id, blunder_id)).last_served_at) == NOW

    with ThreadPoolExecutor(max_workers=1) as pool:
        if order == "delete_first":
            with pg_session_factory() as db:
                _, examined, removed = cleanup._fact_batch(db, None, rows=10, apply=True)
                assert (examined, removed) == (1, 1)
                db.commit()
            pool.submit(advance).result(timeout=20)
            with pg_session_factory() as db:
                # A genuinely new decision may reinsert; a replay never reaches here.
                assert advanced_to_now(db)
        elif order == "committed_upsert_first":
            pool.submit(advance).result(timeout=20)
            with pg_session_factory() as db:
                _, examined, removed = cleanup._fact_batch(db, None, rows=10, apply=True)
                # The row no longer qualifies, so it is never even returned.
                assert (examined, removed) == (0, 0)
                db.commit()
            with pg_session_factory() as db:
                assert advanced_to_now(db)
        else:
            holding, release = threading.Event(), threading.Event()
            writing = pool.submit(advance, holding, release)
            try:
                assert holding.wait(10)
                with pg_session_factory() as db:
                    # Had the batch waited on the held row instead of skipping it,
                    # this timeout is what turns that into a failure.
                    db.execute(text("SET LOCAL lock_timeout = '2s'"))
                    started = time.monotonic()
                    _, examined, removed = cleanup._fact_batch(db, None, rows=10, apply=True)
                    assert time.monotonic() - started < 2
                    assert (examined, removed) == (0, 0)
                    db.commit()
            finally:
                release.set()
            writing.result(timeout=20)
            with pg_session_factory() as db:
                assert advanced_to_now(db)


@pg_required
def test_pg_targeted_counters_keep_one_snapshot_while_cleanup_deletes(
    pg_session_factory, pg_engine, frozen, activated,
):
    """The counter statement reads facts and reach evidence at one instant.

    The reader's snapshot opens, cleanup commits a deletion, and the reader's
    single statement must still see the pair it started with.
    """
    from app.srs_opportunity import targeted_counters_query

    with pg_session_factory() as db:
        session = _session(db, deadline=ENVELOPE_CUTOFF - TICK)
        blunder_id = _blunder_id(db)
        _fact(db, session, blunder_id=blunder_id, last_served_at=FACT_CUTOFF - TICK)
        db.commit()

    with pg_session_factory() as reader:
        reader.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        reader.execute(text("SELECT 1"))  # Pin the snapshot before cleanup runs.
        assert cleanup.sweep(pg_session_factory, apply=True).facts_deleted == 1
        counted = targeted_counters_query(
            reader, cutoff=FACT_CUTOFF - timedelta(days=1), source="facts",
            blunder_ids=[blunder_id], user_id=123,
        ).all()
        assert [(row.blunder_id, row.targeted_30d) for row in counted] == [(blunder_id, 1)]
        reader.rollback()
    with pg_session_factory() as db:
        assert db.query(OpponentTargetFact).count() == 0


@pg_required
def test_pg_replay_and_drill_proof_still_fail_closed_after_pruning(
    pg_client, pg_session_factory, auth_headers, monkeypatch, activated,
):
    """Deleting already-unreadable envelopes changes no user-visible behaviour.

    The four deletion races live in test_opponent_session_expiry.py; this is the
    integrated statement that the actual cleaner, not a hand-written DELETE,
    leaves those consumers in the same fail-closed state.
    """
    from test_opponent_session_expiry import assert_expired, seed_pg
    from test_drill_root_confirmation import _decision_row, _route_check
    from test_drill_root_confirmation import E4_FEN

    sid = seed_pg(pg_session_factory, target=E4_FEN, player_color="black")
    with pg_session_factory() as db:
        decision = _decision_row(sid, ply_before=0, resulting_fen=E4_FEN)
        db.add(decision)
        db.execute(
            text("UPDATE game_sessions SET opponent_decisions_expires_at = "
                 "clock_timestamp() - interval '2 hours' WHERE id = :sid"), {"sid": sid},
        )
        db.commit()
        body = {"current_fen": E4_FEN, "current_ply": 1, "decision_id": str(decision.decision_id)}

    confirmed = _route_check(pg_client, auth_headers, str(sid), **body)
    assert_expired(confirmed)

    monkeypatch.setenv(cleanup.CLEANUP_NOT_BEFORE_ENV, "2000-01-01T00:00:00+00:00")
    report = cleanup.sweep(pg_session_factory, apply=True)
    assert report.envelopes_deleted == 1

    assert_expired(_route_check(pg_client, auth_headers, str(sid), **body))
    with pg_session_factory() as db:
        session = db.get(GameSession, sid)
        assert session.drill_state == "active" and session.drill_root_reached_ply is None


@pg_required
@pytest.mark.parametrize("shape", ["backlog", "steady_state"])
def test_pg_the_sweep_plans_never_scan_the_session_history(
    pg_session_factory, frozen, activated, shape,
):
    """Guard the one shape this design refuses: cleanup paying for old sessions.

    A bounded census-shaped fixture — 607 sessions that still own envelopes, plus
    3,000 emptied ones standing in for the history that accumulates — not a
    benchmark; scripts/size_opponent_decision_retention.py owns the byte and
    throughput evidence. What is pinned here is the access path, and it is pinned
    on the statements the sweep builds, not on hand-written lookalikes: a
    lookalike that omits the parent lookup cannot fail the assertion about it.
    """
    live = NOW + timedelta(days=30)
    sessions, envelopes = [], []

    def _row(session_id, expired):
        return {
            "id": session_id, "user_id": 123, "started_at": NOW - timedelta(days=30),
            "status": "completed", "engine_elo": 1500, "player_color": "white",
            "opponent_decisions_expires_at": (ENVELOPE_CUTOFF - TICK) if expired else live,
        }

    for index in range(607):
        expired = shape == "backlog" or index % 3 == 0
        session_id = uuid.uuid4()
        sessions.append(_row(session_id, expired))
        for ply in range(16):
            envelopes.append({
                "decision_id": uuid.uuid4(), "session_id": session_id,
                "request_fingerprint": uuid.uuid4().hex, "request_fen_hash": "hash",
                "uci_history": "[]", "ply_before": ply, "served_at": NOW - timedelta(days=20),
                "response_payload": "{}", "reaches_drill_root": False,
            })
    # Long-drained history. These own no envelopes, so the sweep must never look
    # at them — and their presence must not change any plan.
    drained = [_row(uuid.uuid4(), True) for _ in range(3_000)]

    with pg_session_factory() as db:
        db.add(User(id=123))
        db.flush()
        db.execute(insert(GameSession), sessions + drained)
        db.execute(insert(OpponentDecision), envelopes)
        db.commit()
        db.execute(text("ANALYZE opponent_decisions"))
        db.execute(text("ANALYZE game_sessions"))
        db.commit()

    def plan(build):
        with pg_session_factory() as db:
            compiled = build(db).compile(
                db.get_bind(), compile_kwargs={"literal_binds": True},
            )
            return "\n".join(
                db.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {compiled}")).scalars().all()
            )

    candidates = plan(cleanup.candidate_sessions_query)
    # The queue itself reads the replay index and nothing else, and the parent is
    # reached one primary-key probe at a time. A join here plans as a hash over
    # the whole of game_sessions, which is the cost this design exists to avoid.
    assert "uq_opponent_decisions_session_fingerprint" in candidates
    assert "game_sessions_pkey" in candidates
    assert "Seq Scan" not in candidates
    assert "Hash Join" not in candidates and "Merge Join" not in candidates

    batch = plan(lambda db: cleanup.envelope_batch_query(db, sessions[0]["id"]))
    # One session's rows by index, its parent by primary key. No new index is
    # added for this: the per-session sort is over ~16 rows.
    assert "game_sessions_pkey" in batch
    assert "Seq Scan" not in batch

    facts = plan(cleanup.fact_batch_query)
    # Facts age on their own timestamp; neither other table belongs in this plan.
    assert "opponent_decisions" not in facts and "game_sessions" not in facts

    expected = 16 * sum(1 for row in sessions
                        if row["opponent_decisions_expires_at"] != live)
    report = cleanup.sweep(pg_session_factory, apply=True)
    # 607, not 3,607: an emptied session has left the work queue for good.
    assert report.sessions_scanned == 607
    assert report.envelopes_deleted == expected
    # The whole backlog drains inside one default run budget.
    assert not report.budget_exhausted and report.eligible_envelopes == 0
