"""Migration tests for the Release A schema (g-accuracy-schema).

Two dialect paths plus the disposable-database fixture's safety contract:

- **SQLite** via the Alembic command API against a hand-built pre-A schema
  stamped at ``20260708_01``. The full pre-A history cannot replay on SQLite
  (earlier migrations use Postgres-only ``ALTER ... ADD CONSTRAINT``), so the
  test builds the starting tables directly and stamps the revision, then drives
  the two Release A migrations up and back down at explicit revisions.
- **PostgreSQL** against a disposable database created from base by
  ``pg_migration_db``, exercising the ``NOT VALID`` CHECK, the ``CONCURRENTLY``
  durable-head index, and the no-Sort head-query plan — none of which the SQLite
  path can express.
- **Safety contract** unit tests for ``pg_migration_db``'s naming / maintenance
  authority guards and required-mode failure.
"""

from __future__ import annotations

import pathlib

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

import pg_gate_plugin

_BACKEND_DIR = pathlib.Path(__file__).resolve().parent

# Representative pre-A game_sessions: enough columns to drive the migration plus
# a NAMED CHECK, which SQLite batch-mode reflection round-trips (unnamed CHECKs
# are dropped on recreate). We assert this constraint survives the upgrade.
_PRE_A_GAME_SESSIONS_DDL = """
    CREATE TABLE game_sessions (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        started_at TIMESTAMP NOT NULL,
        status VARCHAR(20) NOT NULL,
        engine_elo INTEGER NOT NULL,
        is_rated BOOLEAN NOT NULL DEFAULT 1,
        player_color VARCHAR(5) NOT NULL DEFAULT 'white',
        session_mode VARCHAR(10) NOT NULL DEFAULT 'normal',
        drill_state VARCHAR(12),
        CONSTRAINT ck_game_sessions_session_mode CHECK (session_mode IN ('normal','drill'))
    )
"""

_PRE_A_RATING_HISTORY_DDL = """
    CREATE TABLE rating_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        game_session_id TEXT NOT NULL,
        rating INTEGER NOT NULL,
        is_provisional BOOLEAN NOT NULL,
        games_played INTEGER NOT NULL,
        recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(game_session_id)
    )
"""


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    # Make script discovery independent of the process CWD.
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return cfg


def _index_names(conn, table: str) -> set[str]:
    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = :t").bindparams(t=table)
    )
    return {r[0] for r in rows}


def _game_sessions_columns(conn) -> set[str]:
    return {r[1] for r in conn.execute(text("PRAGMA table_info(game_sessions)"))}


def test_sqlite_release_a_upgrade_downgrade(tmp_path, monkeypatch):
    db_path = tmp_path / "release_a.db"
    url = f"sqlite:///{db_path}"

    # --- pre-A state: build tables + old index, insert an un-accuracied row ---
    setup = create_engine(url)
    with setup.begin() as conn:
        conn.execute(text(_PRE_A_GAME_SESSIONS_DDL))
        conn.execute(text(_PRE_A_RATING_HISTORY_DDL))
        conn.execute(
            text("CREATE INDEX idx_rating_history_user_timestamp ON rating_history (user_id, recorded_at)")
        )
        conn.execute(
            text(
                "INSERT INTO game_sessions (id, user_id, started_at, status, engine_elo, session_mode) "
                "VALUES ('sess-preA', 1, '2026-01-01T00:00:00', 'active', 1500, 'normal')"
            )
        )
    setup.dispose()

    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()
    command.stamp(cfg, "20260708_01")

    # --- upgrade to 20260709_01: accuracy columns + range CHECK ---
    command.upgrade(cfg, "20260709_01")
    eng = create_engine(url)
    with eng.connect() as conn:
        cols = _game_sessions_columns(conn)
        assert {"player_accuracy", "player_accuracy_algo_version"} <= cols
        # The pre-existing (unstamped) row is left NULL by the additive columns.
        assert conn.execute(
            text("SELECT player_accuracy, player_accuracy_algo_version FROM game_sessions WHERE id = 'sess-preA'")
        ).one() == (None, None)
    # Valid + NULL accuracy accepted.
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO game_sessions (id, user_id, started_at, status, engine_elo, session_mode, "
                "player_accuracy, player_accuracy_algo_version) "
                "VALUES ('sess-ok', 1, '2026-01-01', 'active', 1500, 'normal', 100, 1)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO game_sessions (id, user_id, started_at, status, engine_elo, session_mode, "
                "player_accuracy) VALUES ('sess-null', 1, '2026-01-01', 'active', 1500, 'normal', NULL)"
            )
        )
    # Out-of-range accuracy rejected by the new CHECK.
    with eng.connect() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO game_sessions (id, user_id, started_at, status, engine_elo, session_mode, "
                    "player_accuracy) VALUES ('sess-bad', 1, '2026-01-01', 'active', 1500, 'normal', 150)"
                )
            )
    # Pre-existing NAMED CHECK survived the batch-mode table recreate.
    with eng.connect() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO game_sessions (id, user_id, started_at, status, engine_elo, session_mode) "
                    "VALUES ('sess-mode', 1, '2026-01-01', 'active', 1500, 'bogus')"
                )
            )
    eng.dispose()

    # --- upgrade to 20260709_02: both indexes present ---
    command.upgrade(cfg, "20260709_02")
    eng = create_engine(url)
    with eng.connect() as conn:
        idx = _index_names(conn, "rating_history")
        assert "idx_rating_history_user_chain" in idx
        assert "idx_rating_history_user_timestamp" in idx  # old index untouched
    eng.dispose()

    # --- downgrade to 20260709_01: only the chain index is dropped ---
    command.downgrade(cfg, "20260709_01")
    eng = create_engine(url)
    with eng.connect() as conn:
        idx = _index_names(conn, "rating_history")
        assert "idx_rating_history_user_chain" not in idx
        assert "idx_rating_history_user_timestamp" in idx
    eng.dispose()

    # --- downgrade to 20260708_01: CHECK dropped BEFORE the columns, both gone ---
    command.downgrade(cfg, "20260708_01")
    eng = create_engine(url)
    with eng.connect() as conn:
        cols = _game_sessions_columns(conn)
        assert "player_accuracy" not in cols
        assert "player_accuracy_algo_version" not in cols
        gs_sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='game_sessions'")
        ).scalar()
        assert "ck_game_sessions_player_accuracy" not in gs_sql
        # The original row round-tripped through both recreates intact.
        assert conn.execute(text("SELECT count(*) FROM game_sessions WHERE id = 'sess-preA'")).scalar() == 1
    eng.dispose()


def test_pg_disposable_release_a_migration(pg_migration_db, monkeypatch):
    """Disposable-DB PostgreSQL migration: NOT VALID CHECK, concurrent index, no-Sort plan."""
    url = pg_migration_db
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()

    # --- explicit upgrade to 20260709_01 ---
    command.upgrade(cfg, "20260709_01")
    eng = create_engine(url)
    with eng.connect() as conn:
        # CHECK exists but is NOT validated against existing rows (Release A rule).
        assert conn.execute(
            text("SELECT convalidated FROM pg_constraint WHERE conname = 'ck_game_sessions_player_accuracy'")
        ).scalar() is False
    # A NEW invalid write is still rejected despite NOT VALID.
    with eng.connect() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO game_sessions (id, user_id, started_at, status, engine_elo, player_accuracy) "
                    "VALUES (gen_random_uuid(), 1, now(), 'active', 1500, 150)"
                )
            )
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO game_sessions (id, user_id, started_at, status, engine_elo, player_accuracy) "
                "VALUES (gen_random_uuid(), 1, now(), 'active', 1500, 50)"
            )
        )
    eng.dispose()

    # --- explicit upgrade to 20260709_02: index valid + correctly ordered ---
    command.upgrade(cfg, "20260709_02")
    eng = create_engine(url)
    with eng.connect() as conn:
        assert conn.execute(
            text("SELECT indisvalid FROM pg_index WHERE indexrelid = 'idx_rating_history_user_chain'::regclass")
        ).scalar() is True
        indexdef = conn.execute(
            text("SELECT pg_get_indexdef('idx_rating_history_user_chain'::regclass)")
        ).scalar()
        assert "(user_id, games_played DESC, recorded_at DESC, id DESC)" in indexdef

    # --- seed enough rows and prove the head query uses the index with no Sort ---
    with eng.begin() as conn:
        uid = conn.execute(text("INSERT INTO users (username) VALUES ('idxprobe') RETURNING id")).scalar()
        conn.execute(
            text(
                "INSERT INTO game_sessions (id, user_id, started_at, status, engine_elo) "
                "SELECT gen_random_uuid(), :u, now(), 'active', 1500 FROM generate_series(1, 600)"
            ).bindparams(u=uid)
        )
        conn.execute(
            text(
                "INSERT INTO rating_history (user_id, game_session_id, rating, is_provisional, games_played, recorded_at) "
                "SELECT :u, gs.id, 1500 + (row_number() OVER ())::int, false, (row_number() OVER ())::int, "
                "now() + ((row_number() OVER ()) || ' seconds')::interval "
                "FROM game_sessions gs WHERE gs.user_id = :u"
            ).bindparams(u=uid)
        )
        conn.execute(text("ANALYZE rating_history"))
    with eng.connect() as conn:
        conn.execute(text("SET enable_seqscan = off"))
        plan = "\n".join(
            r[0]
            for r in conn.execute(
                text(
                    f"EXPLAIN SELECT * FROM rating_history WHERE user_id = {int(uid)} "
                    "ORDER BY games_played DESC, recorded_at DESC, id DESC LIMIT 1"
                )
            )
        )
    assert "idx_rating_history_user_chain" in plan
    assert "Sort" not in plan
    eng.dispose()


# ---------------------------------------------------------------------------
# pg_migration_db safety contract (no database needed — pure guard logic).
# ---------------------------------------------------------------------------


def test_assert_disposable_rejects_non_disposable_names(monkeypatch):
    monkeypatch.delenv("GHOSTREPLAY_TEST_PG_URL", raising=False)
    monkeypatch.delenv("TEST_DATABASE_URL_PG", raising=False)
    # A well-formed disposable name is accepted.
    pg_gate_plugin._assert_disposable("ghostreplay_mig_test_0123abcdef")
    for bad in [
        "postgres",
        "ghostreplay_test",
        "ghostreplay_mig_test",  # no token
        "ghostreplay_mig_test_ABC",  # uppercase not in [0-9a-f]
        "ghostreplay_mig_test_x; DROP DATABASE prod",
        "'; DROP DATABASE x; --",
    ]:
        with pytest.raises(RuntimeError):
            pg_gate_plugin._assert_disposable(bad)


def test_assert_disposable_refuses_shared_test_db(monkeypatch):
    # Even a name matching the disposable pattern is refused when it IS the shared
    # test database, so a fixture misfire can never drop the shared DB.
    monkeypatch.setenv(
        "GHOSTREPLAY_TEST_PG_URL", "postgresql://u:p@h:5432/ghostreplay_mig_test_shared"
    )
    monkeypatch.delenv("TEST_DATABASE_URL_PG", raising=False)
    with pytest.raises(RuntimeError):
        pg_gate_plugin._assert_disposable("ghostreplay_mig_test_shared")


def test_maintenance_authority_not_derived_from_test_url(monkeypatch):
    # A test URL alone grants NO maintenance authority; without a maintenance URL
    # the developer-default gate skips.
    monkeypatch.setenv("GHOSTREPLAY_TEST_PG_URL", "postgresql://u:p@h:5432/ghostreplay_test")
    monkeypatch.delenv("GHOSTREPLAY_TEST_PG_MAINT_URL", raising=False)
    monkeypatch.delenv("GHOSTREPLAY_REQUIRE_PG_TESTS", raising=False)
    assert pg_gate_plugin._pg_maint_url() is None
    with pytest.raises(pytest.skip.Exception):
        pg_gate_plugin._require_maint_url_or_gate()


def test_required_mode_missing_maint_url_fails(monkeypatch):
    monkeypatch.setenv("GHOSTREPLAY_REQUIRE_PG_TESTS", "1")
    monkeypatch.delenv("GHOSTREPLAY_TEST_PG_MAINT_URL", raising=False)
    with pytest.raises(pytest.fail.Exception):
        pg_gate_plugin._require_maint_url_or_gate()
