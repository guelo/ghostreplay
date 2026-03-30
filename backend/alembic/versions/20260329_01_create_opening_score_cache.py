"""Create opening score cache tables.

Revision ID: 20260329_01
Revises: 20260320_01
Create Date: 2026-03-29

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260329_01"
down_revision = "20260320_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bigint_sqlite = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    op.create_table(
        "opening_score_batches",
        sa.Column("id", bigint_sqlite, primary_key=True, autoincrement=True),
        sa.Column("user_id", bigint_sqlite, nullable=False),
        sa.Column("player_color", sa.String(length=5), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("player_color in ('white','black')", name="ck_opening_score_batches_player_color"),
        sa.UniqueConstraint("user_id", "player_color", "generation", name="uq_opening_score_batches_user_color_generation"),
    )
    op.create_index(
        "idx_opening_score_batches_user_color",
        "opening_score_batches",
        ["user_id", "player_color", "generation"],
    )

    op.create_table(
        "opening_score_cursors",
        sa.Column("user_id", bigint_sqlite, primary_key=True, nullable=False),
        sa.Column("player_color", sa.String(length=5), primary_key=True, nullable=False),
        sa.Column("latest_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("player_color in ('white','black')", name="ck_opening_score_cursors_player_color"),
    )

    op.create_table(
        "user_opening_scores",
        sa.Column("id", bigint_sqlite, primary_key=True, autoincrement=True),
        sa.Column("batch_id", bigint_sqlite, nullable=False),
        sa.Column("user_id", bigint_sqlite, nullable=False),
        sa.Column("player_color", sa.String(length=5), nullable=False),
        sa.Column("opening_key", sa.Text(), nullable=False),
        sa.Column("opening_name", sa.Text(), nullable=False),
        sa.Column("opening_family", sa.Text(), nullable=False),
        sa.Column("opening_score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("coverage", sa.Float(), nullable=False),
        sa.Column("weighted_depth", sa.Float(), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("last_practiced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("strongest_branch_name", sa.Text(), nullable=True),
        sa.Column("strongest_branch_score", sa.Float(), nullable=True),
        sa.Column("weakest_branch_name", sa.Text(), nullable=True),
        sa.Column("weakest_branch_score", sa.Float(), nullable=True),
        sa.Column("underexposed_branch_name", sa.Text(), nullable=True),
        sa.Column("underexposed_branch_value", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("player_color in ('white','black')", name="ck_user_opening_scores_player_color"),
        sa.ForeignKeyConstraint(["batch_id"], ["opening_score_batches.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("batch_id", "opening_key", name="uq_user_opening_scores_batch_opening"),
    )
    op.create_index("idx_user_opening_scores_batch", "user_opening_scores", ["batch_id"])
    op.create_index(
        "idx_user_opening_scores_user_color",
        "user_opening_scores",
        ["user_id", "player_color"],
    )


def downgrade() -> None:
    op.drop_index("idx_user_opening_scores_user_color", table_name="user_opening_scores")
    op.drop_index("idx_user_opening_scores_batch", table_name="user_opening_scores")
    op.drop_table("user_opening_scores")
    op.drop_table("opening_score_cursors")
    op.drop_index("idx_opening_score_batches_user_color", table_name="opening_score_batches")
    op.drop_table("opening_score_batches")
