"""Create position_analysis + position_analysis_conflicts storage tables.

Foundation for separating trusted position truth (one winner per normalized FEN)
from per-move evidence (g-position-analysis Phase 1). No code writes these tables
in Phase 1; Phase 2 backfill populates position_analysis and records disagreements
in position_analysis_conflicts, Phase 3 owns winner-replacement writes.

position_analysis is keyed by normalized_fen (one winning analysis per canonical
position), distinct from analysis_cache's (fen_before, move_uci) per-move grain. It
carries updated_at because winners are replaced over time; see the model comment for
the upsert caveat (onupdate does not fire on on_conflict_do_update). fen is a
provenance/sample column only, never a lookup/uniqueness key.

position_analysis_conflicts is an append-only audit sink (no updated_at): many
records may accrue per normalized_fen across recomputes, so normalized_fen is
indexed but not unique. position_analysis_id is a plain nullable bigint (no FK) to
keep backfill/delete ordering simple in both Postgres and the FK-off SQLite schema.

Revision ID: 20260617_01
Revises: 20260616_01
Create Date: 2026-06-17

"""
import sqlalchemy as sa
from alembic import op


revision = "20260617_01"
down_revision = "20260616_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bigint_sqlite = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    op.create_table(
        "position_analysis",
        sa.Column("id", bigint_sqlite, primary_key=True, autoincrement=True),
        sa.Column("normalized_fen", sa.Text(), nullable=False),
        sa.Column("fen", sa.Text(), nullable=False),
        sa.Column("best_move_uci", sa.String(length=5), nullable=False),
        sa.Column("best_move_san", sa.String(length=10), nullable=True),
        sa.Column("best_line_uci", sa.Text(), nullable=True),
        sa.Column("best_eval", sa.Integer(), nullable=True),
        sa.Column("best_eval_mate", sa.Integer(), nullable=True),
        sa.Column(
            "source", sa.String(length=20), nullable=False, server_default="precomputed"
        ),
        sa.Column("analysis_profile_id", sa.String(length=64), nullable=True),
        sa.Column("engine_name", sa.String(length=64), nullable=True),
        sa.Column("engine_version", sa.String(length=64), nullable=True),
        sa.Column("engine_build", sa.String(length=128), nullable=True),
        sa.Column("network_id", sa.String(length=128), nullable=True),
        sa.Column("search_limit_type", sa.String(length=16), nullable=True),
        sa.Column("search_limit_value", sa.Integer(), nullable=True),
        sa.Column("threads", sa.Integer(), nullable=True),
        sa.Column("hash_mb", sa.Integer(), nullable=True),
        sa.Column("multipv", sa.Integer(), nullable=True),
        sa.Column("eval_file_id", sa.Text(), nullable=True),
        sa.Column("eval_file_small_id", sa.Text(), nullable=True),
        sa.Column("analyzer_protocol_version", sa.String(length=64), nullable=True),
        sa.Column("profile_manifest_digest", sa.String(length=64), nullable=True),
        sa.Column("evidence_contract_id", sa.String(length=64), nullable=True),
        sa.Column("source_cache_id", bigint_sqlite, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "normalized_fen", name="uq_position_analysis_normalized_fen"
        ),
    )

    op.create_table(
        "position_analysis_conflicts",
        sa.Column("id", bigint_sqlite, primary_key=True, autoincrement=True),
        sa.Column("normalized_fen", sa.Text(), nullable=False),
        sa.Column("position_analysis_id", bigint_sqlite, nullable=True),
        sa.Column("candidate_cache_ids", sa.Text(), nullable=True),
        sa.Column("candidate_summaries", sa.Text(), nullable=True),
        sa.Column("best_move_disagreement", sa.Text(), nullable=True),
        sa.Column("pv_disagreement", sa.Text(), nullable=True),
        sa.Column("best_eval_disagreement", sa.Text(), nullable=True),
        sa.Column("best_eval_mate_disagreement", sa.Text(), nullable=True),
        sa.Column("policy_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_position_analysis_conflicts_norm",
        "position_analysis_conflicts",
        ["normalized_fen"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_position_analysis_conflicts_norm",
        table_name="position_analysis_conflicts",
    )
    op.drop_table("position_analysis_conflicts")
    op.drop_table("position_analysis")
