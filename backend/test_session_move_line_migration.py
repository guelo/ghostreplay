"""Schema gates for the thin revision-fenced session move-line protocol."""

from __future__ import annotations

import pathlib
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

import pg_gate_plugin
from app.models import GameSession


_BACKEND_DIR = pathlib.Path(__file__).resolve().parent
_PREVIOUS_REVISION = "20260810_01"
_REVISION = "20260814_01"

_PRE_MOVE_LINE_GAME_SESSIONS_DDL = """
CREATE TABLE game_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    started_at TIMESTAMP NOT NULL,
    status VARCHAR(20) NOT NULL,
    result VARCHAR(20),
    engine_elo INTEGER NOT NULL,
    is_rated BOOLEAN NOT NULL DEFAULT 1,
    player_color VARCHAR(5) NOT NULL DEFAULT 'white',
    session_mode VARCHAR(10) NOT NULL DEFAULT 'normal',
    drill_state VARCHAR(12),
    drill_terminal_reason VARCHAR(20)
)
"""

_PRE_MOVE_LINE_UPLOAD_RECEIPT_DDL = """
CREATE TABLE session_upload_receipt (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    client_request_id TEXT NOT NULL,
    server_request_id TEXT,
    recompute_opportunity BOOLEAN NOT NULL,
    session_mode TEXT,
    terminal_action TEXT,
    content_length_bytes INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
)
"""


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return cfg


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}


def _sqlite_table_count(conn, table: str) -> int:
    return conn.execute(
        text("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=:table"),
        {"table": table},
    ).scalar_one()


def test_sqlite_move_line_migration_upgrade_downgrade_reupgrade(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'move-line.db'}"
    setup = create_engine(url)
    with setup.begin() as conn:
        conn.execute(text(_PRE_MOVE_LINE_GAME_SESSIONS_DDL))
        conn.execute(text(_PRE_MOVE_LINE_UPLOAD_RECEIPT_DDL))
        conn.execute(
            text(
                "INSERT INTO game_sessions "
                "(id, user_id, started_at, status, engine_elo) "
                "VALUES ('existing', 1, CURRENT_TIMESTAMP, 'active', 1500)"
            )
        )
    setup.dispose()

    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()
    command.stamp(cfg, _PREVIOUS_REVISION)
    command.upgrade(cfg, _REVISION)

    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT move_line_revision, terminal_line_reconciled "
                "FROM game_sessions WHERE id='existing'"
            )
        ).one() == (0, 0)
        assert {"move_line_revision", "terminal_line_reconciled"} <= _columns(
            conn, "game_sessions"
        )
        assert {"move_line_revision", "line_proof_verdict"} <= _columns(
            conn, "session_upload_receipt"
        )
        assert "line_sync_verdict" not in _columns(conn, "session_upload_receipt")
        assert _sqlite_table_count(conn, "session_move_truncation_receipt") == 0
        assert (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == _REVISION
        )

    command.downgrade(cfg, _PREVIOUS_REVISION)
    with engine.connect() as conn:
        assert "move_line_revision" not in _columns(conn, "game_sessions")
        assert "terminal_line_reconciled" not in _columns(conn, "game_sessions")
        assert "move_line_revision" not in _columns(conn, "session_upload_receipt")
        assert "line_proof_verdict" not in _columns(conn, "session_upload_receipt")
        assert _sqlite_table_count(conn, "session_move_truncation_receipt") == 0
    engine.dispose()

    command.upgrade(cfg, _REVISION)
    reupgraded = create_engine(url)
    with reupgraded.connect() as conn:
        assert conn.execute(
            text(
                "SELECT move_line_revision, terminal_line_reconciled "
                "FROM game_sessions WHERE id='existing'"
            )
        ).one() == (0, 0)
        assert _sqlite_table_count(conn, "session_move_truncation_receipt") == 0
    reupgraded.dispose()


def test_handwritten_sqlite_schema_rejects_negative_revision(
    db_session, create_game_session
):
    session_id = create_game_session(user_id=8190)
    schema_sql = db_session.execute(
        text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='game_sessions'"
        )
    ).scalar_one()
    assert "ck_game_sessions_move_line_revision" in schema_sql
    assert (
        db_session.get(GameSession, uuid.UUID(session_id)).terminal_line_reconciled
        is False
    )

    with pytest.raises(IntegrityError):
        row = db_session.get(GameSession, uuid.UUID(session_id))
        assert row is not None
        row.move_line_revision = -1
        db_session.flush()
    db_session.rollback()


@pg_gate_plugin.pg_gate
def test_pg_move_line_migration_catalog_defaults_and_constraints(
    pg_migration_db, monkeypatch
):
    url = pg_migration_db
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()
    command.upgrade(cfg, _PREVIOUS_REVISION)

    engine = create_engine(url)
    with engine.begin() as conn:
        user_id = conn.execute(
            text(
                "INSERT INTO users (username) "
                "VALUES ('move-line-migration') RETURNING id"
            )
        ).scalar_one()
        session_id = conn.execute(
            text(
                "INSERT INTO game_sessions "
                "(id, user_id, started_at, status, engine_elo, is_rated) "
                "VALUES (gen_random_uuid(), :user_id, now(), 'active', 1500, false) "
                "RETURNING id"
            ),
            {"user_id": user_id},
        ).scalar_one()
    engine.dispose()

    statements: list[str] = []

    def capture_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.lower().split()))

    event.listen(Engine, "before_cursor_execute", capture_statement)
    try:
        command.upgrade(cfg, _REVISION)
    finally:
        event.remove(Engine, "before_cursor_execute", capture_statement)

    revision_ddl = next(
        index
        for index, statement in enumerate(statements)
        if "alter table game_sessions add column move_line_revision" in statement
    )
    assert "set local lock_timeout = '5s'" in statements[:revision_ddl]
    assert "set local statement_timeout = '60s'" in statements[:revision_ddl]
    add_check = next(
        statement
        for statement in statements
        if "add constraint ck_game_sessions_move_line_revision" in statement
    )
    assert add_check.endswith("check (move_line_revision >= 0)")
    assert "not valid" not in add_check
    assert not any(
        "validate constraint ck_game_sessions_move_line_revision" in statement
        for statement in statements
    )

    engine = create_engine(url)
    with engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT move_line_revision, terminal_line_reconciled "
                    "FROM game_sessions WHERE id=:session_id"
                ),
                {"session_id": session_id},
            ).one()
            == (0, False)
        )
        columns = {
            (row[0], row[1]): row[2]
            for row in conn.execute(
                text(
                    "SELECT table_name, column_name, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND "
                    "table_name IN ('game_sessions', 'session_upload_receipt')"
                )
            )
        }
        assert columns[("game_sessions", "move_line_revision")] == "0"
        assert columns[("game_sessions", "terminal_line_reconciled")] == "false"
        assert ("session_upload_receipt", "move_line_revision") in columns
        assert ("session_upload_receipt", "line_proof_verdict") in columns
        assert ("session_upload_receipt", "line_sync_verdict") not in columns
        assert (
            conn.execute(
                text("SELECT to_regclass('session_move_truncation_receipt')")
            ).scalar_one()
            is None
        )
        assert (
            conn.execute(
                text(
                    "SELECT convalidated FROM pg_constraint "
                    "WHERE conname='ck_game_sessions_move_line_revision'"
                )
            ).scalar_one()
            is True
        )

    with engine.connect() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text("UPDATE game_sessions SET move_line_revision=-1 WHERE id=:session_id"),
            {"session_id": session_id},
        )

    engine.dispose()
    command.downgrade(cfg, _PREVIOUS_REVISION)
    downgraded = create_engine(url)
    with downgraded.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='game_sessions' AND column_name IN "
                    "('move_line_revision', 'terminal_line_reconciled')"
                )
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT to_regclass('session_move_truncation_receipt')")
            ).scalar_one()
            is None
        )
    downgraded.dispose()

    command.upgrade(cfg, _REVISION)
    reupgraded = create_engine(url)
    with reupgraded.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT move_line_revision, terminal_line_reconciled "
                    "FROM game_sessions WHERE id=:session_id"
                ),
                {"session_id": session_id},
            ).one()
            == (0, False)
        )
    reupgraded.dispose()
