"""Expand opponent targeting storage; no backfill, expiry or reader activation.

Revision ID: 20260919_01
Revises: 20260919_02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20260919_01"
# The independent drill-route expansion landed first; graph order, not the
# revision's numeric suffix, determines upgrade order.
down_revision = "20260919_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "game_sessions",
        sa.Column("opponent_decisions_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "opponent_target_facts",
        sa.Column("session_id", UUID(as_uuid=True),
                  sa.ForeignKey("game_sessions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("blunder_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  sa.ForeignKey("blunders.id"), primary_key=True),
        sa.Column("last_served_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_opponent_target_facts_target_served", "opponent_target_facts",
                    ["blunder_id", "last_served_at"])
    op.create_index("idx_opponent_target_facts_expiry", "opponent_target_facts",
                    ["last_served_at", "session_id", "blunder_id"])


def downgrade() -> None:
    op.drop_table("opponent_target_facts")
    op.drop_column("game_sessions", "opponent_decisions_expires_at")
