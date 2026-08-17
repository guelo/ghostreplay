"""Add revision-fenced session move-line truncation.

PostgreSQL 11+ adds the non-null constant-default revision and terminal-
reconciliation marker columns without a table rewrite, but acquiring the
required ACCESS EXCLUSIVE DDL lock can still
wait behind live session writers. The transaction-local five-second lock timeout
makes deployment fail cleanly instead of stalling indefinitely, while the
statement timeout bounds the validating CHECK scan. Alembic runs the revision in
one transaction, so splitting ADD CHECK NOT VALID from VALIDATE would still hold
the original ACCESS EXCLUSIVE lock until commit; create the validated constraint
in one statement instead of implying a weaker-lock phase that cannot occur here.

SQLite cannot add the game_sessions CHECK without batch-rewriting the table and
risking its existing unnamed checks, so that dialect adds only the column. The
hand-written SQLite test schema includes the named check; runtime revision CAS is
the load-bearing defense on every dialect.

Revision ID: 20260814_01
Revises: 20260810_01
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement


revision = "20260814_01"
down_revision = "20260810_01"
branch_labels = None
depends_on = None


class statement_timestamp(FunctionElement):  # noqa: N801
    """Statement clock on PostgreSQL, portable current time elsewhere."""

    inherit_cache = True
    name = "statement_timestamp"
    type = sa.DateTime(timezone=True)


@compiles(statement_timestamp)
def _compile_statement_timestamp_default(element, compiler, **kw) -> str:
    return "CURRENT_TIMESTAMP"


@compiles(statement_timestamp, "postgresql")
def _compile_statement_timestamp_postgresql(element, compiler, **kw) -> str:
    return "statement_timestamp()"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute("SET LOCAL statement_timeout = '60s'")

    op.add_column(
        "game_sessions",
        sa.Column(
            "move_line_revision",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "game_sessions",
        sa.Column(
            "terminal_line_reconciled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    if bind.dialect.name == "postgresql":
        # Alembic keeps both statements in one transaction, so NOT VALID then
        # VALIDATE would run the same scan while the ADD's ACCESS EXCLUSIVE lock
        # remained held. Keep one atomic, proven catalog state; statement_timeout
        # bounds the scan and fails the whole migration if it cannot finish.
        op.create_check_constraint(
            "ck_game_sessions_move_line_revision",
            "game_sessions",
            "move_line_revision >= 0",
        )

    op.add_column(
        "session_upload_receipt",
        sa.Column("move_line_revision", sa.Integer(), nullable=True),
    )
    op.add_column(
        "session_upload_receipt",
        sa.Column("line_proof_verdict", sa.Text(), nullable=True),
    )
    op.add_column(
        "session_upload_receipt",
        sa.Column("line_sync_verdict", sa.Text(), nullable=True),
    )

    op.create_table(
        "session_move_truncation_receipt",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("game_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("from_revision", sa.Integer(), nullable=False),
        sa.Column("to_revision", sa.Integer(), nullable=False),
        sa.Column("after_ply", sa.Integer(), nullable=False),
        sa.Column("deleted_move_count", sa.Integer(), nullable=False),
        sa.Column("evidence_changed", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=statement_timestamp(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "session_id",
            "client_request_id",
            name="uq_session_move_truncation_receipt_request",
        ),
        sa.UniqueConstraint(
            "session_id",
            "to_revision",
            name="uq_session_move_truncation_receipt_revision",
        ),
        sa.CheckConstraint(
            "from_revision >= 0",
            name="ck_session_move_truncation_receipt_from_revision",
        ),
        sa.CheckConstraint(
            "to_revision >= 0",
            name="ck_session_move_truncation_receipt_to_revision",
        ),
        sa.CheckConstraint(
            "after_ply >= 0",
            name="ck_session_move_truncation_receipt_after_ply",
        ),
        sa.CheckConstraint(
            "deleted_move_count >= 0",
            name="ck_session_move_truncation_receipt_deleted_move_count",
        ),
        sa.CheckConstraint(
            "to_revision = from_revision + 1",
            name="ck_session_move_truncation_receipt_revision_step",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("session_move_truncation_receipt")
    op.drop_column("session_upload_receipt", "line_sync_verdict")
    op.drop_column("session_upload_receipt", "line_proof_verdict")
    op.drop_column("session_upload_receipt", "move_line_revision")
    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "ck_game_sessions_move_line_revision",
            "game_sessions",
            type_="check",
        )
    op.drop_column("game_sessions", "move_line_revision")
    op.drop_column("game_sessions", "terminal_line_reconciled")
