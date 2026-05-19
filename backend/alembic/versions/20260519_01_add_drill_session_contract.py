"""Add drill session contract fields.

Revision ID: 20260519_01
Revises: 20260513_01
Create Date: 2026-05-19

"""
from alembic import op
import sqlalchemy as sa


revision = "20260519_01"
down_revision = "20260513_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("game_sessions") as batch_op:
        batch_op.add_column(sa.Column("session_mode", sa.String(length=10), nullable=False, server_default="normal"))
        batch_op.add_column(sa.Column("drill_state", sa.String(length=12), nullable=True))
        batch_op.add_column(sa.Column("drill_opening_key", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("drill_strictness", sa.String(length=12), nullable=True))
        batch_op.add_column(sa.Column("normal_started_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("rated_start_ply", sa.Integer(), nullable=True))
        batch_op.create_check_constraint("ck_game_sessions_session_mode", "session_mode in ('normal','drill')")
        batch_op.create_check_constraint(
            "ck_game_sessions_drill_state",
            "drill_state is null or drill_state in ('active','failed','abandoned','converted')",
        )
        batch_op.create_check_constraint(
            "ck_game_sessions_drill_strictness",
            "drill_strictness is null or drill_strictness in ('lenient','standard','strict')",
        )
        batch_op.create_check_constraint(
            "ck_game_sessions_mode_drill_state",
            "((session_mode = 'normal' and drill_state is null) "
            "or (session_mode = 'drill' and drill_state is not null))",
        )
        batch_op.create_check_constraint(
            "ck_game_sessions_rated_start_ply",
            "rated_start_ply is null or rated_start_ply >= 0",
        )
        batch_op.create_check_constraint(
            "ck_game_sessions_drill_rating_boundary",
            "session_mode = 'normal' "
            "or (drill_state = 'converted' and is_rated = true and normal_started_at is not null "
            "and converted_at is not null and rated_start_ply is not null) "
            "or (drill_state in ('active','failed','abandoned') and is_rated = false and rated_start_ply is null)",
        )

    op.create_index("idx_game_sessions_user_mode_status", "game_sessions", ["user_id", "session_mode", "status"])
    op.create_index("idx_game_sessions_drill_state", "game_sessions", ["drill_state"])

    with op.batch_alter_table("session_moves") as batch_op:
        batch_op.add_column(sa.Column("segment", sa.String(length=10), nullable=False, server_default="normal"))
        batch_op.create_check_constraint("ck_session_moves_segment", "segment in ('drill','normal')")

    op.create_index("idx_session_moves_session_segment", "session_moves", ["session_id", "segment"])


def downgrade() -> None:
    op.drop_index("idx_session_moves_session_segment", table_name="session_moves")
    with op.batch_alter_table("session_moves") as batch_op:
        batch_op.drop_constraint("ck_session_moves_segment", type_="check")
        batch_op.drop_column("segment")

    op.drop_index("idx_game_sessions_drill_state", table_name="game_sessions")
    op.drop_index("idx_game_sessions_user_mode_status", table_name="game_sessions")
    with op.batch_alter_table("game_sessions") as batch_op:
        batch_op.drop_constraint("ck_game_sessions_rated_start_ply", type_="check")
        batch_op.drop_constraint("ck_game_sessions_drill_rating_boundary", type_="check")
        batch_op.drop_constraint("ck_game_sessions_mode_drill_state", type_="check")
        batch_op.drop_constraint("ck_game_sessions_drill_strictness", type_="check")
        batch_op.drop_constraint("ck_game_sessions_drill_state", type_="check")
        batch_op.drop_constraint("ck_game_sessions_session_mode", type_="check")
        batch_op.drop_column("rated_start_ply")
        batch_op.drop_column("converted_at")
        batch_op.drop_column("normal_started_at")
        batch_op.drop_column("drill_strictness")
        batch_op.drop_column("drill_opening_key")
        batch_op.drop_column("drill_state")
        batch_op.drop_column("session_mode")
