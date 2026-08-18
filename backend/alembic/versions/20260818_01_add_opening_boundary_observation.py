"""Add observation-only opening-boundary state.

All nullable additions are metadata-only on PostgreSQL. The constant-false
``opening_phase_exhausted`` default is metadata-only on PostgreSQL 11+, but each
ALTER still needs ACCESS EXCLUSIVE briefly; a transaction-local lock timeout
makes deployment abort instead of stalling live writers.

SQLite receives additive columns only. ALTER-added checks would require a batch
rewrite that can drop older unnamed checks, so the hand-written test schema owns
SQLite constraint parity.

Revision ID: 20260818_01
Revises: 20260814_01
Create Date: 2026-08-18
"""

import sqlalchemy as sa
from alembic import op


revision = "20260818_01"
down_revision = "20260814_01"
branch_labels = None
depends_on = None


_CHECKS = (
    (
        "ck_game_sessions_opening_phase_protocol",
        "opening_phase_protocol_version IS NULL OR opening_phase_protocol_version = 1",
    ),
    (
        "ck_game_sessions_opening_probe_ply",
        "opening_phase_probe_ply IS NULL OR opening_phase_probe_ply > 0",
    ),
    (
        "ck_game_sessions_opening_candidate_ply",
        "opening_middle_candidate_ply IS NULL OR opening_middle_candidate_ply > 0",
    ),
    (
        "ck_game_sessions_opening_middle_ply",
        "opening_middle_ply IS NULL OR opening_middle_ply > 0",
    ),
    (
        "ck_game_sessions_opening_probe_verdict",
        "opening_phase_probe_verdict IS NULL OR opening_phase_probe_verdict IN "
        "('passed','wrong_row_count','coordinate_mismatch','nonstandard_start',"
        "'illegal_or_discontinuous_line','exhausted','capped')",
    ),
    (
        "ck_game_sessions_opening_ready_requires_candidate_baseline",
        "opening_middle_ready_at IS NULL OR "
        "(opening_middle_candidate_ply IS NOT NULL AND opening_score_baseline IS NOT NULL)",
    ),
    (
        "ck_game_sessions_opening_marker_requires_baseline",
        "opening_middle_ply IS NULL OR "
        "(opening_score_baseline IS NOT NULL AND opening_middle_ply = opening_middle_candidate_ply)",
    ),
    (
        "ck_game_sessions_opening_exhausted_clears_state",
        "NOT opening_phase_exhausted OR "
        "(opening_phase_probe_ply IS NULL AND opening_middle_candidate_ply IS NULL "
        "AND opening_middle_ready_at IS NULL AND opening_middle_ply IS NULL "
        "AND opening_phase_probe_verdict = 'exhausted')",
    ),
    (
        "ck_game_sessions_opening_shadow_requires_protocol",
        "opening_boundary_shadow_terminal_at IS NULL OR "
        "opening_phase_protocol_version = 1",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute("SET LOCAL statement_timeout = '60s'")

    columns = (
        sa.Column("opening_phase_protocol_version", sa.SmallInteger(), nullable=True),
        sa.Column("opening_phase_probe_ply", sa.Integer(), nullable=True),
        sa.Column("opening_phase_probe_verdict", sa.String(length=40), nullable=True),
        sa.Column("opening_middle_candidate_ply", sa.Integer(), nullable=True),
        sa.Column("opening_middle_ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opening_middle_ply", sa.Integer(), nullable=True),
        sa.Column(
            "opening_phase_exhausted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "opening_boundary_shadow_terminal_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    for column in columns:
        op.add_column("game_sessions", column)

    if bind.dialect.name == "postgresql":
        for name, condition in _CHECKS:
            op.create_check_constraint(name, "game_sessions", condition)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        for name, _condition in reversed(_CHECKS):
            op.drop_constraint(name, "game_sessions", type_="check")
    for name in (
        "opening_boundary_shadow_terminal_at",
        "opening_phase_exhausted",
        "opening_middle_ply",
        "opening_middle_ready_at",
        "opening_middle_candidate_ply",
        "opening_phase_probe_verdict",
        "opening_phase_probe_ply",
        "opening_phase_protocol_version",
    ):
        op.drop_column("game_sessions", name)
