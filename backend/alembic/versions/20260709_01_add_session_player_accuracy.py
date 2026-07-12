"""Add cached session accuracy columns + named range CHECK (Release A, g-accuracy-schema).

Adds ``game_sessions.player_accuracy`` (INTEGER, 0..100 or NULL) and
``game_sessions.player_accuracy_algo_version`` (SMALLINT, NULL) plus the named
range CHECK ``ck_game_sessions_player_accuracy``. This migration defines the
schema; the Release A serving write hooks (g-accuracy-hooks) then maintain the
columns forward from every game-end / post-end /moves upload. Release A performs
no backfill of pre-existing rows and does not switch any read onto the cache —
CHECK validation, the backfill, and the cache-only reads are Release B.

PostgreSQL path (production):

- Both columns are nullable with no server default, so each ``ADD COLUMN`` is a
  metadata-only catalog change (no table rewrite) and is expected to complete in
  sub-second time.
- A transaction-local five-second ``lock_timeout`` bounds how long the
  ``ACCESS EXCLUSIVE`` lock waits behind concurrent session writers; if it cannot
  be acquired promptly the migration aborts (SQLSTATE 55P03) rather than stalling
  live traffic. It resets automatically at the migration's COMMIT.
- The CHECK is created ``NOT VALID``: it is enforced for every INSERT/UPDATE from
  this point on but the existing rows are NOT scanned, so no lengthy validation
  lock is taken. Release A deliberately does NOT ``VALIDATE`` it — validation is
  Release B work once the columns are known-clean.

SQLite path (tests only — production is Postgres): ``batch_alter_table`` recreates
the table with the two columns and a normal (immediately-enforced) named CHECK.

Downgrade drops the named CHECK BEFORE the columns it references, then the two
columns.

Revision ID: 20260709_01
Revises: 20260708_01
Create Date: 2026-07-09

"""
import sqlalchemy as sa
from alembic import op


revision = "20260709_01"
down_revision = "20260708_01"
branch_labels = None
depends_on = None

CHECK_NAME = "ck_game_sessions_player_accuracy"
CHECK_CONDITION = "player_accuracy IS NULL OR (player_accuracy >= 0 AND player_accuracy <= 100)"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == "postgresql":
        # Transaction-local: bounds the ACCESS EXCLUSIVE lock wait and resets at
        # COMMIT. Keep it ahead of the DDL so a stuck queue aborts fast.
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.add_column("game_sessions", sa.Column("player_accuracy", sa.Integer(), nullable=True))
        op.add_column(
            "game_sessions",
            sa.Column("player_accuracy_algo_version", sa.SmallInteger(), nullable=True),
        )
        # NOT VALID: enforce new/updated rows without scanning existing rows.
        # Release A never validates it (Release B does).
        op.create_check_constraint(
            CHECK_NAME,
            "game_sessions",
            CHECK_CONDITION,
            postgresql_not_valid=True,
        )
    else:
        # SQLite cannot ALTER-ADD a CHECK, so recreate the table via batch mode.
        # The CHECK is immediately enforced (SQLite has no NOT VALID).
        with op.batch_alter_table("game_sessions") as batch_op:
            batch_op.add_column(sa.Column("player_accuracy", sa.Integer(), nullable=True))
            batch_op.add_column(
                sa.Column("player_accuracy_algo_version", sa.SmallInteger(), nullable=True)
            )
            batch_op.create_check_constraint(CHECK_NAME, CHECK_CONDITION)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == "postgresql":
        # Drop the CHECK before the columns it references.
        op.drop_constraint(CHECK_NAME, "game_sessions", type_="check")
        op.drop_column("game_sessions", "player_accuracy_algo_version")
        op.drop_column("game_sessions", "player_accuracy")
    else:
        with op.batch_alter_table("game_sessions") as batch_op:
            batch_op.drop_constraint(CHECK_NAME, type_="check")
            batch_op.drop_column("player_accuracy_algo_version")
            batch_op.drop_column("player_accuracy")
