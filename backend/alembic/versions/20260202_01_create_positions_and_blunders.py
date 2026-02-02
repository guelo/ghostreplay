"""Create positions and blunders tables.

Revision ID: 20260202_01
Revises: None
Create Date: 2026-02-02

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260202_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "positions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("fen_hash", sa.String(length=64), nullable=False),
        sa.Column("fen_raw", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "fen_hash", name="uq_positions_user_fen_hash"),
    )
    op.create_index("idx_positions_user", "positions", ["user_id"])
    op.create_index("idx_positions_fen_hash", "positions", ["user_id", "fen_hash"])

    op.create_table(
        "blunders",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("bad_move_san", sa.String(length=10), nullable=False),
        sa.Column("best_move_san", sa.String(length=10), nullable=False),
        sa.Column("eval_loss_cp", sa.Integer(), nullable=False),
        sa.Column("pass_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"]),
        sa.UniqueConstraint("user_id", "position_id", name="uq_blunders_user_position"),
    )
    op.create_index("idx_blunders_user", "blunders", ["user_id"])
    op.create_index("idx_blunders_position", "blunders", ["position_id"])
    op.create_index("idx_blunders_due", "blunders", ["user_id", "pass_streak", "last_reviewed_at"])


def downgrade() -> None:
    op.drop_index("idx_blunders_due", table_name="blunders")
    op.drop_index("idx_blunders_position", table_name="blunders")
    op.drop_index("idx_blunders_user", table_name="blunders")
    op.drop_table("blunders")
    op.drop_index("idx_positions_fen_hash", table_name="positions")
    op.drop_index("idx_positions_user", table_name="positions")
    op.drop_table("positions")
