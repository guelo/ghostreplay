"""The shared read-only REPEATABLE READ snapshot seam.

A multi-statement read that must reflect ONE consistent view of the database
starts its transaction here. PostgreSQL's default READ COMMITTED would let each
statement see different committed data, so a concurrent writer could produce an
answer that corresponds to no single snapshot.

Its own leaf module (no ``app.*`` imports) so a scorer-attested caller can use it
without dragging an unrelated backfill's import closure into the source-binding
manifest.
"""

from __future__ import annotations

from sqlalchemy.orm import Session


def begin_readonly_snapshot(session: Session) -> None:
    """Start this transaction as REPEATABLE READ + READ ONLY on PostgreSQL.

    ``READ ONLY`` documents and enforces that the caller never writes inside the
    snapshot. Must run BEFORE the transaction's first statement: setting the
    isolation level mid-transaction does not take effect (SQLAlchemy only warns
    that the execution options were ignored), so the caller silently keeps READ
    COMMITTED. A no-op on SQLite, whose single StaticPool connection already reads
    consistently within a transaction and which has no REPEATABLE READ level.
    """
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        session.connection(
            execution_options={
                "isolation_level": "REPEATABLE READ",
                "postgresql_readonly": True,
            }
        )
