"""Create opponent_decisions table (g-ghost-target-server-record).

An authoritative, replayable record of every served opponent decision. Before this
table the decision was computed, returned and forgotten — the only server-side trace
was a fire-and-forget PostHog event, and ``session_moves.target_blunder_id`` reached
the database solely as a CLIENT echo. That makes it unusable as the denominator of a
targeted p_reach: a session that never uploads silently drops a FAILED steer.

Grain is an opaque ``decision_id`` PK with ``UNIQUE (session_id, request_fingerprint)``
as the replay key AND the concurrency arbiter — the normal ghost/engine path holds no
row lock, so two concurrent identical requests can both compute, and this constraint
(not a lock) decides which one is served. ``(session_id, ply_before)`` is deliberately
NOT the grain: a post-revert branch legitimately requests a decision at the same ply
from a different position and history.

``session_id`` carries an FK with ON DELETE CASCADE — retention is session-lifetime
with no independent TTL. This diverges from ``session_upload_receipt``'s plain FK-free
column on purpose: that receipt must stay a pure sink alongside the ``evidence_seq``
cursor bump, while ``next-opponent-move`` never writes the cursor. See the
``OpponentDecision`` model docstring for the full lock analysis.

Revision ID: 20260726_01
Revises: 20260724_01
Create Date: 2026-07-26

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement


revision = "20260726_01"
down_revision = "20260724_01"
branch_labels = None
depends_on = None


class statement_timestamp(FunctionElement):  # noqa: N801 (renders as a SQL function)
    """Wall-clock time of the CURRENT STATEMENT — ``statement_timestamp()`` on
    Postgres, ``CURRENT_TIMESTAMP`` elsewhere.

    DELIBERATELY DUPLICATED from ``app.models`` rather than imported: a revision must
    pin the exact DDL it shipped, and importing mutable application code would let a
    later rename break fresh replay — or silently make this same revision id emit
    different DDL. ``test_opponent_decision_record.py`` asserts this frozen copy still
    compiles identically to the model's construct on both dialects, so the duplication
    cannot drift unnoticed.
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
        "opponent_decisions",
        # Allocated application-side (uuid4) and stamped into response_payload BEFORE
        # the insert, so the payload always carries the decision_id of its own row.
        # No server default: an id unknown until after the INSERT would force either a
        # post-insert payload rewrite or a payload disagreeing with its own row.
        sa.Column("decision_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("game_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("request_fen_hash", sa.Text(), nullable=False),
        # Canonical JSON array of the client's move list — NOT the space-joined
        # encode_uci_line form, which is lossy over an unvalidated list[str]. NOT NULL:
        # "[]" is a real empty history, never an absent one.
        sa.Column("uci_history", sa.Text(), nullable=False),
        sa.Column("ply_before", sa.Integer(), nullable=False),
        # Backstop only — the ORM stamps this app-side. The DDL default is the
        # STATEMENT clock, never now(): Postgres' now() is TRANSACTION-start time,
        # which for a request that waited on the drill row lock would predate the
        # insert. Frozen local copy of the model's construct (parity asserted by test)
        # so metadata and migration emit identical DDL, and so SQLite gets a default
        # it can actually execute.
        sa.Column(
            "served_at",
            sa.DateTime(timezone=True),
            server_default=statement_timestamp(),
            nullable=False,
        ),
        sa.Column("response_payload", sa.Text(), nullable=False),
        # Bare FK, mirroring session_moves.target_blunder_id. No ondelete: SET NULL
        # would leave this extracted column disagreeing with its own response_payload,
        # and CASCADE would delete decision rows root confirmation still needs.
        sa.Column(
            "target_blunder_id",
            bigint_sqlite,
            sa.ForeignKey("blunders.id"),
            nullable=True,
        ),
        sa.Column("resulting_fen", sa.Text(), nullable=True),
        sa.Column(
            "reaches_drill_root",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.CheckConstraint("ply_before >= 0", name="ck_opponent_decisions_ply_before"),
        sa.UniqueConstraint(
            "session_id",
            "request_fingerprint",
            name="uq_opponent_decisions_session_fingerprint",
        ),
    )
    # The targeted-counters aggregate: both the 30-day cutoff and the after-created
    # predicate apply PER DECISION ROW, before any (session_id, target_blunder_id)
    # grouping.
    op.create_index(
        "idx_opponent_decisions_target_served",
        "opponent_decisions",
        ["target_blunder_id", "served_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_opponent_decisions_target_served",
        table_name="opponent_decisions",
    )
    op.drop_table("opponent_decisions")
