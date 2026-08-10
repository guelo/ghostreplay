"""PostgreSQL-only migration and trigger gates for g-f3m4."""

from __future__ import annotations

import pathlib
import threading

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

import pg_gate_plugin


_BACKEND_DIR = pathlib.Path(__file__).resolve().parent


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return cfg


@pg_gate_plugin.pg_gate
def test_pg_baseline_watermark_migration_cycle_and_truncate_contract(
    pg_migration_db, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", pg_migration_db)
    cfg = _alembic_config()
    command.upgrade(cfg, "20260802_01")

    engine = create_engine(pg_migration_db)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'game_sessions' "
                "AND column_name LIKE 'baseline_watermark_%'"
            )
        ).scalar_one() == 0
    engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(pg_migration_db)
    with engine.begin() as connection:
        assert connection.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'game_sessions' "
                "AND column_name LIKE 'baseline_watermark_%'"
            )
        ).scalar_one() == 3
        assert connection.execute(
            text(
                "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND "
                "tgname LIKE 'trg_%_evidence_epoch_%' AND "
                "tgrelid IN ('analysis_cache'::regclass, "
                "'position_analysis'::regclass, "
                "'analysis_cache_submission'::regclass)"
            )
        ).scalar_one() == 12
        connection.execute(
            text(
                "INSERT INTO analysis_cache "
                "(fen_before, normalized_fen_before, move_uci, move_san, played_eval) "
                "VALUES ('raw-fen', 'norm-fen', 'e2e4', 'e4', 1)"
            )
        )
        epoch = connection.execute(
            text("SELECT value FROM evidence_epoch WHERE id = 1")
        ).scalar_one()
        assert connection.execute(
            text(
                "SELECT kind, fen, last_changed_epoch "
                "FROM shared_evidence_scope_versions ORDER BY kind"
            )
        ).all() == [
            ("norm", "norm-fen", epoch),
            ("raw", "raw-fen", epoch),
        ]

        connection.execute(
            text("TRUNCATE analysis_cache, analysis_cache_submission")
        )
        truncate_epoch = connection.execute(
            text("SELECT value FROM evidence_epoch WHERE id = 1")
        ).scalar_one()
        assert truncate_epoch > epoch
        assert connection.execute(
            text(
                "SELECT kind, last_changed_epoch "
                "FROM shared_evidence_scope_invalidations ORDER BY kind"
            )
        ).all() == [("norm", truncate_epoch), ("raw", truncate_epoch)]

    for statement in (
        "DELETE FROM evidence_epoch WHERE id = 1",
        "UPDATE evidence_epoch SET value = value WHERE id = 1",
        "TRUNCATE evidence_epoch",
    ):
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(text(statement))
    engine.dispose()

    command.downgrade(cfg, "20260802_01")
    engine = create_engine(pg_migration_db)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT to_regclass('shared_evidence_scope_versions') IS NULL"
            )
        ).scalar_one() is True
        assert connection.execute(
            text(
                "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND "
                "tgname IN ('trg_analysis_cache_evidence_epoch', "
                "'trg_position_analysis_evidence_epoch', "
                "'trg_analysis_cache_submission_evidence_epoch')"
            )
        ).scalar_one() == 3
    engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(pg_migration_db)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT to_regclass('shared_evidence_scope_versions') IS NOT NULL")
        ).scalar_one() is True
    engine.dispose()


@pg_gate_plugin.pg_gate
def test_pg_submission_multirow_opposite_orders_do_not_deadlock(
    pg_engine, pg_session_factory
):
    with pg_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username) VALUES "
                "(7001, 'scope-order-a'), (7002, 'scope-order-b')"
            )
        )
        cache_ids = connection.execute(
            text(
                "INSERT INTO analysis_cache "
                "(fen_before, normalized_fen_before, move_uci, move_san, played_eval) "
                "VALUES ('raw-a', 'norm-a', 'a2a3', 'a3', 1), "
                "('raw-b', 'norm-b', 'b2b3', 'b3', 1) RETURNING id"
            )
        ).scalars().all()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(user_id: int, ordered_ids: list[int]) -> None:
        session = pg_session_factory()
        try:
            barrier.wait(timeout=5.0)
            session.execute(
                text(
                    "INSERT INTO analysis_cache_submission "
                    "(analysis_cache_id, user_id) VALUES "
                    "(:first, :user), (:second, :user)"
                ),
                {
                    "first": ordered_ids[0],
                    "second": ordered_ids[1],
                    "user": user_id,
                },
            )
            session.commit()
        except BaseException as exc:  # noqa: BLE001 - thread handoff
            session.rollback()
            errors.append(exc)
        finally:
            session.close()

    first = threading.Thread(target=writer, args=(7001, cache_ids))
    second = threading.Thread(target=writer, args=(7002, list(reversed(cache_ids))))
    first.start()
    second.start()
    first.join(timeout=10.0)
    second.join(timeout=10.0)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    with pg_engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM analysis_cache_submission")
        ).scalar_one() == 4
        function_sql = connection.execute(
            text(
                "SELECT pg_get_functiondef("
                "'track_analysis_cache_submission_evidence_insert()'::regprocedure)"
            )
        ).scalar_one()
        assert "ORDER BY affected.kind, affected.fen" in function_sql
