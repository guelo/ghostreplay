"""Add opening registry fingerprint to opening score batches.

Revision ID: 20260330_01
Revises: 20260329_02
Create Date: 2026-03-30

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260330_01"
down_revision = "20260329_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("opening_score_batches", sa.Column("registry_fingerprint", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("opening_score_batches", "registry_fingerprint")
