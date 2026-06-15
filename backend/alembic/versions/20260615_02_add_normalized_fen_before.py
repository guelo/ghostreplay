"""Add normalized_fen_before + index to analysis_cache.

The horizontal opening tree (g-tree-eval-cache) resolves a move-node eval from the
global analysis_cache by (parent_fen, move_uci). The tree replays the selected UCI
line, producing a full six-field parent FEN whose move clocks may differ from the
stored fen_before (transpositions). To support an indexed normalized-FEN fallback
without scanning, store the normalized 4-field FEN alongside fen_before and index
it together with move_uci.

normalize_fen canonicalizes the en-passant field using python-chess, so the backfill
must run in Python rather than pure SQL.

Revision ID: 20260615_02
Revises: 20260615_01
Create Date: 2026-06-15

"""
import logging

import sqlalchemy as sa
from alembic import op

from app.fen import normalize_fen

revision = "20260615_02"
down_revision = "20260615_01"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

_BATCH = 1000


def upgrade() -> None:
    with op.batch_alter_table("analysis_cache") as batch_op:
        batch_op.add_column(sa.Column("normalized_fen_before", sa.Text(), nullable=True))

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, fen_before FROM analysis_cache")
    ).fetchall()

    failures = 0
    updates: list[dict] = []
    for row in rows:
        try:
            norm = normalize_fen(row.fen_before)
        except Exception:  # leave NULL on an unparseable FEN; exact lookups still work
            failures += 1
            continue
        updates.append({"id": row.id, "norm": norm})

    stmt = sa.text(
        "UPDATE analysis_cache SET normalized_fen_before = :norm WHERE id = :id"
    )
    for start in range(0, len(updates), _BATCH):
        conn.execute(stmt, updates[start : start + _BATCH])

    if failures:
        log.warning(
            "normalized_fen_before backfill: %d row(s) left NULL (FEN parse failed)",
            failures,
        )

    op.create_index(
        "idx_analysis_cache_norm_move",
        "analysis_cache",
        ["normalized_fen_before", "move_uci"],
    )


def downgrade() -> None:
    op.drop_index("idx_analysis_cache_norm_move", table_name="analysis_cache")
    with op.batch_alter_table("analysis_cache") as batch_op:
        batch_op.drop_column("normalized_fen_before")
