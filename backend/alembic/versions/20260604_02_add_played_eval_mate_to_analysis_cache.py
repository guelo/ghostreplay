"""Add played_eval_mate to analysis_cache.

Stores the white-relative mate count for the played move so the live analysis
path (worker -> hook -> presentation) can render M1/M2/# from cached entries.

Revision ID: 20260604_02
Revises: 20260604_01
Create Date: 2026-06-04

"""
import sqlalchemy as sa
from alembic import op


revision = "20260604_02"
down_revision = "20260604_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("analysis_cache") as batch_op:
        batch_op.add_column(sa.Column("played_eval_mate", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("analysis_cache") as batch_op:
        batch_op.drop_column("played_eval_mate")
