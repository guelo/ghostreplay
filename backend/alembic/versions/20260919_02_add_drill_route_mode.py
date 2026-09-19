"""Persist opponent route preference for lineage drills.

SQLite uses additive DDL without checks, preserving existing unnamed checks.
Its application test schema declares these checks; PostgreSQL installs them here.
"""
import sqlalchemy as sa
from alembic import op

revision = "20260919_02"
down_revision = "20260818_01"
branch_labels = None
depends_on = None

MODE_CHECK = "ck_game_sessions_drill_route_mode"
MODE_CONDITION = "drill_route_mode IN ('auto','prefer_line')"
LINE_CHECK = "ck_game_sessions_prefer_line_requires_drill_line"
LINE_CONDITION = "drill_route_mode != 'prefer_line' OR (session_mode = 'drill' AND drill_line IS NOT NULL)"


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("game_sessions", sa.Column(
        "drill_route_mode", sa.String(12), nullable=False, server_default="auto",
    ))
    if op.get_bind().dialect.name == "postgresql":
        op.create_check_constraint(MODE_CHECK, "game_sessions", MODE_CONDITION)
        op.create_check_constraint(LINE_CHECK, "game_sessions", LINE_CONDITION)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.drop_constraint(LINE_CHECK, "game_sessions", type_="check")
        op.drop_constraint(MODE_CHECK, "game_sessions", type_="check")
    op.drop_column("game_sessions", "drill_route_mode")
