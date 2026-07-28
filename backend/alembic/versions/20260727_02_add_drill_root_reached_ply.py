"""Add game_sessions.drill_root_reached_ply (g-root-confirm-api).

The drill's EVIDENCE BOUNDARY: the ply at which the opening root was CONFIRMED reached.
Plies at or before it are scripted route play — the drill steers the player there — so
counting them as ghost-steering opportunities is what inflated the SRS opportunity
counters and collapsed steering onto a single target.

Nullable with no backfill on purpose. The column is stamped only by the route-check
confirmation, which validates the arrival against a server-recorded opponent decision
before writing; serving the route move that WOULD reach the root does not stamp it. NULL
therefore means "no confirmed root", which is the correct reading for every session
predating confirmation — a boundary invented here would be a claim no client ever made.
Legacy FEN reconstruction is a separate, explicitly one-shot backfill.

``ck_game_sessions_root_ply_requires_drill`` keeps the column drill-only: a normal
session has no route and no root, so a ply there could only be a write-path bug.

PostgreSQL path (production). ``ADD COLUMN`` is nullable with no server default, so it
is a metadata-only catalog change. Both CHECKs are created VALIDATED rather than
``NOT VALID`` (Release A's pattern): the column is brand new, so every existing row holds
NULL and satisfies both predicates by construction, and the ``ADD COLUMN`` above already
holds ``ACCESS EXCLUSIVE`` for the whole migration transaction — deferring validation
would not shorten that hold, it would only leave two permanently unproven constraints in
the catalog. A transaction-local ``lock_timeout`` bounds how long the lock waits behind
live session writers; if it cannot be acquired promptly the migration aborts (SQLSTATE
55P03) instead of stalling traffic.

SQLite path (tests only). The column is added WITHOUT the CHECKs. SQLite cannot
ALTER-ADD a constraint, and ``batch_alter_table`` would rewrite ``game_sessions`` from a
reflection that does NOT carry its unnamed CHECKs — verified: batch mode silently drops
``drill_state IN (...)`` and ``rated_start_ply >= 0``. Trading real constraints for
test-only ones is not a trade worth making. The application's SQLite test schema is
``backend/conftest.py``'s hand-written DDL, which declares both CHECKs, so constraint
behaviour stays covered.

Revision ID: 20260727_02
Revises: 20260727_01
Create Date: 2026-07-27

"""
import sqlalchemy as sa
from alembic import op


revision = "20260727_02"
down_revision = "20260727_01"
branch_labels = None
depends_on = None

NON_NEGATIVE_CHECK = "ck_game_sessions_drill_root_reached_ply"
NON_NEGATIVE_CONDITION = "drill_root_reached_ply IS NULL OR drill_root_reached_ply >= 0"
DRILL_ONLY_CHECK = "ck_game_sessions_root_ply_requires_drill"
DRILL_ONLY_CONDITION = "drill_root_reached_ply IS NULL OR session_mode = 'drill'"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == "postgresql":
        # Transaction-local: bounds the ACCESS EXCLUSIVE lock wait and resets at
        # COMMIT. Ahead of the DDL so a stuck queue aborts fast.
        op.execute("SET LOCAL lock_timeout = '5s'")

    op.add_column(
        "game_sessions",
        sa.Column("drill_root_reached_ply", sa.Integer(), nullable=True),
    )

    if dialect == "postgresql":
        op.create_check_constraint(
            NON_NEGATIVE_CHECK, "game_sessions", NON_NEGATIVE_CONDITION
        )
        op.create_check_constraint(
            DRILL_ONLY_CHECK, "game_sessions", DRILL_ONLY_CONDITION
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # Drop the CHECKs before the column they reference.
        op.drop_constraint(DRILL_ONLY_CHECK, "game_sessions", type_="check")
        op.drop_constraint(NON_NEGATIVE_CHECK, "game_sessions", type_="check")

    op.drop_column("game_sessions", "drill_root_reached_ply")
