"""Schema gates for the observation-only opening-boundary rollout."""

from __future__ import annotations

import pathlib
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

import pg_gate_plugin
from app.models import GameSession


_BACKEND_DIR = pathlib.Path(__file__).resolve().parent
_PREVIOUS_REVISION = "20260814_01"
_REVISION = "20260818_01"
_COLUMNS = {
    "opening_phase_protocol_version",
    "opening_phase_probe_ply",
    "opening_phase_probe_verdict",
    "opening_middle_candidate_ply",
    "opening_middle_ready_at",
    "opening_middle_ply",
    "opening_phase_exhausted",
    "opening_boundary_shadow_terminal_at",
}
_CONSTRAINTS = {
    "ck_game_sessions_opening_phase_protocol",
    "ck_game_sessions_opening_probe_ply",
    "ck_game_sessions_opening_candidate_ply",
    "ck_game_sessions_opening_middle_ply",
    "ck_game_sessions_opening_probe_verdict",
    "ck_game_sessions_opening_ready_requires_candidate_baseline",
    "ck_game_sessions_opening_marker_requires_baseline",
    "ck_game_sessions_opening_exhausted_clears_state",
    "ck_game_sessions_opening_shadow_requires_protocol",
}


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return cfg


def _sqlite_columns(conn) -> set[str]:
    return {
        row[1] for row in conn.execute(text("PRAGMA table_info(game_sessions)"))
    }


def test_sqlite_boundary_migration_upgrade_downgrade(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'opening-boundary.db'}"
    setup = create_engine(url)
    with setup.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE game_sessions ("
                "id TEXT PRIMARY KEY, opening_score_baseline TEXT, "
                "move_line_revision INTEGER NOT NULL DEFAULT 0)"
            )
        )
        conn.execute(text("INSERT INTO game_sessions (id) VALUES ('existing')"))
    setup.dispose()

    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()
    command.stamp(cfg, _PREVIOUS_REVISION)
    command.upgrade(cfg, _REVISION)

    engine = create_engine(url)
    with engine.connect() as conn:
        assert _COLUMNS <= _sqlite_columns(conn)
        assert conn.execute(
            text(
                "SELECT opening_phase_protocol_version, opening_phase_exhausted "
                "FROM game_sessions WHERE id='existing'"
            )
        ).one() == (None, 0)

    command.downgrade(cfg, _PREVIOUS_REVISION)
    with engine.connect() as conn:
        assert not (_COLUMNS & _sqlite_columns(conn))
    engine.dispose()


def test_handwritten_sqlite_boundary_constraints(db_session, create_game_session):
    session_id = create_game_session(user_id=8310)
    schema_sql = db_session.execute(
        text(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='game_sessions'"
        )
    ).scalar_one()
    assert _CONSTRAINTS <= {
        name for name in _CONSTRAINTS if name in schema_sql
    }

    with pytest.raises(IntegrityError):
        row = db_session.get(GameSession, uuid.UUID(session_id))
        assert row is not None
        row.opening_phase_protocol_version = 2
        db_session.flush()
    db_session.rollback()


@pg_gate_plugin.pg_gate
def test_pg_boundary_migration_constraints(pg_migration_db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_migration_db)
    cfg = _alembic_config()
    command.upgrade(cfg, _PREVIOUS_REVISION)

    engine = create_engine(pg_migration_db)
    with engine.begin() as conn:
        user_id = conn.execute(
            text("INSERT INTO users (username) VALUES ('boundary-migration') RETURNING id")
        ).scalar_one()
        session_id = conn.execute(
            text(
                "INSERT INTO game_sessions "
                "(id, user_id, started_at, status, engine_elo) "
                "VALUES (gen_random_uuid(), :user_id, now(), 'active', 1500) "
                "RETURNING id"
            ),
            {"user_id": user_id},
        ).scalar_one()
    engine.dispose()

    command.upgrade(cfg, _REVISION)
    engine = create_engine(pg_migration_db)
    with engine.connect() as conn:
        columns = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='game_sessions'"
                )
            )
        }
        assert _COLUMNS <= columns
        constraints = {
            row[0]: row[1]
            for row in conn.execute(
                text(
                    "SELECT conname, convalidated FROM pg_constraint "
                    "WHERE conname = ANY(:names)"
                ),
                {"names": list(_CONSTRAINTS)},
            )
        }
        assert constraints == {name: True for name in _CONSTRAINTS}
        assert conn.execute(
            text(
                "SELECT opening_phase_protocol_version, opening_phase_exhausted "
                "FROM game_sessions WHERE id=:id"
            ),
            {"id": session_id},
        ).one() == (None, False)

    for assignment in (
        "opening_phase_protocol_version=2",
        "opening_middle_ready_at=now()",
        "opening_middle_ply=4",
    ):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(f"UPDATE game_sessions SET {assignment} WHERE id=:id"),
                {"id": session_id},
            )
    engine.dispose()
