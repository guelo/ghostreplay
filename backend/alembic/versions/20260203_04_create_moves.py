"""Create moves table.

Revision ID: 20260203_04
Revises: 20260203_03
Create Date: 2026-02-03

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260203_04"
down_revision = "20260203_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "moves",
        sa.Column("from_position_id", sa.BigInteger(), nullable=False),
        sa.Column("to_position_id", sa.BigInteger(), nullable=False),
        sa.Column("move_san", sa.String(length=10), nullable=False),
        sa.ForeignKeyConstraint(["from_position_id"], ["positions.id"]),
        sa.ForeignKeyConstraint(["to_position_id"], ["positions.id"]),
        sa.PrimaryKeyConstraint("from_position_id", "move_san"),
    )


def downgrade() -> None:
    op.drop_table("moves")
