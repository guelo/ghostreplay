"""Add game_count to user_opening_scores.

Adds a persisted distinct-game count (number of game_sessions that contributed
evidence to an opening's reachable subtree) alongside the existing sample_size
(move-observation count). The "Games" label in the openings UI reads this column
so it reflects games, not plies.

NOT NULL with server_default 0 so batches written before this column existed
backfill to 0; they repopulate with real counts on the next score recompute,
which is forced one-time by the OPENING_EVIDENCE_INPUTS_VERSION raw-v1 -> raw-v2
bump (changes the inputs fingerprint -> cache miss -> recompute).

Revision ID: 20260614_01
Revises: 20260613_01
Create Date: 2026-06-14

"""
import sqlalchemy as sa
from alembic import op


revision = "20260614_01"
down_revision = "20260613_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_opening_scores") as batch_op:
        batch_op.add_column(
            sa.Column(
                "game_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("user_opening_scores") as batch_op:
        batch_op.drop_column("game_count")
