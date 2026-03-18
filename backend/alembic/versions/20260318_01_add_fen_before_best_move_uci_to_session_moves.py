"""Add fen_before and best_move_uci columns to session_moves.

Revision ID: 20260318_01
Revises: 20260302_01
Create Date: 2026-03-18

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260318_01"
down_revision = "20260302_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("session_moves", sa.Column("fen_before", sa.Text(), nullable=True))
    op.add_column("session_moves", sa.Column("best_move_uci", sa.String(5), nullable=True))


def downgrade() -> None:
    op.drop_column("session_moves", "best_move_uci")
    op.drop_column("session_moves", "fen_before")
