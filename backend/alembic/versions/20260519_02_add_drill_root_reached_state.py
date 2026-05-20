"""Add drill root reached state.

Revision ID: 20260519_02
Revises: 20260519_01
Create Date: 2026-05-19

"""
from alembic import op


revision = "20260519_02"
down_revision = "20260519_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("game_sessions") as batch_op:
        batch_op.drop_constraint("ck_game_sessions_drill_rating_boundary", type_="check")
        batch_op.drop_constraint("ck_game_sessions_drill_state", type_="check")
        batch_op.create_check_constraint(
            "ck_game_sessions_drill_state",
            "drill_state is null or drill_state in ('active','root_reached','failed','abandoned','converted')",
        )
        batch_op.create_check_constraint(
            "ck_game_sessions_drill_rating_boundary",
            "session_mode = 'normal' "
            "or (drill_state = 'converted' and is_rated = true and normal_started_at is not null "
            "and converted_at is not null and rated_start_ply is not null) "
            "or (drill_state in ('active','root_reached','failed','abandoned') "
            "and is_rated = false and rated_start_ply is null)",
        )


def downgrade() -> None:
    with op.batch_alter_table("game_sessions") as batch_op:
        batch_op.drop_constraint("ck_game_sessions_drill_rating_boundary", type_="check")
        batch_op.drop_constraint("ck_game_sessions_drill_state", type_="check")
        batch_op.create_check_constraint(
            "ck_game_sessions_drill_state",
            "drill_state is null or drill_state in ('active','failed','abandoned','converted')",
        )
        batch_op.create_check_constraint(
            "ck_game_sessions_drill_rating_boundary",
            "session_mode = 'normal' "
            "or (drill_state = 'converted' and is_rated = true and normal_started_at is not null "
            "and converted_at is not null and rated_start_ply is not null) "
            "or (drill_state in ('active','failed','abandoned') and is_rated = false and rated_start_ply is null)",
        )
