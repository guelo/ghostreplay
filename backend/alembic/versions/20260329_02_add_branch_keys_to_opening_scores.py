"""Add branch key columns to opening score cache.

Revision ID: 20260329_02
Revises: 20260329_01
Create Date: 2026-03-29

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260329_02"
down_revision = "20260329_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_opening_scores", sa.Column("strongest_branch_key", sa.Text(), nullable=True))
    op.add_column("user_opening_scores", sa.Column("weakest_branch_key", sa.Text(), nullable=True))
    op.add_column("user_opening_scores", sa.Column("underexposed_branch_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_opening_scores", "underexposed_branch_key")
    op.drop_column("user_opening_scores", "weakest_branch_key")
    op.drop_column("user_opening_scores", "strongest_branch_key")
