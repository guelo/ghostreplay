"""PostgreSQL test gate + fixtures (g-accuracy-schema).

Importable pytest plugin that owns everything PostgreSQL-backed tests need:

- the ``pg_required`` marker and its skip/fail gate,
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
``pg_required`` so ``from conftest import pg_required`` keeps working.
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
# Marker + gate. `from conftest import pg_required` re-exports this object.
# ---------------------------------------------------------------------------

pg_required = pytest.mark.pg_required


def pytest_configure(config: pytest.Config) -> None:
    # Registering the marker keeps it valid under --strict-markers.
    config.addinivalue_line(
        "markers",
        "pg_required: test needs a PostgreSQL URL (GHOSTREPLAY_TEST_PG_URL); "
        "skips in developer-default mode, fails when GHOSTREPLAY_REQUIRE_PG_TESTS=1",
    )


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Gate ``@pg_required`` tests on the PostgreSQL URL, at setup time."""
    if item.get_closest_marker("pg_required") is None:
        return
    if _pg_url():
        return
    if _require_pg():
        pytest.fail(
            "GHOSTREPLAY_REQUIRE_PG_TESTS=1 but GHOSTREPLAY_TEST_PG_URL is not set",
            pytrace=False,
        )
    pytest.skip("GHOSTREPLAY_TEST_PG_URL not set; PostgreSQL-backed test skipped")


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
