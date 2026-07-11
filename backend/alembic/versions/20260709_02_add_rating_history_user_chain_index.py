"""Add rating_history durable-head index (Release A, g-accuracy-schema).

Creates ``idx_rating_history_user_chain`` over
``(user_id, games_played DESC, recorded_at DESC, id DESC)``. This serves the
"latest rated row for a user" head lookup ordered by games_played first
(``WHERE user_id = ? ORDER BY games_played DESC, recorded_at DESC, id DESC
LIMIT 1``); the trailing DESC columns let Postgres satisfy that ORDER BY from
the index with no Sort node. The existing ``idx_rating_history_user_timestamp``
``(user_id, recorded_at)`` index is left in place for chronological reads.

PostgreSQL path (production): the index is built (and, on downgrade, dropped)
``CONCURRENTLY`` inside an Alembic ``autocommit_block`` so rated game-end writes
to ``rating_history`` stay available for the duration of the build instead of
being blocked by an exclusive lock. ``CREATE INDEX CONCURRENTLY`` cannot run in
a transaction, which is why the autocommit block is required. Record the
production row count and observed build duration in the release run notes.

Operational caveat: a ``CONCURRENTLY`` build that fails partway (crash,
statement cancel, deadlock) can leave an ``INVALID`` index behind. It must be
DROPPED (``DROP INDEX CONCURRENTLY``) and rebuilt from scratch — an invalid
index is never usable and cannot be "validated in place". Check with
``SELECT indisvalid FROM pg_index WHERE ...`` after a failed build.

SQLite path (tests only): the index is created/dropped normally (no concurrent
build, no autocommit block).

Revision ID: 20260709_02
Revises: 20260709_01
Create Date: 2026-07-09

"""
import sqlalchemy as sa
from alembic import op


revision = "20260709_02"
down_revision = "20260709_01"
branch_labels = None
depends_on = None

INDEX_NAME = "idx_rating_history_user_chain"
# user_id leads ASC (equality predicate); the trailing columns are DESC so the
# games-played-first head ORDER BY is answered straight from the index.
INDEX_COLUMNS = [
    "user_id",
    sa.text("games_played DESC"),
    sa.text("recorded_at DESC"),
    sa.text("id DESC"),
]


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == "postgresql":
        # CONCURRENTLY cannot run inside a transaction; autocommit_block commits
        # the surrounding migration txn and runs this statement in autocommit.
        with op.get_context().autocommit_block():
            op.create_index(
                INDEX_NAME,
                "rating_history",
                INDEX_COLUMNS,
                postgresql_concurrently=True,
            )
    else:
        op.create_index(INDEX_NAME, "rating_history", INDEX_COLUMNS)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == "postgresql":
        with op.get_context().autocommit_block():
            op.drop_index(
                INDEX_NAME,
                table_name="rating_history",
                postgresql_concurrently=True,
            )
    else:
        op.drop_index(INDEX_NAME, table_name="rating_history")
