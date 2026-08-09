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
    with op.batch_alter_table("session_moves") as batch_op:
        batch_op.add_column(
            sa.Column("decision_source", sa.String(length=20), nullable=True)
        )
        batch_op.add_column(
            sa.Column("target_blunder_id", sa.BigInteger(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_session_moves_target_blunder_id_blunders",
            "blunders",
            ["target_blunder_id"],
            ["id"],
        )
        batch_op.create_check_constraint(
            "ck_session_moves_decision_source",
            "decision_source is null or decision_source in ('ghost_path','backend_engine','local_fallback')",
        )


def downgrade() -> None:
    with op.batch_alter_table("session_moves") as batch_op:
        batch_op.drop_constraint(
            "ck_session_moves_decision_source", type_="check"
        )
        batch_op.drop_constraint(
            "fk_session_moves_target_blunder_id_blunders", type_="foreignkey"
        )
        batch_op.drop_column("target_blunder_id")
        batch_op.drop_column("decision_source")
