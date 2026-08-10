"""From-scratch SQLite coverage for the complete Alembic revision chain."""

from __future__ import annotations

import pathlib

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.models import Base


_BACKEND_DIR = pathlib.Path(__file__).resolve().parent


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return cfg


def test_sqlite_full_chain_matches_model_schema_and_downgrades(tmp_path, monkeypatch):
    """Migrations and ``Base.metadata`` must describe the same tables and columns.

    Replaying from an empty database catches dialect-incompatible historical DDL.
    Comparing names catches columns or tables that exist in only the migration
    chain or only the model-driven test schema.
    """
    url = f"sqlite:///{tmp_path / 'full-chain.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()

    assert len(heads) == 1

    command.upgrade(cfg, "head")

    engine = create_engine(url)
    inspector = inspect(engine)
    migrated_tables = set(inspector.get_table_names()) - {"alembic_version"}
    model_tables = set(Base.metadata.tables)

    assert migrated_tables == model_tables
    assert {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in migrated_tables
    } == {
        table: {column.name for column in Base.metadata.tables[table].columns}
        for table in model_tables
    }
    assert {constraint["name"] for constraint in inspector.get_check_constraints("game_sessions")} >= {
        "ck_game_sessions_player_color"
    }
    assert {constraint["name"] for constraint in inspector.get_check_constraints("positions")} >= {
        "ck_positions_active_color"
    }
    assert {constraint["name"] for constraint in inspector.get_check_constraints("session_moves")} >= {
        "ck_session_moves_decision_source"
    }
    assert {
        (tuple(foreign_key["constrained_columns"]), foreign_key["referred_table"])
        for foreign_key in inspector.get_foreign_keys("blunders")
    } >= {(('source_session_id',), "game_sessions")}
    assert {
        (tuple(foreign_key["constrained_columns"]), foreign_key["referred_table"])
        for foreign_key in inspector.get_foreign_keys("session_moves")
    } >= {(('target_blunder_id',), "blunders")}

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == heads[0]
    engine.dispose()

    command.downgrade(cfg, "base")

    engine = create_engine(url)
    assert inspect(engine).get_table_names() == ["alembic_version"]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 0
    engine.dispose()


def test_baseline_watermark_revision_downgrade_upgrade_restores_trigger_generations(
    tmp_path, monkeypatch
):
    url = f"sqlite:///{tmp_path / 'baseline-watermark-cycle.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO analysis_cache "
                "(id, fen_before, normalized_fen_before, move_uci, move_san, played_eval) "
                "VALUES (1, 'raw-fen', 'norm-fen', 'e2e4', 'e4', 1)"
            )
        )
        assert connection.execute(
            text(
                "SELECT kind, fen FROM shared_evidence_scope_versions "
                "ORDER BY kind"
            )
        ).all() == [("norm", "norm-fen"), ("raw", "raw-fen")]
    engine.dispose()

    command.downgrade(cfg, "20260802_01")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert "shared_evidence_scope_versions" not in inspector.get_table_names()
    assert "baseline_watermark_seq" not in {
        column["name"] for column in inspector.get_columns("game_sessions")
    }
    with engine.connect() as connection:
        trigger_sql = connection.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'trg_analysis_cache_evidence_epoch_insert'"
            )
        ).scalar_one()
        assert "shared_evidence_scope_versions" not in trigger_sql
    engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert "shared_evidence_scope_versions" in inspector.get_table_names()
    assert "baseline_watermark_seq" in {
        column["name"] for column in inspector.get_columns("game_sessions")
    }
    engine.dispose()

def test_sqlite_early_color_migrations_backfill_and_preserve_rows(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'color-backfill.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()

    command.upgrade(cfg, "20260203_01")

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO positions (id, user_id, fen_hash, fen_raw, created_at) VALUES "
                "(1, 7, 'white-fen', '8/8/8/8/8/8/8/8 w - - 0 1', CURRENT_TIMESTAMP), "
                "(2, 7, 'black-fen', '8/8/8/8/8/8/8/8 b - - 0 1', CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO game_sessions "
                "(id, user_id, started_at, status, engine_elo) VALUES "
                "('00000000-0000-0000-0000-000000000001', 7, CURRENT_TIMESTAMP, 'active', 1500)"
            )
        )
    engine.dispose()

    command.upgrade(cfg, "20260203_03")

    engine = create_engine(url)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT active_color FROM positions ORDER BY id")
        ).scalars().all() == ["white", "black"]
        assert connection.execute(
            text("SELECT player_color FROM game_sessions")
        ).scalar_one() == "white"

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text("UPDATE positions SET active_color = 'red' WHERE id = 1"))
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text("UPDATE game_sessions SET player_color = 'red'"))
    engine.dispose()

    command.downgrade(cfg, "20260203_01")

    engine = create_engine(url)
    inspector = inspect(engine)
    assert "active_color" not in {
        column["name"] for column in inspector.get_columns("positions")
    }
    assert "player_color" not in {
        column["name"] for column in inspector.get_columns("game_sessions")
    }
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM positions")).scalar_one() == 2
        assert connection.execute(text("SELECT count(*) FROM game_sessions")).scalar_one() == 1
    engine.dispose()


def test_sqlite_active_color_backfill_rejects_malformed_side_fields(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'malformed-color.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()

    command.upgrade(cfg, "20260203_02")

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO positions (id, user_id, fen_hash, fen_raw, created_at) VALUES "
                "(1, 7, 'long-side', '8/8/8/8/8/8/8/8 white - - 0 1', CURRENT_TIMESTAMP), "
                "(2, 7, 'missing-side', 'w', CURRENT_TIMESTAMP)"
            )
        )
    engine.dispose()

    with pytest.raises(IntegrityError):
        command.upgrade(cfg, "20260203_03")

    engine = create_engine(url)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT active_color FROM positions ORDER BY id")
        ).scalars().all() == [None, None]
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == "20260203_02"
    engine.dispose()
