"""Expansion, coverage and real PostgreSQL snapshot/concurrent-writer proofs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, delete, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from conftest import await_pg_lock, pg_required
from app.models import GameSession, OpponentDecision, OpponentTargetFact, User
from app.opponent_target_facts import backfill_target_facts
from scripts.backfill_opponent_target_facts import compare_target_facts, run
from test_srs_opportunity import _targeted_only_setup, _decision

REVISION = "20260919_01"
PREVIOUS = "20260919_02"


def _config():
    backend = Path(__file__).parent
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    return cfg


def _assert_schema(conn):
    schema = inspect(conn)
    table = OpponentTargetFact.__table__
    columns = schema.get_columns(table.name)
    assert {c["name"] for c in columns} == set(table.columns.keys())
    assert all(not c["nullable"] for c in columns)
    assert schema.get_pk_constraint(table.name)["constrained_columns"] == ["session_id", "blunder_id"]
    assert {i["name"]: i["column_names"] for i in schema.get_indexes(table.name)} == {
        i.name: [c.name for c in i.columns] for i in table.indexes
    }
    assert {tuple(fk["constrained_columns"]):
            (fk["referred_table"], fk["options"].get("ondelete"))
            for fk in schema.get_foreign_keys(table.name)} == {
        ("session_id",): ("game_sessions", "CASCADE"),
        ("blunder_id",): ("blunders", None),
    }
    deadline = next(c for c in schema.get_columns("game_sessions")
                    if c["name"] == "opponent_decisions_expires_at")
    assert deadline["nullable"] and deadline["default"] is None
    assert GameSession.__table__.c.opponent_decisions_expires_at.nullable
    assert not any("opponent_decisions_expires_at" in i["column_names"]
                   for i in schema.get_indexes("game_sessions"))
    if conn.dialect.name == "postgresql":
        assert deadline["type"].timezone
        assert next(c for c in columns if c["name"] == "last_served_at")["type"].timezone


def test_fact_model_matches_handwritten_fixture(db_session):
    _assert_schema(db_session.connection())


def test_sqlite_fact_expansion_populated_upgrade(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'facts.db'}"
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE game_sessions (id TEXT PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE blunders (id INTEGER PRIMARY KEY)"))
            conn.execute(text("INSERT INTO game_sessions VALUES ('old-session')"))
        monkeypatch.setenv("DATABASE_URL", url)
        cfg = _config()
        command.stamp(cfg, PREVIOUS)
        command.upgrade(cfg, REVISION)
        with engine.connect() as conn:
            _assert_schema(conn)
            assert conn.execute(text("SELECT opponent_decisions_expires_at FROM game_sessions")).one() == (None,)
        command.downgrade(cfg, PREVIOUS)
        with engine.connect() as conn:
            assert "opponent_target_facts" not in inspect(conn).get_table_names()
            assert conn.execute(text("SELECT id FROM game_sessions")).scalar_one() == "old-session"
    finally:
        engine.dispose()


@pg_required
def test_pg_fact_expansion_populated_upgrade(pg_migration_db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_migration_db)
    cfg = _config()
    command.upgrade(cfg, PREVIOUS)
    engine = create_engine(pg_migration_db)
    try:
        # Pre-expansion model inserts cannot mention the future nullable column.
        with engine.begin() as conn:
            conn.execute(User.__table__.insert().values(id=123))
            session_id = conn.execute(text(
                "INSERT INTO game_sessions (id,user_id,started_at,status,engine_elo) "
                "VALUES (gen_random_uuid(),123,now(),'active',1500) RETURNING id"
            )).scalar_one()
        with Session(engine) as db:
            from test_srs_opportunity import _position, _blunder
            pos = _position(db, user_id=123, fen="8/8/8/8/8/8/K7/4k3 w - - 0 1", active_color="white")
            target = _blunder(db, user_id=123, position=pos)
            target_id = target.id
            # A minimal duck type avoids loading the expanded GameSession model.
            from types import SimpleNamespace
            _decision(db, session=SimpleNamespace(id=session_id), blunder=target,
                      served_at=datetime.now(timezone.utc))
            db.commit()
        command.upgrade(cfg, REVISION)
        with engine.connect() as conn:
            _assert_schema(conn)
        with Session(engine) as db:
            # A migration test targets this revision, while the live model may
            # already contain columns owned by a subsequent revision.
            assert db.execute(text(
                "SELECT opponent_decisions_expires_at FROM game_sessions WHERE id=:id"
            ), {"id": session_id}).scalar_one() is None
            assert db.query(OpponentDecision).count() == 1
            assert db.query(OpponentTargetFact).count() == 0
            assert backfill_target_facts(db) == 1
            assert backfill_target_facts(db) == 0
            db.commit()
            assert compare_target_facts(db, now=datetime.now(timezone.utc)).matches
            # Fact survives independent envelope deletion; ordinary FKs still hold.
            db.execute(delete(OpponentDecision))
            db.commit()
            with pytest.raises(IntegrityError):
                db.execute(text("DELETE FROM blunders WHERE id=:id"), {"id": target_id})
            db.rollback()
            db.execute(delete(GameSession).where(GameSession.id == session_id))
            db.commit()
            assert db.query(OpponentTargetFact).count() == 0
        command.downgrade(cfg, PREVIOUS)
        with engine.connect() as conn:
            assert "opponent_target_facts" not in inspect(conn).get_table_names()
            assert "drill_route_mode" in {
                column["name"] for column in inspect(conn).get_columns("game_sessions")
            }
        command.upgrade(cfg, REVISION)
        with engine.connect() as conn:
            _assert_schema(conn)
    finally:
        engine.dispose()


def test_coverage_reports_missing_stale_extra_and_does_not_write(db_session):
    now, target, session = _targeted_only_setup(db_session)
    db_session.commit()
    before = compare_target_facts(db_session, now=now)
    assert (before.missing_pairs, before.counter_mismatches) == (1, 1)
    assert db_session.query(OpponentTargetFact).count() == 0
    assert backfill_target_facts(db_session) == 1
    assert compare_target_facts(db_session, now=now).matches
    assert backfill_target_facts(db_session) == 0
    fact = db_session.get(OpponentTargetFact, (session.id, target.id))
    fact.last_served_at = now - timedelta(days=31)
    db_session.commit()
    report = compare_target_facts(db_session, now=now)
    assert report.timestamp_mismatches == 1 and report.counter_mismatches == 1
    backfill_target_facts(db_session)
    db_session.commit()
    assert compare_target_facts(db_session, now=now).matches
    db_session.execute(delete(OpponentDecision))
    db_session.commit()
    report = compare_target_facts(db_session, now=now)
    assert report.extra_pairs == 1 and not report.matches


@pytest.mark.parametrize("deadline_offset", [-1, 1])
def test_backfill_refuses_any_initialized_deadline(db_session, deadline_offset):
    from test_srs_opportunity import _session

    now, _, _ = _targeted_only_setup(db_session)
    # Even an unrelated, untargeted session with a future deadline closes backfill.
    other = _session(db_session, user_id=123)
    other.opponent_decisions_expires_at = now + timedelta(days=deadline_offset)
    db_session.commit()
    with pytest.raises(ValueError, match="disabled after retention deadlines are initialized"):
        backfill_target_facts(db_session)
    db_session.commit()
    assert db_session.query(OpponentTargetFact).count() == 0


def test_operator_verification_requires_postgres(db_session):
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        run(db_session.get_bind(), apply=True)


@pg_required
def test_pg_backfill_command_refuses_initialized_deadlines(pg_session_factory, pg_engine):
    with pg_session_factory() as db:
        db.add(User(id=123))
        db.flush()
        now, _, session = _targeted_only_setup(db)
        sid = session.id
        db.commit()
    changed, report = run(pg_engine)
    assert changed == 0 and report.missing_pairs == 1
    changed, report = run(pg_engine, apply=True)
    assert changed == 1 and report.matches
    assert run(pg_engine, apply=True)[0] == 0
    with pg_session_factory() as db:
        db.get(GameSession, sid).opponent_decisions_expires_at = now + timedelta(days=7)
        db.execute(delete(OpponentTargetFact))
        db.commit()
    with pytest.raises(ValueError, match="disabled after retention deadlines are initialized"):
        run(pg_engine, apply=True)
    with pg_session_factory() as db:
        assert db.query(OpponentTargetFact).count() == 0
    # Read-only diagnosis remains available and cannot resurrect missing facts.
    assert run(pg_engine)[1].missing_pairs == 1


@pg_required
def test_pg_verification_uses_readonly_repeatable_snapshot(pg_session_factory, pg_engine):
    from test_opponent_decision_record import _record_target
    from test_srs_opportunity import _session

    with pg_session_factory() as db:
        db.add(User(id=123))
        db.flush()
        _, target, _ = _targeted_only_setup(db)
        second_session = _session(db, user_id=123)
        sid, bid = second_session.id, target.id
        backfill_target_facts(db)
        db.commit()
    observed = []

    def publish_after_snapshot(conn, cursor, statement, parameters, context, executemany):
        if observed or not statement.startswith("SELECT") or "opponent_decisions" not in statement:
            return
        observed.append(True)
        assert conn.get_isolation_level() == "REPEATABLE READ"
        assert conn.scalar(text("SHOW transaction_read_only")) == "on"
        with pg_session_factory() as writer:
            _record_target(writer, sid, bid)

    event.listen(pg_engine, "after_cursor_execute", publish_after_snapshot)
    try:
        changed, report = run(pg_engine)
    finally:
        event.remove(pg_engine, "after_cursor_execute", publish_after_snapshot)
    assert observed and changed == 0 and report.matches
    assert report.decision_pairs == report.fact_pairs == 1
    _, later = run(pg_engine)
    assert later.matches and later.decision_pairs == later.fact_pairs == 2


@pg_required
def test_pg_backfill_cannot_overwrite_concurrent_winning_fact(pg_session_factory, pg_engine):
    from test_opponent_decision_record import _record_target
    with pg_session_factory() as db:
        db.add(User(id=123))
        db.flush()
        now, target, session = _targeted_only_setup(db)
        db.commit()
        sid, bid = session.id, target.id
        assert backfill_target_facts(db) == 1
        db.commit()
    # Hold the live publication just before commit, with the newer fact locked.
    published, finish = threading.Event(), threading.Event()
    original_commit = Session.commit

    def live():
        with pg_session_factory() as db:
            def held_commit():
                published.set()
                assert finish.wait(10)
                original_commit(db)
            # This Session is transaction-local, never a long-lived singleton.
            db.commit = held_commit
            return _record_target(db, sid, bid, "concurrent-live")

    pid_ready = threading.Event()
    worker_pid = []

    def backfill():
        with pg_session_factory() as db:
            worker_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
            pid_ready.set()
            changed = backfill_target_facts(db)
            db.commit()
            return changed

    with ThreadPoolExecutor(max_workers=2) as pool:
        live_future = pool.submit(live)
        try:
            assert published.wait(10)
            fill_future = pool.submit(backfill)
            assert pid_ready.wait(10)
            with pg_engine.connect() as observer:
                assert await_pg_lock(observer, worker_pid[0]), "worker never reached its lock barrier"
        finally:
            finish.set()
        response, replayed = live_future.result(timeout=10)
        assert not replayed
        assert fill_future.result(timeout=10) == 0
    with pg_session_factory() as db:
        fact = db.get(OpponentTargetFact, (sid, bid))
        envelope = db.get(OpponentDecision, response.decision_id)
        assert fact.last_served_at == envelope.served_at
        assert fact.last_served_at > now - timedelta(hours=1)
        assert backfill_target_facts(db) == 0
        db.commit()
        assert compare_target_facts(db, now=now).matches


@pg_required
def test_pg_target_fact_reached_join_has_one_snapshot(pg_session_factory, pg_engine, monkeypatch):
    from app.models import BlunderOpportunityEvent
    from app.srs_opportunity import _load_targeted_counters

    monkeypatch.setenv("OPPONENT_TARGET_SOURCE", "facts")
    with pg_session_factory() as db:
        db.add(User(id=123))
        db.flush()
        now, target, session = _targeted_only_setup(db)
        db.add(BlunderOpportunityEvent(session_id=session.id, blunder_id=target.id,
                                      opportunity=True, reached=True, occurred_at=now))
        db.flush()
        backfill_target_facts(db)
        db.commit()
        bid = target.id
    key = 59318923
    statements = []
    worker_pid = []
    ready = threading.Event()

    def reader():
        with pg_session_factory() as db:
            conn = db.connection()
            worker_pid.append(db.scalar(text("SELECT pg_backend_pid()")))

            def barrier(conn, cursor, statement, parameters, context, executemany):
                if "opponent_target_facts" in statement:
                    statements.append(statement)
                    assert "blunder_opportunity_events" in statement
                    # The real counter SELECT takes its snapshot, then waits in
                    # the server before scanning its fact/reached inputs.
                    statement = (
                        f"WITH barrier AS MATERIALIZED (SELECT pg_advisory_xact_lock({key})) "
                        + statement.replace("FROM (SELECT", "FROM barrier, (SELECT", 1)
                    )
                return statement, parameters

            event.listen(conn, "before_cursor_execute", barrier, retval=True)
            ready.set()
            return _load_targeted_counters(db, [bid], user_id=123,
                cutoff=now - timedelta(days=30), exclude_session_id=None)

    with pg_engine.connect() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        future = pool.submit(reader)
        try:
            assert ready.wait(10)
            with pg_engine.connect() as observer:
                assert await_pg_lock(observer, worker_pid[0]), "worker never reached its lock barrier"
            with pg_engine.begin() as writer:
                writer.execute(delete(OpponentTargetFact))
                writer.execute(delete(BlunderOpportunityEvent))
        finally:
            blocker.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        assert future.result(timeout=10) == {bid: (1, 1)}
    assert len(statements) == 1
    with pg_session_factory() as db:
        assert _load_targeted_counters(db, [bid], user_id=123,
            cutoff=now - timedelta(days=30), exclude_session_id=None) == {}
