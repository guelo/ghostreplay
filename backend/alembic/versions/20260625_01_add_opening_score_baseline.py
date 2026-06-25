"""Add opening_score_baseline to game_sessions.

Per-session JSON snapshot ({opening_key: opening_score}) of the user's opening
scores at session start, captured before any of the session's moves are uploaded.
End-of-session opening-score deltas diff the recomputed "after" scores against
this stable "before" (live play feeds request_recompute incrementally, so the
cached score otherwise already reflects most of the just-played game). Stored as
Text (JSON-as-string per repo convention). NULL on older sessions; "{}" means a
snapshot was taken but the user had no scored openings yet.

Revision ID: 20260625_01
Revises: 20260622_01
Create Date: 2026-06-25

"""
import sqlalchemy as sa
from alembic import op


revision = "20260625_01"
down_revision = "20260622_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("game_sessions") as batch_op:
        batch_op.add_column(
            sa.Column("opening_score_baseline", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("game_sessions") as batch_op:
        batch_op.drop_column("opening_score_baseline")
