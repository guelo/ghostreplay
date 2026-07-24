from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure backend package is importable when running alembic from backend/.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database_url import resolve_database_url  # noqa: E402
from app.migration_guard import (  # noqa: E402
    MIGRATION_APP_NAME,
    MIGRATION_LOCK_TIMEOUT_S,
    _acquire_migration_guard,
    _label_connection,
    _log_backend_pid,
    _migration_test_barrier,
    _release_migration_guard,
    migration_stall_probe,
)
from app.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Use the same database URL resolution as the application.
config.set_main_option("sqlalchemy.url", resolve_database_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # Config.attributes is the test seam: absent in production, so the frozen
    # constant is used; a test passes a Config carrying an override. Read here (not
    # via monkeypatching the freshly-executed module) so the override survives
    # Alembic re-executing env.py on every run.
    lock_timeout_s = context.config.attributes.get(
        "migration_lock_timeout_s", MIGRATION_LOCK_TIMEOUT_S
    )
    # Take the SESSION-scoped guard on a dedicated connection BEFORE the migration
    # connection, and release it in a finally. On PostgreSQL this serializes the
    # whole upgrade process; on every other dialect (and offline mode) it is a no-op.
    guard = _acquire_migration_guard(connectable, lock_timeout_s=lock_timeout_s)
    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)

            try:
                with context.begin_transaction():
                    # INSIDE the transaction Alembic owns — never before it.
                    # Executing anything on the connection before configure() makes
                    # SQLAlchemy autobegin a transaction Alembic then treats as
                    # EXTERNAL, degrading begin_transaction() to a no-op that commits
                    # nothing: the whole run (alembic_version included) is rolled back
                    # at close under NullPool while appearing to succeed, and the
                    # stall probe fires while the row locks are still held. So:
                    # configure first, open the transaction, then label.
                    _label_connection(connection, MIGRATION_APP_NAME)
                    _log_backend_pid(connection, MIGRATION_APP_NAME)
                    context.run_migrations()
                    # Test-only pause seam; a no-op whenever its env var is unset.
                    _migration_test_barrier(connectable)
            finally:
                # After COMMIT — or ROLLBACK — returns: the moment atomic mode's row
                # locks are actually released, and the only place the whole hold can
                # be observed. Consumes/clears state, logs, never raises.
                migration_stall_probe.report()
    finally:
        _release_migration_guard(guard)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
