"""Optimize position lookup indexes.

Drop redundant and low-selectivity indexes on positions, add composite
indexes that support the ghost-move recursive CTE join pattern.

Revision ID: 20260206_01
Revises: 20260203_04
Create Date: 2026-02-06

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260206_01"
down_revision = "20260203_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop redundant index (duplicate of uq_positions_user_fen_hash unique constraint)
    op.drop_index("idx_positions_fen_hash", table_name="positions")

    # Drop useless low-selectivity index (only two values: 'white'/'black')
    op.drop_index("idx_positions_active_color", table_name="positions")

    # Composite index for player_color-scoped ghost queries
    op.create_index(
        "idx_positions_user_active_color",
        "positions",
        ["user_id", "active_color"],
    )

    # Drop single-column index superseded by composite below
    op.drop_index("idx_blunders_position", table_name="blunders")

    # Composite index covering the CTE final join:
    #   blunders b ON b.position_id = r.position_id WHERE b.user_id = :user_id
    op.create_index(
        "idx_blunders_position_user",
        "blunders",
        ["position_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_blunders_position_user", table_name="blunders")
    op.create_index("idx_blunders_position", "blunders", ["position_id"])
    op.drop_index("idx_positions_user_active_color", table_name="positions")
    op.create_index("idx_positions_active_color", "positions", ["active_color"])
    op.create_index("idx_positions_fen_hash", "positions", ["user_id", "fen_hash"])
