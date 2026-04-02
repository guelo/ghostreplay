"""Add decision source observability to session moves.

Revision ID: 20260401_01
Revises: 20260330_01
Create Date: 2026-04-01

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260401_01"
down_revision = "20260330_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("session_moves", sa.Column("decision_source", sa.String(length=20), nullable=True))
    op.add_column("session_moves", sa.Column("target_blunder_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_session_moves_target_blunder_id_blunders",
        "session_moves",
        "blunders",
        ["target_blunder_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_session_moves_decision_source",
        "session_moves",
        "decision_source is null or decision_source in ('ghost_path','backend_engine','local_fallback')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_session_moves_decision_source", "session_moves", type_="check")
    op.drop_constraint("fk_session_moves_target_blunder_id_blunders", "session_moves", type_="foreignkey")
    op.drop_column("session_moves", "target_blunder_id")
    op.drop_column("session_moves", "decision_source")
