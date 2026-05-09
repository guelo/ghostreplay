"""Add opportunity-aware SRS tracking.

Revision ID: 20260508_01
Revises: 20260401_01
Create Date: 2026-05-08

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "20260508_01"
down_revision = "20260401_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("blunders", sa.Column("opening_family", sa.Text(), nullable=True))
    op.create_table(
        "blunder_opportunity_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("blunder_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opportunity", sa.Boolean(), nullable=False),
        sa.Column("reached", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["blunder_id"], ["blunders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["game_sessions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("session_id", "blunder_id", name="uq_blunder_opportunity_session_blunder"),
    )
    op.create_index(
        "idx_blunder_opportunity_blunder_time",
        "blunder_opportunity_events",
        ["blunder_id", "occurred_at"],
    )
    op.create_index(
        "idx_blunder_opportunity_session",
        "blunder_opportunity_events",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_blunder_opportunity_session", table_name="blunder_opportunity_events")
    op.drop_index("idx_blunder_opportunity_blunder_time", table_name="blunder_opportunity_events")
    op.drop_table("blunder_opportunity_events")
    op.drop_column("blunders", "opening_family")
