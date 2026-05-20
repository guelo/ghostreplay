"""Add drill_strictness_cp to game_sessions.

Revision ID: 20260520_01
Revises: 20260519_02
Create Date: 2026-05-20

"""
import sqlalchemy as sa
from alembic import op


revision = "20260520_01"
down_revision = "20260519_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("game_sessions") as batch_op:
        batch_op.add_column(sa.Column("drill_strictness_cp", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "ck_game_sessions_drill_strictness_cp",
            "drill_strictness_cp is null or (drill_strictness_cp >= 0 and drill_strictness_cp <= 50)",
        )


def downgrade() -> None:
    with op.batch_alter_table("game_sessions") as batch_op:
        batch_op.drop_constraint("ck_game_sessions_drill_strictness_cp", type_="check")
        batch_op.drop_column("drill_strictness_cp")
