"""Add idempotency keys for SRS and blunder POSTs.

Adds nullable idempotency bookkeeping so duplicate SRS reviews and blunder
recordings can be deduplicated:
  * ``blunder_reviews.idempotency_key`` — supplied key for an SRS review; a
    partial unique index ``uq_blunder_reviews_idempotency`` over
    ``(blunder_id, idempotency_key)`` dedupes retries. The PostgreSQL form uses
    ``WHERE idempotency_key IS NOT NULL`` so two keyless reviews are both
    allowed; SQLite (which lacks partial-index support in this codebase's
    create_all path) gets a plain unique index where multiple NULLs are still
    permitted by SQLite's NULL-distinct semantics.
  * ``game_sessions.recorded_blunder_id`` — mirrors ``blunders.id`` for the
    first recorded blunder so retries echo back the real id (BigInteger on PG to
    match the blunders PK; Integer on SQLite).
  * ``game_sessions.blunder_idempotency_key`` — the decision key for the
    first-blunder recording.

All columns are nullable; existing rows stay NULL and the current frontend keeps
working unchanged.

Revision ID: 20260613_01
Revises: 20260611_02
Create Date: 2026-06-13

"""
import sqlalchemy as sa
from alembic import op


revision = "20260613_01"
down_revision = "20260611_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "blunder_reviews",
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
    )
    # pass_streak after the review applied; lets an idempotent retry reconstruct
    # the original response even after later reviews mutate the blunder.
    op.add_column(
        "blunder_reviews",
        sa.Column("pass_streak_after", sa.Integer(), nullable=True),
    )
    if op.get_context().dialect.name == "postgresql":
        op.create_index(
            "uq_blunder_reviews_idempotency",
            "blunder_reviews",
            ["blunder_id", "idempotency_key"],
            unique=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        )
        op.add_column(
            "game_sessions",
            sa.Column("recorded_blunder_id", sa.BigInteger(), nullable=True),
        )
    else:
        op.create_index(
            "uq_blunder_reviews_idempotency",
            "blunder_reviews",
            ["blunder_id", "idempotency_key"],
            unique=True,
        )
        op.add_column(
            "game_sessions",
            sa.Column("recorded_blunder_id", sa.Integer(), nullable=True),
        )
    op.add_column(
        "game_sessions",
        sa.Column("blunder_idempotency_key", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("game_sessions", "blunder_idempotency_key")
    op.drop_column("game_sessions", "recorded_blunder_id")
    op.drop_index("uq_blunder_reviews_idempotency", table_name="blunder_reviews")
    op.drop_column("blunder_reviews", "pass_streak_after")
    op.drop_column("blunder_reviews", "idempotency_key")
