"""Create game_sessions table.

Revision ID: 20260202_02
Revises: 20260202_01
Create Date: 2026-02-02

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "20260202_02"
down_revision = "20260202_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "game_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result", sa.String(length=20)),
        sa.Column("engine_elo", sa.Integer(), nullable=False),
        sa.Column("blunder_recorded", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("pgn", sa.Text()),
    )
    op.create_index("idx_game_sessions_user", "game_sessions", ["user_id"])
    op.create_index("idx_game_sessions_status", "game_sessions", ["status"])
    op.create_index("idx_game_sessions_user_started", "game_sessions", ["user_id", "started_at"])


def downgrade() -> None:
    op.drop_index("idx_game_sessions_user_started", table_name="game_sessions")
    op.drop_index("idx_game_sessions_status", table_name="game_sessions")
    op.drop_index("idx_game_sessions_user", table_name="game_sessions")
    op.drop_table("game_sessions")
