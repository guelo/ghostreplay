"""Create analysis_cache_submission (g-v21l).

Submitter-eligibility associations for read-time trust of non-canonical
browser-analysis evidence. ``analysis_cache`` is globally keyed on
``(fen_before, move_uci)`` and carries no owner column, so granting a
non-authoritative row a read/reuse capability unscoped would convert "a user can
lie to themselves" into "a user can lie to everyone". Eligibility is therefore a
(row, user) ASSOCIATION: a non-authoritative row satisfies an owner-scoped
capability only for a user who independently submitted a consistent tuple.

The pair IS the identity, so ``(analysis_cache_id, user_id)`` is the composite
primary key and there is no surrogate id. The separate reverse-order index serves
the viewer-scoped read (``WHERE user_id = :viewer AND analysis_cache_id IN :ids``),
whose leading column the primary key does not serve. Both FKs cascade on delete:
dropping an evidence row drops its associations, and dropping a user cannot strand
eligibility rows that a recycled id would inherit.

The table joins ``EVIDENCE_EPOCH_SHARED_TABLES``: associations gate the
OPENING_EVIDENCE trust filter, so an association-only write must advance
``evidence_epoch`` exactly as an evidence write does, or the cheap freshness check
would re-arm an opening-score batch computed when its user could not read evidence
they can now read.

NO BACKFILL and no new column on ``analysis_cache``: a pre-existing browser row
becomes readable when its submitter resubmits the identical tuple, which takes the
``SAME_PROFILE_IDEMPOTENT`` branch, changes no evidence column, and gains an
association through the writer's claim pass.

Revision ID: 20260727_01
Revises: 20260726_01
Create Date: 2026-07-27

"""
import sqlalchemy as sa
from alembic import op


revision = "20260727_01"
down_revision = "20260726_01"
branch_labels = None
depends_on = None

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

_TABLE = "analysis_cache_submission"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    op.create_table(
        _TABLE,
        sa.Column(
            "analysis_cache_id",
            BIGINT,
            sa.ForeignKey("analysis_cache.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "user_id",
            BIGINT,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    # Reverse order of the PK: the viewer-scoped read filters on user_id first.
    op.create_index(
        "idx_analysis_cache_submission_user",
        _TABLE,
        ["user_id", "analysis_cache_id"],
    )

    # Same evidence-epoch trigger shape as the 20260708_01 shared tables. The
    # bump function already exists on Postgres (CREATE OR REPLACE keeps this
    # migration independently re-runnable against a database missing it).
    if dialect == "postgresql":
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
        op.execute(
            f"""
            CREATE TRIGGER trg_{_TABLE}_evidence_epoch
            AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON {_TABLE}
            FOR EACH STATEMENT EXECUTE FUNCTION bump_evidence_epoch()
            """
        )
    else:
        for event in ("INSERT", "UPDATE", "DELETE"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{_TABLE}_evidence_epoch_{event.lower()}
                AFTER {event} ON {_TABLE}
                BEGIN
                    UPDATE evidence_epoch SET value = value + 1 WHERE id = 1;
                END
                """
            )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_evidence_epoch ON {_TABLE}")
    else:
        for event in ("insert", "update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_evidence_epoch_{event}")

    op.drop_index("idx_analysis_cache_submission_user", table_name=_TABLE)
    op.drop_table(_TABLE)
