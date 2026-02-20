"""Add source_session_id column to blunders.

Tracks which game session originally recorded each blunder, enabling
the blunder review page to load the full game context.

Revision ID: 20260217_01
Revises: 20260216_01
Create Date: 2026-02-17

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "20260217_01"
down_revision = "20260216_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "blunders",
        sa.Column(
            "source_session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("game_sessions.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("blunders", "source_session_id")
