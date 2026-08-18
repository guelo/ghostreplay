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


revision = "20260814_01"
down_revision = "20260810_01"
branch_labels = None
depends_on = None


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


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
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
