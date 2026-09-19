"""Required PostgreSQL schema, publication and lock contracts for B50."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
import time

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from conftest import pg_gate
from app.models import Base, OpeningScoreBatch
from app.opening_score_storage import (
    OPENING_SCORE_LOCK_CLASSID,
    PublicationSuperseded,
    ScoreHandle,
    ScorePayload,
    StorageFormat,
    _GROUPS,
    _recover,
    acquire_publication_lock,
    convert_pair,
    handle_is_live,
    publication_lock_key,
    publish_scores,
    read_payload,
)
from test_opening_score_storage import candidate, payload, publish


def _schema_contract(conn):
    inspector = inspect(conn)
    assert inspector.get_columns("opening_score_batches")[-1]["name"] == "storage_format"
    assert list(OpeningScoreBatch.__table__.c.keys())[-1] == "storage_format"
    for _, table, _, _ in _GROUPS:
        columns = {c["name"]: c for c in inspector.get_columns(table.name)}
        assert set(columns) == set(table.c.keys())
        assert {n: c["nullable"] for n, c in columns.items()} == {
            c.name: c.nullable for c in table.c
        }
        assert inspector.get_pk_constraint(table.name)["constrained_columns"] == [
            c.name for c in table.primary_key
        ]
        uniques = {
            tuple(c["column_names"])
            for c in inspector.get_unique_constraints(table.name)
        }
        expected = {
            tuple(c.columns.keys())
            for c in table.constraints
            if c.__class__.__name__ == "UniqueConstraint"
        }
        assert uniques == expected
        # Unique constraints provide all prefix access; no extra indexes.
        assert all(
            i.get("duplicates_constraint") for i in inspector.get_indexes(table.name)
        )
        collations = dict(
            conn.execute(
                text("""
            SELECT a.attname, c.collname FROM pg_attribute a
            JOIN pg_collation c ON c.oid = a.attcollation
            WHERE a.attrelid = to_regclass(:table) AND a.attnum > 0
        """),
                {"table": table.name},
            ).all()
        )
        for col in table.c:
            if getattr(col.type, "collation", None) == "C":
                assert collations[col.name] == "C"
    for table in ("opening_current_roots", "opening_current_positions"):
        assert conn.scalar(
            text("SELECT reloptions FROM pg_class WHERE oid=to_regclass(:t)"),
            {"t": table},
        ) == ["fillfactor=50"]


@pg_gate
def test_pg_current_schema_model_and_migration_parity(pg_engine, pg_session_factory):
    with pg_engine.begin() as conn:
        _schema_contract(conn)
        # Model-only creation in an isolated schema must have identical settings.
        conn.execute(text("CREATE SCHEMA score_model_parity"))
        conn.execute(text("SET LOCAL search_path TO score_model_parity"))
        Base.metadata.create_all(conn)
        _schema_contract(conn)
        conn.execute(text("SET LOCAL search_path TO public"))
        conn.execute(text("DROP SCHEMA score_model_parity CASCADE"))


@pg_gate
def test_pg_conversion_retirement_and_downgrade_guard(pg_migration_db, monkeypatch):
    # Exercise explicit machine-key collation under a non-C database default.
    from pg_gate_plugin import _assert_disposable, _require_maint_url_or_gate
    from sqlalchemy.engine import make_url

    name = make_url(pg_migration_db).database
    _assert_disposable(name)
    maint = create_engine(_require_maint_url_or_gate(), isolation_level="AUTOCOMMIT")
    try:
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}"'))
            conn.execute(
                text(
                    f"CREATE DATABASE \"{name}\" TEMPLATE template0 LOCALE_PROVIDER icu ICU_LOCALE 'und'"
                )
            )
    finally:
        maint.dispose()
    monkeypatch.setenv("DATABASE_URL", pg_migration_db)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "20260919_03")
    engine = create_engine(pg_migration_db)
    try:
        with engine.connect() as conn:
            _schema_contract(conn)
            assert (
                conn.scalar(
                    text(
                        "SELECT datlocprovider FROM pg_database WHERE datname=current_database()"
                    )
                )
                == "i"
            )
        with Session(engine, expire_on_commit=False) as db:
            old = publish(db, format=StorageFormat.LEGACY)
            old_handle = ScoreHandle.from_batch(old)
            db.rollback()
            converted = convert_pair(db, 123, "white", StorageFormat.CURRENT)
            assert read_payload(db, ScoreHandle.from_batch(converted)) == payload()
            assert not handle_is_live(db, old_handle)
            db.rollback()
        with pytest.raises(RuntimeError, match="reverse-convert"):
            command.downgrade(cfg, "20260919_01")
        with Session(engine, expire_on_commit=False) as db:
            # An empty current publication is still incompatible with old readers.
            empty = publish(db, ScorePayload())
            assert empty.storage_format == StorageFormat.CURRENT.value
        with pytest.raises(RuntimeError, match="markers"):
            command.downgrade(cfg, "20260919_01")
        with Session(engine) as db:
            convert_pair(db, 123, "white", StorageFormat.LEGACY)
        command.downgrade(cfg, "20260919_01")
        with engine.connect() as conn:
            assert "storage_format" not in {
                c["name"] for c in inspect(conn).get_columns("opening_score_batches")
            }
        command.upgrade(cfg, "20260919_03")
    finally:
        engine.dispose()


@pg_gate
def test_pg_atomic_failure_boundaries_and_commit_recovery(
    pg_engine, pg_session_factory, monkeypatch
):
    with Session(pg_engine, expire_on_commit=False) as db:
        first = publish(db)
        handle = ScoreHandle.from_batch(first)
        for stage in (
            "opening_current_roots",
            "opening_current_positions",
            "opening_current_edges",
            "opening_current_scope",
            "INSERT INTO opening_score_batches",
            "DELETE FROM opening_score_batches",
        ):
            batch = candidate(db)

            def fail(conn, cursor, statement, parameters, context, many):
                if stage in statement and statement.startswith(
                    ("INSERT", "UPDATE", "DELETE")
                ):
                    raise RuntimeError("injected failure")

            event.listen(pg_engine, "before_cursor_execute", fail)
            try:
                with pytest.raises(RuntimeError, match="injected"):
                    publish_scores(
                        db, batch, ScorePayload(), storage_format=StorageFormat.CURRENT
                    )
            finally:
                event.remove(pg_engine, "before_cursor_execute", fail)
            assert read_payload(db, handle) == payload()
        batch = candidate(db)
        original = Session.commit

        def fail_commit(self):
            if self is db:
                raise RuntimeError("before send")
            return original(self)

        with monkeypatch.context() as m:
            m.setattr(Session, "commit", fail_commit)
            with pytest.raises(RuntimeError, match="before send"):
                publish_scores(
                    db, batch, ScorePayload(), storage_format=StorageFormat.CURRENT
                )
        assert read_payload(db, handle) == payload()
        batch = candidate(db)

        def lost_ack(self):
            original(self)
            if self is db:
                raise RuntimeError("lost ack")

        with monkeypatch.context() as m:
            m.setattr(Session, "commit", lost_ack)
            confirmed = publish_scores(
                db, batch, ScorePayload(), storage_format=StorageFormat.CURRENT
            )
        assert confirmed.generation == batch.generation
        assert read_payload(db, ScoreHandle.from_batch(confirmed)) == ScorePayload()
        db.rollback()
        # A lingering backend must not leave ambiguous-commit recovery waiting
        # forever. Exercise the real PostgreSQL lock timeout, not a mocked lock.
        with Session(pg_engine) as holder:
            acquire_publication_lock(holder, 123, "white")
            with pytest.raises(OperationalError, match="lock timeout"):
                _recover(pg_engine, 123, "white")
        assert _recover(pg_engine, 123, "white").generation == confirmed.generation



@pg_gate
def test_pg_publication_serialization_supersession_and_evidence_order(
    pg_engine, pg_session_factory
):
    with Session(pg_engine, expire_on_commit=False) as db:
        low = candidate(db, evidence_seq=900)
        high = candidate(db, evidence_seq=1)
    started = Event()

    def late_low():
        with Session(pg_engine, expire_on_commit=False) as db:
            started.set()
            with pytest.raises(PublicationSuperseded) as caught:
                publish_scores(
                    db, low, ScorePayload(), storage_format=StorageFormat.CURRENT
                )
            return caught.value.batch

    with (
        Session(pg_engine, expire_on_commit=False) as locker,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        acquire_publication_lock(locker, 123, "white")
        future = pool.submit(late_low)
        assert started.wait(3)
        # Prove it reached PostgreSQL's lock wait, not just a Python thread start.
        deadline = time.monotonic() + 5
        waiting = False
        while time.monotonic() < deadline:
            with pg_engine.connect() as probe:
                waiting = bool(
                    probe.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' AND classid=:c AND NOT granted)"
                        ),
                        {"c": OPENING_SCORE_LOCK_CLASSID},
                    )
                )
            if waiting:
                break
            Event().wait(0.01)
        try:
            assert waiting
            # Publisher lock never holds the cursor/evidence counter row lock.
            with Session(pg_engine) as independent:
                independent.execute(text("SET LOCAL lock_timeout='250ms'"))
                independent.execute(
                    text(
                        "UPDATE opening_score_cursors SET evidence_seq=evidence_seq+1 WHERE user_id=123 AND player_color='white'"
                    )
                )
                independent.commit()
            # Stage the winning publication on the connection holding the guard.
            from app.opening_score_storage import _publish_scores

            winner = _publish_scores(
                locker,
                high,
                payload(),
                storage_format=StorageFormat.CURRENT,
                locked=True,
            )
        finally:
            locker.rollback()
        result = future.result(timeout=5)
        assert result.id == winner.id
        assert result.evidence_seq == 1


@pg_gate
def test_pg_lock_namespaces_colors_and_collision_isolation(
    pg_engine, pg_session_factory, monkeypatch
):
    with Session(pg_engine) as holder:
        acquire_publication_lock(holder, 123, "white")
        classid, objid = publication_lock_key(123, "white")
        with pg_engine.begin() as other:
            other.execute(text("SET LOCAL lock_timeout='250ms'"))
            # Different advisory families and the other color never contend.
            other.execute(
                text("SELECT pg_advisory_xact_lock(1734239597, :id)"), {"id": objid}
            )
            other.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:id AS bigint))"), {"id": objid}
            )
            other.execute(
                text("SELECT pg_advisory_xact_lock(:c, :id)"),
                {"c": classid, "id": objid ^ 1},
            )
        holder.rollback()
    monkeypatch.setattr(
        "app.opening_score_storage.publication_lock_key", lambda *args: (classid, 7)
    )
    with Session(pg_engine, expire_on_commit=False) as db:
        first = publish(db)
        second = publish(db, ScorePayload(), owner=456)
        assert read_payload(db, ScoreHandle.from_batch(first)) == payload()
        assert read_payload(db, ScoreHandle.from_batch(second)) == ScorePayload()


@pg_gate
def test_pg_exact_diff_ids_collation_and_snapshot_rollback(
    pg_engine, pg_session_factory
):
    with Session(pg_engine, expire_on_commit=False) as db:
        value = payload()
        positions = tuple(
            {**value.positions[0], "normalized_fen": key}
            for key in ("Z", "a", "é", "A")
        )
        value = replace(value, positions=positions)
        first = publish(db, value)
        first_handle = ScoreHandle.from_batch(first)
        table = _GROUPS[1][1]
        ids = dict(db.execute(select(table.c.normalized_fen, table.c.id)).all())
        assert list(
            db.scalars(select(table.c.normalized_fen).order_by(table.c.normalized_fen))
        ) == ["A", "Z", "a", "é"]
        db.rollback()
        with pg_engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as snapshot:
            snapshot.execute(text("SELECT 1 FROM opening_score_batches"))
            second = publish(
                db,
                replace(
                    value, positions=tuple({**r, "confidence": None} for r in positions)
                ),
            )
            assert (
                dict(db.execute(select(table.c.normalized_fen, table.c.id)).all())
                == ids
            )
            assert (
                snapshot.scalar(select(OpeningScoreBatch.id)) == first_handle.batch_id
            )
            assert all(v == 0.25 for v in snapshot.scalars(select(table.c.confidence)))
            assert not handle_is_live(db, first_handle)
            assert all(
                r["confidence"] is None
                for r in read_payload(db, ScoreHandle.from_batch(second)).positions
            )
