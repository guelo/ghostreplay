"""Behavior contract for the failed-drill historical repair."""

from __future__ import annotations

import pathlib

from alembic import command
from alembic.config import Config
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine


_BACKEND_DIR = pathlib.Path(__file__).resolve().parent
_DOWN_REVISION = "20260809_01"
_REVISION = "20260810_01"


def _alembic_config() -> Config:
    config = Config(str(_BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return config


def _states(engine: Engine, session_ids: list[str]) -> dict[str, str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, drill_state FROM game_sessions WHERE id IN :session_ids"
            ).bindparams(bindparam("session_ids", expanding=True)),
            {"session_ids": session_ids},
        ).all()
    return {str(session_id): drill_state for session_id, drill_state in rows}


def test_failed_drill_backfill_targets_only_the_overwrite_shape(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'failed-drill-backfill.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = _alembic_config()
    command.upgrade(config, _DOWN_REVISION)

    cases = [
        # The two historical defect shapes.
        (
            "00000000-0000-0000-0000-000000000001",
            "abandoned",
            "ended",
            "drill_abandon",
            "accuracy",
        ),
        (
            "00000000-0000-0000-0000-000000000002",
            "abandoned",
            "ended",
            "drill_abandon",
            "off_route",
        ),
        # Upgrade leaves a post-fix row alone; downgrade restores old semantics.
        (
            "00000000-0000-0000-0000-000000000003",
            "failed",
            "ended",
            "drill_abandon",
            "accuracy",
        ),
        # Each negative keeps drill_state='abandoned' and changes one other term.
        (
            "00000000-0000-0000-0000-000000000004",
            "abandoned",
            "active",
            "drill_abandon",
            "accuracy",
        ),
        (
            "00000000-0000-0000-0000-000000000005",
            "abandoned",
            "ended",
            None,
            "accuracy",
        ),
        (
            "00000000-0000-0000-0000-000000000006",
            "abandoned",
            "ended",
            "drill_abandon",
            "natural_end",
        ),
        (
            "00000000-0000-0000-0000-000000000007",
            "abandoned",
            "ended",
            "drill_abandon",
            None,
        ),
    ]
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO game_sessions "
                "(id, user_id, started_at, ended_at, status, result, engine_elo, "
                "is_rated, player_color, session_mode, drill_state, "
                "drill_terminal_reason) VALUES "
                "(:id, 1, '2026-07-01T00:00:00', '2026-07-01T00:05:00', "
                ":status, :result, 1500, false, 'white', 'drill', :state, "
                ":reason)"
            ),
            [
                {
                    "id": session_id,
                    "state": state,
                    "status": status,
                    "result": result,
                    "reason": reason,
                }
                for session_id, state, status, result, reason in cases
            ],
        )

    session_ids = [case[0] for case in cases]
    command.upgrade(config, _REVISION)
    assert _states(engine, session_ids) == {
        session_ids[0]: "failed",
        session_ids[1]: "failed",
        session_ids[2]: "failed",
        session_ids[3]: "abandoned",
        session_ids[4]: "abandoned",
        session_ids[5]: "abandoned",
        session_ids[6]: "abandoned",
    }

    command.downgrade(config, _DOWN_REVISION)
    assert _states(engine, session_ids) == {
        session_id: "abandoned" for session_id in session_ids
    }
    engine.dispose()
