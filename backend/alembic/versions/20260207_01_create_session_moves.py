"""Create session_moves table.

Revision ID: 20260207_01
Revises: 20260206_01
Create Date: 2026-02-07

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "20260207_01"
down_revision = "20260206_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_moves",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        sa.Column("move_number", sa.Integer(), nullable=False),
        sa.Column("color", sa.String(length=5), nullable=False),
        sa.Column("move_san", sa.String(length=10), nullable=False),
        sa.Column("fen_after", sa.Text(), nullable=False),
        sa.Column("eval_cp", sa.Integer(), nullable=True),
        sa.Column("eval_mate", sa.Integer(), nullable=True),
        sa.Column("best_move_san", sa.String(length=10), nullable=True),
        sa.Column("best_move_eval_cp", sa.Integer(), nullable=True),
        sa.Column("eval_delta", sa.Integer(), nullable=True),
        sa.Column("classification", sa.String(length=20), nullable=True),
        sa.CheckConstraint("color in ('white','black')", name="ck_session_moves_color"),
        sa.ForeignKeyConstraint(["session_id"], ["game_sessions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("session_id", "move_number", "color", name="uq_session_moves_session_move_color"),
    )
    op.create_index("idx_session_moves_session", "session_moves", ["session_id"])


def downgrade() -> None:
    op.drop_index("idx_session_moves_session", table_name="session_moves")
    op.drop_table("session_moves")
