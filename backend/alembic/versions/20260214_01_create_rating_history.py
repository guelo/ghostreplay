"""Create rating_history table.

Revision ID: 20260214_01
Revises: 20260208_01
Create Date: 2026-02-14

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "20260214_01"
down_revision = "20260208_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rating_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("game_session_id", UUID(as_uuid=True), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("is_provisional", sa.Boolean(), nullable=False),
        sa.Column("games_played", sa.Integer(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["game_session_id"], ["game_sessions.id"]),
    )
    op.create_index(
        "idx_rating_history_user_timestamp",
        "rating_history",
        ["user_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_rating_history_user_timestamp", table_name="rating_history")
    op.drop_table("rating_history")
