"""Add player_color to game_sessions.

Revision ID: 20260203_02
Revises: 20260203_01
Create Date: 2026-02-03

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260203_02"
down_revision = "20260203_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "game_sessions",
        sa.Column("player_color", sa.String(length=5), nullable=False, server_default="white"),
    )
    op.execute("UPDATE game_sessions SET player_color = 'white' WHERE player_color IS NULL")
    op.create_check_constraint(
        "ck_game_sessions_player_color",
        "game_sessions",
        "player_color in ('white','black')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_game_sessions_player_color", "game_sessions", type_="check")
    op.drop_column("game_sessions", "player_color")
