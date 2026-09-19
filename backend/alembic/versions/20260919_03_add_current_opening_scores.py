"""Add inactive B50 opening storage; refuse downgrade until reverse conversion.

Revision ID: 20260919_03
Revises: 20260919_01
"""

from alembic import op
import sqlalchemy as sa

revision = "20260919_03"
down_revision = "20260919_01"
branch_labels = None
depends_on = None

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
KEY = sa.Text(collation="C").with_variant(sa.Text(collation="BINARY"), "sqlite")


def upgrade():
    op.add_column(
        "opening_score_batches",
        sa.Column(
            "storage_format", sa.String(16), nullable=False, server_default="legacy"
        ),
    )
    op.create_table(
        "opening_current_roots",
        sa.Column("id", BIGINT, nullable=False, primary_key=True),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("player_color", sa.String(5), nullable=False),
        sa.Column("opening_key", KEY, nullable=False),
        sa.Column("opening_name", sa.Text(), nullable=False),
        sa.Column("opening_family", sa.Text(), nullable=False),
        sa.Column("opening_score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("coverage", sa.Float(), nullable=False),
        sa.Column("weighted_depth", sa.Float(), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("game_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_practiced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("strongest_branch_name", sa.Text(), nullable=True),
        sa.Column("strongest_branch_key", KEY, nullable=True),
        sa.Column("strongest_branch_score", sa.Float(), nullable=True),
        sa.Column("weakest_branch_name", sa.Text(), nullable=True),
        sa.Column("weakest_branch_key", KEY, nullable=True),
        sa.Column("weakest_branch_score", sa.Float(), nullable=True),
        sa.Column("underexposed_branch_name", sa.Text(), nullable=True),
        sa.Column("underexposed_branch_key", KEY, nullable=True),
        sa.Column("underexposed_branch_value", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "player_color in ('white','black')", name="ck_opening_current_roots_color"
        ),
        sa.UniqueConstraint(
            "user_id",
            "player_color",
            "opening_key",
            name="uq_opening_current_roots_identity",
        ),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "opening_current_positions",
        sa.Column("id", BIGINT, nullable=False, primary_key=True),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("player_color", sa.String(5), nullable=False),
        sa.Column("normalized_fen", KEY, nullable=False),
        sa.Column("in_book", sa.Boolean(), nullable=False),
        sa.Column("has_evidence", sa.Boolean(), nullable=False),
        sa.Column("opening_score", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("coverage", sa.Float(), nullable=True),
        sa.Column("weighted_depth", sa.Float(), nullable=True),
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("game_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_practiced_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "player_color in ('white','black')",
            name="ck_opening_current_positions_color",
        ),
        sa.UniqueConstraint(
            "user_id",
            "player_color",
            "normalized_fen",
            name="uq_opening_current_positions_identity",
        ),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "opening_current_edges",
        sa.Column("id", BIGINT, nullable=False, primary_key=True),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("player_color", sa.String(5), nullable=False),
        sa.Column("parent_fen", KEY, nullable=False),
        sa.Column("child_fen", KEY, nullable=False),
        sa.Column("uci", KEY, nullable=False),
        sa.Column("traversal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("live_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("live_passes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("live_fails", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "player_color in ('white','black')", name="ck_opening_current_edges_color"
        ),
        sa.UniqueConstraint(
            "user_id",
            "player_color",
            "parent_fen",
            "child_fen",
            name="uq_opening_current_edges_identity",
        ),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "opening_current_scope",
        sa.Column("user_id", BIGINT, nullable=False, primary_key=True),
        sa.Column("player_color", sa.String(5), nullable=False, primary_key=True),
        sa.Column("kind", KEY, nullable=False, primary_key=True),
        sa.Column("fen", KEY, nullable=False, primary_key=True),
        sa.CheckConstraint(
            "player_color in ('white','black')", name="ck_opening_current_scope_color"
        ),
        sa.CheckConstraint(
            "kind in ('raw','norm')", name="ck_opening_current_scope_kind"
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE opening_current_roots SET (fillfactor = 50)")
        op.execute("ALTER TABLE opening_current_positions SET (fillfactor = 50)")


def downgrade():
    bind = op.get_bind()
    # Include orphan payloads: silently dropping them would hide an incomplete
    # conversion. Drain all publishers before this operational migration.
    for table in (
        "opening_current_roots",
        "opening_current_positions",
        "opening_current_edges",
        "opening_current_scope",
    ):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(
                "reverse-convert current opening scores before downgrade"
            )
    if bind.execute(
        sa.text(
            "SELECT 1 FROM opening_score_batches WHERE storage_format <> 'legacy' LIMIT 1"
        )
    ).first():
        raise RuntimeError("reverse-convert current opening markers before downgrade")
    for table in (
        "opening_current_scope",
        "opening_current_edges",
        "opening_current_positions",
        "opening_current_roots",
    ):
        op.drop_table(table)
    op.drop_column("opening_score_batches", "storage_format")
