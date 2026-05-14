"""Add Glicko rating tracks.

Revision ID: 20260513_01
Revises: 20260508_01
Create Date: 2026-05-13

"""
from alembic import op
import sqlalchemy as sa


revision = "20260513_01"
down_revision = "20260508_01"
branch_labels = None
depends_on = None


def _assert_no_duplicate_rating_sessions() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(sa.text("""
        SELECT game_session_id, COUNT(*) AS row_count
        FROM rating_history
        GROUP BY game_session_id
        HAVING COUNT(*) > 1
        LIMIT 10
    """)).fetchall()
    if duplicates:
        sample = ", ".join(f"{row.game_session_id} ({row.row_count})" for row in duplicates)
        raise RuntimeError(
            "rating_history has duplicate game_session_id rows; "
            "dedupe before adding uq_rating_history_game_session. "
            f"Sample: {sample}"
        )


def upgrade() -> None:
    op.add_column("rating_history", sa.Column("chesscom_rating", sa.Float(), nullable=True))
    op.add_column("rating_history", sa.Column("chesscom_rd", sa.Float(), nullable=True))
    op.add_column("rating_history", sa.Column("lichess_rating", sa.Float(), nullable=True))
    op.add_column("rating_history", sa.Column("lichess_rd", sa.Float(), nullable=True))
    op.add_column("rating_history", sa.Column("lichess_volatility", sa.Float(), nullable=True))
    _assert_no_duplicate_rating_sessions()
    op.create_index(
        "uq_rating_history_game_session",
        "rating_history",
        ["game_session_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_rating_history_game_session", table_name="rating_history")
    op.drop_column("rating_history", "lichess_volatility")
    op.drop_column("rating_history", "lichess_rd")
    op.drop_column("rating_history", "lichess_rating")
    op.drop_column("rating_history", "chesscom_rd")
    op.drop_column("rating_history", "chesscom_rating")
