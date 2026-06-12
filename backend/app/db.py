from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.database_url import resolve_database_url

DATABASE_URL = resolve_database_url()

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
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
