"""Add index on moves(to_position_id).

Speeds up reverse-edge queries that walk the ghost graph backwards
(Move.from_position_id WHERE Move.to_position_id IN frontier), notably
_reverse_ancestor_position_ids during opportunity-event recompute. Without
this index every reverse step is a full seq scan over the moves table.

Revision ID: 20260622_01
Revises: 20260618_01
Create Date: 2026-06-22

"""
from alembic import op


revision = "20260622_01"
down_revision = "20260618_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_moves_to_position_id", "moves", ["to_position_id"])


def downgrade() -> None:
    op.drop_index("idx_moves_to_position_id", table_name="moves")
