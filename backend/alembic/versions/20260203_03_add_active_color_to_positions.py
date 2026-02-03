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
    op.execute(
        """
        UPDATE positions
        SET active_color = CASE split_part(fen_raw, ' ', 2)
            WHEN 'w' THEN 'white'
            WHEN 'b' THEN 'black'
        END
        """
    )
    op.alter_column("positions", "active_color", nullable=False)
    op.create_check_constraint(
        "ck_positions_active_color",
        "positions",
        "active_color in ('white','black')",
    )
    op.create_index("idx_positions_active_color", "positions", ["active_color"])


def downgrade() -> None:
    op.drop_index("idx_positions_active_color", table_name="positions")
    op.drop_constraint("ck_positions_active_color", "positions", type_="check")
    op.drop_column("positions", "active_color")
