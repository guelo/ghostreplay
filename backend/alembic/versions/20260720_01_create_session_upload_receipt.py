"""Create session_upload_receipt table (g-upload-observe).

A durable, transactional receipt of the end-of-session ``final_full`` move-history
upload. Written inside the SAME transaction as the moves and keyed by the
middleware-normalized ``client_request_id``, it is the join target that turns an
observed client timeout into an exact commit classification (receipt present ⇒
committed; receipt absent past the adjudication horizon ⇒ did NOT commit),
independent of fire-and-forget PostHog delivery.

``session_id`` is a PLAIN column (NO FK) so the append-only insert takes no
``KEY SHARE`` lock on ``game_sessions`` and cannot perturb the writer-lock DAG;
the /moves endpoint flushes it BEFORE the ``evidence_seq`` cursor bump.
``client_request_id`` is NOT NULL — a ``final_full`` upload lacking a valid client
id is rejected 400 before any writes, so no null-id receipt can exist.

Deploy this migration + backend BEFORE the frontend that starts sending
``X-Client-Request-ID`` / ``terminal_action``; otherwise new client events would
hit an old server that writes no receipt, manufacturing false "loss".

Revision ID: 20260720_01
Revises: 20260719_01
Create Date: 2026-07-20

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement


revision = "20260720_01"
down_revision = "20260719_01"
branch_labels = None
depends_on = None


class statement_timestamp(FunctionElement):  # noqa: N801 (renders as a SQL function)
    """Wall-clock time of the CURRENT STATEMENT — ``statement_timestamp()`` on
    Postgres, ``CURRENT_TIMESTAMP`` elsewhere.

    DELIBERATELY DUPLICATED from ``app.models`` rather than imported: a revision
    must pin the exact DDL it shipped, and importing mutable application code would
    let a later rename break fresh replay — or worse, silently make this same
    revision id emit different DDL. ``test_session_upload_receipt.py`` asserts this
    frozen copy still compiles identically to the model's construct on both
    dialects, so the duplication cannot drift unnoticed.
    """

    type = sa.DateTime(timezone=True)
    name = "statement_timestamp"
    inherit_cache = True


@compiles(statement_timestamp)
def _compile_statement_timestamp_default(element, compiler, **kw) -> str:
    return "CURRENT_TIMESTAMP"


@compiles(statement_timestamp, "postgresql")
def _compile_statement_timestamp_postgresql(element, compiler, **kw) -> str:
    return "statement_timestamp()"


def upgrade() -> None:
    bigint_sqlite = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    op.create_table(
        "session_upload_receipt",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", bigint_sqlite, nullable=False),
        sa.Column("client_request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("server_request_id", sa.Text(), nullable=True),
        sa.Column("recompute_opportunity", sa.Boolean(), nullable=False),
        sa.Column("session_mode", sa.Text(), nullable=True),
        sa.Column("terminal_action", sa.Text(), nullable=True),
        sa.Column("content_length_bytes", sa.Integer(), nullable=True),
        # Backstop only — the ORM stamps this app-side at flush. The DDL default is
        # the STATEMENT clock, never now(): Postgres' now() is TRANSACTION-start
        # time, which for a request that waited on the session row lock would be
        # seconds earlier than the receipt's actual insertion. Frozen local copy of
        # the model's construct (parity is asserted by test) so metadata and
        # migration emit identical DDL, and so SQLite gets a default it can
        # actually execute (it has no statement_timestamp()).
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=statement_timestamp(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_session_upload_receipt_session",
        "session_upload_receipt",
        ["session_id"],
    )
    op.create_index(
        "idx_session_upload_receipt_user",
        "session_upload_receipt",
        ["user_id"],
    )
    op.create_index(
        "idx_session_upload_receipt_client_request_id",
        "session_upload_receipt",
        ["client_request_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_session_upload_receipt_client_request_id",
        table_name="session_upload_receipt",
    )
    op.drop_index(
        "idx_session_upload_receipt_user",
        table_name="session_upload_receipt",
    )
    op.drop_index(
        "idx_session_upload_receipt_session",
        table_name="session_upload_receipt",
    )
    op.drop_table("session_upload_receipt")
