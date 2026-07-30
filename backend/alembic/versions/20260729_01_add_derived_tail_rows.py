"""Add game_sessions.derived_tail_rows (g-short-move-rows).

Durable marker for the terminal row reconcile: the count of tail rows derived
from the session's own terminal PGN — by ``POST /api/game/end`` or by the
historical repair script — because the client's final move upload never
committed them. Once derivation runs, the row grid alone can no longer
distinguish a reconciled session from one whose uploads simply arrived
unresolved; the fire-and-forget ``game_ended`` analytics event carries the
same count but may drop. This column is the in-database record that makes
reconcile recurrence measurable. It is written in the same transaction as the
terminal ``status``/``pgn`` mutation and is deliberately NOT part of
``session_upload_receipt`` semantics — the reconcile never writes a receipt.

Nullable with no backfill on purpose: NULL means "no derivation recorded",
which is the correct reading for every session predating the reconcile. The
47-session historical cohort gets its values stamped by the repair script's
guarded per-session transactions, not here.

PostgreSQL path (production). ``ADD COLUMN`` is nullable with no server
default, so it is a metadata-only catalog change. The CHECK is created
VALIDATED: the column is brand new, every existing row holds NULL and
satisfies the predicate by construction, and the ``ADD COLUMN`` already holds
ACCESS EXCLUSIVE for the whole migration transaction. A transaction-local
``lock_timeout`` bounds the lock wait behind live session writers; if it
cannot be acquired promptly the migration aborts (SQLSTATE 55P03) instead of
stalling traffic.

SQLite path (tests only). The column is added WITHOUT the CHECK — SQLite
cannot ALTER-ADD a constraint, and ``batch_alter_table`` rewrites
``game_sessions`` from a reflection that drops its unnamed CHECKs. The
application's SQLite test schema is ``backend/conftest.py``'s hand-written
DDL, which declares the named CHECK, so constraint behaviour stays covered.

Revision ID: 20260729_01
Revises: 20260727_02
Create Date: 2026-07-29

"""
import sqlalchemy as sa
from alembic import op


revision = "20260729_01"
down_revision = "20260727_02"
branch_labels = None
depends_on = None

CHECK_NAME = "ck_game_sessions_derived_tail_rows"
CHECK_CONDITION = "derived_tail_rows IS NULL OR derived_tail_rows > 0"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == "postgresql":
        # Transaction-local: bounds the ACCESS EXCLUSIVE lock wait and resets
        # at COMMIT. Ahead of the DDL so a stuck queue aborts fast.
        op.execute("SET LOCAL lock_timeout = '5s'")

    op.add_column(
        "game_sessions",
        sa.Column("derived_tail_rows", sa.Integer(), nullable=True),
    )

    if dialect == "postgresql":
        op.create_check_constraint(CHECK_NAME, "game_sessions", CHECK_CONDITION)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(CHECK_NAME, "game_sessions", type_="check")

    op.drop_column("game_sessions", "derived_tail_rows")
