"""Add classification column to analysis_cache.

Revision ID: 20260320_01
Revises: 20260318_01
Create Date: 2026-03-20

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260320_01"
down_revision = "20260318_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analysis_cache", sa.Column("classification", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_cache", "classification")
