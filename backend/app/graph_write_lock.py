"""Per-user graph-write serialization lock (g-q0aw / g-graph-lock, Postgres only).

The shared ghost graph keys Position rows by the ``(user_id, fen_hash)`` unique
index and Move rows by ``(from_position_id, move_san)``. Several same-user writers
touch those shared rows — the deferred ``/moves`` evidence worker and both
blunder-recording paths (auto + manual). Two of them replaying transposing lines
can insert overlapping positions in *opposite* orders and deadlock on the unique
indexes (Postgres 40P01). Funnelling every shared-graph writer for one user
through ``pg_advisory_xact_lock(user_id)`` removes that class of deadlock: same-user
writers queue behind one lock, different users stay fully independent.

The guarantee is mutual exclusion for one user, NOT FIFO acquisition — callers must
not assume any particular winner when two contend.

The lock is transaction-scoped (the ``_xact_`` in the function name): it releases at
the holding transaction's COMMIT/ROLLBACK with no explicit unlock. ``lock_timeout``
bounds how long acquisition waits on a stuck queue (advisory locks go through the PG
lock manager) and ``statement_timeout`` bounds any single pathological query, so a
degenerate case fails fast (SQLSTATE 55P03 / 57014) instead of hanging ~166s. Both
SET LOCALs reset at that same COMMIT/ROLLBACK, which is exactly the critical section.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

# Tunable by patching these constants in tests to keep the timeout-path tests fast.
GRAPH_LOCK_TIMEOUT = "5s"
GRAPH_STATEMENT_TIMEOUT = "10s"


def acquire_graph_write_lock(
    db: Session, *, user_id: int, dialect_name: str
) -> None:
    """Serialize shared Position/Move graph writes for one user (Postgres only).

    On PostgreSQL: set the txn-local ``lock_timeout``/``statement_timeout`` guardrails
    then take ``pg_advisory_xact_lock(user_id)``. Call this ONCE, before the first
    shared Position/Move write of the transaction, and hold it (do not commit) through
    the graph upserts and any bookkeeping until the caller's single commit. The
    advisory lock and both SET LOCALs release together at that COMMIT/ROLLBACK.

    On every other dialect (SQLite in tests) this is a no-op: those backends do not
    race on a shared advisory lock and never emit the timeout SQLSTATEs.

    Raises ``OperationalError`` (SQLSTATE 55P03 / 57014) if acquisition or a later
    statement times out. Because the lock is acquired before any entity write, a
    timeout here leaves nothing to undo beyond a rollback; the caller owns that
    rollback (and, for the worker only, the retry-once policy).
    """
    if dialect_name != "postgresql":
        return
    # set_config(name, value, is_local=true) is the txn-local form of `SET LOCAL`
    # and accepts a normal bind param — PG utility statements like `SET LOCAL x = :v`
    # are awkward/unsupported with server-side bind params. Both timeouts reset at
    # the caller's commit below.
    db.execute(
        text("SELECT set_config('lock_timeout', :v, true)").bindparams(
            v=GRAPH_LOCK_TIMEOUT
        )
    )
    db.execute(
        text("SELECT set_config('statement_timeout', :v, true)").bindparams(
            v=GRAPH_STATEMENT_TIMEOUT
        )
    )
    db.execute(text("SELECT pg_advisory_xact_lock(:uid)").bindparams(uid=user_id))
