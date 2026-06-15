import os
import pathlib
import uuid
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("JWT_SECRET", "test-secret-32-bytes-minimum-length")

from app.database_url import _normalize_postgres_scheme
from app.db import get_db
from app.main import app
from app.models import Base, GameSession, User
from app.security import create_access_token, hash_password

SQLALCHEMY_TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _create_test_schema(conn) -> None:
    conn.execute(text("PRAGMA foreign_keys=ON"))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(50) UNIQUE,
            password_hash VARCHAR(255),
            is_anonymous BOOLEAN NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS game_sessions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            started_at TIMESTAMP NOT NULL,
            ended_at TIMESTAMP,
            status VARCHAR(20) NOT NULL,
            result VARCHAR(20),
            engine_elo INTEGER NOT NULL,
            blunder_recorded BOOLEAN NOT NULL DEFAULT 0,
            is_rated BOOLEAN NOT NULL DEFAULT 1,
            player_color VARCHAR(5) NOT NULL DEFAULT 'white',
            pgn TEXT,
            session_mode VARCHAR(10) NOT NULL DEFAULT 'normal',
            drill_state VARCHAR(12),
            drill_opening_key TEXT,
            drill_strictness VARCHAR(12),
            drill_strictness_cp INTEGER,
            drill_terminal_reason VARCHAR(20),
            normal_started_at TIMESTAMP,
            converted_at TIMESTAMP,
            rated_start_ply INTEGER,
            recorded_blunder_id INTEGER,
            blunder_idempotency_key VARCHAR(64),
            CHECK (session_mode IN ('normal','drill')),
            CHECK (drill_state IS NULL OR drill_state IN ('active','root_reached','failed','abandoned','converted')),
            CHECK (drill_strictness IS NULL OR drill_strictness IN ('lenient','standard','strict')),
            CHECK (drill_strictness_cp IS NULL OR (drill_strictness_cp >= 0 AND drill_strictness_cp <= 50)),
            CHECK (drill_terminal_reason IS NULL OR drill_terminal_reason IN ('off_route','accuracy','natural_end')),
            CHECK ((session_mode = 'normal' AND drill_state IS NULL) OR (session_mode = 'drill' AND drill_state IS NOT NULL)),
            CHECK (rated_start_ply IS NULL OR rated_start_ply >= 0),
            CHECK (
                session_mode = 'normal'
                OR (
                    drill_state = 'converted'
                    AND is_rated = true
                    AND normal_started_at IS NOT NULL
                    AND converted_at IS NOT NULL
                    AND rated_start_ply IS NOT NULL
                )
                OR (
                    drill_state IN ('active','root_reached','failed','abandoned')
                    AND is_rated = false
                    AND rated_start_ply IS NULL
                )
            )
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            fen_hash VARCHAR(64) NOT NULL,
            fen_raw TEXT NOT NULL,
            active_color VARCHAR(5) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, fen_hash)
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS blunders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            position_id INTEGER NOT NULL,
            bad_move_san VARCHAR(10) NOT NULL,
            best_move_san VARCHAR(10) NOT NULL,
            eval_loss_cp INTEGER NOT NULL,
            pass_streak INTEGER NOT NULL DEFAULT 0,
            last_reviewed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            source_session_id TEXT,
            opening_family TEXT,
            UNIQUE(user_id, position_id),
            FOREIGN KEY (position_id) REFERENCES positions(id),
            FOREIGN KEY (source_session_id) REFERENCES game_sessions(id)
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS blunder_opportunity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            blunder_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            occurred_at TIMESTAMP,
            opportunity BOOLEAN NOT NULL,
            reached BOOLEAN NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id, blunder_id),
            FOREIGN KEY (blunder_id) REFERENCES blunders(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES game_sessions(id) ON DELETE CASCADE
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS moves (
            from_position_id INTEGER NOT NULL,
            move_san VARCHAR(10) NOT NULL,
            to_position_id INTEGER NOT NULL,
            PRIMARY KEY (from_position_id, move_san),
            FOREIGN KEY (from_position_id) REFERENCES positions(id),
            FOREIGN KEY (to_position_id) REFERENCES positions(id)
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS session_moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            move_number INTEGER NOT NULL,
            color VARCHAR(5) NOT NULL,
            move_san VARCHAR(10) NOT NULL,
            fen_after TEXT NOT NULL,
            eval_cp INTEGER,
            eval_mate INTEGER,
            best_move_san VARCHAR(10),
            best_move_eval_cp INTEGER,
            eval_delta INTEGER,
            classification VARCHAR(20),
            fen_before TEXT,
            best_move_uci VARCHAR(5),
            best_line_uci TEXT,
            decision_source VARCHAR(20),
            target_blunder_id INTEGER,
            segment VARCHAR(10) NOT NULL DEFAULT 'normal',
            UNIQUE(session_id, move_number, color),
            FOREIGN KEY (session_id) REFERENCES game_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (target_blunder_id) REFERENCES blunders(id),
            CHECK (segment IN ('drill', 'normal')),
            CHECK (decision_source IS NULL OR decision_source IN ('ghost_path', 'backend_engine', 'local_fallback'))
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS rating_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            game_session_id TEXT NOT NULL,
            rating INTEGER NOT NULL,
            is_provisional BOOLEAN NOT NULL,
            games_played INTEGER NOT NULL,
            chesscom_rating FLOAT,
            chesscom_rd FLOAT,
            lichess_rating FLOAT,
            lichess_rd FLOAT,
            lichess_volatility FLOAT,
            recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(game_session_id)
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS analysis_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fen_before TEXT NOT NULL,
            move_uci VARCHAR(5) NOT NULL,
            move_san VARCHAR(10) NOT NULL,
            best_move_uci VARCHAR(5),
            best_move_san VARCHAR(10),
            best_line_uci TEXT,
            played_eval INTEGER,
            played_eval_mate INTEGER,
            best_eval INTEGER,
            best_eval_mate INTEGER,
            eval_delta INTEGER,
            classification VARCHAR(20),
            source VARCHAR(20) NOT NULL DEFAULT 'game',
            analysis_profile_id VARCHAR(64),
            engine_name VARCHAR(64),
            engine_version VARCHAR(64),
            engine_build VARCHAR(128),
            network_id VARCHAR(128),
            search_limit_type VARCHAR(16),
            search_limit_value INTEGER,
            threads INTEGER,
            hash_mb INTEGER,
            multipv INTEGER,
            eval_file_id TEXT,
            eval_file_small_id TEXT,
            analyzer_protocol_version VARCHAR(64),
            profile_manifest_digest VARCHAR(64),
            evidence_contract_id VARCHAR(64),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(fen_before, move_uci)
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS opening_score_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            player_color VARCHAR(5) NOT NULL,
            generation INTEGER NOT NULL,
            registry_fingerprint TEXT,
            inputs_fingerprint TEXT,
            computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, player_color, generation)
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS opening_score_cursors (
            user_id INTEGER NOT NULL,
            player_color VARCHAR(5) NOT NULL,
            latest_generation INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, player_color)
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS user_opening_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            player_color VARCHAR(5) NOT NULL,
            opening_key TEXT NOT NULL,
            opening_name TEXT NOT NULL,
            opening_family TEXT NOT NULL,
            opening_score FLOAT NOT NULL,
            confidence FLOAT NOT NULL,
            coverage FLOAT NOT NULL,
            weighted_depth FLOAT NOT NULL,
            sample_size INTEGER NOT NULL,
            game_count INTEGER NOT NULL DEFAULT 0,
            last_practiced_at TIMESTAMP,
            strongest_branch_name TEXT,
            strongest_branch_key TEXT,
            strongest_branch_score FLOAT,
            weakest_branch_name TEXT,
            weakest_branch_key TEXT,
            weakest_branch_score FLOAT,
            underexposed_branch_name TEXT,
            underexposed_branch_key TEXT,
            underexposed_branch_value FLOAT,
            computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(batch_id, opening_key),
            FOREIGN KEY (batch_id) REFERENCES opening_score_batches(id) ON DELETE CASCADE
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS opening_position_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            player_color VARCHAR(5) NOT NULL,
            normalized_fen TEXT NOT NULL,
            in_book BOOLEAN NOT NULL,
            has_evidence BOOLEAN NOT NULL,
            opening_score FLOAT,
            confidence FLOAT,
            coverage FLOAT,
            weighted_depth FLOAT,
            sample_size INTEGER NOT NULL DEFAULT 0,
            game_count INTEGER NOT NULL DEFAULT 0,
            last_practiced_at TIMESTAMP,
            computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(batch_id, normalized_fen),
            FOREIGN KEY (batch_id) REFERENCES opening_score_batches(id) ON DELETE CASCADE
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS blunder_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            blunder_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            reviewed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            passed BOOLEAN NOT NULL,
            move_played_san VARCHAR(10) NOT NULL,
            eval_delta_cp INTEGER NOT NULL,
            idempotency_key VARCHAR(64),
            pass_streak_after INTEGER,
            FOREIGN KEY (blunder_id) REFERENCES blunders(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES game_sessions(id),
            UNIQUE(blunder_id, idempotency_key)
        )
    """))
    conn.commit()


def _reset_test_schema(conn) -> None:
    conn.execute(text("DROP TABLE IF EXISTS blunder_reviews"))
    conn.execute(text("DROP TABLE IF EXISTS blunder_opportunity_events"))
    conn.execute(text("DROP TABLE IF EXISTS opening_position_scores"))
    conn.execute(text("DROP TABLE IF EXISTS user_opening_scores"))
    conn.execute(text("DROP TABLE IF EXISTS opening_score_cursors"))
    conn.execute(text("DROP TABLE IF EXISTS opening_score_batches"))
    conn.execute(text("DROP TABLE IF EXISTS analysis_cache"))
    conn.execute(text("DROP TABLE IF EXISTS rating_history"))
    conn.execute(text("DROP TABLE IF EXISTS session_moves"))
    conn.execute(text("DROP TABLE IF EXISTS moves"))
    conn.execute(text("DROP TABLE IF EXISTS blunders"))
    conn.execute(text("DROP TABLE IF EXISTS positions"))
    conn.execute(text("DROP TABLE IF EXISTS game_sessions"))
    conn.execute(text("DROP TABLE IF EXISTS users"))
    conn.commit()
    _create_test_schema(conn)


def _override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _db_override():
    app.dependency_overrides[get_db] = _override_get_db
    with engine.connect() as conn:
        _reset_test_schema(conn)
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _no_op_recompute_scheduler():
    """Stop API endpoints from touching the real opening-score scheduler.

    The scheduler runs recomputes on its own thread against its own
    ``SessionLocal`` session, which would bypass ``TestingSessionLocal`` and
    never coalesce in tests. Patch the bound aliases imported into the API
    modules so ``/moves`` and SRS review enqueue into a no-op recorder. Tests
    that need to assert recompute behaviour drive the scheduler directly or
    patch these aliases themselves.
    """
    with patch("app.api.session.request_recompute") as session_stub, patch(
        "app.api.srs.request_recompute"
    ) as srs_stub:
        yield session_stub, srs_stub


@pytest.fixture
def client(_db_override):
    with patch("app.main.engine", engine), patch("app.main.get_scheduler") as get_scheduler:
        with TestClient(app) as client:
            yield client


@pytest.fixture
def db_session(_db_override):
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def auth_headers():
    def _auth_headers(user_id: int = 123, username: str = "ghost_test", is_anonymous: bool = True) -> dict:
        token = create_access_token(user_id=user_id, username=username, is_anonymous=is_anonymous)
        return {"Authorization": f"Bearer {token}"}

    return _auth_headers


@pytest.fixture
def create_user(db_session):
    def _create_user(username: str, password: str, is_anonymous: bool = True) -> User:
        user = User(
            username=username,
            password_hash=hash_password(password),
            is_anonymous=is_anonymous,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        return user

    return _create_user


# ---------------------------------------------------------------------------
# PostgreSQL-backed fixtures (opt-in via GHOSTREPLAY_TEST_PG_URL).
#
# These exercise behaviour SQLite cannot: real SELECT ... FOR UPDATE row locks
# and the partial unique index on blunder_reviews. Tests decorated with
# @pg_required skip cleanly when no Postgres URL is configured (e.g. locally),
# and run for real in CI where the postgres service is available.
#
# The schema under test is the ALEMBIC-MIGRATED one (never create_all from
# models, never drop_all), so PG behaviour tests always exercise the real
# migrated DDL — including the partial unique index and BigInteger columns the
# model metadata alone would not validate. The schema is session-scoped and
# per-test isolation is via TRUNCATE.
# ---------------------------------------------------------------------------

_PG_URL = os.getenv("GHOSTREPLAY_TEST_PG_URL")

pg_required = pytest.mark.skipif(
    not _PG_URL,
    reason="GHOSTREPLAY_TEST_PG_URL not set; PostgreSQL-backed tests skipped",
)


@pytest.fixture(scope="session")
def pg_engine():
    if not _PG_URL:
        pytest.skip("GHOSTREPLAY_TEST_PG_URL not set")
    url = _normalize_postgres_scheme(_PG_URL)

    # Ensure the migrated schema via Alembic (idempotent: a no-op when CI has
    # already run `alembic upgrade head`). env.py resolves the URL from
    # DATABASE_URL, so point it at the test DB for the duration of the upgrade.
    alembic_ini = pathlib.Path(__file__).resolve().parent / "alembic.ini"
    prior_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(Config(str(alembic_ini)), "head")
    finally:
        if prior_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior_database_url

    pg = create_engine(url)
    yield pg
    pg.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=pg_engine)


@pytest.fixture
def pg_client(pg_engine, pg_session_factory):
    """TestClient backed by Postgres, with per-test truncation for isolation.

    Overrides get_db AFTER the autouse SQLite ``_db_override`` so Postgres wins.
    Each request gets its own session, so concurrent requests can contend for
    real row locks.
    """
    table_names = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    with pg_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))

    def _override_pg_db():
        db = pg_session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_pg_db
    with patch("app.main.engine", pg_engine), patch("app.main.get_scheduler"):
        with TestClient(app) as pg_test_client:
            yield pg_test_client
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def create_game_session(client, auth_headers, db_session):
    def _create_game_session(
        user_id: int = 123,
        player_color: str = "white",
        blunder_recorded: bool = False,
    ) -> str:
        response = client.post(
            "/api/game/start",
            json={"engine_elo": 1500, "player_color": player_color},
            headers=auth_headers(user_id=user_id),
        )
        assert response.status_code == 201
        session_id = response.json()["session_id"]

        if blunder_recorded:
            session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
            if session:
                session.blunder_recorded = True
                db_session.commit()

        return session_id

    return _create_game_session
