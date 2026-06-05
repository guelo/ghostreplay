"""Add inputs_fingerprint to opening_score_batches.

Content fingerprint over the consumed evidence + registry/config inputs, used to
skip recompute (and thus avoid append-only batch growth) when scoring inputs are
unchanged. NULL for pre-migration batches, which simply fall through to a recompute
once and then participate in the guard normally.

Revision ID: 20260604_01
Revises: 20260602_01
Create Date: 2026-06-04

"""
import sqlalchemy as sa
from alembic import op


revision = "20260604_01"
down_revision = "20260602_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("opening_score_batches") as batch_op:
        batch_op.add_column(sa.Column("inputs_fingerprint", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("opening_score_batches") as batch_op:
        batch_op.drop_column("inputs_fingerprint")
