"""Guard + stall-observation proofs for Release B's single Alembic runner
(g-b-runner-guard).

This suite owns the GENERIC migration-process boundary: the session-scoped advisory
guard on a dedicated connection, the connection labelling / PID observability, the
narrow named acquisition failure, the fail-safe release, the "Alembic owns the
migration transaction" ordering, and the stall probe's state lifecycle. The
revision-specific data algorithm lives in ``test_release_b_migrations.py`` /
``test_release_b_pg_matrix.py``; this file never re-proves it.

Test harness (why subprocesses + a database-side barrier, not threads):

* Alembic's context proxy is process-global — installing an environment overwrites
  the module-level ``_proxy`` — so two threaded ``command.upgrade()`` calls can route
  operations through each other's environment. And ``command.upgrade()`` re-imports
  the revision module on every run, so monkeypatching an already-loaded revision does
  not affect the run. Both make in-process concurrency and in-process
  monkeypatch-pausing invalid harnesses.
* So each upgrade under a concurrency/pause proof runs in ITS OWN subprocess (its own
  process-global proxy, its own fresh revision import), and pausing is a
  DATABASE-SIDE barrier owned by ``env.py`` (``_migration_test_barrier``), keyed by an
  env var and a no-op in production. The parent holds a distinct advisory lock; the
  child parks in the barrier — after ``run_migrations()``, still inside
  ``begin_transaction()``, still holding the guard — until the parent releases it.

The guard module itself (``app.migration_guard``) is a stable ``sys.modules`` object,
NOT re-imported per run, so ``ConcurrentMigrationError`` / ``migration_stall_probe``
keep the SAME identity across the many ``command.upgrade()`` calls a test process
makes — which is what lets the unit tests below inspect the probe and assert the
error type directly.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time
import uuid

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

import pg_gate_plugin
from app.database_url import _normalize_postgres_scheme
from app.migration_guard import (
    GUARD_APP_NAME,
    MIGRATION_APP_NAME,
    MIGRATION_LOCK_CLASSID,
    MIGRATION_LOCK_OBJID,
    MIGRATION_LOCK_TIMEOUT_S,
    MIGRATION_TEST_BARRIER_ENV,
    RUNNER_APP_NAME,
    ConcurrentMigrationError,
    _acquire_migration_guard,
    _log_backend_pid,
    _release_migration_guard,
    migration_stall_probe,
)
from test_release_b_migrations import (
    BROKEN_PLIES,
    BROKEN_UNGUARDED_ACCURACY,
    INTACT_ACCURACY,
    INTACT_PLIES,
    PREVIOUS_HEAD,
    REVISION,
    _build_pre_b,
    _seed_fixture,
    _seed_session,
)
from test_release_b_pg_matrix import _seed_pg_session

_BACKEND_DIR = pathlib.Path(__file__).resolve().parent
_REPO_VENV_PYTHON = _BACKEND_DIR / ".venv" / "bin" / "python"
# Prefer the repo venv when present (local dev); otherwise the interpreter running
# pytest, which on CI is where the deps were installed. Mirrors the capture E2E tests.
_VENV_PYTHON = _REPO_VENV_PYTHON if _REPO_VENV_PYTHON.exists() else pathlib.Path(sys.executable)


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return cfg


# The REAL head of the chain (a later revision descends from 20260719_01). The
# subprocesses always `upgrade head`, so the durable stamp is this, not REVISION.
_HEAD = ScriptDirectory.from_config(_alembic_config()).get_heads()[0]

# A barrier key DISTINCT from the guard key (different classid), so the parent's hold
# on it cannot be confused with, or contend against, the guard advisory lock.
_BARRIER_CLASSID = 987_654_321
_BARRIER_OBJID = 1
_BARRIER_KEY = f"{_BARRIER_CLASSID},{_BARRIER_OBJID}"


# ---------------------------------------------------------------------------
# Subprocess + polling helpers.
# ---------------------------------------------------------------------------

# Runs a single `alembic upgrade <target>` in a fresh interpreter (own proxy, own
# revision import). cwd is the backend dir so alembic.ini / script_location resolve;
# env.py appends the backend dir to sys.path itself so `app` imports.
_CHILD_PROGRAM = (
    "import sys\n"
    "from alembic import command\n"
    "from alembic.config import Config\n"
    "cfg = Config('alembic.ini')\n"
    "cfg.set_main_option('script_location', 'alembic')\n"
    "command.upgrade(cfg, sys.argv[1] if len(sys.argv) > 1 else 'head')\n"
)


def _spawn_upgrade(url, *, barrier_key=None, target="head", extra_env=None):
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    if barrier_key:
        env[MIGRATION_TEST_BARRIER_ENV] = barrier_key
    else:
        env.pop(MIGRATION_TEST_BARRIER_ENV, None)
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [str(_VENV_PYTHON), "-c", _CHILD_PROGRAM, target],
        cwd=str(_BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _poll_until(predicate, *, eng, children=(), timeout=120.0, interval=0.1):
    """Poll ``predicate(conn)`` (fresh connection each call) until truthy.

    Fails fast — with the child's stderr — if any watched child exits before the
    condition holds, so a crashed migration surfaces as its traceback rather than a
    bare timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with eng.connect() as conn:
            result = predicate(conn)
        if result:
            return result
        for child in children:
            if child.poll() is not None:
                out, err = child.communicate()
                raise AssertionError(
                    f"migration child exited early rc={child.returncode}\n"
                    f"STDOUT:\n{out}\nSTDERR:\n{err}"
                )
        time.sleep(interval)
    raise AssertionError("timed out waiting for migration condition")


def _guard_lock_rows(conn):
    """Every advisory-lock row on the frozen guard key, with its objsubid/granted.

    ``objsubid`` distinguishes the two-key form (2) from the one-argument per-user
    graph lock (1); ``classid``/``objid`` are compared as bigints because pg_locks
    stores them as ``oid``.
    """
    return conn.execute(
        text(
            "SELECT objsubid, granted FROM pg_locks WHERE locktype = 'advisory' "
            "AND classid::bigint = :c AND objid::bigint = :o"
        ).bindparams(c=MIGRATION_LOCK_CLASSID, o=MIGRATION_LOCK_OBJID)
    ).all()


def _engine_for(url):
    return create_engine(_normalize_postgres_scheme(url))


def _hold_barrier(eng):
    """Open a connection, take the SESSION-scoped barrier advisory lock, and COMMIT.

    The commit is load-bearing: pg_advisory_lock autobegins a transaction, and a
    barrier connection left idle-IN-TRANSACTION makes a child's CREATE INDEX
    CONCURRENTLY (20260709_02) wait on it indefinitely (CONCURRENTLY waits for every
    open transaction that could see the table). A SESSION-scoped advisory lock
    survives the commit, so the barrier still holds while the connection sits idle
    but NOT in a transaction.
    """
    barrier = eng.connect()
    barrier.execute(
        text("SELECT pg_advisory_lock(:c, :o)").bindparams(
            c=_BARRIER_CLASSID, o=_BARRIER_OBJID
        )
    )
    barrier.commit()
    return barrier


def _release_barrier(barrier):
    barrier.execute(
        text("SELECT pg_advisory_unlock(:c, :o)").bindparams(
            c=_BARRIER_CLASSID, o=_BARRIER_OBJID
        )
    )
    barrier.commit()


# ===========================================================================
# 1. Whole-chain hold across the autocommit block (from base, in a subprocess).
# ===========================================================================


@pg_gate_plugin.pg_gate
def test_pg_guard_held_across_the_whole_chain_from_base(pg_migration_db):
    """A session-scoped guard survives 20260709_02's autocommit-block commit.

    Run `upgrade head` from base parked at the barrier (past the autocommit block).
    SYNCHRONIZE on confirmed acquisition — the guard granted AND
    ``idx_rating_history_user_chain`` present with ``indisvalid`` from a second
    connection, the only committed cross-connection proof that the autocommit block
    ran. (The 20260709_02 stamp is deliberately NOT the barrier: it stays in the
    still-open outer transaction and is never visible to another connection during
    the paused run.) A transaction-scoped lock would already be gone here; the
    session-scoped guard is still held, in the two-key form (objsubid = 2).
    """
    url = pg_migration_db
    eng = _engine_for(url)
    barrier = _hold_barrier(eng)
    child = _spawn_upgrade(url, barrier_key=_BARRIER_KEY, target="head")
    try:
        def _parked(conn):
            granted = any(g for _, g in _guard_lock_rows(conn))
            idx_valid = conn.execute(
                text(
                    "SELECT i.indisvalid FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname = 'idx_rating_history_user_chain'"
                )
            ).scalar()
            return granted and idx_valid is True

        _poll_until(_parked, eng=eng, children=(child,))

        # The guard is held throughout the post-autocommit window, in the two-key
        # form. objsubid = 2 is the load-bearing assertion: a one-argument per-user
        # graph lock would be objsubid = 1, a different lock space.
        with eng.connect() as conn:
            rows = _guard_lock_rows(conn)
        assert any(objsubid == 2 and granted for objsubid, granted in rows), rows
        assert all(objsubid == 2 for objsubid, _ in rows), rows

        # The guard connection is labelled and observable while it holds the lock.
        with eng.connect() as conn:
            assert conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name = :n"
                ).bindparams(n=GUARD_APP_NAME)
            ).scalar() >= 1
    finally:
        _release_barrier(barrier)
        barrier.close()

    out, err = child.communicate(timeout=120)
    assert child.returncode == 0, f"child failed rc={child.returncode}\nSTDERR:\n{err}"
    with eng.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == _HEAD
    eng.dispose()


# ===========================================================================
# 2. Seeded concurrent backfill (20260709_02 -> head), two separate processes.
# ===========================================================================

# One user_id for every Test-2 seed, so the audit trigger can scope to exactly these
# rows and still count EVERY firing (no value-change condition).
_T2_USER_ID = 930_002
_AUDIT_SCHEMA = "_gr_test_audit"


def _install_audit_counters(eng) -> None:
    """Regular (NOT temp) schema-qualified audit relations + triggers.

    A TEMP table is session-local and invisible to the migration subprocesses' own
    sessions — which are the sessions whose triggers do the inserts — so the writes
    would fail on a missing relation. Regular committed relations are seen by every
    subprocess session. Both triggers count EVERY firing with NO value-change
    condition: a same-value idempotent rewrite (a second application) MUST still be
    counted, which is the entire point of the audit.
    """
    head = _HEAD.replace("'", "''")
    with eng.begin() as conn:
        conn.exec_driver_sql(f"CREATE SCHEMA {_AUDIT_SCHEMA}")
        conn.exec_driver_sql(
            f"CREATE TABLE {_AUDIT_SCHEMA}.accuracy_writes "
            f"(id serial PRIMARY KEY, session_id uuid)"
        )
        conn.exec_driver_sql(
            f"CREATE TABLE {_AUDIT_SCHEMA}.version_writes "
            f"(id serial PRIMARY KEY, version text)"
        )
        conn.exec_driver_sql(
            f"CREATE FUNCTION {_AUDIT_SCHEMA}.on_accuracy_write() RETURNS trigger AS $$ "
            f"BEGIN INSERT INTO {_AUDIT_SCHEMA}.accuracy_writes(session_id) VALUES (NEW.id); "
            f"RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        conn.exec_driver_sql(
            f"CREATE FUNCTION {_AUDIT_SCHEMA}.on_version_write() RETURNS trigger AS $$ "
            f"BEGIN INSERT INTO {_AUDIT_SCHEMA}.version_writes(version) VALUES (NEW.version_num); "
            f"RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        # AFTER UPDATE OF <cols> fires whenever those columns are in the UPDATE's
        # target list, changed or not — so every write attempt is counted. Scoped to
        # the seeded rows by user_id.
        conn.exec_driver_sql(
            f"CREATE TRIGGER _gr_test_accuracy_audit "
            f"AFTER UPDATE OF player_accuracy, player_accuracy_algo_version ON game_sessions "
            f"FOR EACH ROW WHEN (NEW.user_id = {_T2_USER_ID}) "
            f"EXECUTE FUNCTION {_AUDIT_SCHEMA}.on_accuracy_write()"
        )
        # Every event writing the HEAD value, INSERT or UPDATE, without a
        # value-changed condition (a re-stamp writing the same head must still count).
        conn.exec_driver_sql(
            f"CREATE TRIGGER _gr_test_version_audit "
            f"AFTER INSERT OR UPDATE ON alembic_version "
            f"FOR EACH ROW WHEN (NEW.version_num = '{head}') "
            f"EXECUTE FUNCTION {_AUDIT_SCHEMA}.on_version_write()"
        )


def _drop_audit_counters(eng) -> None:
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "DROP TRIGGER IF EXISTS _gr_test_accuracy_audit ON game_sessions"
        )
        conn.exec_driver_sql(
            "DROP TRIGGER IF EXISTS _gr_test_version_audit ON alembic_version"
        )
        conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {_AUDIT_SCHEMA} CASCADE")


@pg_gate_plugin.pg_gate
def test_pg_seeded_concurrent_backfill_is_applied_exactly_once(pg_migration_db):
    """Two subprocess upgrades serialize on the guard; the work is applied once.

    Upgrade to 20260709_02, seed a backfill population (intact/unstamped) and a
    repair population (broken/stamped), and an out-of-population clean-stamped row.
    Two subprocesses race `upgrade head` with the barrier set: one wins the guard and
    parks at the barrier (work done, uncommitted); the other WAITS (granted=false) on
    the same key — serialization, observed. Release the barrier; the winner commits,
    the loser then acquires, reads head and no-ops. Both exit zero. "Exactly once" is
    proven by AUDIT COUNTERS: correct final values plus one alembic_version row cannot
    distinguish one application from two idempotent ones — the counters can.
    """
    url = pg_migration_db
    cfg = _alembic_config()
    prior = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(cfg, "20260709_02")
    finally:
        if prior is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior

    eng = _engine_for(url)
    backfill_ids = [uuid.uuid4() for _ in range(2)]
    repair_id = uuid.uuid4()
    clean_stamped_id = uuid.uuid4()
    with eng.begin() as conn:
        for sid in backfill_ids:
            _seed_pg_session(
                conn, sid, status="ended", mode="normal", drill_state=None,
                version=None, plies=INTACT_PLIES, user_id=_T2_USER_ID,
            )
        # Broken grid, already stamped by the unguarded Release-A hook: only the
        # REPAIR reaches it, nulling the served value.
        _seed_pg_session(
            conn, repair_id, status="ended", mode="normal", drill_state=None,
            version=1, accuracy=BROKEN_UNGUARDED_ACCURACY, plies=BROKEN_PLIES,
            user_id=_T2_USER_ID,
        )
        # Clean grid, already stamped: out of BOTH populations — must never be written.
        _seed_pg_session(
            conn, clean_stamped_id, status="ended", mode="normal", drill_state=None,
            version=1, accuracy=42, plies=INTACT_PLIES, user_id=_T2_USER_ID,
        )

    _install_audit_counters(eng)
    # Hold the barrier BEFORE spawning, so the winner parks instead of racing through.
    barrier = _hold_barrier(eng)
    child_a = _spawn_upgrade(url, barrier_key=_BARRIER_KEY, target="head")
    child_b = _spawn_upgrade(url, barrier_key=_BARRIER_KEY, target="head")
    barrier_released = False
    try:
        # Serialization, observed: exactly one guard row granted, one waiting.
        def _one_granted_one_waiting(conn):
            rows = _guard_lock_rows(conn)
            grants = sorted(g for _, g in rows)
            return grants == [False, True]

        _poll_until(_one_granted_one_waiting, eng=eng, children=(child_a, child_b))

        _release_barrier(barrier)
        barrier_released = True
    finally:
        if not barrier_released:
            _release_barrier(barrier)
        barrier.close()

    out_a, err_a = child_a.communicate(timeout=180)
    out_b, err_b = child_b.communicate(timeout=180)
    assert child_a.returncode == 0, f"child A failed\nSTDERR:\n{err_a}"
    assert child_b.returncode == 0, f"child B failed\nSTDERR:\n{err_b}"

    try:
        with eng.connect() as conn:
            # Correct final values.
            for sid in backfill_ids:
                assert conn.execute(
                    text(
                        "SELECT player_accuracy, player_accuracy_algo_version "
                        "FROM game_sessions WHERE id = CAST(:i AS uuid)"
                    ).bindparams(i=str(sid))
                ).one() == (INTACT_ACCURACY, 1)
            assert conn.execute(
                text(
                    "SELECT player_accuracy, player_accuracy_algo_version "
                    "FROM game_sessions WHERE id = CAST(:i AS uuid)"
                ).bindparams(i=str(repair_id))
            ).one() == (None, 1)
            assert conn.execute(
                text(
                    "SELECT player_accuracy, player_accuracy_algo_version "
                    "FROM game_sessions WHERE id = CAST(:i AS uuid)"
                ).bindparams(i=str(clean_stamped_id))
            ).one() == (42, 1)

            # Exactly once: one accuracy write per in-population row (2 backfill + 1
            # repair = 3), zero for the out-of-population clean-stamped row; and the
            # head stamp transition happened exactly once.
            assert conn.execute(
                text(f"SELECT count(*) FROM {_AUDIT_SCHEMA}.accuracy_writes")
            ).scalar() == 3
            assert conn.execute(
                text(
                    f"SELECT count(*) FROM {_AUDIT_SCHEMA}.accuracy_writes "
                    f"WHERE session_id = CAST(:i AS uuid)"
                ).bindparams(i=str(clean_stamped_id))
            ).scalar() == 0
            assert conn.execute(
                text(f"SELECT count(*) FROM {_AUDIT_SCHEMA}.version_writes")
            ).scalar() == 1
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == _HEAD
    finally:
        _drop_audit_counters(eng)
        eng.dispose()


# ===========================================================================
# 3. Acquisition timeout control (+ companion narrow-translation unit test).
# ===========================================================================


@pg_gate_plugin.pg_gate
def test_pg_acquisition_timeout_raises_concurrent_migration_error(pg_migration_db, monkeypatch):
    """Hold the guard from an independent session; a 1s-timeout upgrade names the cause.

    The timeout is lowered through the Config.attributes seam
    (``migration_lock_timeout_s`` = 1), NOT by monkeypatching the module constant a
    freshly-executed env.py would ignore. The run never reaches the work.
    """
    url = pg_migration_db
    eng = _engine_for(url)
    holder = eng.connect()
    holder.execute(
        text("SELECT pg_advisory_lock(:c, :o)").bindparams(
            c=MIGRATION_LOCK_CLASSID, o=MIGRATION_LOCK_OBJID
        )
    )
    holder.commit()  # session-scoped: the lock outlives this commit
    try:
        monkeypatch.setenv("DATABASE_URL", url)
        cfg = _alembic_config()
        cfg.attributes["migration_lock_timeout_s"] = 1
        with pytest.raises(ConcurrentMigrationError) as excinfo:
            command.upgrade(cfg, "head")
        message = str(excinfo.value)
        assert "migration guard advisory lock" in message
        assert "concurrent migration" in message
        # Nothing was applied: acquisition failed before the migration connection.
        with eng.connect() as conn:
            assert conn.execute(
                text("SELECT to_regclass('public.session_upload_receipt')")
            ).scalar() is None
    finally:
        holder.execute(
            text("SELECT pg_advisory_unlock(:c, :o)").bindparams(
                c=MIGRATION_LOCK_CLASSID, o=MIGRATION_LOCK_OBJID
            )
        )
        holder.close()
        eng.dispose()


class _FakeOrig:
    """DBAPI-original stand-in carrying an arbitrary SQLSTATE."""

    def __init__(self, sqlstate):
        self.sqlstate = sqlstate


class _RaisingConn:
    """Minimal connection whose advisory-lock execute raises a chosen OperationalError.

    Everything up to the lock acquisition (label, pid, lock_timeout) succeeds; the
    ``pg_advisory_lock`` execute raises, exercising ONLY the translation branch.
    """

    def __init__(self, exc):
        self._exc = exc
        self.rolled_back = False
        self.closed = False

    def execute(self, clause):
        if "pg_advisory_lock" in str(clause):
            raise self._exc
        return _ScalarResult()

    def rollback(self):
        self.rolled_back = True

    def invalidate(self):
        pass

    def close(self):
        self.closed = True


class _ScalarResult:
    def scalar(self):
        return 4242


class _FakeEngine:
    def __init__(self, conn):
        self._conn = conn
        self.dialect = type("D", (), {"name": "postgresql"})()

    def connect(self):
        return self._conn


def test_acquire_guard_translates_only_lock_timeout_sqlstate():
    """55P03 becomes ConcurrentMigrationError; any other OperationalError propagates
    UNCHANGED — reporting a dropped connection as a held lock points at a phantom
    cause. Uses the importable guard directly (the module is not re-executed)."""
    timeout_exc = OperationalError("stmt", {}, _FakeOrig("55P03"))
    with pytest.raises(ConcurrentMigrationError):
        _acquire_migration_guard(_FakeEngine(_RaisingConn(timeout_exc)))

    for sqlstate in ("57P01", "08006", None):  # admin shutdown, disconnect, no code
        other = OperationalError("stmt", {}, _FakeOrig(sqlstate))
        conn = _RaisingConn(other)
        with pytest.raises(OperationalError) as excinfo:
            _acquire_migration_guard(_FakeEngine(conn))
        assert not isinstance(excinfo.value, ConcurrentMigrationError)
        # The broken connection is cleaned up (rolled back and closed) without
        # masking the propagating error.
        assert conn.rolled_back and conn.closed


def test_acquire_guard_is_a_noop_off_postgresql():
    engine = type("E", (), {"dialect": type("D", (), {"name": "sqlite"})()})()
    assert _acquire_migration_guard(engine) is None


class _BoolResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _RecordingGuard:
    """Guard connection stand-in that records call order and can fail any step."""

    def __init__(
        self, *, unlock_result=True, unlock_raises=False, commit_raises=False,
        invalidate_raises=False, close_raises=False,
    ):
        self.unlock_result = unlock_result
        self.unlock_raises = unlock_raises
        self.commit_raises = commit_raises
        self.invalidate_raises = invalidate_raises
        self.close_raises = close_raises
        self.calls: list[str] = []

    def execute(self, clause):
        self.calls.append("execute")
        if self.unlock_raises:
            raise OperationalError("stmt", {}, _FakeOrig("08006"))
        return _BoolResult(self.unlock_result)

    def commit(self):
        self.calls.append("commit")
        if self.commit_raises:
            raise OperationalError("commit", {}, _FakeOrig("08006"))

    def invalidate(self):
        self.calls.append("invalidate")
        if self.invalidate_raises:
            raise RuntimeError("invalidate boom")

    def close(self):
        self.calls.append("close")
        if self.close_raises:
            raise RuntimeError("close boom")


def test_release_guard_none_is_a_noop():
    _release_migration_guard(None)  # must not raise


def test_release_guard_clean_unlock_closes_without_invalidate():
    guard = _RecordingGuard(unlock_result=True)
    _release_migration_guard(guard)
    assert "invalidate" not in guard.calls
    assert guard.calls[-1] == "close"


@pytest.mark.parametrize(
    "kwargs,expect_invalidate",
    [
        # unlock returned false -> fall back to invalidate.
        (dict(unlock_result=False), True),
        # unlock raised -> fall back to invalidate.
        (dict(unlock_raises=True), True),
        # unlock returned true but the COMMIT raised -> still fall back to invalidate.
        (dict(unlock_result=True, commit_raises=True), True),
        # the invalidate fallback ITSELF raises -> still swallowed, close still runs.
        (dict(unlock_raises=True, invalidate_raises=True), True),
        # everything downstream raises -> release still never raises.
        (dict(unlock_result=False, invalidate_raises=True, close_raises=True), True),
        # clean unlock but close raises -> swallowed, no invalidate.
        (dict(unlock_result=True, close_raises=True), False),
    ],
)
def test_release_guard_never_raises_and_falls_back_to_invalidate(kwargs, expect_invalidate):
    """Release runs from env.py's outer finally, so a cleanup failure must never
    replace the migration result. Every failure mode here is swallowed, close is
    always attempted last, and invalidate is the fallback whenever the unlock did not
    cleanly return true."""
    guard = _RecordingGuard(**kwargs)
    _release_migration_guard(guard)  # never raises
    assert "close" in guard.calls
    assert ("invalidate" in guard.calls) is expect_invalidate


def test_log_backend_pid_uses_the_passed_application_name(caplog):
    """The shared PID logger reports the NAME IT WAS GIVEN, not a hardcoded constant —
    so g-b-runtime-envelope's runner call logs ghostreplay_accuracy_backfill, not
    ghostreplay_alembic_migration."""

    class _PidConn:
        dialect = type("D", (), {"name": "postgresql"})()

        def execute(self, clause):
            assert "pg_backend_pid" in str(clause)
            return _ScalarResult()  # pid 4242

    with caplog.at_level("INFO", logger="alembic.runtime.migration"):
        _log_backend_pid(_PidConn(), MIGRATION_APP_NAME)
        _log_backend_pid(_PidConn(), RUNNER_APP_NAME)
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        f"application_name={MIGRATION_APP_NAME}" in m and "pid=4242" in m for m in messages
    ), messages
    assert any(f"application_name={RUNNER_APP_NAME}" in m for m in messages), messages


def test_log_backend_pid_is_a_noop_off_postgresql():
    class _SqliteConn:
        dialect = type("D", (), {"name": "sqlite"})()

        def execute(self, clause):
            raise AssertionError("must not query the connection off PostgreSQL")

    _log_backend_pid(_SqliteConn(), MIGRATION_APP_NAME)  # no query, no raise


# ===========================================================================
# 4. Release on failure.
# ===========================================================================


@pg_gate_plugin.pg_gate
def test_pg_guard_is_released_when_the_migration_fails(pg_migration_db, monkeypatch):
    """Force a NATURAL post-acquisition failure (invalid backfill mode, no monkeypatch
    of a re-executed module) and prove the finally release ran: the advisory lock is
    absent from pg_locks afterward."""
    url = pg_migration_db
    monkeypatch.setenv("DATABASE_URL", url)
    # Resolved inside the revision's upgrade(), AFTER the guard is taken in env.py.
    monkeypatch.setenv("GHOSTREPLAY_ACCURACY_BACKFILL_MODE", "not-a-real-mode")
    cfg = _alembic_config()
    with pytest.raises(RuntimeError):
        command.upgrade(cfg, "head")

    eng = _engine_for(url)
    with eng.connect() as conn:
        assert _guard_lock_rows(conn) == []
    eng.dispose()


# ===========================================================================
# 5. Alembic owns the migration transaction.
# ===========================================================================


@pg_gate_plugin.pg_gate
def test_pg_alembic_owns_the_migration_transaction(pg_migration_db, monkeypatch):
    """Nothing autobegins before configure(), and the run is durable.

    A configure() spy asserts ``connection.in_transaction()`` is False at configure
    time. After a successful upgrade the revision's rows AND the alembic_version stamp
    are visible from a FRESH connection — the durability assertion that fails if an
    edit executes anything on the migration connection before configure() (which would
    make begin_transaction() a no-op and silently roll the run back at close under
    NullPool).
    """
    import alembic.context as alembic_context

    url = pg_migration_db
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()
    command.upgrade(cfg, PREVIOUS_HEAD)

    eng = _engine_for(url)
    sid = uuid.uuid4()
    with eng.begin() as conn:
        _seed_pg_session(
            conn, sid, status="ended", mode="normal", drill_state=None,
            version=None, plies=INTACT_PLIES, user_id=940_001,
        )

    seen: dict[str, bool] = {}
    original_configure = alembic_context.configure

    def _spy_configure(*args, **kwargs):
        connection = kwargs.get("connection")
        if connection is not None:
            seen["in_transaction_at_configure"] = connection.in_transaction()
        return original_configure(*args, **kwargs)

    monkeypatch.setattr(alembic_context, "configure", _spy_configure)
    command.upgrade(cfg, REVISION)

    assert seen.get("in_transaction_at_configure") is False

    with eng.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == REVISION
        assert conn.execute(
            text(
                "SELECT player_accuracy, player_accuracy_algo_version "
                "FROM game_sessions WHERE id = CAST(:i AS uuid)"
            ).bindparams(i=str(sid))
        ).one() == (INTACT_ACCURACY, 1)
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_migration_application_name_is_visible_in_flight(pg_migration_db):
    """The migration backend's application_name is visible in pg_stat_activity WHILE
    the run is in flight — a session-level SET takes effect at execution, not at
    commit — which is what the cancellation probe depends on. Proven by parking the
    run at the barrier and observing from the parent."""
    url = pg_migration_db
    eng = _engine_for(url)
    barrier = _hold_barrier(eng)
    child = _spawn_upgrade(url, barrier_key=_BARRIER_KEY, target="head")
    try:
        def _migration_labelled(conn):
            return conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name = :n"
                ).bindparams(n=MIGRATION_APP_NAME)
            ).scalar() >= 1

        _poll_until(_migration_labelled, eng=eng, children=(child,))
    finally:
        _release_barrier(barrier)
        barrier.close()

    out, err = child.communicate(timeout=120)
    assert child.returncode == 0, f"child failed\nSTDERR:\n{err}"
    with eng.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == _HEAD
    eng.dispose()


# ===========================================================================
# 6. Stall-probe lifecycle regression (unit + env.py wiring, no PostgreSQL).
# ===========================================================================


def test_stall_probe_is_first_lock_wins_and_consume_and_clear(monkeypatch):
    migration_stall_probe.reset()
    try:
        # first-lock-wins: a later record does not move the timestamp.
        migration_stall_probe.record_first_row_lock(100.0)
        migration_stall_probe.record_first_row_lock(200.0)
        assert migration_stall_probe._first_row_lock_at == 100.0

        # consume-and-clear: report() reads-and-clears, so a second report is silent.
        migration_stall_probe.report()
        assert migration_stall_probe._first_row_lock_at is None
        migration_stall_probe.report()  # no state -> no raise, nothing logged

        # never raises, even if the clock explodes AFTER the state was consumed.
        migration_stall_probe.record_first_row_lock(300.0)

        class _BoomClock:
            @staticmethod
            def monotonic():
                raise RuntimeError("clock unavailable")

        monkeypatch.setattr("app.migration_guard.time", _BoomClock)
        migration_stall_probe.report()  # swallowed
        assert migration_stall_probe._first_row_lock_at is None
    finally:
        migration_stall_probe.reset()


def _seed_failing_sqlite_db(tmp_path, name):
    """A pre-B SQLite db whose coverage assertion fails, so run_migrations raises."""
    url = f"sqlite:///{tmp_path / name}"
    _build_pre_b(url)
    _seed_fixture(url)
    eng = create_engine(url)
    with eng.begin() as conn:
        # Stamped at a version the backfill predicate cannot reach but the coverage
        # assertion counts -> a real run raises phase=assert coverage.
        _seed_session(conn, "s-future-version", plies=INTACT_PLIES, accuracy=50, version=2)
    eng.dispose()
    return url


def test_stall_probe_env_wiring_no_stale_leak_across_sequential_runs(tmp_path, monkeypatch):
    """The env.py inner-finally wiring: report() fires on FAILURE too, and a failed
    recorded run cannot leak a stale measurement into a later no-lock run.

    Driven through real SQLite ``command.upgrade`` calls, which exercise env.py's
    ``migration_stall_probe.report()`` placement in the inner finally (SQLite skips the
    guard/label/barrier, but report() still runs). ``migration_stall_probe`` is the
    stable shared instance env.py imports, so its state is observable here.
    """
    migration_stall_probe.reset()

    reports: list[str] = []
    real_report = migration_stall_probe.report

    def _spy_report():
        reports.append("fired")
        return real_report()

    monkeypatch.setattr(migration_stall_probe, "report", _spy_report)

    # --- Run 1: records a first-row-lock timestamp, then FAILS. ---
    failing_url = _seed_failing_sqlite_db(tmp_path, "stall_fail.db")
    monkeypatch.setenv("DATABASE_URL", failing_url)
    cfg = _alembic_config()
    command.stamp(cfg, PREVIOUS_HEAD)  # (also drives env.py; ignore its report())
    # Simulate the revision having recorded a first row lock during the run: the real
    # record() call site is the revision's, but here we only need state present when
    # the inner finally fires on the failing run.
    migration_stall_probe.record_first_row_lock(time.monotonic())
    reports.clear()  # count only the measured upgrade's report()
    with pytest.raises(RuntimeError, match="phase=assert coverage"):
        command.upgrade(cfg, REVISION)
    # report() fired FROM THE INNER FINALLY on failure, and consumed the timestamp.
    assert reports == ["fired"]
    assert migration_stall_probe._first_row_lock_at is None

    # --- Run 2: a clean no-lock run. report() fires again and observes nothing. ---
    clean_url = f"sqlite:///{tmp_path / 'stall_clean.db'}"
    _build_pre_b(clean_url)
    _seed_fixture(clean_url)
    monkeypatch.setenv("DATABASE_URL", clean_url)
    cfg2 = _alembic_config()
    command.stamp(cfg2, PREVIOUS_HEAD)
    reports.clear()  # count only the measured upgrade's report()
    command.upgrade(cfg2, REVISION)
    assert reports == ["fired"]  # inner finally fired on success too
    assert migration_stall_probe._first_row_lock_at is None  # no stale leak
    migration_stall_probe.reset()
