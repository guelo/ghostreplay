"""Create the durable opening-session replay cache (g-overlay-cold-bootstrap).

The table is a replaceable, best-effort L2 for the graph-independent replay
product derived from one game session. It contains no computed quality, shared
analysis fallback, overlay node/edge, user, or colour column. The session primary
key is also the only lookup index; the source-session FK cascades retention.

There is intentionally no backfill. The first authoritative overlay after rollout
derives and seeds entries naturally, and subsequent process starts hydrate them.

Revision ID: 20260802_01
Revises: 20260729_02
Create Date: 2026-08-02

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement


revision = "20260802_01"
down_revision = "20260729_02"
branch_labels = None
depends_on = None


class statement_timestamp(FunctionElement):  # noqa: N801
    """Frozen migration copy of the model's portable statement-time default."""

    type = sa.DateTime(timezone=True)
    name = "statement_timestamp"
    inherit_cache = True


@compiles(statement_timestamp)
def _compile_statement_timestamp_default(element, compiler, **kw) -> str:
    return "CURRENT_TIMESTAMP"


@compiles(statement_timestamp, "postgresql")
def _compile_statement_timestamp_postgresql(element, compiler, **kw) -> str:
    return "statement_timestamp()"


def upgrade() -> None:
    op.create_table(
        "opening_session_replay_cache",
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("game_sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("content_hash", sa.String(length=40), nullable=False),
        sa.Column("divider_version", sa.Text(), nullable=False),
        sa.Column("inputs_version", sa.Text(), nullable=False),
        sa.Column("payload_version", sa.SmallInteger(), nullable=False),
        sa.Column("move_count", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=statement_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "move_count >= 0",
            name="ck_opening_session_replay_cache_move_count",
        ),
    )


def downgrade() -> None:
    op.drop_table("opening_session_replay_cache")
