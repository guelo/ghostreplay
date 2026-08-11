"""Ordered SQL statement + COMMIT capture, and the cursor-is-last assertions.

The serving write paths that touch opening evidence share one invariant:
``app.opening_cache.bump_evidence_seq`` updates the shared per-(user,color)
``opening_score_cursors`` row with ``INSERT ... ON CONFLICT DO UPDATE`` as the
transaction's final blocking statement before commit, and exactly once. The
row is the transaction's most contended lock; anything appended after its bump
(a write *or* a read) widens that lock window.

``before_cursor_execute`` alone cannot pin this: it never sees the COMMIT, so it
cannot tell "last statement of the transaction" from "last statement observed".
Listening to the engine ``commit`` event too gives the boundary.

Not a ``test_`` module on purpose — pytest must not collect it.
"""

from __future__ import annotations

import contextlib

from sqlalchemy import event

from conftest import engine


class StatementLog:
    """Ordered log of ``(kind, sql)`` where kind is ``"sql"`` or ``"commit"``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def _on_cursor(self, conn, cursor, statement, parameters, context, executemany) -> None:
        self.events.append(("sql", statement.lower()))

    def _on_commit(self, conn) -> None:
        self.events.append(("commit", ""))

    def statements(self) -> list[str]:
        return [sql for kind, sql in self.events if kind == "sql"]

    def statements_before_first_commit(self) -> list[str]:
        pre: list[str] = []
        for kind, sql in self.events:
            if kind == "commit":
                break
            pre.append(sql)
        return pre

    def statements_after_first_commit(self) -> list[str]:
        seen_commit = False
        post: list[str] = []
        for kind, sql in self.events:
            if kind == "commit":
                seen_commit = True
                continue
            if seen_commit:
                post.append(sql)
        return post

    def commit_attempt_count(self) -> int:
        """How many commits were ATTEMPTED — not how many became durable.

        SQLAlchemy's ``_commit_impl`` dispatches the ``commit`` event BEFORE
        calling ``do_commit`` (engine/base.py), so a commit that then raises
        still registers here. SRS review, for one, wraps ``db.commit()`` in
        ``try/except IntegrityError`` and rolls back. Use this only to confirm
        the log was sliced at a real transaction boundary; prove durability by
        re-reading the row from the DB.
        """
        return sum(1 for kind, _ in self.events if kind == "commit")


@contextlib.contextmanager
def capture_statements(target_engine=engine):
    """Record every statement and commit issued on ``target_engine``.

    The listener is attached to the shared engine, so ANY traffic during the
    block lands in the log — build auth headers and commit all setup BEFORE
    entering, or a setup commit will truncate ``statements_before_first_commit``
    to the setup's own statements.
    """
    log = StatementLog()
    event.listen(target_engine, "before_cursor_execute", log._on_cursor)
    event.listen(target_engine, "commit", log._on_commit)
    try:
        yield log
    finally:
        event.remove(target_engine, "before_cursor_execute", log._on_cursor)
        event.remove(target_engine, "commit", log._on_commit)


def is_write(statement: str) -> bool:
    return statement.lstrip().startswith(("insert", "update", "delete"))


def cursor_write_indices(statements: list[str]) -> list[int]:
    """Positions of the evidence-cursor WRITES in ``statements``.

    Filters on ``is_write`` as well as the table name so a future cursor READ
    added to one of these paths is not miscounted as a bump. (No such read
    exists today: the terminal delta path takes ``_delta_items_from_cache``,
    which deliberately skips the freshness check.)
    """
    return [i for i, s in enumerate(statements) if is_write(s) and "opening_score_cursors" in s]


def _assert_no_post_commit_writes(log: StatementLog) -> None:
    """No write of ANY kind ran after the commit the cursor accounts for.

    Post-commit SELECTs are fine and expected (``db.refresh`` + the opening-score
    delta reads). A post-commit WRITE is not: it lands in a transaction the bump
    does not cover, so either it never commits and is silently lost at the
    ``db.close()`` teardown, or it commits and the evidence counter under-counts
    the change. Neither is caught by the cursor-cardinality check when the write
    touches some other table.
    """
    writes = [s for s in log.statements_after_first_commit() if is_write(s)]
    assert not writes, f"writes ran in a second transaction after the commit: {writes}"


def cursor_last_before_commit(log: StatementLog) -> tuple[list[str], int]:
    """The request bumped the evidence cursor exactly once ACROSS THE WHOLE
    REQUEST, and that bump was the FINAL STATEMENT (not merely the final write)
    before the commit. Returns ``(pre, cursor_idx)`` so the caller can order its
    own writes against the bump.

    The two windows differ ON PURPOSE. ORDERING is checked over
    ``statements_before_first_commit()`` — some paths legitimately read after the
    commit (``db.refresh`` + the opening-score delta) and those reads are none of
    the invariant's business. CARDINALITY is checked over ``statements()``, the
    whole request, so a second bump in a later transaction cannot hide in the
    tail.

    Says NOTHING about durability — the commit event is an attempt, not a fact.
    Callers MUST additionally assert the persisted cursor row.
    """
    assert log.commit_attempt_count() == 1, log.events
    _assert_no_post_commit_writes(log)

    all_bumps = cursor_write_indices(log.statements())
    assert len(all_bumps) == 1, (
        f"expected exactly 1 cursor write across the request, got {len(all_bumps)}: {log.statements()}"
    )

    pre = log.statements_before_first_commit()
    idxs = cursor_write_indices(pre)
    assert len(idxs) == 1, f"the cursor bump is not in the committed txn: {pre}"
    assert idxs[0] == len(pre) - 1, f"statements ran after the cursor bump: {pre[idxs[0] + 1:]}"
    return pre, idxs[0]


def no_cursor_bump(log: StatementLog) -> list[str]:
    """The request committed and did NOT bump the cursor ANYWHERE.

    Callers MUST additionally assert the domain write persisted — every endpoint
    here has a successful, write-free early return that would satisfy this
    vacuously.
    """
    assert log.commit_attempt_count() == 1, log.events
    _assert_no_post_commit_writes(log)
    assert cursor_write_indices(log.statements()) == [], log.statements()
    return log.statements_before_first_commit()
