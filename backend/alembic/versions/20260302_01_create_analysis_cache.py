"""Create analysis_cache table.

Revision ID: 20260302_01
Revises: 20260217_01
Create Date: 2026-03-02

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260302_01"
down_revision = "20260217_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_cache",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("fen_before", sa.Text(), nullable=False),
        sa.Column("move_uci", sa.String(length=5), nullable=False),
        sa.Column("move_san", sa.String(length=10), nullable=False),
        sa.Column("best_move_uci", sa.String(length=5), nullable=True),
        sa.Column("best_move_san", sa.String(length=10), nullable=True),
        sa.Column("played_eval", sa.Integer(), nullable=True),
        sa.Column("best_eval", sa.Integer(), nullable=True),
        sa.Column("eval_delta", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="game"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("fen_before", "move_uci", name="uq_analysis_cache_fen_move"),
    )
    op.create_index("idx_analysis_cache_fen", "analysis_cache", ["fen_before"])


def downgrade() -> None:
    op.drop_index("idx_analysis_cache_fen", table_name="analysis_cache")
    op.drop_table("analysis_cache")
