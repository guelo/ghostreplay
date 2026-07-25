"""Single-runner Alembic execution boundary (Release B, g-b-runner-guard).

``alembic upgrade head`` opens the migration transaction with no mutual exclusion,
so two overlapping replica starts run two upgrades concurrently. ``SKIP LOCKED``
does not make that safe: it protects individual session rows, not ``alembic_version``
stamping, not ``VALIDATE CONSTRAINT``, and not atomic mode's single transaction.

This module owns the GENERIC migration-process boundary that closes that gap, and
lives OUTSIDE ``env.py``'s namespace on purpose. Alembic loads ``env.py`` and every
revision by executing the file fresh on each run (``spec.loader.exec_module``), so
anything a test monkeypatches in the *loaded* ``env.py`` is discarded the next time
``command.upgrade()`` runs. ``app.migration_guard`` is a normal package module
resolved through ``sys.modules`` — imported once, its object stable across the many
``command.upgrade()`` calls a test process makes. That stability is what lets the
stall-probe state persist and be asserted, and lets the guard be unit-tested
directly. Runtime overrides therefore travel through ``Config.attributes`` (the
acquisition timeout), NEVER through monkeypatching this re-executed-caller's
namespace.

Three connections exist across a finished Release B run, each labelled
session-scoped via ``set_config('application_name', :v, false)`` so an operator — or
the cancellation probe — can tell them apart:

    Connection       | application_name              | Delivered by
    -----------------|-------------------------------|---------------------
    Guard            | ghostreplay_alembic_guard     | THIS module
    Migration        | ghostreplay_alembic_migration | THIS module (env.py)
    Per-batch runner | ghostreplay_accuracy_backfill | revision 20260719_01

``RUNNER_APP_NAME`` is a frozen shared constant here, alongside the shared
``_label_connection`` / PID-log helpers, so the runner labels itself with the same
string a probe filters on: 20260719_01's per-batch mode opens that connection and
calls both helpers, and this module opens none. Distinct names stay load-bearing —
an ATOMIC run has no runner connection at all, so a probe that knows only the
runner's name finds nothing, and this module guarantees the two names that ALWAYS
exist are observable.

Why the guard guarantee holds:

* **Session scope, not transaction scope.** ``pg_advisory_lock(classid, objid)`` is
  released only by an explicit ``pg_advisory_unlock`` or when the backend session
  ends. No COMMIT/ROLLBACK on any connection releases it — not Alembic's migration
  transaction, not 20260709_02's ``autocommit_block`` (which COMMITS the migration
  transaction mid-chain and would drop a ``pg_advisory_xact_lock``), and not the
  per-batch runner's per-batch commits. The lock spans the whole upgrade process.
* **Separate connection.** The guard never participates in the migration
  transaction, so nothing a revision does to that transaction can touch it.
* **Three-layer release.** Explicit unlock, then ``invalidate()`` if the unlock
  returns false or raises, then close last. Under ``pool.NullPool`` (env.py) close is
  a real DBAPI close, so the backend session ends and PostgreSQL drops every session
  lock — a leaked lock would require the process to survive its own ``finally`` with
  a live socket, and a crashed process closes the socket anyway.
* **Two-key form.** The per-user graph lock uses one-argument
  ``pg_advisory_xact_lock(user_id)``; PostgreSQL keeps one- and two-argument advisory
  locks in separate spaces (``pg_locks.objsubid`` 1 vs 2), so a two-key migration key
  cannot collide with any user_id.

What actually endangers the guard lock: ``pg_cancel_backend()`` cancels the current
query and is INERT against the guard (it runs no query after acquisition, and a
``state = 'active'`` probe filter excludes it). What releases the lock is
``pg_advisory_unlock``, the session ending (close under NullPool, or the process
dying), or ``pg_terminate_backend()``. So the rule: NEVER ``pg_terminate_backend()``
the guard connection — that drops the lock mid-run and admits a second migration.

https://alembic.sqlalchemy.org/en/latest/api/runtime.html
https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS
"""

from __future__ import annotations

import logging
import os
import time

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

logger = logging.getLogger("alembic.runtime.migration")

# Frozen; arbitrary but never reused. Two-key form so it cannot collide with the
# one-argument per-user graph-write lock (app/graph_write_lock.py), which lives in a
# separate advisory-lock space (pg_locks.objsubid = 1 there, 2 here).
MIGRATION_LOCK_CLASSID = 1_734_239_597
MIGRATION_LOCK_OBJID = 1
MIGRATION_LOCK_TIMEOUT_S = 900

GUARD_APP_NAME = "ghostreplay_alembic_guard"        # the guard connection
MIGRATION_APP_NAME = "ghostreplay_alembic_migration"  # the migration connection
# RESERVED here; the per-batch runner connection, its label, its PID log, and its
# tests are delivered by g-b-runtime-envelope. Frozen so the runner labels itself
# with the same string this module reserves.
RUNNER_APP_NAME = "ghostreplay_accuracy_backfill"

# Test-only barrier seam (see _migration_test_barrier). Absent in production.
MIGRATION_TEST_BARRIER_ENV = "GHOSTREPLAY_MIGRATION_TEST_BARRIER_KEY"


class ConcurrentMigrationError(RuntimeError):
    """Guard acquisition exceeded the lock timeout — another alembic upgrade holds
    the migration guard.

    RuntimeError base so ``command.upgrade``-driven tests can assert on
    RuntimeError + message rather than on class identity across Alembic's revision
    re-import.
    """


# ---------------------------------------------------------------------------
# Guard acquisition / release.
# ---------------------------------------------------------------------------


def _acquire_migration_guard(engine, lock_timeout_s=MIGRATION_LOCK_TIMEOUT_S):
    """Take the SESSION-scoped two-key advisory lock on a dedicated guard connection.

    Order is fixed: label application_name (is_local=false) -> fetch and log the
    backend PID -> set a transaction-local lock_timeout -> acquire the lock ->
    commit. Only a lock-timeout (SQLSTATE 55P03) becomes ConcurrentMigrationError;
    every other OperationalError propagates unchanged. Returns the live guard
    connection (PostgreSQL) or None (any other dialect).
    """
    if engine.dialect.name != "postgresql":
        return None
    guard = engine.connect()
    try:
        # 1. Label the guard SESSION (is_local=false). set_config(name, value,
        #    is_local) is the parameter-safe form of SET; PG utility statements do
        #    not accept server-side bind params. Same pattern as
        #    app/graph_write_lock.py. Never `SET LOCAL x = :value`. A session-level
        #    SET survives the commit two steps below.
        guard.execute(
            text("SELECT set_config('application_name', :v, false)").bindparams(
                v=GUARD_APP_NAME
            )
        )
        # 2. Fetch and log the guard backend PID (exact where a name is a filter).
        pid = guard.execute(text("SELECT pg_backend_pid()")).scalar()
        logger.info(
            "migration guard backend pid=%s application_name=%s", pid, GUARD_APP_NAME
        )
        # 3. Transaction-local lock timeout (is_local=true): bounds ONLY the
        #    acquisition wait, then reverts when the acquisition txn commits at step
        #    5. The executes above have already autobegun that txn.
        guard.execute(
            text("SELECT set_config('lock_timeout', :v, true)").bindparams(
                v=f"{lock_timeout_s}s"
            )
        )
        # 4. Acquire the SESSION-scoped two-key advisory lock.
        try:
            guard.execute(
                text("SELECT pg_advisory_lock(:classid, :objid)").bindparams(
                    classid=MIGRATION_LOCK_CLASSID, objid=MIGRATION_LOCK_OBJID
                )
            )
        except OperationalError as exc:
            # Translate ONLY lock_timeout (SQLSTATE 55P03, lock_not_available) into
            # the named concurrent-migration error. Every OTHER operational failure
            # — a disconnect, an admin shutdown, a server crash — must propagate
            # UNCHANGED: reporting a dropped connection as "another migration is
            # holding the lock" points an operator at a cause that is not there.
            # Read the SQLSTATE off the DBAPI original; if the driver did not attach
            # one, it is not the timeout, so re-raise.
            sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
            if sqlstate != "55P03":
                raise
            raise ConcurrentMigrationError(
                f"could not acquire migration guard advisory lock "
                f"({MIGRATION_LOCK_CLASSID},{MIGRATION_LOCK_OBJID}) within "
                f"{lock_timeout_s}s; another alembic upgrade is holding it "
                f"(concurrent migration)"
            ) from exc
        # 5. Commit the acquisition txn; the SESSION lock survives it and
        #    lock_timeout (transaction-local) reverts.
        guard.commit()
    except Exception:
        # Clean up WITHOUT masking the error being raised: rollback AND close on an
        # already-broken connection can each throw, and that secondary exception
        # must not replace ConcurrentMigrationError (or the original disconnect).
        # BOTH cleanup steps are individually guarded; a failed rollback falls
        # through to invalidate (drop the DBAPI connection outright) so a wedged
        # connection cannot be returned or leaked. The in-flight exception is always
        # the one that propagates.
        try:
            guard.rollback()
        except Exception:
            try:
                guard.invalidate()
            except Exception:
                pass
        try:
            guard.close()
        except Exception:
            pass
        raise
    return guard


def _release_migration_guard(guard):
    """Release the guard lock and NEVER raise.

    Called from env.py's OUTER finally, so a cleanup failure here must not replace
    the migration's own result — neither masking the real migration failure nor
    reporting failure after a successful commit. Every step is therefore guarded
    INDIVIDUALLY: explicit unlock (+ commit); on a false return OR any unlock/commit
    failure, fall through to ``invalidate()`` (drop the DBAPI connection so the
    backend session ends and PostgreSQL releases the session lock); and close last.
    ``invalidate()`` and ``close()`` can each throw on an already-broken connection,
    so both are wrapped too — a leaked lock cannot outlast the ended session anyway.
    """
    if guard is None:
        return
    try:
        released = guard.execute(
            text("SELECT pg_advisory_unlock(:classid, :objid)").bindparams(
                classid=MIGRATION_LOCK_CLASSID, objid=MIGRATION_LOCK_OBJID
            )
        ).scalar()
        guard.commit()
    except Exception:
        released = False  # any unlock/commit failure -> invalidate below
    if not released:
        try:
            guard.invalidate()  # kill the backend; PG drops the session lock
        except Exception:
            pass
    try:
        guard.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Shared connection observability (guard + migration; runner reuses these).
# ---------------------------------------------------------------------------


def _label_connection(connection, app_name):
    """Label a connection's SESSION with application_name (PostgreSQL only).

    set_config(..., is_local=false) is a session-level SET: it survives the commits
    the run performs and is visible in pg_stat_activity immediately (a SET takes
    effect at execution, not at commit), which is all the cancellation probe needs.
    A no-op off PostgreSQL (SQLite has no set_config).
    """
    if connection.dialect.name != "postgresql":
        return
    connection.execute(
        text("SELECT set_config('application_name', :v, false)").bindparams(v=app_name)
    )


def _log_backend_pid(connection, app_name):
    """Log a connection's backend PID and application_name at INFO (PostgreSQL only).

    ``app_name`` is a PARAMETER, not a hardcoded constant, precisely because this is
    a SHARED helper: the runtime-envelope runner calls it for RUNNER_APP_NAME, and a
    hardcoded ``MIGRATION_APP_NAME`` would mislabel that backend as the migration
    connection. Pass the SAME name most recently given to ``_label_connection`` so the
    logged name matches the backend's actual ``application_name``. pg_backend_pid() is
    exact where a name is merely a filter — in a test environment where several
    migrations run in separate processes, the PID is what disambiguates them.
    """
    if connection.dialect.name != "postgresql":
        return
    pid = connection.execute(text("SELECT pg_backend_pid()")).scalar()
    logger.info("migration backend pid=%s application_name=%s", pid, app_name)


def _migration_test_barrier(engine):
    """Database-side pause seam, owned by env.py's wrapper, keyed by an env var.

    Absent in production -> no-op, so it costs nothing. When set, take and
    immediately release a distinct advisory lock on a fresh connection: the parent
    test holds that lock, so a running migration parks HERE — after run_migrations()
    but still inside begin_transaction() and still holding the guard — until the
    parent releases it. Deterministic and cross-process: no proxy sharing, no
    monkeypatch, no reliance on the revision runner.
    """
    key = os.environ.get(MIGRATION_TEST_BARRIER_ENV)
    if not key or engine.dialect.name != "postgresql":
        return
    classid, objid = (int(x) for x in key.split(","))
    with engine.connect() as c:  # fresh connection, no lock_timeout
        c.execute(
            text("SELECT pg_advisory_lock(:c, :o)").bindparams(c=classid, o=objid)
        )
        c.execute(
            text("SELECT pg_advisory_unlock(:c, :o)").bindparams(c=classid, o=objid)
        )
        c.commit()


# ---------------------------------------------------------------------------
# Stall probe — atomic-mode row-lock hold observation.
# ---------------------------------------------------------------------------


class _MigrationStallProbe:
    """Measures the atomic-mode row-lock hold: from the FIRST row lock a revision
    takes to the COMMIT/ROLLBACK that releases it.

    Inert unless a revision recorded a first-row-lock timestamp, so every other
    revision, every SQLite run, and the both-populations-zero path pay nothing. The
    record() call site is the revision's (20260719_01's atomic mode, at t_stall_0);
    THIS module owns the probe and its report() call site in env.py. Instance state
    (not module globals) so the object is trivially resettable in unit tests while
    remaining stable across the many command.upgrade() calls a test process makes.

    Why the classification lives here rather than in the revision: atomic mode's lock
    hold ends when Alembic's transaction commits, and that happens when env.py exits
    context.begin_transaction() — AFTER upgrade() has already returned. There is no
    per-batch commit for the revision to observe and no code of the revision's still
    running when the locks are released, so a measurement that stopped at the last
    assertion would be a measurement of the hold MINUS its commit. The revision
    therefore hands over the threshold and the projection at record time and lets
    report() compare them.
    """

    # No instance __dict__, on purpose. ``env.py`` and the revision both hold a
    # DIRECT reference to the module singleton below, so an instance-level
    # attribute would shadow the class for every later caller in the process — and
    # ``monkeypatch.setattr(instance, "report", spy)`` creates exactly that on
    # UNDO: it captures the inherited bound method and writes it back into the
    # instance, permanently. A later test that patches the CLASS then silently
    # never fires, in a different file, only in full-suite order. __slots__ turns
    # that into an immediate AttributeError at the offending call site. Patch the
    # CLASS (``_MigrationStallProbe.report``), never the singleton.
    __slots__ = ("_first_row_lock_at", "_max_stall_ms", "_projected_stall_ms")

    def __init__(self) -> None:
        self._first_row_lock_at: float | None = None  # monotonic seconds
        self._max_stall_ms: float | None = None
        self._projected_stall_ms: float | None = None

    def record_first_row_lock(
        self,
        ts: float,
        *,
        max_stall_ms: float | None = None,
        projected_stall_ms: float | None = None,
    ) -> None:
        """Anchor the measurement, and optionally hand it the numbers to judge by.

        FIRST-LOCK-WINS: only the first row lock in a run sets the timestamp; later
        locks do not overwrite it, so the measured hold spans first lock -> release.
        The two threshold arguments are OPTIONAL on purpose — every other revision
        records only a timestamp and must keep its existing INFO-only behaviour, so
        a bare ``record_first_row_lock(ts)`` never produces an ERROR line. They are
        stored beside the winning timestamp, and only by it: a later call cannot
        retro-fit a threshold onto an earlier anchor.

        A timestamp with nothing to compare against can only ever be logged, which
        is why the classification lives here rather than in the caller — the caller
        is a revision, and the event that ENDS the measurement happens after that
        revision's last line of code has run.
        """
        if self._first_row_lock_at is None:
            self._first_row_lock_at = ts
            self._max_stall_ms = max_stall_ms
            self._projected_stall_ms = projected_stall_ms

    def report(self) -> None:
        """Log the observed hold; classify it when a threshold was supplied. NEVER raises.

        Called from env.py's finally around ``context.begin_transaction()``, which
        runs precisely when COMMIT returns on the success path and ROLLBACK returns
        on the failure path — the moment the row locks are actually released, on
        both paths. Evidence, not enforcement: the event that ends the measurement
        is the same event that releases the lock, so there is nothing left to
        prevent, and raising after a commit that already happened would fail a
        deploy whose data is durable.

        This log line is the ONLY empirical check on the atomic-mode stall
        projection, because atomic mode's hold ends after ``upgrade()`` has already
        returned and no measurement taken inside the revision can include the
        commit. Sizing qualification reads ``observed_atomic_stall_ms`` from it and
        requires it to be at or below both ``projected_stall_ms`` and
        ``max_stall_ms``.
        """
        # CONSUME-AND-CLEAR all three before logging; NEVER raises. Reading and
        # clearing first guarantees the next run in the same process starts clean
        # regardless of what logging does.
        ts, self._first_row_lock_at = self._first_row_lock_at, None
        max_stall_ms, self._max_stall_ms = self._max_stall_ms, None
        projected_ms, self._projected_stall_ms = self._projected_stall_ms, None
        if ts is None:
            return
        try:
            held_ms = (time.monotonic() - ts) * 1000.0
            projected = "n/a" if projected_ms is None else f"{projected_ms:.1f}"
            if max_stall_ms is not None and held_ms > max_stall_ms:
                logger.error(
                    "atomic migration row-lock hold BREACHED the writer-stall bound: "
                    "observed_atomic_stall_ms=%.1f max_stall_ms=%.1f projected_stall_ms=%s",
                    held_ms,
                    max_stall_ms,
                    projected,
                )
                return
            logger.info(
                "atomic migration row-lock hold=%.1fms observed_atomic_stall_ms=%.1f "
                "projected_stall_ms=%s max_stall_ms=%s",
                held_ms,
                held_ms,
                projected,
                "n/a" if max_stall_ms is None else f"{max_stall_ms:.1f}",
            )
        except Exception:
            pass  # observation must never fail the migration

    def reset(self) -> None:
        """Test hook: drop any recorded state without logging."""
        self._first_row_lock_at = None
        self._max_stall_ms = None
        self._projected_stall_ms = None


# The single stable instance imported by env.py (report) and the revision (record).
migration_stall_probe = _MigrationStallProbe()
