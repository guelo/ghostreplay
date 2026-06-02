"""Add best_line_uci to session_moves and analysis_cache.

Stores the root best-move principal variation (space-joined UCI moves) so the
AnalysisBoard engine-line popup can render a full continuation for the cached
best move instead of a single move.

Revision ID: 20260602_01
Revises: 20260520_02
Create Date: 2026-06-02

"""
import sqlalchemy as sa
from alembic import op


revision = "20260602_01"
down_revision = "20260520_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("session_moves") as batch_op:
        batch_op.add_column(sa.Column("best_line_uci", sa.Text(), nullable=True))
    with op.batch_alter_table("analysis_cache") as batch_op:
        batch_op.add_column(sa.Column("best_line_uci", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("analysis_cache") as batch_op:
        batch_op.drop_column("best_line_uci")
    with op.batch_alter_table("session_moves") as batch_op:
        batch_op.drop_column("best_line_uci")
