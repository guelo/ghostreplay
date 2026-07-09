"""Cheap opening-score freshness signal (g-jact).

Replaces the O(evidence) row-hash digest on the freshness verdict path with a
partitioned cheap signal:

- ``opening_score_cursors.evidence_seq``: per-(user,color) counter over the
  PER-USER evidence surfaces, bumped app-side in the same txn as each write.
- ``evidence_epoch``: single-row global counter over the SHARED evidence tables
  (``analysis_cache`` / ``position_analysis``), advanced by MANDATORY triggers on
  every INSERT/UPDATE/DELETE so any writer (repo txn, repair delete, backfill,
  future scripts) bumps it with no app-level discipline. The singleton row is
  seeded here — the triggers ``UPDATE ... WHERE id = 1`` and silently no-op if
  the row is missing.
- ``opening_score_batches.evidence_seq / cache_epoch / scoped_shared_digest``:
  the signal stamped on each batch at build time (NULL on pre-migration batches,
  which the paired OPENING_EVIDENCE_INPUTS_VERSION bump already forces to
  rebuild via registry_fingerprint drift — no data backfill needed).
- ``opening_score_batch_shared_scope``: the batch's shared-FEN scope so an
  epoch drift is resolved by re-hashing only this batch's positions.

Revision ID: 20260708_01
Revises: 20260625_01
Create Date: 2026-07-08

"""
import sqlalchemy as sa
from alembic import op


revision = "20260708_01"
down_revision = "20260625_01"
branch_labels = None
depends_on = None

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# Tables whose writes advance the global shared-cache epoch.
_SHARED_TABLES = ("analysis_cache", "position_analysis")


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    op.add_column(
        "opening_score_cursors",
        sa.Column("evidence_seq", BIGINT, nullable=False, server_default="0"),
    )

    with op.batch_alter_table("opening_score_batches") as batch_op:
        batch_op.add_column(sa.Column("evidence_seq", BIGINT, nullable=True))
        batch_op.add_column(sa.Column("cache_epoch", BIGINT, nullable=True))
        batch_op.add_column(sa.Column("scoped_shared_digest", sa.Text(), nullable=True))

    op.create_table(
        "evidence_epoch",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("value", BIGINT, nullable=False, server_default="0"),
        sa.CheckConstraint("id = 1", name="ck_evidence_epoch_singleton"),
    )
    # Seed the singleton row the triggers UPDATE. Without it the epoch never
    # advances and freshness can never be proven (safe but slow).
    op.execute("INSERT INTO evidence_epoch (id, value) VALUES (1, 0)")

    op.create_table(
        "opening_score_batch_shared_scope",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column(
            "batch_id",
            BIGINT,
            sa.ForeignKey("opening_score_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fen", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(4), nullable=False),
        sa.CheckConstraint(
            "kind in ('raw','norm')", name="ck_opening_score_batch_shared_scope_kind"
        ),
    )
    op.create_index(
        "idx_opening_score_batch_shared_scope_batch",
        "opening_score_batch_shared_scope",
        ["batch_id"],
    )

    if dialect == "postgresql":
        # Statement-level on Postgres: one bump per statement is enough (only
        # change matters, not magnitude) and avoids hot-row churn on bulk writes.
        op.execute(
            """
            CREATE OR REPLACE FUNCTION bump_evidence_epoch() RETURNS trigger AS $$
            BEGIN
                UPDATE evidence_epoch SET value = value + 1 WHERE id = 1;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in _SHARED_TABLES:
            # TRUNCATE included: a maintenance truncate removes consumed shared
            # evidence exactly like a DELETE, and only a trigger can see it.
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_evidence_epoch
                AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION bump_evidence_epoch()
                """
            )
    else:
        # SQLite: row-level (no statement-level triggers, no multi-event trigger
        # syntax). N bumps per batch write is harmless — only change matters.
        for table in _SHARED_TABLES:
            for event in ("INSERT", "UPDATE", "DELETE"):
                op.execute(
                    f"""
                    CREATE TRIGGER trg_{table}_evidence_epoch_{event.lower()}
                    AFTER {event} ON {table}
                    BEGIN
                        UPDATE evidence_epoch SET value = value + 1 WHERE id = 1;
                    END
                    """
                )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == "postgresql":
        for table in _SHARED_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_evidence_epoch ON {table}")
        op.execute("DROP FUNCTION IF EXISTS bump_evidence_epoch()")
    else:
        for table in _SHARED_TABLES:
            for event in ("insert", "update", "delete"):
                op.execute(
                    f"DROP TRIGGER IF EXISTS trg_{table}_evidence_epoch_{event}"
                )

    op.drop_index(
        "idx_opening_score_batch_shared_scope_batch",
        table_name="opening_score_batch_shared_scope",
    )
    op.drop_table("opening_score_batch_shared_scope")
    op.drop_table("evidence_epoch")

    with op.batch_alter_table("opening_score_batches") as batch_op:
        batch_op.drop_column("scoped_shared_digest")
        batch_op.drop_column("cache_epoch")
        batch_op.drop_column("evidence_seq")

    with op.batch_alter_table("opening_score_cursors") as batch_op:
        batch_op.drop_column("evidence_seq")
