"""Create opening_position_scores read-model table.

Generation-scoped direct position-score read model for the horizontal opening
tree (g-tree-score-model). A sibling of user_opening_scores under the same
opening_score_batches generation, keyed by (batch_id, normalized_fen) instead of
a named-root key, so the tree read path can serve direct per-position metrics
without running compute_root_score once per visible card.

batch_id is an ON DELETE CASCADE foreign key to opening_score_batches(id), exactly
like user_opening_scores, so prune_old_opening_score_batches removes these direct
rows through the same generation-retention path (no unbounded leak across
recomputes). The four metric columns are nullable: has_evidence=false rows are
no-data (null score/confidence/coverage/weighted_depth, zero sample/game counts).

Revision ID: 20260615_01
Revises: 20260614_01
Create Date: 2026-06-15

"""
import sqlalchemy as sa
from alembic import op


revision = "20260615_01"
down_revision = "20260614_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bigint_sqlite = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    op.create_table(
        "opening_position_scores",
        sa.Column("id", bigint_sqlite, primary_key=True, autoincrement=True),
        sa.Column("batch_id", bigint_sqlite, nullable=False),
        sa.Column("user_id", bigint_sqlite, nullable=False),
        sa.Column("player_color", sa.String(length=5), nullable=False),
        sa.Column("normalized_fen", sa.Text(), nullable=False),
        sa.Column("in_book", sa.Boolean(), nullable=False),
        sa.Column("has_evidence", sa.Boolean(), nullable=False),
        sa.Column("opening_score", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("coverage", sa.Float(), nullable=True),
        sa.Column("weighted_depth", sa.Float(), nullable=True),
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("game_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_practiced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "player_color in ('white','black')",
            name="ck_opening_position_scores_player_color",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["opening_score_batches.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "batch_id", "normalized_fen", name="uq_opening_position_scores_batch_fen"
        ),
    )
    op.create_index(
        "idx_opening_position_scores_batch_fen",
        "opening_position_scores",
        ["batch_id", "normalized_fen"],
    )
    op.create_index(
        "idx_opening_position_scores_user_color",
        "opening_position_scores",
        ["user_id", "player_color"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_opening_position_scores_user_color",
        table_name="opening_position_scores",
    )
    op.drop_index(
        "idx_opening_position_scores_batch_fen",
        table_name="opening_position_scores",
    )
    op.drop_table("opening_position_scores")
