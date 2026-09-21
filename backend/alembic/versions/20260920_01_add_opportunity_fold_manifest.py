"""Add the committed fold manifest and the recovery-window anchor.

Two additions, both of which exist to make deletion reversible for a BOUNDED
time and provably irreversible after it:

* ``opportunity_fold_batches`` — one row per committed fold, written inside the
  same transaction that deletes the raw rows. It names the verified export, the
  two hashes that identify it, and the per-blunder contributions a restore
  subtracts back. It expires seven days after the commit.
* ``opportunity_retention_policy.first_fold_committed_at`` — when the first raw
  row was deleted anywhere. It anchors the seven-day full-rollback window, and
  it is on the singleton rather than derived from the manifests because those
  are deleted at expiry: a deadline that recedes as its own evidence is cleaned
  up is not a deadline.

Additive and inert. Nothing here folds anything; ``cleanup_enabled`` still gates
that, and this migration does not touch it.

The DOWNGRADE is where the boundary is enforced. Going back past this revision
is the first step of a raw-history schema rollback, so it refuses while any
batch still describes rows that are deleted, and refuses permanently once the
recovery window has closed — at that point the exports that could have refilled
those rows are gone and only a compact-aware release is a rollback target.

Revision ID: 20260920_01
Revises: 20260919_05
"""

from datetime import timedelta, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20260920_01"
down_revision = "20260919_05"
branch_labels = None
depends_on = None

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# Mirrors app.opportunity_fold.RECOVERY_WINDOW. A literal, not an import: a
# migration must keep describing the schema it created even if the constant it
# was written against later changes.
RECOVERY_WINDOW_DAYS = 7


def upgrade():
    op.create_table(
        "opportunity_fold_batches",
        sa.Column("batch_id", UUID(as_uuid=True), nullable=False, primary_key=True),
        sa.Column("user_id", BIGINT, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("rowset_hash", sa.String(64), nullable=False),
        sa.Column("hash_version", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("statement_timestamp()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_session_started_at", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("targeted_discarded_max_served_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("contributions", sa.JSON(), nullable=False),
        sa.CheckConstraint("row_count > 0", name="ck_opportunity_fold_batch_rows"),
        sa.CheckConstraint("expires_at > committed_at",
                           name="ck_opportunity_fold_batch_expiry"),
    )
    op.create_index("idx_opportunity_fold_batches_expiry",
                    "opportunity_fold_batches", ["expires_at"])
    op.create_index("idx_opportunity_fold_batches_user",
                    "opportunity_fold_batches", ["user_id", "committed_at"])
    # Nullable with no default and no backfill: NULL means "nothing has ever been
    # folded", which is true of every database this migration runs against.
    op.add_column(
        "opportunity_retention_policy",
        sa.Column("first_fold_committed_at", sa.DateTime(timezone=True),
                  nullable=True),
    )


def downgrade():
    bind = op.get_bind()
    # The hard boundary. Seven days after the first deletion the exports have been
    # pruned, so no amount of care can refill the raw rows a pre-compaction reader
    # expects to find. This is announced in advance (scripts/RECOVER_SRS_FOLD.md)
    # precisely so it is never discovered here.
    anchor = bind.execute(sa.text(
        "SELECT first_fold_committed_at FROM opportunity_retention_policy "
        "WHERE id = 1"
    )).scalar()
    if anchor is not None and _utc(_database_clock(bind)) - _utc(anchor) > timedelta(
        days=RECOVERY_WINDOW_DAYS
    ):
        raise RuntimeError(
            "the SRS fold recovery window closed "
            f"{RECOVERY_WINDOW_DAYS} days after {anchor}; the exports that could "
            "refill the deleted raw rows are gone, so a raw-history schema "
            "downgrade is impossible. Roll back to a compact-aware release instead"
        )

    # A batch with no restored_at still describes rows that are PHYSICALLY GONE.
    # Dropping the manifest would take the only pointer to their export with it,
    # so the raw rows could never be put back — refuse and make the operator
    # restore first. Not "restore or expire": inside the window nothing has
    # reached its expiry yet, and the only way to expire one early would be to
    # throw away the recovery this refusal exists to protect.
    if bind.execute(sa.text(
        "SELECT 1 FROM opportunity_fold_batches WHERE restored_at IS NULL LIMIT 1"
    )).first():
        raise RuntimeError(
            "unrestored SRS fold batches exist; put their rows back first "
            "(python scripts/fold_srs_opportunities.py restore) — the deleted raw "
            "rows are what a pre-compaction reader expects to find"
        )
    op.drop_column("opportunity_retention_policy", "first_fold_committed_at")
    op.drop_index("idx_opportunity_fold_batches_user",
                  table_name="opportunity_fold_batches")
    op.drop_index("idx_opportunity_fold_batches_expiry",
                  table_name="opportunity_fold_batches")
    op.drop_table("opportunity_fold_batches")


def _database_clock(bind):
    """Statement-time database clock, never the process clock.

    The deadline it decides is a property of the deployment, not of whichever
    machine happens to be running alembic, and those two clocks are exactly as
    free to disagree here as they are anywhere else in this epic.
    """
    return bind.execute(sa.text(
        "SELECT clock_timestamp()"
        if bind.dialect.name == "postgresql"
        else "SELECT CURRENT_TIMESTAMP"
    )).scalar()


def _utc(value):
    """SQLite hands back a naive datetime; PostgreSQL an aware one. Compare as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
