"""Add drill_line to game_sessions.

Persists the full UCI line from the start position to an ad-hoc drill target
(space-joined via encode_uci_line / decode_uci_line). NULL for registered-root
drills, whose routing uses the transposition-tolerant book BFS and needs no line.

Revision ID: 20260618_01
Revises: 20260617_01
Create Date: 2026-06-18

"""
import sqlalchemy as sa
from alembic import op


revision = "20260618_01"
down_revision = "20260617_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("game_sessions") as batch_op:
        batch_op.add_column(sa.Column("drill_line", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("game_sessions") as batch_op:
        batch_op.drop_column("drill_line")
