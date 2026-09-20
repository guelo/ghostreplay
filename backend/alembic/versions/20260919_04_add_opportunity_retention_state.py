"""Add SRS opportunity retention policy, per-user state and per-blunder summaries.

Additive only. Cleanup stays disabled and readiness stays false: this migration
creates the storage and the ZERO baseline, it does not fold anything and it does
not authorize a retention deadline.

The backfill is deliberately zero-valued and idempotent. Every blunder gets a
summary and every user gets a retention-state row so that a later missing row is
unambiguous evidence of loss rather than of a partial rollout. Inserting only the
rows that are absent (NOT EXISTS / ON CONFLICT DO NOTHING) makes a concurrent
review or blunder insert safe: it can create the same row first, and its values
are never overwritten here.

Revision ID: 20260919_04
Revises: 20260919_03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20260919_04"
down_revision = "20260919_03"
branch_labels = None
depends_on = None

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade():
    op.create_table(
        "opportunity_retention_policy",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True,
                  autoincrement=False),
        # Seeded at the decided horizon (M = 60 days, G = 1 hour), not at a
        # placeholder: the INSERT below supplies only id, so these defaults ARE
        # the policy every deployment starts with. Both switches stay false, so
        # the values do nothing until activation flips them deliberately.
        sa.Column("mutation_window_days", sa.Integer(), nullable=False,
                  server_default="60"),
        sa.Column("grace_seconds", sa.Integer(), nullable=False,
                  server_default="3600"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("freeze_enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("cleanup_enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("readiness", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_opportunity_retention_policy_singleton"),
        sa.CheckConstraint("mutation_window_days > 0",
                           name="ck_opportunity_retention_policy_window"),
        sa.CheckConstraint("grace_seconds >= 0",
                           name="ck_opportunity_retention_policy_grace"),
        sa.CheckConstraint("version >= 1",
                           name="ck_opportunity_retention_policy_version"),
        sa.CheckConstraint("freeze_enabled = false or readiness = true",
                           name="ck_opportunity_retention_policy_ready_before_freeze"),
        sa.CheckConstraint("cleanup_enabled = false or freeze_enabled = true",
                           name="ck_opportunity_retention_policy_freeze_before_cleanup"),
    )
    op.create_table(
        "user_opportunity_retention_state",
        sa.Column("user_id", BIGINT, nullable=False, primary_key=True,
                  autoincrement=False),
        sa.Column("folded_through_started_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("targeted_discarded_max_served_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("sweep_progress_started_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "blunder_opportunity_summaries",
        sa.Column("blunder_id", BIGINT, nullable=False, primary_key=True,
                  autoincrement=False),
        sa.Column("folded_eligible_count", BIGINT, nullable=False,
                  server_default="0"),
        sa.Column("folded_opportunities_since_review", BIGINT, nullable=False,
                  server_default="0"),
        sa.Column("folded_reached_since_review", BIGINT, nullable=False,
                  server_default="0"),
        sa.Column("latest_review_id", BIGINT, nullable=True),
        sa.Column("latest_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_review_session_id", UUID(as_uuid=True), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["blunder_id"], ["blunders.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "folded_eligible_count >= 0"
            " and folded_opportunities_since_review >= 0"
            " and folded_reached_since_review >= 0",
            name="ck_blunder_opportunity_summary_nonnegative",
        ),
        sa.CheckConstraint(
            "folded_reached_since_review <= folded_opportunities_since_review",
            name="ck_blunder_opportunity_summary_reached_within_opportunities",
        ),
        sa.CheckConstraint(
            "folded_opportunities_since_review <= folded_eligible_count",
            name="ck_blunder_opportunity_summary_since_review_within_lifetime",
        ),
    )

    bind = op.get_bind()
    conflict = (
        " ON CONFLICT DO NOTHING" if bind.dialect.name in ("postgresql", "sqlite")
        else ""
    )
    op.execute(sa.text(
        "INSERT INTO opportunity_retention_policy (id) VALUES (1)" + conflict
    ))
    # NOT EXISTS keeps the statement a no-op on re-run even on a dialect with no
    # upsert clause; ON CONFLICT then covers the concurrent-insert race.
    op.execute(sa.text(
        "INSERT INTO user_opportunity_retention_state (user_id) "
        "SELECT u.id FROM users u WHERE NOT EXISTS ("
        "  SELECT 1 FROM user_opportunity_retention_state s WHERE s.user_id = u.id)"
        + conflict
    ))
    # The summary is created WITH the blunder's current review basis, not with a
    # NULL one. After readiness the reader compares that basis against the live
    # latest review and raises when they disagree, so a basis-less backfill would
    # make flipping readiness raise for every blunder ever reviewed — on the
    # ghost-move path, which must never fail a move. The correlated ORDER BY
    # matches the reader's ranking exactly (reviewed_at DESC, id DESC) so a
    # same-instant pair cannot be broken two different ways.
    op.execute(sa.text(
        "INSERT INTO blunder_opportunity_summaries "
        "  (blunder_id, latest_review_id, latest_review_at, latest_review_session_id) "
        "SELECT b.id, r.id, r.reviewed_at, r.session_id "
        "  FROM blunders b "
        "  LEFT JOIN blunder_reviews r ON r.id = ("
        "    SELECT r2.id FROM blunder_reviews r2 WHERE r2.blunder_id = b.id "
        "     ORDER BY r2.reviewed_at DESC, r2.id DESC LIMIT 1) "
        " WHERE NOT EXISTS ("
        "  SELECT 1 FROM blunder_opportunity_summaries s WHERE s.blunder_id = b.id)"
        + conflict
    ))
    # This backfill is NOT sufficient on its own: old application instances keep
    # recording blunders and reviews until the deploy finishes, and they do not
    # write a basis. app.opportunity_store.reconcile_review_basis is the
    # re-runnable form of the same two statements, and readiness is gated on it
    # reporting zero work left. See scripts/RETAIN_SRS_OPPORTUNITIES.md.


def downgrade():
    bind = op.get_bind()
    # A nonzero summary means raw rows were physically deleted and these totals
    # are their ONLY remaining record. Dropping the table would destroy evidence
    # that cannot be recomputed, so refuse rather than lose it.
    if bind.execute(sa.text(
        "SELECT 1 FROM blunder_opportunity_summaries "
        "WHERE folded_eligible_count > 0 LIMIT 1"
    )).first():
        raise RuntimeError("folded opportunity evidence exists; recover before downgrade")
    if bind.execute(sa.text(
        "SELECT 1 FROM user_opportunity_retention_state "
        "WHERE folded_through_started_at IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("fold prefixes exist; recover before downgrade")
    op.drop_table("blunder_opportunity_summaries")
    op.drop_table("user_opportunity_retention_state")
    op.drop_table("opportunity_retention_policy")
