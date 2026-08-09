"""Add active_color to positions.

Revision ID: 20260203_03
Revises: 20260203_02
Create Date: 2026-02-03

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260203_03"
down_revision = "20260203_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("active_color", sa.String(length=5), nullable=True))
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            """
            UPDATE positions
            SET active_color = CASE substr(
                fen_raw,
                instr(fen_raw || ' ', ' ') + 1,
                instr(
                    substr(fen_raw, instr(fen_raw || ' ', ' ') + 1) || ' ',
                    ' '
                ) - 1
            )
                WHEN 'w' THEN 'white'
                WHEN 'b' THEN 'black'
            END
            """
        )
    else:
        op.execute(
            """
            UPDATE positions
            SET active_color = CASE split_part(fen_raw, ' ', 2)
                WHEN 'w' THEN 'white'
                WHEN 'b' THEN 'black'
            END
            """
        )
    with op.batch_alter_table("positions") as batch_op:
        batch_op.alter_column("active_color", nullable=False)
        batch_op.create_check_constraint(
            "ck_positions_active_color",
            "active_color in ('white','black')",
        )
    op.create_index("idx_positions_active_color", "positions", ["active_color"])


def downgrade() -> None:
    op.drop_index("idx_positions_active_color", table_name="positions")
    with op.batch_alter_table("positions") as batch_op:
        batch_op.drop_constraint("ck_positions_active_color", type_="check")
        batch_op.drop_column("active_color")
