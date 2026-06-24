import os
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.database_url import resolve_database_url

DATABASE_URL = resolve_database_url()

# Pool sizing is env-overridable so it can be tuned against the deployment's
# actual Postgres ``max_connections`` without a code change. Per-user graph-write
# serialization (g-q0aw) means a queued waiter briefly holds a pool connection
# while blocked on the advisory lock (bounded now by lock_timeout), and the
# opening-score scheduler daemon thread also holds one — so the default of 5 was
# tight. WARNING: the effective ceiling is (pool_size + max_overflow) PER PROCESS;
# multiply by worker/replica count before raising these against max_connections.
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "10"))

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
)


if engine.dialect.name == "sqlite":
    # SQLite ignores foreign keys (and thus ON DELETE CASCADE) unless this pragma
    # is enabled per connection. Without it, deleting a parent row orphans its
    # children — e.g. pruning an OpeningScoreBatch leaves user_opening_scores rows
    # behind, and rowid reuse then collides on (batch_id, opening_key).
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_conn, _record):  # pragma: no cover - thin
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
