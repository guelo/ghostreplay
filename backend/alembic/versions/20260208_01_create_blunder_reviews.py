"""Create blunder_reviews table.

Revision ID: 20260208_01
Revises: 20260207_01
Create Date: 2026-02-08

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "20260208_01"
down_revision = "20260207_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blunder_reviews",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("blunder_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("move_played_san", sa.String(length=10), nullable=False),
        sa.Column("eval_delta_cp", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["blunder_id"], ["blunders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["game_sessions.id"]),
    )
    op.create_index(
        "idx_blunder_reviews_blunder",
        "blunder_reviews",
        ["blunder_id", sa.text("reviewed_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_blunder_reviews_blunder", table_name="blunder_reviews")
    op.drop_table("blunder_reviews")
