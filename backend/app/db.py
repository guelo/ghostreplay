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

# Discard pooled connections older than this at checkout — generic max-lifetime
# hygiene against a socket that went stale while idle in the pool. NOT calibrated
# to any proxy idle timeout: prod reaches Postgres over Railway private networking
# (DATABASE_PRIVATE_URL), which bypasses the public TCP proxy entirely, so 1800 is
# just a conservative default. pool_pre_ping already rejects a stale-at-checkout
# connection, so this is a proactive backstop — and note it does NOT save a
# connection that dies mid-request (only the TCP keepalives below do). See g-q6w5.
DB_POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800"))

_engine_kwargs = {
    "pool_pre_ping": True,
    "pool_size": DB_POOL_SIZE,
    "max_overflow": DB_MAX_OVERFLOW,
    "pool_recycle": DB_POOL_RECYCLE,
}

if DATABASE_URL.startswith("postgresql"):
    # libpq TCP keepalives: detect a dead peer quickly and stop an intermediate
    # proxy from reaping an idle gap *within* a long request. These are
    # psycopg/Postgres-specific connect args — SQLite (tests) rejects them, so
    # only attach them for a Postgres URL.
    _engine_kwargs["connect_args"] = {
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }

engine = create_engine(DATABASE_URL, **_engine_kwargs)


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
