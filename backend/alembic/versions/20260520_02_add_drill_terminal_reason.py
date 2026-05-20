"""Add drill_terminal_reason to game_sessions.

Revision ID: 20260520_02
Revises: 20260520_01
Create Date: 2026-05-20

"""
import sqlalchemy as sa
from alembic import op


revision = "20260520_02"
down_revision = "20260520_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("game_sessions") as batch_op:
        batch_op.add_column(sa.Column("drill_terminal_reason", sa.String(length=20), nullable=True))
        batch_op.create_check_constraint(
            "ck_game_sessions_drill_terminal_reason",
            "drill_terminal_reason is null or drill_terminal_reason in ('off_route','accuracy','natural_end')",
        )


def downgrade() -> None:
    with op.batch_alter_table("game_sessions") as batch_op:
        batch_op.drop_constraint("ck_game_sessions_drill_terminal_reason", type_="check")
        batch_op.drop_column("drill_terminal_reason")
