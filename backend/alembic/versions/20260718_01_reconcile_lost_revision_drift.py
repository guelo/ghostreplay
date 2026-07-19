"""Reconcile prod schema drift left by the lost 20260713_01 revision (g-alembic-drift-recon).

Production was stamped at revision ``20260713_01``, applied from a developer
machine that died before the work was committed. The revision exists in no
commit, so ``alembic upgrade head`` could not resolve a path and the Railway
deploy crash-looped. The version pointer was reset with
``alembic stamp 20260709_02``; this migration codifies what that lost revision
actually did to the schema so ``compare_metadata`` returns clean and genuine
future drift stops hiding inside known noise.

The lost revision did two separable things:

1. DROPPED four indexes, each a strict left-prefix of an existing unique
   constraint's btree. Postgres already answers those queries from the unique
   index, so the drops cost nothing at read time and save write overhead — a
   legitimate cleanup we adopt rather than revert. Prod already lacks them;
   fresh databases still create them in earlier migrations, so the drop runs
   for real there and is a no-op on prod.

2. ADDED three nullable columns and one index for a "baseline evidence
   watermark" feature. No model or code references them — the feature's
   application code died with the machine. They are dropped here so prod stops
   carrying an orphan. Dropping ``opening_baseline_evidence_seq`` also removes
   ``idx_game_sessions_baseline_watermark`` implicitly, since Postgres drops
   indexes that depend on a dropped column.

Dialect handling: the index drops run everywhere. The column drops are guarded
to PostgreSQL — the columns exist only in prod (SQLite test databases are built
by replaying migrations, which never created them), and SQLite has no
``DROP COLUMN IF EXISTS``.

Downgrade restores the four indexes, since earlier migrations declare them and a
downgrade past this point should leave those migrations' state intact. It does
NOT restore the orphan columns: they belong to no model, no code reads them, and
recreating empty columns for a feature that no longer exists would reintroduce
the drift this migration removes.

Revision ID: 20260718_01
Revises: 20260709_02
Create Date: 2026-07-18

"""
from alembic import op


revision = "20260718_01"
down_revision = "20260709_02"
branch_labels = None
depends_on = None

# (index name, table, columns) — each is a left-prefix of the unique constraint
# named alongside it, which is why dropping it is read-neutral.
REDUNDANT_INDEXES = [
    # covered by uq_analysis_cache_fen_move (fen_before, move_uci)
    ("idx_analysis_cache_fen", "analysis_cache", ["fen_before"]),
    # covered by uq_blunder_opportunity_session_blunder (session_id, blunder_id)
    ("idx_blunder_opportunity_session", "blunder_opportunity_events", ["session_id"]),
    # covered by uq_opening_position_edges_batch_parent_child (batch_id, parent_fen, child_fen)
    (
        "idx_opening_position_edges_batch_parent",
        "opening_position_edges",
        ["batch_id", "parent_fen"],
    ),
    # exact duplicate of uq_opening_position_scores_batch_fen (batch_id, normalized_fen)
    (
        "idx_opening_position_scores_batch_fen",
        "opening_position_scores",
        ["batch_id", "normalized_fen"],
    ),
]

# Orphaned by the lost revision: nullable, referenced by no model or query.
ORPHAN_COLUMNS = [
    ("game_sessions", "opening_baseline_evidence_seq"),
    ("opening_score_batches", "evidence_seq_end"),
    ("opening_score_batches", "scoped_shared_digest_end"),
]


def upgrade() -> None:
    for index_name, _table, _columns in REDUNDANT_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")

    if op.get_bind().dialect.name != "postgresql":
        return

    # Explicit even though the column drop below cascades to it, so the intent is
    # legible and the statement is safe to run against a partially-cleaned DB.
    op.execute("DROP INDEX IF EXISTS idx_game_sessions_baseline_watermark")
    for table, column in ORPHAN_COLUMNS:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")


def downgrade() -> None:
    for index_name, table, columns in REDUNDANT_INDEXES:
        op.create_index(index_name, table, columns)
