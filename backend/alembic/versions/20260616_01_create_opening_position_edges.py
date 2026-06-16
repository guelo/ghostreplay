"""Create opening_position_edges read-model table.

Generation-scoped observed-edge read model for the horizontal opening tree
(g-tree-fast-cache). A sibling of opening_position_scores under the same
opening_score_batches generation, keyed by (batch_id, parent_fen, child_fen)
instead of a single-FEN key — mirroring the EvidenceOverlay edge key. It
materializes the observed move edges (structural shape plus the traversal_count /
live_attempts / live_passes counters) the /api/openings/tree builder needs, so the
tree read path no longer rebuilds overlay_evidence (a full session-history replay)
on the request thread; warm reads cost only bounded per-parent indexed lookups.

batch_id is an ON DELETE CASCADE foreign key to opening_score_batches(id), exactly
like opening_position_scores, so prune_old_opening_score_batches removes these edge
rows through the same generation-retention path (no unbounded leak across
recomputes). quality_sum / quality_count are intentionally omitted (the tree never
reads them); add them only if the scorer is changed to read from this table.

Revision ID: 20260616_01
Revises: 20260615_02
Create Date: 2026-06-16

"""
import sqlalchemy as sa
from alembic import op


revision = "20260616_01"
down_revision = "20260615_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bigint_sqlite = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    op.create_table(
        "opening_position_edges",
        sa.Column("id", bigint_sqlite, primary_key=True, autoincrement=True),
        sa.Column("batch_id", bigint_sqlite, nullable=False),
        sa.Column("user_id", bigint_sqlite, nullable=False),
        sa.Column("player_color", sa.String(length=5), nullable=False),
        sa.Column("parent_fen", sa.Text(), nullable=False),
        sa.Column("child_fen", sa.Text(), nullable=False),
        sa.Column("uci", sa.Text(), nullable=False),
        sa.Column("traversal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("live_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("live_passes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("live_fails", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "player_color in ('white','black')",
            name="ck_opening_position_edges_player_color",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["opening_score_batches.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "batch_id", "parent_fen", "child_fen",
            name="uq_opening_position_edges_batch_parent_child",
        ),
    )
    op.create_index(
        "idx_opening_position_edges_batch_parent",
        "opening_position_edges",
        ["batch_id", "parent_fen"],
    )
    op.create_index(
        "idx_opening_position_edges_user_color",
        "opening_position_edges",
        ["user_id", "player_color"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_opening_position_edges_user_color",
        table_name="opening_position_edges",
    )
    op.drop_index(
        "idx_opening_position_edges_batch_parent",
        table_name="opening_position_edges",
    )
    op.drop_table("opening_position_edges")
