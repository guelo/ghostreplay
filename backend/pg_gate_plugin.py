"""PostgreSQL test gate + fixtures (g-accuracy-schema).

Importable pytest plugin that owns everything PostgreSQL-backed tests need:

- the ``pg_gate`` marker (aliased as ``pg_required``) and its skip/fail gate,
- the fixed ``REQUIRED_PG_GATE_TESTS`` / ``REQUIRED_PG_GATE_PARAM_CASES``
  manifests plus the required-mode collection guards and skip-promotion
  hookwrapper that make the gate fail closed rather than pass with missing
  coverage,
- the shared migrated-schema fixtures (``pg_engine`` / ``pg_session_factory`` /
  ``pg_client``) moved out of ``conftest.py``, and
- ``pg_migration_db``, a disposable-database fixture for migration tests that
  need to upgrade a fresh database from base.

All environment reads happen at fixture / collection call time (never frozen at
import) so a test can monkeypatch the relevant variables and the gate reacts.

Gate policy (see ``_pg_url`` / ``_require_pg``):

- Developer default (no URL, ``GHOSTREPLAY_REQUIRE_PG_TESTS`` unset): PG-backed
  tests SKIP cleanly.
- Required mode (``GHOSTREPLAY_REQUIRE_PG_TESTS=1``): a missing URL FAILS instead
  of skipping, so CI cannot silently drop PostgreSQL coverage.

``conftest.py`` activates this via ``pytest_plugins`` and re-exports
``pg_required`` / ``pg_gate`` so ``from conftest import pg_required`` keeps
working.

The required PostgreSQL gate command (CI and the release rehearsal) is::

    GHOSTREPLAY_REQUIRE_PG_TESTS=1 \\
    GHOSTREPLAY_TEST_PG_URL="postgresql://.../ghostreplay_test" \\
    GHOSTREPLAY_TEST_PG_MAINT_URL="postgresql://.../postgres" \\
    pytest -m pg_gate --strict-markers -rs
"""

from __future__ import annotations

import os
import pathlib
import re
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Environment reads (always call-time, never module-level constants).
# ---------------------------------------------------------------------------


def _pg_url() -> str | None:
    """URL of the shared PostgreSQL test database, or None when unset."""
    return os.getenv("GHOSTREPLAY_TEST_PG_URL") or os.getenv("TEST_DATABASE_URL_PG")


def _pg_maint_url() -> str | None:
    """Maintenance URL used ONLY to CREATE/DROP disposable databases.

    Deliberately separate from the app/test URL: authority to create and drop
    databases must come from an explicitly-provisioned maintenance connection,
    never from the connection the tests run their queries on.
    """
    return os.getenv("GHOSTREPLAY_TEST_PG_MAINT_URL")


def _require_pg() -> bool:
    """True when missing PostgreSQL URLs must FAIL rather than skip."""
    return os.getenv("GHOSTREPLAY_REQUIRE_PG_TESTS") == "1"


# ---------------------------------------------------------------------------
# Marker + gate.
#
# ``pg_gate`` is the Release-A PostgreSQL gate marker: it identifies exactly the
# migration and concurrency proofs that the required-mode CI command
# (``GHOSTREPLAY_REQUIRE_PG_TESTS=1 pytest -m pg_gate``) must run against a real
# PostgreSQL. ``pg_required`` is a backward-compatible alias so the many
# ``from conftest import pg_required`` / ``@pg_required`` call sites keep applying
# this same marker object. The pre-existing analysis-cache and position-analysis
# PostgreSQL suites deliberately do NOT use this marker (they define their own
# module-local ``skipif``), so they stay out of the Release-A gate and keep their
# own skip behaviour.
# ---------------------------------------------------------------------------

pg_gate = pytest.mark.pg_gate
pg_required = pg_gate  # alias: both apply the pg_gate marker object.


# Fixed manifest of every Release-A PostgreSQL gate test, keyed by node identity
# (``path::function`` with any parametrization stripped). In required mode the
# collection guard fails hard if any identity here is absent from the gated
# selection, so a deleted / renamed / accidentally-unmarked invariant cannot
# silently drop out of CI coverage. Keep in lockstep with the ``@pg_gate``
# decorations across the Release-A test files.
REQUIRED_PG_GATE_TESTS = frozenset({
    # game-end / post-end /moves cached-accuracy write hooks (g-accuracy-hooks)
    "test_accuracy_hooks.py::test_pg_game_end_first_then_late_moves_heals",
    "test_accuracy_hooks.py::test_pg_game_end_lock_serializes_concurrent_late_moves",
    "test_accuracy_hooks.py::test_pg_moves_first_then_game_end_sees_committed_inputs",
    "test_accuracy_hooks.py::test_pg_moves_lock_serializes_concurrent_game_end",
    # checkmate final-ply eval backfill: Phase A REPEATABLE READ read-only snapshot,
    # parent-session FOR NO KEY UPDATE lock, and cached-accuracy recompute on the
    # migrated schema (g-eh2w data repair for g-hs78)
    "test_backfill_checkmate_final_ply_evals.py::test_pg_run_recomputes_accuracy_and_bumps_under_real_locks",
    # blunder NKU idempotency (g-writer-locks)
    "test_blunder_api.py::test_record_blunder_concurrent_same_key_records_once",
    # advisory lock before the first graph write + cursor-is-last on the
    # first-blunder path (g-n6c2). Postgres-only by necessity: the lock is a no-op
    # off Postgres, so the SQLite suite is blind to its position.
    "test_blunder_api.py::test_pg_blunder_advisory_lock_precedes_writes_and_cursor_is_last",
    # Avg CPL aggregates reach round_half_up_cpl as a Decimal, un-cast (g-22t8.5).
    # SQLite's AVG already returns a float, so this cast guard only bites on the real
    # dialect — it is the one check a float() regression cannot pass.
    "test_centipawn_loss.py::test_pg_cpl_aggregates_reach_helper_as_decimal",
    # branch-scoped route / next-opponent stale-write locks (g-branch-locks)
    "test_branch_locks.py::test_next_opponent_releases_lock_before_engine_so_moves_commits",
    "test_branch_locks.py::test_next_opponent_stale_converted_falls_through",
    "test_branch_locks.py::test_next_opponent_stale_failed_returns_400",
    "test_branch_locks.py::test_route_check_off_route_yields_to_concurrent_root_reached",
    "test_branch_locks.py::test_route_check_root_reached_snapshot_preserves_concurrent_failure",
    "test_branch_locks.py::test_route_check_target_reached_yields_to_concurrent_failure",
    # per-user graph-write advisory lock (g-graph-lock)
    "test_graph_write_lock.py::test_recording_times_out_and_persists_nothing_when_lock_held",
    "test_graph_write_lock.py::test_recording_vs_recording_serialize",
    "test_graph_write_lock.py::test_reverted_lock_reproduces_opposite_order_deadlock",
    "test_graph_write_lock.py::test_worker_vs_recording_serialize",
    # rated game-end users-row lock + games_played-first durable head (g-rating-serial)
    "test_rating_serialize.py::test_concurrent_double_end_one_session_loser_gets_400",
    "test_rating_serialize.py::test_cursor_writer_completes_while_end_paused_in_rating",
    "test_rating_serialize.py::test_same_user_distinct_session_ends_chain_cleanly",
    "test_rating_serialize.py::test_users_lock_prevents_lost_games_played_update",
    # session /moves shared-graph advisory serialization (g-graph-lock)
    "test_session_graph_lock.py::test_moves_concurrent_same_opening_serialize",
    "test_session_graph_lock.py::test_moves_does_not_block_on_held_lock_production_shape",
    "test_session_graph_lock.py::test_moves_graph_lock_retry_succeeds",
    "test_session_graph_lock.py::test_moves_graph_lock_timeout_degrades",
    # SRS review NKU idempotency (g-writer-locks)
    "test_srs_api.py::test_srs_review_concurrent_same_key_single_row",
    # SRS/moves cross-root deadlock matrix (g-writer-locks); param cases pinned below
    "test_writer_locks.py::test_srs_moves_cross_root_lock_matrix",
    # Release-A schema migration on a disposable PostgreSQL DB (g-accuracy-schema)
    "test_release_a_migrations.py::test_pg_disposable_release_a_migration",
})

# The SRS/moves cross-root lock matrix must run all four session/blunder lock
# combinations. Pin the exact bracketed case IDs so silently dropping any row of
# the matrix (e.g. the both-FOR-UPDATE deadlock case) fails the gate rather than
# quietly shrinking it.
REQUIRED_PG_GATE_PARAM_CASES = frozenset({
    "test_writer_locks.py::test_srs_moves_cross_root_lock_matrix[both_for_update]",
    "test_writer_locks.py::test_srs_moves_cross_root_lock_matrix[both_nku]",
    "test_writer_locks.py::test_srs_moves_cross_root_lock_matrix[session_fu_blunder_nku]",
    "test_writer_locks.py::test_srs_moves_cross_root_lock_matrix[session_nku_blunder_fu]",
})


def pytest_configure(config: pytest.Config) -> None:
    # Registering the marker keeps it valid under --strict-markers.
    config.addinivalue_line(
        "markers",
        "pg_gate: Release-A PostgreSQL migration/concurrency proof. Needs "
        "GHOSTREPLAY_TEST_PG_URL; skips in developer-default mode, and under "
        "GHOSTREPLAY_REQUIRE_PG_TESTS=1 a missing URL (or any residual skip) FAILS.",
    )


def _is_gated(item: pytest.Item) -> bool:
    """True for a ``@pg_gate`` (== ``@pg_required``) test."""
    return item.get_closest_marker("pg_gate") is not None


def _gate_identity(nodeid: str) -> str:
    """Function identity of a node id, with any ``[param]`` suffix stripped."""
    return nodeid.split("[", 1)[0]


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Gate ``@pg_gate`` tests on the PostgreSQL URL, at setup time."""
    if not _is_gated(item):
        return
    if _pg_url():
        return
    if _require_pg():
        pytest.fail(
            "GHOSTREPLAY_REQUIRE_PG_TESTS=1 but GHOSTREPLAY_TEST_PG_URL is not set",
            pytrace=False,
        )
    pytest.skip("GHOSTREPLAY_TEST_PG_URL not set; PostgreSQL-backed test skipped")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Required-mode collection guards (no-ops in developer-default mode).

    ``trylast`` so this runs AFTER pytest's own ``-m`` / ``-k`` deselection and
    therefore validates the tests that will actually run. Together these make it
    impossible for the required PostgreSQL gate to pass while silently running
    zero — or an incomplete set of — the Release-A invariants:

    * an empty gated selection is a hard ``UsageError`` (a marker typo or an
      over-narrow ``-k`` would otherwise report "0 selected" and exit green);
    * every identity in ``REQUIRED_PG_GATE_TESTS`` must be collected; and
    * every case in ``REQUIRED_PG_GATE_PARAM_CASES`` must be collected.
    """
    if not _require_pg():
        return
    gated = [item for item in items if _is_gated(item)]
    if not gated:
        raise pytest.UsageError(
            "GHOSTREPLAY_REQUIRE_PG_TESTS=1 but no @pg_gate tests were selected; "
            "the PostgreSQL release gate would report success with zero coverage."
        )
    collected_ids = {_gate_identity(item.nodeid) for item in gated}
    missing = sorted(REQUIRED_PG_GATE_TESTS - collected_ids)
    if missing:
        raise pytest.UsageError(
            "@pg_gate manifest incomplete under GHOSTREPLAY_REQUIRE_PG_TESTS=1; "
            "these required test identities were not collected: " + ", ".join(missing)
        )
    collected_cases = {item.nodeid for item in gated if "[" in item.nodeid}
    missing_cases = sorted(REQUIRED_PG_GATE_PARAM_CASES - collected_cases)
    if missing_cases:
        raise pytest.UsageError(
            "@pg_gate matrix incomplete under GHOSTREPLAY_REQUIRE_PG_TESTS=1; "
            "these required parametrized cases were not collected: "
            + ", ".join(missing_cases)
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """Promote any residual skip on a ``@pg_gate`` test to a failure in required mode.

    The setup gate already turns a missing URL into a failure, but a test body or
    fixture could still ``pytest.skip(...)`` (or carry a ``skip``/``xfail`` marker)
    for some other reason. In required mode that would be an invisible hole in the
    gate, so convert every such skipped report into a failure — **including an
    xfailed one** (a failing ``@pytest.mark.xfail`` / ``pytest.xfail()`` is reported
    as ``outcome="skipped"`` with ``wasxfail``, and an xfailed required proof must
    not exit green). Developer-default mode is untouched, so ``@pg_gate`` tests
    still skip cleanly without a URL.

    This wrapper is registered after core (via conftest ``pytest_plugins``), so it
    is the outermost makereport wrapper and observes the report *after* the core
    skipping plugin has already converted a failing xfail into a skip.
    """
    report = yield
    if _require_pg() and _is_gated(item) and report.skipped:
        was_xfail = hasattr(report, "wasxfail")
        report.outcome = "failed"
        report.longrepr = (
            f"@pg_gate test {item.nodeid} was "
            f"{'XFAILED' if was_xfail else 'SKIPPED'} under "
            f"GHOSTREPLAY_REQUIRE_PG_TESTS=1 (residual "
            f"{'xfail' if was_xfail else 'skip'} promoted to failure): "
            f"{report.longrepr}"
        )
    return report


# ---------------------------------------------------------------------------
# Shared migrated-schema fixtures (moved verbatim from conftest.py).
#
# These exercise behaviour SQLite cannot: real SELECT ... FOR UPDATE row locks
# and the partial unique index on blunder_reviews. The schema under test is the
# ALEMBIC-MIGRATED one (never create_all from models, never drop_all), so PG
# behaviour tests always exercise the real migrated DDL. Session-scoped schema;
# per-test isolation via TRUNCATE.
# ---------------------------------------------------------------------------


def _normalized_pg_url(raw: str) -> str:
    # Imported lazily so plugin import stays cheap and app-independent at collect.
    from app.database_url import _normalize_postgres_scheme

    return _normalize_postgres_scheme(raw)


@pytest.fixture(scope="session")
def pg_engine():
    url = _pg_url()
    if not url:
        if _require_pg():
            pytest.fail(
                "GHOSTREPLAY_REQUIRE_PG_TESTS=1 but GHOSTREPLAY_TEST_PG_URL is not set",
                pytrace=False,
            )
        pytest.skip("GHOSTREPLAY_TEST_PG_URL not set")
    url = _normalized_pg_url(url)

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
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app
    from app.models import Base

    table_names = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    with pg_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
        # Re-seed the evidence_epoch singleton the TRUNCATE just removed — its
        # triggers UPDATE ... WHERE id = 1 and silently no-op without the row.
        conn.execute(text("INSERT INTO evidence_epoch (id, value) VALUES (1, 0)"))

    def _override_pg_db():
        db = pg_session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_pg_db
    with patch("app.main.engine", pg_engine), patch(
        "app.main.get_scheduler"
    ), patch("app.main.get_evidence_scheduler"), patch(
        "app.main.get_baseline_scheduler"
    ), patch("app.main.start_prewarm"):
        with TestClient(app) as pg_test_client:
            yield pg_test_client
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Disposable-database fixture for migration tests.
#
# Migration tests need to upgrade a database from base, which the shared
# session-scoped ``pg_engine`` (already at head) cannot provide. ``pg_migration_db``
# creates a throwaway database, yields its URL, and drops it on teardown under a
# strict safety contract so a misconfigured maintenance URL can never touch the
# shared test database or anything else:
#
#   * maintenance authority comes ONLY from GHOSTREPLAY_TEST_PG_MAINT_URL;
#   * every created/dropped name must match ghostreplay_mig_test_<token> and must
#     not equal the shared test database name;
#   * CREATE/DROP run on an autocommit maintenance connection, and teardown first
#     terminates lingering connections to the disposable database;
#   * required mode fails on a missing maintenance URL instead of skipping.
# ---------------------------------------------------------------------------

_DISPOSABLE_DB_RE = re.compile(r"^ghostreplay_mig_test_[0-9a-f]+$")


def _shared_test_db_name() -> str | None:
    """Database name of the shared test URL (guard: never drop this)."""
    raw = _pg_url()
    if not raw:
        return None
    try:
        return make_url(_normalized_pg_url(raw)).database
    except Exception:
        return None


def _assert_disposable(name: str) -> None:
    """Refuse any name that is not a disposable ghostreplay_mig_test_* database.

    Called before BOTH create and drop so a corrupted name can never cause a
    CREATE/DROP against a real database.
    """
    if not _DISPOSABLE_DB_RE.match(name):
        raise RuntimeError(f"refusing to CREATE/DROP non-disposable database name: {name!r}")
    shared = _shared_test_db_name()
    if shared is not None and name == shared:
        raise RuntimeError(f"refusing to CREATE/DROP the shared test database: {name!r}")


def _require_maint_url_or_gate() -> str:
    """Return the normalized maintenance URL, or skip/fail per gate policy.

    Extracted so the required-mode failure path is unit-testable without driving
    a full fixture setup.
    """
    maint_url = _pg_maint_url()
    if not maint_url:
        if _require_pg():
            pytest.fail(
                "GHOSTREPLAY_REQUIRE_PG_TESTS=1 but GHOSTREPLAY_TEST_PG_MAINT_URL is not set",
                pytrace=False,
            )
        pytest.skip("GHOSTREPLAY_TEST_PG_MAINT_URL not set; disposable-DB migration test skipped")
    return _normalized_pg_url(maint_url)


@pytest.fixture
def pg_migration_db():
    maint_url = _require_maint_url_or_gate()
    db_name = f"ghostreplay_mig_test_{uuid.uuid4().hex}"
    _assert_disposable(db_name)  # validate the freshly minted name before touching the server

    # Autocommit: CREATE DATABASE / DROP DATABASE cannot run inside a transaction.
    maint_engine = create_engine(maint_url, isolation_level="AUTOCOMMIT")
    # render_as_string(hide_password=False), NOT str(): str() masks the password
    # as *** and the disposable URL would fail to connect wherever a password is set.
    disposable_url = make_url(maint_url).set(database=db_name).render_as_string(hide_password=False)
    try:
        with maint_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        yield disposable_url
    finally:
        _assert_disposable(db_name)  # re-validate before the drop, defensively
        with maint_engine.connect() as conn:
            # Terminate lingering sessions on the disposable DB so DROP succeeds.
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ).bindparams(d=db_name)
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        maint_engine.dispose()
