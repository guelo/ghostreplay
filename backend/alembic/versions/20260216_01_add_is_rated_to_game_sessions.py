"""Add is_rated column to game_sessions.

Revision ID: 20260216_01
Revises: 20260214_01
Create Date: 2026-02-16

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260216_01"
down_revision = "20260214_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "game_sessions",
        sa.Column("is_rated", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("game_sessions", "is_rated")
