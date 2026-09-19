"""Additive route preference migration and application-schema constraint gates."""
from pathlib import Path
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text, CheckConstraint
from sqlalchemy.exc import IntegrityError

from app.models import GameSession
from pg_gate_plugin import pg_gate

PREVIOUS = '20260818_01'
REVISION = '20260919_02'
CHECKS = {'ck_game_sessions_drill_route_mode', 'ck_game_sessions_prefer_line_requires_drill_line'}


def config():
    backend = Path(__file__).resolve().parent
    cfg = Config(str(backend / 'alembic.ini'))
    cfg.set_main_option('script_location', str(backend / 'alembic'))
    return cfg


def test_sqlite_route_mode_upgrade_downgrade_preserves_unnamed_checks(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'route-mode.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE game_sessions (id TEXT PRIMARY KEY, "
                          "session_mode TEXT NOT NULL DEFAULT 'normal', drill_line TEXT, "
                          "drill_state TEXT CHECK (drill_state IS NULL OR drill_state IN ('active','failed')), "
                          "rated_start_ply INTEGER CHECK (rated_start_ply IS NULL OR rated_start_ply >= 0))"))
        conn.execute(text("INSERT INTO game_sessions (id, session_mode, drill_line, drill_state) "
                          "VALUES ('old', 'drill', 'e2e4', 'active')"))
        rootpage = conn.execute(text("SELECT rootpage FROM sqlite_master WHERE name='game_sessions'")).scalar_one()
    monkeypatch.setenv('DATABASE_URL', url)
    cfg = config()
    command.stamp(cfg, PREVIOUS)
    for _ in range(2):
        command.upgrade(cfg, REVISION)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT drill_line, drill_route_mode FROM game_sessions WHERE id='old'")).one() == ('e2e4', 'auto')
            assert conn.execute(text("SELECT rootpage FROM sqlite_master WHERE name='game_sessions'")).scalar_one() == rootpage
        for assignment in ("drill_state='invalid'", 'rated_start_ply=-1'):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text(f"UPDATE game_sessions SET {assignment}"))
        command.downgrade(cfg, PREVIOUS)
        with engine.connect() as conn:
            assert 'drill_route_mode' not in {row[1] for row in conn.execute(text('PRAGMA table_info(game_sessions)'))}
            assert conn.execute(text("SELECT drill_line FROM game_sessions WHERE id='old'")).scalar_one() == 'e2e4'
    engine.dispose()


@pytest.mark.parametrize('assignment', ["drill_route_mode='invalid'", "drill_route_mode='prefer_line'", "drill_route_mode='prefer_line', drill_line='e2e4'"])
def test_route_mode_model_and_handwritten_schema_constraints(db_session, create_game_session, assignment):
    sid = create_game_session(user_id=8310)
    model_checks = {constraint.name for constraint in GameSession.__table__.constraints if isinstance(constraint, CheckConstraint)}
    assert CHECKS <= model_checks
    with pytest.raises(IntegrityError):
        db_session.execute(text(f'UPDATE game_sessions SET {assignment} WHERE id=:id'), {'id': uuid.UUID(sid).hex})
        db_session.flush()
    db_session.rollback()


@pg_gate
def test_pg_route_mode_migration_constraints(pg_migration_db, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', pg_migration_db)
    cfg = config()
    command.upgrade(cfg, PREVIOUS)
    engine = create_engine(pg_migration_db)
    with engine.begin() as conn:
        sid = conn.execute(text("INSERT INTO game_sessions (id, user_id, started_at, status, engine_elo, "
                                "session_mode, drill_state, is_rated, drill_line) "
                                "VALUES (gen_random_uuid(), 8310, now(), 'active', 1500, 'drill', 'active', false, 'e2e4') RETURNING id")).scalar_one()
    command.upgrade(cfg, REVISION)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT drill_route_mode, drill_line FROM game_sessions WHERE id=:id'), {'id': sid}).one() == ('auto', 'e2e4')
        checks = dict(conn.execute(text('SELECT conname, convalidated FROM pg_constraint WHERE conname = ANY(:names)'), {'names': list(CHECKS)}).all())
        assert checks == {name: True for name in CHECKS}
    for assignment in ("drill_route_mode='invalid'", "drill_route_mode='prefer_line', drill_line=NULL",
                       "drill_route_mode='prefer_line', session_mode='normal', drill_state=NULL"):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text(f'UPDATE game_sessions SET {assignment} WHERE id=:id'), {'id': sid})
    with engine.begin() as conn:
        conn.execute(text("UPDATE game_sessions SET drill_route_mode='prefer_line' WHERE id=:id"), {'id': sid})
    command.downgrade(cfg, PREVIOUS)
    command.upgrade(cfg, REVISION)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT drill_route_mode, drill_line FROM game_sessions WHERE id=:id'), {'id': sid}).one() == ('auto', 'e2e4')
    engine.dispose()
