import os
import threading
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("JWT_SECRET", "test-secret-32-bytes-minimum-length")
# Analytics must never emit during tests. Force-disable UNCONDITIONALLY: a plain
# setdefault would preserve an ambient POSTHOG_DISABLED=false, which combined with
# an ambient POSTHOG_PROJECT_TOKEN makes get_client() build a live client that
# sends real events. Also drop any ambient token so get_client() can never build
# a client even if the disable flag is later changed.
os.environ["POSTHOG_DISABLED"] = "true"
os.environ.pop("POSTHOG_PROJECT_TOKEN", None)

from app.api import session as session_api
from app.db import get_db
from app.main import app
from app.models import (
    GameSession,
    User,
    ensure_evidence_epoch_infrastructure,
)
from app.security import create_access_token, hash_password

# Activate the PostgreSQL gate plugin (fixtures + @pg_gate marker + gate +
# required-mode manifests/guards). `from conftest import pg_required` stays valid
# via the re-export below (pg_required is an alias for the pg_gate marker).
# Request assert-rewriting BEFORE the import so pytest can instrument the plugin.
pytest.register_assert_rewrite("pg_gate_plugin")
pytest_plugins = ["pg_gate_plugin"]
from pg_gate_plugin import pg_gate, pg_required  # noqa: E402,F401

SQLALCHEMY_TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Serializes the ``auth_headers`` users-row seed. The seed runs on the shared
# in-memory SQLite StaticPool connection, which is a single DBAPI connection; the
# fixture is called concurrently from Postgres concurrency tests, so the seed must
# not race on that connection.
_AUTH_SEED_LOCK = threading.Lock()


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
            drill_line TEXT,
            drill_strictness VARCHAR(12),
            drill_strictness_cp INTEGER,
            drill_terminal_reason VARCHAR(20),
            normal_started_at TIMESTAMP,
            converted_at TIMESTAMP,
            rated_start_ply INTEGER,
            drill_root_reached_ply INTEGER,
            recorded_blunder_id INTEGER,
            blunder_idempotency_key VARCHAR(64),
            opening_score_baseline TEXT,
            player_accuracy INTEGER,
            player_accuracy_algo_version SMALLINT,
            derived_tail_rows INTEGER,
            CHECK (session_mode IN ('normal','drill')),
            CONSTRAINT ck_game_sessions_player_accuracy CHECK (player_accuracy IS NULL OR (player_accuracy >= 0 AND player_accuracy <= 100)),
            CONSTRAINT ck_game_sessions_derived_tail_rows CHECK (derived_tail_rows IS NULL OR derived_tail_rows > 0),
            CHECK (drill_state IS NULL OR drill_state IN ('active','root_reached','failed','abandoned','converted')),
            CHECK (drill_strictness IS NULL OR drill_strictness IN ('lenient','standard','strict')),
            CHECK (drill_strictness_cp IS NULL OR (drill_strictness_cp >= 0 AND drill_strictness_cp <= 50)),
            CHECK (drill_terminal_reason IS NULL OR drill_terminal_reason IN ('off_route','accuracy','natural_end')),
            CHECK ((session_mode = 'normal' AND drill_state IS NULL) OR (session_mode = 'drill' AND drill_state IS NOT NULL)),
            CHECK (rated_start_ply IS NULL OR rated_start_ply >= 0),
            CONSTRAINT ck_game_sessions_drill_root_reached_ply CHECK (drill_root_reached_ply IS NULL OR drill_root_reached_ply >= 0),
            CONSTRAINT ck_game_sessions_root_ply_requires_drill CHECK (drill_root_reached_ply IS NULL OR session_mode = 'drill'),
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
            CONSTRAINT ck_blunder_opportunity_reached_implies_opportunity
                CHECK (reached = false OR opportunity = true),
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
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_moves_to_position_id "
        "ON moves (to_position_id)"
    ))
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
            -- g-mk1d: JSON browser-game-v2 dynamic provenance for this move's own
            -- local search; the live operand for the REQUIRES_COMPARISON overlay.
            browser_provenance TEXT,
            UNIQUE(session_id, move_number, color),
            FOREIGN KEY (session_id) REFERENCES game_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (target_blunder_id) REFERENCES blunders(id),
            CHECK (segment IN ('drill', 'normal')),
            CHECK (decision_source IS NULL OR decision_source IN ('ghost_path', 'backend_engine', 'local_fallback'))
        )
    """))
    # g-upload-observe: durable final_full upload receipt. session_id is a PLAIN
    # column (NO FK) so the append-only insert takes no parent-row lock; mirrors
    # the ORM/Alembic definition so backend tests run the same schema.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS session_upload_receipt (
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
    # Release A durable-head index, kept in sync with the ORM/Alembic definition
    # (idx_rating_history_user_chain) so backend tests run against the same
    # rating_history metadata as production.
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_rating_history_user_chain "
        "ON rating_history (user_id, games_played DESC, recorded_at DESC, id DESC)"
    ))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS analysis_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fen_before TEXT NOT NULL,
            normalized_fen_before TEXT,
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
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_analysis_cache_norm_move "
        "ON analysis_cache(normalized_fen_before, move_uci)"
    ))
    # Submitter-eligibility associations (g-v21l). The PAIR is the identity, so
    # it is the composite primary key and there is no surrogate id; the separate
    # reverse-order index serves the viewer-scoped read. Mirrors migration
    # 20260727_01. (SQLite only enforces the ON DELETE CASCADE when
    # ``PRAGMA foreign_keys=ON`` is set on the connection; the clause is declared
    # here so the cascade regression can assert real behavior under that pragma.)
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS analysis_cache_submission (
            analysis_cache_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (analysis_cache_id, user_id),
            FOREIGN KEY (analysis_cache_id)
                REFERENCES analysis_cache(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_analysis_cache_submission_user "
        "ON analysis_cache_submission(user_id, analysis_cache_id)"
    ))
    # Trusted position winner (one per normalized_fen) — see PositionAnalysisRow.
    # Distinct grain from analysis_cache; fen is provenance-only, normalized_fen is
    # the lookup/uniqueness key. Mirrors the 20260617_01 migration.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS position_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            normalized_fen TEXT NOT NULL,
            fen TEXT NOT NULL,
            best_move_uci VARCHAR(5) NOT NULL,
            best_move_san VARCHAR(10),
            best_line_uci TEXT,
            best_eval INTEGER,
            best_eval_mate INTEGER,
            source VARCHAR(20) NOT NULL DEFAULT 'precomputed',
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
            source_cache_id INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_position_analysis_normalized_fen UNIQUE(normalized_fen)
        )
    """))
    # Append-only disagreement audit sink — many rows per normalized_fen, so the
    # FEN is indexed but not unique and there is no updated_at.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS position_analysis_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            normalized_fen TEXT NOT NULL,
            position_analysis_id INTEGER,
            candidate_cache_ids TEXT,
            candidate_summaries TEXT,
            best_move_disagreement TEXT,
            pv_disagreement TEXT,
            best_eval_disagreement TEXT,
            best_eval_mate_disagreement TEXT,
            policy_reason VARCHAR(64),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_position_analysis_conflicts_norm "
        "ON position_analysis_conflicts(normalized_fen)"
    ))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS opening_score_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            player_color VARCHAR(5) NOT NULL,
            generation INTEGER NOT NULL,
            registry_fingerprint TEXT,
            inputs_fingerprint TEXT,
            evidence_seq INTEGER,
            cache_epoch INTEGER,
            scoped_shared_digest TEXT,
            computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, player_color, generation)
        )
    """))
    # evidence_seq: per-(user,color) counter over the PER-USER evidence surfaces
    # (see OpeningScoreCursor.evidence_seq). OUT-OF-BAND-WRITER CONTRACT: anything
    # mutating session_moves / game_sessions eligibility / blunders /
    # blunder_reviews outside the app choke-points must bump
    # OPENING_EVIDENCE_INPUTS_VERSION or advance the affected cursors.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS opening_score_cursors (
            user_id INTEGER NOT NULL,
            player_color VARCHAR(5) NOT NULL,
            latest_generation INTEGER NOT NULL DEFAULT 0,
            evidence_seq INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, player_color)
        )
    """))
    # Global shared-cache change counter (g-jact). The singleton row MUST be
    # seeded: its triggers UPDATE ... WHERE id = 1 and silently no-op when the
    # row is missing (epoch never advances -> freshness never provable). The
    # seed + mandatory shared-table triggers are installed by the shared helper
    # below (single runtime copy, also used by the E2E seed script); the alembic
    # migration 20260708_01 carries its own frozen copy of the same DDL.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS evidence_epoch (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            value INTEGER NOT NULL DEFAULT 0
        )
    """))
    ensure_evidence_epoch_infrastructure(conn)
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS opening_score_batch_shared_scope (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            fen TEXT NOT NULL,
            kind VARCHAR(4) NOT NULL,
            CHECK (kind IN ('raw','norm')),
            FOREIGN KEY (batch_id) REFERENCES opening_score_batches(id) ON DELETE CASCADE
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_opening_score_batch_shared_scope_batch "
        "ON opening_score_batch_shared_scope(batch_id)"
    ))
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
        CREATE TABLE IF NOT EXISTS opening_position_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            player_color VARCHAR(5) NOT NULL,
            parent_fen TEXT NOT NULL,
            child_fen TEXT NOT NULL,
            uci TEXT NOT NULL,
            traversal_count INTEGER NOT NULL DEFAULT 0,
            live_attempts INTEGER NOT NULL DEFAULT 0,
            live_passes INTEGER NOT NULL DEFAULT 0,
            live_fails INTEGER NOT NULL DEFAULT 0,
            computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(batch_id, parent_fen, child_fen),
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
    # g-ghost-target-server-record: authoritative, replayable opponent-decision log.
    # Mirrors the ORM/Alembic definition so backend tests run the same schema. The
    # UNIQUE is the replay key AND the concurrency arbiter (ON CONFLICT DO NOTHING);
    # session_id cascades because retention is session-lifetime with no TTL.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS opponent_decisions (
            decision_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            request_fen_hash TEXT NOT NULL,
            uci_history TEXT NOT NULL,
            ply_before INTEGER NOT NULL,
            served_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            response_payload TEXT NOT NULL,
            target_blunder_id INTEGER,
            resulting_fen TEXT,
            reaches_drill_root BOOLEAN DEFAULT false NOT NULL,
            CONSTRAINT ck_opponent_decisions_ply_before CHECK (ply_before >= 0),
            CONSTRAINT uq_opponent_decisions_session_fingerprint
                UNIQUE (session_id, request_fingerprint),
            FOREIGN KEY (session_id) REFERENCES game_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (target_blunder_id) REFERENCES blunders(id)
        )
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_opponent_decisions_target_served
        ON opponent_decisions (target_blunder_id, served_at)
    """))
    conn.commit()


def _reset_test_schema(conn) -> None:
    # Before game_sessions / blunders: opponent_decisions references both.
    conn.execute(text("DROP TABLE IF EXISTS opponent_decisions"))
    conn.execute(text("DROP TABLE IF EXISTS blunder_reviews"))
    conn.execute(text("DROP TABLE IF EXISTS blunder_opportunity_events"))
    conn.execute(text("DROP TABLE IF EXISTS opening_position_edges"))
    conn.execute(text("DROP TABLE IF EXISTS opening_position_scores"))
    conn.execute(text("DROP TABLE IF EXISTS user_opening_scores"))
    # NB: dropping analysis_cache / position_analysis / analysis_cache_submission
    # below also drops their evidence_epoch triggers (sqlite drops triggers with
    # their table).
    conn.execute(text("DROP TABLE IF EXISTS analysis_cache_submission"))
    conn.execute(text("DROP TABLE IF EXISTS opening_score_batch_shared_scope"))
    conn.execute(text("DROP TABLE IF EXISTS evidence_epoch"))
    conn.execute(text("DROP TABLE IF EXISTS opening_score_cursors"))
    conn.execute(text("DROP TABLE IF EXISTS opening_score_batches"))
    conn.execute(text("DROP TABLE IF EXISTS position_analysis_conflicts"))
    conn.execute(text("DROP TABLE IF EXISTS position_analysis"))
    conn.execute(text("DROP TABLE IF EXISTS analysis_cache"))
    conn.execute(text("DROP TABLE IF EXISTS rating_history"))
    conn.execute(text("DROP TABLE IF EXISTS session_upload_receipt"))
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
    modules so ``/moves`` and SRS review enqueue into a no-op recorder. Also
    patch the source-module facade: game/drill end and cache-reader helpers
    lazy-import it at call time, so bound-alias patches cannot intercept those
    paths. If left live under CI's PostgreSQL ``DATABASE_URL``, the singleton
    daemon outlives the mocked lifespan getter and can deadlock a later
    ``pg_client`` all-table TRUNCATE.

    Tests that need to assert recompute behaviour drive an injected scheduler
    directly or patch the relevant alias/facade themselves.
    """
    with patch("app.api.session.request_recompute") as session_stub, patch(
        "app.api.srs.request_recompute"
    ) as srs_stub, patch("app.opening_score_scheduler.request_recompute"), patch(
        "app.opening_score_delta_lane.enqueue_scoped_delta"
    ):
        yield session_stub, srs_stub


@pytest.fixture(autouse=True)
def _sync_session_evidence():
    """Run /moves evidence side effects synchronously on the request's session.

    In production ``enqueue_session_evidence`` defers the graph/opportunity/
    analysis-cache/recompute pipeline to a background worker thread bound to the
    real ``SessionLocal``. Tests assert on those side effects immediately after
    the response, so patch the bound alias in the API module with a synchronous
    shim that runs the real ``_run_session_move_evidence_side_effects`` on the
    REQUEST's ``db`` — correct for the SQLite default AND the Postgres
    ``_override_pg_db`` path. ``_no_op_recompute_scheduler`` still stubs the
    opening ``request_recompute`` invoked inside the side effects. Tests that need
    the deferred (production-shape) behaviour patch ``enqueue_session_evidence``
    again locally, which overrides this autouse shim for that test only.
    """

    def _run_sync(
        db,
        *,
        session_id,
        user_id,
        player_color,
        evidence_moves,
        move_count,
        recompute_opportunity: bool = True,
        is_final: bool = False,
    ):
        session_api._run_session_move_evidence_side_effects(
            db,
            session_id=session_id,
            user_id=user_id,
            player_color=player_color,
            evidence_moves=evidence_moves,
            move_count=move_count,
            dialect_name=db.bind.dialect.name,
            run_opportunity=recompute_opportunity,
            is_final=is_final,
        )

    with patch("app.api.session.enqueue_session_evidence", _run_sync):
        yield


@pytest.fixture(autouse=True)
def _no_op_baseline_enqueue():
    """Stop /start handlers from spawning the real opening-baseline daemon (g-mxeo).

    In production ``start_game`` / ``start_drill`` call ``enqueue_baseline_snapshot``,
    which enqueues onto the module-singleton ``OpeningBaselineScheduler`` bound to the
    real ``SessionLocal`` — a background thread that would bypass ``TestingSessionLocal``.
    Patch the bound aliases imported into the API modules so the default is a no-op
    recorder. Tests that assert async capture inject their own non-autostart
    ``OpeningBaselineScheduler(session_factory=TestingSessionLocal)`` and patch these
    aliases to enqueue into it, then drive it with ``run_due()``.
    """
    with patch("app.api.game.enqueue_baseline_snapshot") as game_stub, patch(
        "app.api.drills.enqueue_baseline_snapshot"
    ) as drills_stub:
        yield game_stub, drills_stub


def stop_scheduler_daemon(singleton) -> bool:
    """Stop ``singleton``'s worker if it is running, and leave it REUSABLE either way.

    Returns True when a running daemon was actually stopped.

    Unlatching is the part that is easy to omit and impossible to notice:
    ``shutdown()`` latches ``_shutdown``, and every ``enqueue`` returns on that flag
    BEFORE it would call ``start()``. A singleton left latched therefore silently
    drops work for the whole rest of the process, and a later test asserting on
    enqueue behaviour passes against a dead object.

    It runs UNCONDITIONALLY, because a latched singleton need not have a live thread
    to clean up: a test that exercises ``app.main.lifespan`` for real start()s and
    then shutdown()s the baseline and evidence singletons, leaving both
    ``_thread=None, _shutdown=True``. Returning early on "nothing to join" would
    preserve exactly the state this exists to clear. Pinned by
    test_opening_baseline_scheduler.py::test_daemon_containment_leaves_the_singleton_reusable.
    """
    thread = getattr(singleton, "_thread", None)
    stopped = thread is not None and thread.is_alive()
    if stopped:
        # drain=False: queued work must not run against the shared database either —
        # the point is that this daemon does no more work at all.
        singleton.shutdown(drain=False, timeout=10.0)
    # Restore the boundary state under the same condition variable ``shutdown()``
    # sets it under. Pending work is dropped too: no queued job on a REAL singleton
    # is ever legitimate across a test boundary, and leaving one behind would let the
    # next ``start()`` run it against the configured database.
    with singleton._cond:
        singleton._shutdown = False
        singleton._pending.clear()
        singleton._inflight.clear()
        if hasattr(singleton, "_cancel_pending"):
            singleton._cancel_pending = False
    return stopped


@pytest.fixture(autouse=True)
def _stop_leaked_scheduler_daemons():
    """Stop any REAL scheduler singleton a test started, at that test's boundary.

    The four module singletons default their ``session_factory`` to ``SessionLocal``,
    i.e. ``DATABASE_URL`` — which in CI is the SHARED PostgreSQL test database, the
    same one ``pg_client`` truncates between tests. A singleton started once is a
    daemon thread for the REST OF THE PROCESS: it opens its own connections, holds its
    own transactions, and writes on its own schedule, none of which any test owns.
    Observed consequences, all order-dependent and all blaming an innocent test:
    ``pg_client``'s all-table TRUNCATE deadlocking against it, and rows a test just
    seeded not being visible to that test's own request (g-rating-serialize-flake).

    The bound-alias patches above stop the DEFAULT path, but a test that deliberately
    binds a real facade (to prove the endpoint survives a scheduler fault) still
    reaches ``enqueue`` -> ``start()``. Containment belongs here, at the boundary,
    rather than in each such test: bounding the daemon's life to the test that started
    it is what keeps the failure attributable.

    Stopping alone is NOT enough to leave the singleton reusable. ``shutdown()``
    latches ``_shutdown = True``, and every ``enqueue`` returns on that flag BEFORE it
    would call ``start()`` — so a latched singleton silently drops work for the whole
    rest of the run, and a later test asserting on enqueue behaviour would pass
    against a dead object. Clearing the flag after the join restores exactly the
    post-construction state (``_thread`` is already None and ``_pending`` was dropped
    by ``drain=False``), so the next test gets a live singleton again.
    """
    yield
    from app.opening_baseline_scheduler import _scheduler as baseline_singleton
    from app.opening_score_delta_lane import _lane as delta_singleton
    from app.opening_score_scheduler import _scheduler as score_singleton
    from app.session_evidence_scheduler import _scheduler as evidence_singleton

    for singleton in (
        baseline_singleton,
        delta_singleton,
        score_singleton,
        evidence_singleton,
    ):
        stop_scheduler_daemon(singleton)


@pytest.fixture(autouse=True)
def _reset_session_evidence_cache():
    """Clear the per-session opening-evidence replay cache between tests (g-25mp).

    The cache is a module-level in-process LRU keyed by session_id; without a
    reset, a session created in one test could serve a stale replay product to
    another, and the instrumented ``reconstruct_board_sequence`` call counts the
    incremental-replay tests assert on would leak across tests.
    """
    from app.opening_evidence import reset_session_evidence_cache

    reset_session_evidence_cache()
    yield
    reset_session_evidence_cache()


@pytest.fixture(autouse=True)
def _reset_scoped_opening_delta_cache():
    """Keep process-local terminal publications/generations test-isolated."""
    from app.opening_score_delta import reset_scoped_delta_cache

    reset_scoped_delta_cache()
    yield
    reset_scoped_delta_cache()


@pytest.fixture
def client(_db_override):
    # Patch the scheduler getters so the FastAPI lifespan never starts a real daemon
    # thread bound to the production SessionLocal during tests; the
    # _sync_session_evidence / _no_op_baseline_enqueue shims handle endpoint behaviour.
    with patch("app.main.engine", engine), patch(
        "app.main.get_scheduler"
    ) as get_scheduler, patch("app.main.get_delta_lane"), patch(
        "app.main.get_evidence_scheduler"
    ), patch(
        "app.main.get_baseline_scheduler"
    ), patch("app.main.start_prewarm"):
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
        # In production a valid token always maps to a real ``users`` row (register
        # inserts the row, then mints the token off ``user.id``). Rated
        # /api/game/end now takes a FOR NO KEY UPDATE lock on that row and fails
        # closed with 500 when it is missing (g-rating-serial), so mirror the
        # invariant here: idempotently seed the backing row for every token minted.
        # Seed ``username=None`` (SQLite/PG both allow repeated NULLs under the
        # unique-username constraint) so a single test can mint tokens for several
        # distinct user_ids without colliding on the default username.
        #
        # The seed touches the shared in-memory SQLite StaticPool connection, so it
        # must serialize: Postgres concurrency tests call this fixture from several
        # threads at once (e.g. one token per worker), and unguarded concurrent use
        # of the single SQLite connection corrupts cursor state. The lock keeps
        # ``auth_headers`` safe to call concurrently, as it was before it seeded.
        with _AUTH_SEED_LOCK:
            seed = TestingSessionLocal()
            try:
                if seed.get(User, user_id) is None:
                    seed.add(User(id=user_id, username=None, is_anonymous=is_anonymous))
                    seed.commit()
            finally:
                seed.close()
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
