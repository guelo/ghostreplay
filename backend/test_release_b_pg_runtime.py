"""PostgreSQL proofs for Release B's runtime envelope (g-b-runtime-envelope).

Everything here is structurally invisible to the SQLite suite: SQLite has no
``statement_timeout`` and no ``lock_timeout``, no ``FOR NO KEY UPDATE SKIP
LOCKED``, no second writer to stall, no ``pg_stat_activity``/``pg_locks`` to
observe, and no per-batch transactions on an independent connection.

How these tests reach the code, and why
---------------------------------------
Alembic loads every revision by executing the file fresh on each run
(``spec.loader.exec_module``), so a constant monkeypatched on the module object
this file imported is discarded the moment ``command.upgrade()`` re-imports it.
That splits the suite in two, deliberately:

* **Direct-runner tests** construct :class:`mod._Runner` (or call
  :func:`mod._run_phases`) against a real connection on a disposable database.
  This is the REAL runner — the arming, the pass loop, the transaction envelope,
  the tripwire and the watchdog all live there — and it is the only way to pin a
  narrowed budget, a patched estimate, or an injected slow statement.
* **Alembic-driven tests** use ``command.upgrade`` where the property under test
  IS the full path: the ``VALIDATE`` lock-timeout leak (it needs the migration
  connection mid-transaction) and the ``env.py`` stall probe (its measurement ends
  after ``upgrade()`` has returned).

A competing writer is modelled as a second session taking ``FOR NO KEY UPDATE``
and writing — which is exactly what the Release-A ``/moves`` hook does
(``app/row_locks.py:11``, ``app/api/session.py``). Driving the HTTP surface would
add a request stack to a proof about row locks without changing which locks are
taken.
"""

from __future__ import annotations

import contextlib
import pathlib
import re
import threading
import time
import uuid

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

import pg_gate_plugin
from app import migration_guard
from test_release_b_migrations import (
    BROKEN_PLIES,
    BROKEN_UNGUARDED_ACCURACY,
    INTACT_ACCURACY,
    INTACT_PLIES,
    PREVIOUS_HEAD,
    REVISION,
)
from test_release_b_pg_matrix import _seed_pg_session

_BACKEND_DIR = pathlib.Path(__file__).resolve().parent

_MARKER_RE = re.compile(r"/\* (ghostreplay:[a-z_]+) \*/")
_MS_RE = re.compile(r"^(\d+)ms$")

#: Statements armed with ``SCAN_STMT_TIMEOUT_MS`` rather than with the mode's
#: statement cap. Their armed value is ``min(SCAN_STMT_TIMEOUT_MS, every deadline
#: in force)``, so it does NOT track the residual budget while the budget is above
#: the cap — which is why the "armed values only narrow" assertions are made over
#: the budget-bound statements and the scan statements separately.
_SCAN_CAPPED_MARKERS = frozenset({
    "ghostreplay:backfill_remaining",
    "ghostreplay:backfill_population_count",
    "ghostreplay:repair_populate",
    "ghostreplay:repair_clear",
    "ghostreplay:repair_candidates_ddl",
    "ghostreplay:repair_remaining",
    "ghostreplay:repair_population_count",
    "ghostreplay:coverage_assert",
    "ghostreplay:soundness_assert",
    "ghostreplay:dimension_probe",
})


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return cfg


mod = ScriptDirectory.from_config(_alembic_config()).get_revision(REVISION).module


# ---------------------------------------------------------------------------
# Observation.
# ---------------------------------------------------------------------------


class _Trace:
    """Every armed timeout, every marked statement, and every transaction boundary.

    One ORDERED list, because most of what this suite proves is about ORDER: that
    an arm immediately precedes the statement it bounds, that the materialization's
    transaction closes before the batch loop opens one, that the armed values only
    ever narrow. Two separate lists could not express any of that.
    """

    def __init__(self, target) -> None:
        self.target = target
        self.events: list[tuple[str, object]] = []

    def __enter__(self) -> _Trace:
        event.listen(self.target, "before_cursor_execute", self._cursor)
        event.listen(self.target, "commit", self._commit)
        event.listen(self.target, "rollback", self._rollback)
        event.listen(self.target, "begin", self._begin)
        return self

    def __exit__(self, *exc) -> bool:
        event.remove(self.target, "before_cursor_execute", self._cursor)
        event.remove(self.target, "commit", self._commit)
        event.remove(self.target, "rollback", self._rollback)
        event.remove(self.target, "begin", self._begin)
        return False

    # --- listeners ---
    def _cursor(self, conn, cursor, statement, parameters, context, executemany):
        if "set_config('statement_timeout'" in statement:
            self.events.append(("statement_timeout", _ms(parameters)))
        elif "set_config('lock_timeout'" in statement:
            self.events.append(("lock_timeout", _ms(parameters)))
        else:
            marker = _MARKER_RE.match(statement)
            self.events.append(
                ("stmt", marker.group(1) if marker else statement.split("\n")[0][:48])
            )

    def _commit(self, conn):
        self.events.append(("commit", None))

    def _rollback(self, conn):
        self.events.append(("rollback", None))

    def _begin(self, conn):
        self.events.append(("begin", None))

    # --- readers ---
    def of(self, kind: str) -> list:
        return [value for k, value in self.events if k == kind]

    def markers(self) -> list[str]:
        return self.of("stmt")

    def armed_before(self, marker: str) -> list[int]:
        """The ``statement_timeout`` armed immediately before each execution of
        ``marker`` — which is the only arm that bounds it."""
        armed: list[int] = []
        pending: int | None = None
        for kind, value in self.events:
            if kind == "statement_timeout":
                pending = value
            elif kind == "stmt" and value == marker and pending is not None:
                armed.append(pending)
        return armed

    def arms_by_kind(self, start: int = 0) -> tuple[list[int], list[int]]:
        """``(budget_bound, scan_capped)`` armed values, in order.

        A statement's armed value is ``min(its own cap, every deadline in force)``.
        Only the statements whose cap cannot bind — the mode's own cap, set where it
        never binds in atomic mode — read the remaining budget directly, so only
        those can be required to narrow monotonically. The scan-capped statements
        sit at ``SCAN_STMT_TIMEOUT_MS`` for as long as the budget is above it, and
        requiring them to narrow would be requiring the cap not to exist.
        """
        budget_bound: list[int] = []
        scan_capped: list[int] = []
        pending: int | None = None
        for kind, value in self.events[start:]:
            if kind == "statement_timeout":
                pending = value
            elif kind == "stmt" and pending is not None:
                (scan_capped if value in _SCAN_CAPPED_MARKERS else budget_bound).append(pending)
                pending = None
        return budget_bound, scan_capped

    def pairs(self) -> list[tuple[int, int]]:
        """``(statement_timeout, lock_timeout)`` as each arm issued them, in order."""
        out: list[tuple[int, int]] = []
        stmt: int | None = None
        for kind, value in self.events:
            if kind == "statement_timeout":
                stmt = value
            elif kind == "lock_timeout" and stmt is not None:
                out.append((stmt, value))
                stmt = None
        return out


def _ms(parameters) -> int:
    """The armed value out of a ``set_config`` bind, in whole milliseconds."""
    if isinstance(parameters, dict):
        raw = parameters.get("v")
    else:
        raw = parameters[0] if parameters else None
    match = _MS_RE.match(str(raw))
    if match:
        return int(match.group(1))
    # VALIDATE arms lock_timeout as '10s'; every other arm is milliseconds.
    if str(raw).endswith("s"):
        return int(float(str(raw)[:-1]) * 1000)
    raise AssertionError(f"unparseable armed value {raw!r}")


# ---------------------------------------------------------------------------
# Fixtures / helpers.
# ---------------------------------------------------------------------------


def _at_previous_head(url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(_alembic_config(), PREVIOUS_HEAD)
    return create_engine(url)


def _seed_stale(conn, n, *, user_id=950_001, plies=INTACT_PLIES):
    ids = []
    for _ in range(n):
        sid = uuid.uuid4()
        ids.append(sid)
        _seed_pg_session(
            conn, sid, status="ended", mode="normal", drill_state=None,
            version=None, plies=plies, user_id=user_id,
        )
    return ids


def _seed_repair(conn, n, *, user_id=950_002):
    """Broken grid, already stamped version 1 with a non-NULL value.

    The shape the unguarded Release-A hook left: the backfill's population
    predicate skips it, so only the REPAIR reaches it.
    """
    ids = []
    for _ in range(n):
        sid = uuid.uuid4()
        ids.append(sid)
        _seed_pg_session(
            conn, sid, status="ended", mode="normal", drill_state=None,
            version=1, accuracy=BROKEN_UNGUARDED_ACCURACY, plies=BROKEN_PLIES,
            user_id=user_id,
        )
    return ids


def _cached(conn, sid):
    return conn.execute(
        text(
            "SELECT player_accuracy, player_accuracy_algo_version FROM game_sessions "
            "WHERE id = CAST(:i AS uuid)"
        ).bindparams(i=str(sid))
    ).one()


def _run_phase(conn, phase, *, env, batch_size=1_000, bundle=None, clock=None, stall=None):
    """One phase only, on the connection given — no runner-owned connection.

    Used where the test must hold the runner's connection itself (to observe its
    arms, or to keep a batch's locks). ``_run_phases`` is exercised separately by
    the runner-connection lifecycle test.
    """
    clock = clock or mod._RunClock()
    runner = mod._Runner(
        conn,
        bundle or mod.SQL_PG,
        clock=clock,
        env=env,
        batch_size=batch_size,
        stall=stall,
    )
    with mod._ComputeWatchdog() as watchdog:
        runner.run_phase(phase, watchdog)
    return clock


def _sleeping_bundle(seconds: float, *, bundle=None):
    """``load_moves`` with a ``pg_sleep`` in it, marker preserved.

    A slow statement at the SQL layer, so it runs under the same armed
    ``statement_timeout`` the real load does — which is what makes the cancellation
    a proof about the arm rather than about Python.

    An UNCORRELATED scalar subquery, not a bare ``pg_sleep(n)`` in the select list:
    the latter is evaluated once PER ROW, so a two-session batch of six plies each
    would sleep twelve times the requested interval and the test would be measuring
    its own fixture size.
    """
    base = bundle or mod.SQL_PG
    slowed = base.load_moves.replace(
        "    SELECT session_id",
        f"    SELECT (SELECT pg_sleep({seconds})) AS _slow, session_id",
        1,
    )
    return base._replace(load_moves=slowed)


# ---------------------------------------------------------------------------
# 1. The VALIDATE lock timeout does not leak.
# ---------------------------------------------------------------------------


@pg_gate_plugin.pg_gate
def test_pg_validate_lock_timeout_does_not_leak_into_the_row_locks(
    pg_migration_db, monkeypatch
):
    """``set_config(..., true)`` is SET LOCAL — TRANSACTION-scoped, not statement-scoped.

    ``alembic/env.py`` opens exactly ONE transaction around the whole run, so
    without an explicit re-arm every later row lock on the migration connection
    would silently inherit ``VALIDATE_LOCK_TIMEOUT`` ('10s'): a value chosen for a
    DDL lock wait and never reviewed as a row-lock wait. Observing the armed value
    requires watching the MIGRATION connection mid-transaction, which is what the
    spy below does.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)

    # The CHECK is still NOT VALID at PREVIOUS_HEAD, so this is the real transition.
    with eng.connect() as conn, _Trace(conn) as trace:
        trans = conn.begin()
        assert conn.execute(
            text(
                "SELECT convalidated FROM pg_constraint WHERE conname = :n"
            ).bindparams(n=mod.CHECK_NAME)
        ).scalar() is False
        mod._validate_check(conn, mod._RunClock())
        # SET LOCAL survives to the end of the TRANSACTION, so read it back HERE —
        # inside the same transaction Alembic would still be holding.
        assert conn.execute(text("SHOW lock_timeout")).scalar() == "1s"
        assert mod.ATOMIC_LOCK_WAIT_MS == 1_000
        trans.commit()
    # VALIDATE itself got the DDL wait, and only VALIDATE.
    lock_arms = [v for k, v in trace.events if k == "lock_timeout"]
    assert lock_arms[0] == mod.VALIDATE_LOCK_WAIT_MS, lock_arms

    # And pin the CONSEQUENCE from the outside: a guarded row lock against a row a
    # competing writer holds must fail 55P03 at ~ATOMIC_LOCK_WAIT_MS — not after ten
    # seconds, and not never.
    with eng.begin() as conn:
        [sid] = _seed_stale(conn, 1, user_id=950_011)

    holder = eng.connect()
    try:
        holder.execute(
            text("SELECT id FROM game_sessions WHERE id = CAST(:i AS uuid) FOR NO KEY UPDATE")
            .bindparams(i=str(sid))
        )
        with eng.connect() as conn:
            clock = mod._RunClock()
            trans = conn.begin()
            mod._arm(
                conn,
                clock,
                stmt_cap_ms=mod.REVISION_DEADLINE_S * 1000,
                lock_wait_ms=mod.ATOMIC_LOCK_WAIT_MS,
                phase="backfill",
            )
            started = time.monotonic()
            with pytest.raises(Exception) as exc:
                conn.execute(
                    text(
                        "SELECT id FROM game_sessions WHERE id = CAST(:i AS uuid) "
                        "FOR NO KEY UPDATE"
                    ).bindparams(i=str(sid))
                )
            waited_ms = (time.monotonic() - started) * 1000
            trans.rollback()
        assert mod._sqlstate_of(exc.value) == "55P03"
        # At the designed value, not at VALIDATE's ten seconds.
        assert waited_ms < mod.VALIDATE_LOCK_WAIT_MS
        assert waited_ms >= mod.ATOMIC_LOCK_WAIT_MS * 0.5
    finally:
        holder.rollback()
        holder.close()
        eng.dispose()


# ---------------------------------------------------------------------------
# 2. Budget arming.
# ---------------------------------------------------------------------------


@pg_gate_plugin.pg_gate
def test_pg_per_batch_arming_is_non_increasing_and_never_a_fresh_batch_allowance(
    pg_migration_db, monkeypatch
):
    """The direct proof that the guarded UPDATE does not receive a fresh MAX_BATCH_MS.

    ``statement_timeout`` restarts for every statement, so a fixed
    ``statement_timeout = MAX_BATCH_MS`` bounds each statement individually and
    bounds the BATCH at nothing: the locking SELECT, the move load and the guarded
    UPDATE could each take nearly the full allowance and the batch could hold row
    locks for roughly three times its budget.

    Consecutive EQUAL values are expected and must pass — ``_arm`` truncates the
    remaining budget with ``int()``, so two statements armed inside the same
    millisecond legitimately receive the same value. Requiring a strict decrease
    would flake on exactly the fast path this is supposed to bless (and "strictly
    non-increasing" is a contradiction in terms).
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        _seed_stale(conn, 6, user_id=950_020)

    with eng.connect() as conn, _Trace(conn) as trace:
        conn.commit()  # close the autobegun transaction before the batch loop
        _run_phase(conn, "backfill", env=mod.BATCH_ENV, batch_size=2)

    armed = [value for kind, value in trace.events if kind == "statement_timeout"]
    assert armed, "no statement_timeout was armed at all"
    assert max(armed) <= mod.MAX_BATCH_MS or all(
        a <= mod.SCAN_STMT_TIMEOUT_MS or a <= mod.MAX_BATCH_MS for a in armed
    )
    # Inside a batch the allowance narrows monotonically.
    for marker in ("ghostreplay:select_batch_first_locked", "ghostreplay:guarded_update"):
        assert trace.armed_before(marker), marker
    batch_arms = _within_batch_arms(trace)
    assert batch_arms, "no batch was traced"
    for run in batch_arms:
        assert run == sorted(run, reverse=True), f"armed values increased inside a batch: {run}"
        assert run[0] <= mod.MAX_BATCH_MS
    # The paired lock_timeout is min(cap, remaining) — never above the cap, and
    # never above the statement's own remaining budget.
    for stmt_ms, lock_ms in trace.pairs():
        assert lock_ms == min(mod.BATCH_LOCK_WAIT_MS, stmt_ms), (stmt_ms, lock_ms)
    eng.dispose()


def _within_batch_arms(trace: _Trace) -> list[list[int]]:
    """The armed ``statement_timeout`` sequence of each per-batch transaction.

    Split on ``begin``, because ``MAX_BATCH_MS`` is a per-transaction deadline: the
    allowance narrows monotonically WITHIN a batch and legitimately resets at the
    next one.
    """
    runs: list[list[int]] = []
    current: list[int] | None = None
    for kind, value in trace.events:
        if kind == "begin":
            current = []
            runs.append(current)
        elif kind == "statement_timeout" and current is not None:
            current.append(value)
    return [r for r in runs if r]


@pg_gate_plugin.pg_gate
def test_pg_atomic_arming_draws_on_the_residual_stall_budget_from_the_first_lock(
    pg_migration_db, monkeypatch
):
    """From ``t_stall_0`` onward EVERY statement is armed against the residual budget.

    An implementation that armed scans in atomic mode with a bare
    ``SCAN_STMT_TIMEOUT_MS`` — which is what "arm every scan with the scan timeout"
    reads like until you notice the hold it is inside — fails on the first
    materialization. And the statements issued BEFORE ``t_stall_0`` must be armed
    against the revision deadline only, because there is no stall budget yet: no row
    lock has been taken.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        _seed_stale(conn, 4, user_id=950_030)
        _seed_repair(conn, 2, user_id=950_031)

    clock = mod._RunClock()
    stall = mod._AtomicStall(clock, n_stale=4, n_repair=2, projected_ms=1.0)
    migration_guard.migration_stall_probe.reset()
    with eng.begin() as conn, _Trace(conn) as trace:
        # Statements before the first row lock: the population counts.
        n_stale = mod._count_population(
            conn, mod.SQL_PG.backfill_population_count, clock,
            lock_wait_ms=mod.ATOMIC_LOCK_WAIT_MS,
        )
        assert n_stale == 4
        pre_lock_arms = [v for k, v in trace.events if k == "statement_timeout"]
        assert pre_lock_arms and all(a <= mod.SCAN_STMT_TIMEOUT_MS for a in pre_lock_arms)
        assert clock.atomic_deadline is None

        _run_phase(conn, "backfill", env=mod.ATOMIC_ENV, batch_size=2, clock=clock, stall=stall)
        _run_phase(conn, "repair", env=mod.ATOMIC_ENV, batch_size=2, clock=clock, stall=stall)
        mod._assert_fail_closed(conn, mod.SQL_PG, clock, mod.ATOMIC_ENV)

    assert stall.armed_at is not None
    budget = stall.work_budget_ms
    assert budget < mod.MAX_WRITER_STALL_MS  # the teardown is RESERVED, not armed

    # Everything after the anchor is armed at or below the residual budget.
    anchor_index = next(
        i for i, (k, v) in enumerate(trace.events)
        if k == "stmt" and v == "ghostreplay:guarded_update"
    )
    after = [v for k, v in trace.events[anchor_index:] if k == "statement_timeout"]
    assert after, "nothing was armed after the first lock-bearing statement"
    assert all(a <= budget for a in after), (after, budget)
    # And the statements whose own cap cannot bind read the budget DIRECTLY, so they
    # narrow monotonically — the direct evidence that every statement from t_stall_0
    # onward spends from ONE pot. The scan-capped statements are asserted separately
    # below: they sit at SCAN_STMT_TIMEOUT_MS while the budget is above it, and
    # requiring them to narrow would be requiring the cap not to exist.
    # Sliced from the anchor: the arms BEFORE it legitimately read the revision
    # budget, which is three orders of magnitude larger — that is the separate
    # assertion above.
    budget_bound, scan_capped = trace.arms_by_kind(start=anchor_index - 2)
    assert len(budget_bound) >= 3, budget_bound
    assert budget_bound == sorted(budget_bound, reverse=True), budget_bound
    assert all(a <= budget for a in budget_bound), (budget_bound, budget)
    assert scan_capped and all(a <= mod.SCAN_STMT_TIMEOUT_MS for a in scan_capped)

    # The scans under lock are armed with min(SCAN_STMT_TIMEOUT_MS, the budget) —
    # never with a fresh scan allowance when the budget is smaller.
    for marker in (
        "ghostreplay:repair_populate",
        "ghostreplay:repair_remaining",
        "ghostreplay:coverage_assert",
        "ghostreplay:soundness_assert",
    ):
        arms = trace.armed_before(marker)
        assert arms, marker
        assert all(a <= mod.SCAN_STMT_TIMEOUT_MS for a in arms), (marker, arms)
        assert all(a <= budget for a in arms), (marker, arms)

    for stmt_ms, lock_ms in trace.pairs():
        assert lock_ms == min(mod.ATOMIC_LOCK_WAIT_MS, stmt_ms), (stmt_ms, lock_ms)
    migration_guard.migration_stall_probe.reset()
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_the_materialization_and_the_assertions_never_get_a_batch_allowance(
    pg_migration_db, monkeypatch
):
    """They are not inside a batch budget, so ``MAX_BATCH_MS`` must never be armed
    on them — in EITHER mode.

    ``MAX_BATCH_MS`` (5,000 ms) is an order of magnitude above
    ``SCAN_STMT_TIMEOUT_MS`` (521 ms), so arming a scan with the batch allowance
    would hand it ten times the budget its own measurement justifies.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        _seed_repair(conn, 3, user_id=950_040)

    for env in (mod.ATOMIC_ENV, mod.BATCH_ENV):
        with eng.connect() as conn, _Trace(conn) as trace:
            conn.commit()
            clock = _run_phase(conn, "repair", env=env, batch_size=2)
            mod._assert_fail_closed(conn, mod.SQL_PG, clock, env)
        for marker in (
            "ghostreplay:repair_populate",
            "ghostreplay:repair_remaining",
            "ghostreplay:coverage_assert",
            "ghostreplay:soundness_assert",
        ):
            arms = trace.armed_before(marker)
            assert arms, (env.name, marker)
            assert all(a <= mod.SCAN_STMT_TIMEOUT_MS for a in arms), (env.name, marker, arms)
        # And restore the fixture for the next mode.
        with eng.begin() as conn:
            conn.execute(
                text(
                    "UPDATE game_sessions SET player_accuracy = :a "
                    "WHERE player_accuracy IS NULL AND player_accuracy_algo_version = 1"
                ).bindparams(a=BROKEN_UNGUARDED_ACCURACY)
            )
    eng.dispose()


# ---------------------------------------------------------------------------
# 3. Scan accounting.
# ---------------------------------------------------------------------------

_SESSION_MOVES_SCANS = frozenset({
    "ghostreplay:repair_populate",
    "ghostreplay:repair_remaining",
    "ghostreplay:repair_population_count",
    "ghostreplay:soundness_assert",
})


@pg_gate_plugin.pg_gate
@pytest.mark.parametrize(
    "n_stale,n_repair,expected_under_lock",
    [
        pytest.param(3, 2, 3, id="stale_and_repair"),
        # The case where the SCANS ARE THE ENTIRE STALL.
        pytest.param(3, 0, 3, id="stale_only"),
        # With nothing to back-fill the FIRST row lock is the repair's own
        # FOR NO KEY UPDATE, taken AFTER the materialization — so the
        # materialization is not under lock at all.
        pytest.param(0, 2, 2, id="repair_only"),
    ],
)
def test_pg_atomic_scans_under_lock_is_a_bound_and_each_path_is_exact(
    pg_migration_db, monkeypatch, n_stale, n_repair, expected_under_lock
):
    """``ATOMIC_SCANS_UNDER_LOCK`` is a MAXIMUM over paths, not an identity.

    The admission formula multiplies by it unconditionally, which over-charges the
    zero-stale path by one scan — safe in the only direction that matters, since an
    over-estimate can only reject an atomic run, never wrongly admit one. An
    implementation that hard-coded 3 here, or a test that asserted equality with
    the constant on every path, would be wrong about where the first lock falls.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        if n_stale:
            _seed_stale(conn, n_stale, user_id=950_050)
        if n_repair:
            _seed_repair(conn, n_repair, user_id=950_051)

    clock = mod._RunClock()
    stall = mod._AtomicStall(
        clock, n_stale=n_stale, n_repair=n_repair, projected_ms=1.0
    )
    migration_guard.migration_stall_probe.reset()
    with eng.begin() as conn, _Trace(conn) as trace:
        _run_phase(conn, "backfill", env=mod.ATOMIC_ENV, clock=clock, stall=stall)
        _run_phase(conn, "repair", env=mod.ATOMIC_ENV, clock=clock, stall=stall)
        mod._assert_fail_closed(conn, mod.SQL_PG, clock, mod.ATOMIC_ENV)

    markers = trace.markers()
    first_lock = next(
        (
            i
            for i, m in enumerate(markers)
            if m in ("ghostreplay:guarded_update", "ghostreplay:repair_lock")
        ),
        None,
    )
    assert first_lock is not None, markers
    under_lock = [m for m in markers[first_lock:] if m in _SESSION_MOVES_SCANS]
    assert len(under_lock) == expected_under_lock, (markers, under_lock)
    assert len(under_lock) <= mod.ATOMIC_SCANS_UNDER_LOCK
    # The coverage assertion is scan-bearing too, but it scans game_sessions — its
    # own term, never double-counted inside ATOMIC_SCANS_UNDER_LOCK.
    assert "ghostreplay:coverage_assert" not in _SESSION_MOVES_SCANS
    migration_guard.migration_stall_probe.reset()
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_both_populations_zero_takes_no_row_lock_and_scans_twice(
    pg_migration_db, monkeypatch
):
    """The path a fresh database takes — and a re-entered unstamped revision.

    TWO scan-bearing ``session_moves`` statements, not four: the pre-flight repair
    count and the soundness assertion. The materialization and the convergence
    counts belong to the runner, and the runner did not run. NO row lock is ever
    taken, so nothing is under lock and the atomic stall is not merely small but
    structurally ABSENT — which is also why no execution mode is required here.
    """
    url = pg_migration_db
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv(mod.ENV_MODE, raising=False)
    command.upgrade(_alembic_config(), PREVIOUS_HEAD)
    eng = create_engine(url)
    migration_guard.migration_stall_probe.reset()

    # Listen on the Engine CLASS: env.py builds its own engine from DATABASE_URL, so
    # a listener attached to this test's engine would see none of the revision's SQL.
    trace = _Trace(Engine)
    with trace:
        command.upgrade(_alembic_config(), REVISION)

    markers = trace.markers()
    scans = [m for m in markers if m in _SESSION_MOVES_SCANS]
    assert scans == [
        "ghostreplay:repair_population_count",
        "ghostreplay:soundness_assert",
    ], markers
    assert "ghostreplay:guarded_update" not in markers
    assert "ghostreplay:repair_lock" not in markers
    assert "ghostreplay:repair_populate" not in markers
    # No row lock was taken, so the revision never anchored the probe.
    assert migration_guard.migration_stall_probe._first_row_lock_at is None

    # THE BOUNDARY OF THE WHOLE ACCOUNTING: at a database already stamped, `upgrade
    # head` executes ZERO scan-bearing statements, because it executes no revision
    # at all. This is what keeps "the revision's scans" from being mistaken for "a
    # cost every boot pays".
    trace_stamped = _Trace(Engine)
    with trace_stamped:
        command.upgrade(_alembic_config(), REVISION)
    assert [m for m in trace_stamped.markers() if m in _SESSION_MOVES_SCANS] == []
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_one_scan_per_pass_not_one_per_batch(pg_migration_db, monkeypatch):
    """The test that catches an embedded-detector selector.

    Embedding the set-wide detector in each batch's selection would pay a full
    ``session_moves`` scan PER BATCH, inside the batch's armed budget — a cost the
    sizing model prices at zero, and one that would make
    ``R_formula = floor((MAX_BATCH_MS - MARGINED_MS_PER_SCAN_STMT) /
    MARGINED_MS_PER_REPAIR_ROW)`` go negative on any large move table.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        _seed_repair(conn, 7, user_id=950_060)

    with eng.connect() as conn, _Trace(conn) as trace:
        conn.commit()
        # A repair batch size of 2 over 7 candidates: at least four batches.
        runner = mod._Runner(
            conn, mod.SQL_PG, clock=mod._RunClock(), env=mod.BATCH_ENV,
            batch_size=1_000, stall=None,
        )
        monkeypatch.setattr(mod, "REPAIR_BATCH_SIZE", 2)
        with mod._ComputeWatchdog() as watchdog:
            runner.run_phase("repair", watchdog)

    markers = trace.markers()
    batches = markers.count("ghostreplay:repair_select_first_locked") + markers.count(
        "ghostreplay:repair_select_locked"
    )
    assert batches >= 4, markers
    # ONE materialization and ONE convergence scan for the whole pass.
    assert markers.count("ghostreplay:repair_populate") == 1, markers
    assert markers.count("ghostreplay:repair_remaining") == 1, markers
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_backfill_issues_one_selection_sweep_and_one_convergence_count_per_pass(
    pg_migration_db, monkeypatch
):
    """The same shape on the backfill's side, and the reason its own two constants
    are counted PER PASS rather than per batch."""
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        _seed_stale(conn, 7, user_id=950_070)

    with eng.connect() as conn, _Trace(conn) as trace:
        conn.commit()
        _run_phase(conn, "backfill", env=mod.BATCH_ENV, batch_size=2)

    markers = trace.markers()
    pages = markers.count("ghostreplay:select_batch_first_locked") + markers.count(
        "ghostreplay:select_batch_locked"
    )
    assert pages >= 4, markers  # 7 rows at 2 per page, plus the terminating page
    # One sweep = all of those pages together, and ONE convergence count for it.
    assert markers.count("ghostreplay:backfill_remaining") == 1, markers
    eng.dispose()


# ---------------------------------------------------------------------------
# 4. The materialization's transaction contract is mode-split.
# ---------------------------------------------------------------------------


@pg_gate_plugin.pg_gate
def test_pg_materialization_commits_before_the_batch_loop_in_per_batch_mode(
    pg_migration_db, monkeypatch
):
    """Per-batch mode: its OWN transaction, closed before the first batch begins.

    Asserting one contract in BOTH modes would be asserting something atomic mode
    is structurally incapable of — it cannot open a second transaction, and
    committing would destroy the atomicity that is the mode's whole point.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        _seed_repair(conn, 2, user_id=950_080)

    with eng.connect() as conn, _Trace(conn) as trace:
        conn.commit()
        _run_phase(conn, "repair", env=mod.BATCH_ENV, batch_size=1_000)

    kinds = [(k, v) for k, v in trace.events if k in ("stmt", "commit", "begin")]
    populate = next(i for i, (k, v) in enumerate(kinds) if v == "ghostreplay:repair_populate")
    first_select = next(
        i
        for i, (k, v) in enumerate(kinds)
        if v in ("ghostreplay:repair_select_first_locked", "ghostreplay:repair_select_locked")
    )
    commits_between = [
        i for i, (k, _) in enumerate(kinds) if k == "commit" and populate < i < first_select
    ]
    assert commits_between, kinds[populate : first_select + 1]
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_materialization_runs_inside_alembics_transaction_in_atomic_mode(
    pg_migration_db, monkeypatch
):
    """Atomic mode: no COMMIT between the backfill's first guarded UPDATE and the
    materialization — the backfill's row locks are still held, which is exactly what
    ``ATOMIC_SCANS_UNDER_LOCK`` prices."""
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        _seed_stale(conn, 2, user_id=950_090)
        _seed_repair(conn, 2, user_id=950_091)

    clock = mod._RunClock()
    stall = mod._AtomicStall(clock, n_stale=2, n_repair=2, projected_ms=1.0)
    migration_guard.migration_stall_probe.reset()
    with eng.begin() as conn, _Trace(conn) as trace:
        _run_phase(conn, "backfill", env=mod.ATOMIC_ENV, clock=clock, stall=stall)
        _run_phase(conn, "repair", env=mod.ATOMIC_ENV, clock=clock, stall=stall)

    kinds = [(k, v) for k, v in trace.events if k in ("stmt", "commit")]
    update = next(i for i, (k, v) in enumerate(kinds) if v == "ghostreplay:guarded_update")
    populate = next(i for i, (k, v) in enumerate(kinds) if v == "ghostreplay:repair_populate")
    assert update < populate
    assert not [i for i, (k, _) in enumerate(kinds) if k == "commit" and update < i < populate]
    migration_guard.migration_stall_probe.reset()
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_materialization_takes_no_row_lock_of_its_own_in_either_mode(
    pg_migration_db, monkeypatch
):
    """The one invariant both modes share, probed the one way that means the same
    thing in both.

    A concurrent ``FOR NO KEY UPDATE`` on a REPAIR CANDIDATE THE REPAIR HAS NOT YET
    REACHED must acquire immediately while the materialization runs. Probing an
    already-backfilled row proves nothing: in atomic mode it is SUPPOSED to block,
    and that is the stall.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        candidates = _seed_repair(conn, 3, user_id=950_100)

    for env in (mod.ATOMIC_ENV, mod.BATCH_ENV):
        acquired: list[float] = []

        with eng.connect() as conn:
            conn.commit()
            runner = mod._Runner(
                conn, mod.SQL_PG, clock=mod._RunClock(), env=env,
                batch_size=1_000, stall=None,
            )
            runner._materialize()
            # The materialization has run and (in atomic mode) its transaction is
            # still open. Every candidate must still be lockable from outside.
            with eng.connect() as probe:
                probe.execute(text("SELECT set_config('lock_timeout', '500ms', true)"))
                started = time.monotonic()
                probe.execute(
                    text(
                        "SELECT id FROM game_sessions WHERE id = ANY(CAST(:ids AS uuid[])) "
                        "FOR NO KEY UPDATE NOWAIT"
                    ).bindparams(ids=[str(c) for c in candidates])
                )
                acquired.append((time.monotonic() - started) * 1000)
                probe.rollback()
            conn.rollback()
        assert acquired and acquired[0] < 500, (env.name, acquired)
    eng.dispose()


# ---------------------------------------------------------------------------
# 5. The per-batch runner: convergence, durability, exhaustion.
# ---------------------------------------------------------------------------


@pg_gate_plugin.pg_gate
def test_pg_per_batch_runner_labels_its_own_connection_and_commits_the_setup_first(
    pg_migration_db, monkeypatch
):
    """The runner's connection lifecycle is EXPLICIT, because labelling executes SQL.

    SQLAlchemy autobegins on the first execute, so the ``application_name`` label
    and the ``pg_backend_pid()`` log open a transaction before any batch does. If
    the runner then called ``begin()`` for its first batch without closing that one,
    SQLAlchemy would raise; and if it reused the ambient transaction, a first-batch
    rollback would silently drop the ``application_name`` an operator is watching
    for. Neither is acceptable, so the setup transaction is committed first — and
    ``application_name`` is a SESSION GUC, so it survives that commit and every
    later batch boundary.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        _seed_stale(conn, 4, user_id=950_110)

    seen: dict = {}

    def _observe():
        # Watch from OUTSIDE for the runner's backend, by the name the guard froze.
        deadline = time.monotonic() + 10
        with eng.connect() as watcher:
            while time.monotonic() < deadline and "pid" not in seen:
                row = watcher.execute(
                    text(
                        "SELECT pid FROM pg_stat_activity WHERE application_name = :n LIMIT 1"
                    ).bindparams(n=migration_guard.RUNNER_APP_NAME)
                ).first()
                watcher.rollback()
                if row:
                    seen["pid"] = row.pid
                    return
                time.sleep(0.02)

    watcher = threading.Thread(target=_observe)
    watcher.start()
    with eng.connect() as conn, _Trace(eng) as trace:
        mod._run_phases(
            conn,
            mod.SQL_PG,
            clock=mod._RunClock(),
            env=mod.BATCH_ENV,
            batch_size=2,
            stall=None,
        )
        conn.commit()
    watcher.join()

    assert seen.get("pid"), "the runner connection was never visible by application_name"
    # The setup COMMIT precedes the first batch's BEGIN.
    kinds = [k for k, _ in trace.events]
    first_commit = kinds.index("commit")
    first_lock = next(
        i for i, (k, v) in enumerate(trace.events)
        if k == "stmt" and v.endswith("select_batch_first_locked")
    )
    assert first_commit < first_lock, trace.events[: first_lock + 1]
    with eng.connect() as conn:
        assert mod.remaining_scan(conn, mod.SQL_PG.backfill_remaining) == (0, [])
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_skipped_rows_converge_across_passes_and_a_zero_row_pass_is_never_success(
    pg_migration_db, monkeypatch
):
    """``SKIP LOCKED`` needs repeated passes, and the cursor passing a row does not
    complete it.

    A competing writer holds one stale row's ``FOR NO KEY UPDATE`` and does NOT
    commit. The pass demonstrably SKIPS it, so a runner that treated a zero-row (or
    short) pass as success would stamp everything else and declare victory with that
    row unstamped. Convergence must come from the FRESH REMAINING COUNT — which is
    nonzero, because the writer's uncommitted work is invisible — so the runner
    backs off, and only after the writer commits does a later pass converge.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        ids = _seed_stale(conn, 3, user_id=950_120)
    held = ids[0]

    holder = eng.connect()
    released = threading.Event()

    def _hold_then_release():
        holder.execute(
            text("SELECT id FROM game_sessions WHERE id = CAST(:i AS uuid) FOR NO KEY UPDATE")
            .bindparams(i=str(held))
        )
        released.wait(30)
        # Commit the hook's OWN write: version 1, its own value. The hook always wins.
        holder.execute(
            text(
                "UPDATE game_sessions SET player_accuracy = :a, "
                "player_accuracy_algo_version = 1 WHERE id = CAST(:i AS uuid)"
            ).bindparams(a=55, i=str(held))
        )
        holder.commit()

    writer = threading.Thread(target=_hold_then_release)
    writer.start()
    time.sleep(0.3)  # let the writer take the lock

    passes: list[tuple[int, int]] = []
    real_remaining = mod._Runner._remaining

    def _spy_remaining(self, sql, phase):
        count, sample = real_remaining(self, sql, phase)
        passes.append((len(passes) + 1, count))
        if count and not released.is_set():
            released.set()  # let the writer commit before the next pass
        return count, sample

    monkeypatch.setattr(mod._Runner, "_remaining", _spy_remaining)
    with eng.connect() as conn, _Trace(conn) as trace:
        conn.commit()
        _run_phase(conn, "backfill", env=mod.BATCH_ENV, batch_size=10)
    writer.join()
    holder.close()

    assert len(passes) >= 2, passes
    assert passes[0][1] > 0, "the first pass must NOT have converged"
    assert passes[-1][1] == 0
    # ONE convergence scan per pass, and the row was demonstrably SKIPPED rather
    # than waited for: a pass that blocked would not have completed at all while the
    # writer held the lock uncommitted.
    assert trace.markers().count("ghostreplay:backfill_remaining") == len(passes)
    with eng.connect() as conn:
        # The hook's value survived — the migration yielded the row to it.
        assert _cached(conn, held) == (55, 1)
        for sid in ids[1:]:
            assert _cached(conn, sid) == (INTACT_ACCURACY, 1)
        assert mod.remaining_scan(conn, mod.SQL_PG.backfill_remaining) == (0, [])
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_a_permanently_locked_row_exhausts_the_pass_bound_with_the_exact_template(
    pg_migration_db, monkeypatch
):
    """Between-passes exhaustion: ``remaining``, ``passes`` and ``first_remaining``
    are POPULATED, from the pass's own convergence scan.

    A listener confirms no SECOND diagnostic scan was issued — the ids ride the scan
    the pass already pays for, because fetching them with a separate
    ``SELECT ... LIMIT 20`` would be another full detector scan and would break both
    one-scan-per-pass and the import scan budget.

    The pass bound is narrowed from ``MAX_PASSES`` to 3 for runtime only (20 passes
    of exponential backoff is ~87 seconds of sleeping); the CONTRACT under test is
    the template and where its fields come from.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        ids = _seed_stale(conn, 2, user_id=950_130)
    held = ids[0]

    holder = eng.connect()
    try:
        holder.execute(
            text("SELECT id FROM game_sessions WHERE id = CAST(:i AS uuid) FOR NO KEY UPDATE")
            .bindparams(i=str(held))
        )
        env = mod.BATCH_ENV._replace(max_passes=3)
        with eng.connect() as conn, _Trace(conn) as trace:
            conn.commit()
            with pytest.raises(mod.DeadlineExceeded) as exc:
                _run_phase(conn, "backfill", env=env, batch_size=10)
        message = str(exc.value)
        assert "phase=backfill" in message
        assert "remaining=1" in message
        assert "passes=3/3" in message
        assert f"first_remaining={held}" in message
        assert "sqlstate=n/a" in message
        # Exactly one convergence scan per pass, and no diagnostic re-scan.
        assert trace.markers().count("ghostreplay:backfill_remaining") == 3
        # The row the writer did not hold was stamped and stayed stamped.
        with eng.connect() as conn:
            assert _cached(conn, ids[1]) == (INTACT_ACCURACY, 1)
            assert _cached(conn, held) == (None, None)
    finally:
        holder.rollback()
        holder.close()
        eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_earlier_batches_stay_durable_when_a_later_one_fails(
    pg_migration_db, monkeypatch
):
    """Per-batch mode owns its transactions, so committed progress survives.

    Injected failure in the THIRD batch: the first two must be durable from a fresh
    connection, the failing batch must have stamped nothing, and the rerun must
    process only what remains.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        _seed_stale(conn, 6, user_id=950_140)

    calls = {"n": 0}
    real_apply = mod._Runner._apply

    def _boom_on_third(self, state, results):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("injected batch failure")
        return real_apply(self, state, results)

    monkeypatch.setattr(mod._Runner, "_apply", _boom_on_third)
    with eng.connect() as conn:
        conn.commit()
        with pytest.raises(RuntimeError, match="injected batch failure"):
            _run_phase(conn, "backfill", env=mod.BATCH_ENV, batch_size=2)

    with eng.connect() as conn:
        stamped, _ = mod.remaining_scan(conn, mod.SQL_PG.backfill_remaining)
    assert stamped == 2, "the two committed batches must be durable, the third not"

    monkeypatch.setattr(mod._Runner, "_apply", real_apply)
    with eng.connect() as conn:
        conn.commit()
        _run_phase(conn, "backfill", env=mod.BATCH_ENV, batch_size=2)
    with eng.connect() as conn:
        assert mod.remaining_scan(conn, mod.SQL_PG.backfill_remaining) == (0, [])
    eng.dispose()


# ---------------------------------------------------------------------------
# 6. Enforcement: slow SQL, slow Python, and the cancellation template.
# ---------------------------------------------------------------------------


@pg_gate_plugin.pg_gate
def test_pg_a_slow_statement_is_cancelled_at_its_armed_timeout_and_rolls_back(
    pg_migration_db, monkeypatch
):
    """PostgreSQL cancels it (57014); the batch rolls back BEFORE commit.

    Slow at the SQL layer, so it runs under the same armed ``statement_timeout`` the
    real move load does. Every assertion afterwards is about what the rollback
    guaranteed: nothing stamped, ROW LOCKS RELEASED (a competing
    ``FOR NO KEY UPDATE NOWAIT`` acquires immediately), earlier batches still
    committed, and the runner raised with the frozen template.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        ids = _seed_stale(conn, 4, user_id=950_150)

    # A tiny batch cap so the sleep cannot fit, and a real pg_sleep in the load.
    env = mod.BATCH_ENV._replace(stmt_cap_ms=300)
    with eng.connect() as conn:
        conn.commit()
        with pytest.raises(mod.DeadlineExceeded) as exc:
            _run_phase(
                conn, "backfill", env=env, batch_size=2, bundle=_sleeping_bundle(2.0)
            )
    assert "sqlstate=57014" in str(exc.value)
    assert "phase=backfill" in str(exc.value)
    assert "remaining=n/a" in str(exc.value)
    assert "first_remaining=n/a" in str(exc.value)

    with eng.connect() as probe:
        probe.execute(text("SELECT set_config('lock_timeout', '500ms', true)"))
        started = time.monotonic()
        probe.execute(
            text(
                "SELECT id FROM game_sessions WHERE id = ANY(CAST(:ids AS uuid[])) "
                "FOR NO KEY UPDATE NOWAIT"
            ).bindparams(ids=[str(i) for i in ids])
        )
        assert (time.monotonic() - started) * 1000 < 500, "row locks were not released"
        probe.rollback()
    with eng.connect() as conn:
        remaining, _ = mod.remaining_scan(conn, mod.SQL_PG.backfill_remaining)
    assert remaining == 4, "the cancelled batch must have stamped nothing"
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_the_guarded_update_inherits_only_what_the_load_left(
    pg_migration_db, monkeypatch
):
    """Slow-SQL budget CARRY-OVER: consume most of the batch, then check the arm.

    A load that SUCCEEDS but eats most of the allowance must leave the guarded
    UPDATE armed with the remainder — not with a fresh ``MAX_BATCH_MS``. This is the
    property that makes ``MAX_BATCH_MS`` a batch-wide bound instead of a
    per-statement one.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        _seed_stale(conn, 2, user_id=950_160)

    # The BATCH deadline has to be the binding one, or the mode's own cap is armed
    # again and the test measures the cap rather than the carry-over.
    monkeypatch.setattr(mod, "MAX_BATCH_MS", 2_500)
    env = mod.BATCH_ENV._replace(stmt_cap_ms=2_500)
    with eng.connect() as conn, _Trace(conn) as trace:
        conn.commit()
        _run_phase(
            conn, "backfill", env=env, batch_size=2, bundle=_sleeping_bundle(1.2)
        )

    load_arms = trace.armed_before("ghostreplay:load_moves")
    update_arms = trace.armed_before("ghostreplay:guarded_update")
    assert load_arms and update_arms
    # The UPDATE's allowance is what the 1.2-second load left behind, not a fresh
    # MAX_BATCH_MS — so it is at least a second smaller.
    assert update_arms[0] < load_arms[0] - 900, (load_arms, update_arms)
    assert update_arms[0] < mod.MAX_BATCH_MS
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_slow_python_compute_rolls_the_batch_back(pg_migration_db, monkeypatch):
    """``statement_timeout`` cannot interrupt Python, so the watchdog has to.

    A slow SINGLE SESSION — not a slow loop — so the per-session deadline check
    cannot see it. The batch rolls back with its locks released and nothing stamped,
    and the runner raises the frozen template with ``sqlstate=n/a``: a Python raise
    has no SQLSTATE.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        ids = _seed_stale(conn, 2, user_id=950_170)

    monkeypatch.setattr(mod, "MAX_SINGLE_SESSION_COMPUTE_MS", 20)
    real_accuracy_for = mod._accuracy_for

    def _slow(rows, player_color, pgn):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            pass
        return real_accuracy_for(rows, player_color, pgn)

    monkeypatch.setattr(mod, "_accuracy_for", _slow)
    with eng.connect() as conn:
        conn.commit()
        with pytest.raises(mod.DeadlineExceeded) as exc:
            _run_phase(conn, "backfill", env=mod.BATCH_ENV, batch_size=2)
    assert "sqlstate=n/a" in str(exc.value)
    assert "phase=backfill" in str(exc.value)

    monkeypatch.setattr(mod, "_accuracy_for", real_accuracy_for)
    with eng.connect() as probe:
        probe.execute(text("SELECT set_config('lock_timeout', '500ms', true)"))
        probe.execute(
            text(
                "SELECT id FROM game_sessions WHERE id = ANY(CAST(:ids AS uuid[])) "
                "FOR NO KEY UPDATE NOWAIT"
            ).bindparams(ids=[str(i) for i in ids])
        )
        probe.rollback()
    with eng.connect() as conn:
        assert mod.remaining_scan(conn, mod.SQL_PG.backfill_remaining)[0] == 2
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_repair_batches_obey_the_same_budget(pg_migration_db, monkeypatch):
    """The three per-session repair statements are each armed from the remaining
    deadline, and a breach rolls the repair batch back with NOTHING nulled."""
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        ids = _seed_repair(conn, 3, user_id=950_180)

    with eng.connect() as conn, _Trace(conn) as trace:
        conn.commit()
        _run_phase(conn, "repair", env=mod.BATCH_ENV, batch_size=1_000)
    for marker in (
        "ghostreplay:repair_lock",
        "ghostreplay:ply_detector_one",
        "ghostreplay:repair_update",
    ):
        assert trace.armed_before(marker), marker
        assert all(a <= mod.MAX_BATCH_MS for a in trace.armed_before(marker)), marker

    # Now the breach path, on a fresh repair population.
    with eng.begin() as conn:
        conn.execute(
            text(
                "UPDATE game_sessions SET player_accuracy = :a "
                "WHERE id = ANY(CAST(:ids AS uuid[]))"
            ).bindparams(a=BROKEN_UNGUARDED_ACCURACY, ids=[str(i) for i in ids])
        )
    real_repair_one = mod._Runner._repair_one

    def _slow_repair(self, state, sid):
        time.sleep(0.4)
        return real_repair_one(self, state, sid)

    monkeypatch.setattr(mod._Runner, "_repair_one", _slow_repair)
    env = mod.BATCH_ENV._replace(stmt_cap_ms=200)
    with eng.connect() as conn:
        conn.commit()
        # MAX_BATCH_MS is patched down through the batch deadline, so the batch runs
        # out mid-candidate.
        monkeypatch.setattr(mod, "MAX_BATCH_MS", 300)
        with pytest.raises(mod.DeadlineExceeded) as exc:
            _run_phase(conn, "repair", env=env, batch_size=1_000)
    assert "phase=repair" in str(exc.value)
    with eng.connect() as conn:
        nulled = conn.execute(
            text(
                "SELECT count(*) FROM game_sessions WHERE player_accuracy IS NULL "
                "AND player_accuracy_algo_version = 1"
            )
        ).scalar()
    assert nulled == 0, "a breached repair batch must have nulled nothing"
    eng.dispose()


# ---------------------------------------------------------------------------
# 7. The lock-hold tripwire, and the negative for atomic mode.
# ---------------------------------------------------------------------------


@pg_gate_plugin.pg_gate
def test_pg_the_observed_lock_hold_tripwire_fires_after_teardown_and_raises(
    pg_migration_db, monkeypatch
):
    """A TRIPWIRE ON THE ESTIMATE, explicitly not a bound.

    By the time it fires the lock has already been held too long. What it buys is
    that the NEXT batch does not repeat it, that the migration fails instead of
    grinding through a thousand more over-budget batches, and that the breach
    becomes recorded evidence that the constants are wrong. The test asserts
    explicitly that the PRECEDING batch committed — i.e. that this is a tripwire on
    the next batch, not a bound on the one that breached.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        _seed_stale(conn, 4, user_id=950_190)

    monkeypatch.setattr(mod, "EST_MAX_LOCK_HOLD_MS", 0)
    with eng.connect() as conn:
        conn.commit()
        with pytest.raises(mod.MigrationError, match="observed lock hold") as exc:
            _run_phase(conn, "backfill", env=mod.BATCH_ENV, batch_size=2)
    assert "EST_MAX_LOCK_HOLD_MS" in str(exc.value)

    monkeypatch.setattr(mod, "EST_MAX_LOCK_HOLD_MS", mod.MAX_BATCH_MS + mod.TEARDOWN_ALLOWANCE_MS)
    with eng.connect() as conn:
        # The batch that breached had already COMMITTED when the tripwire fired.
        stamped, _ = mod.remaining_scan(conn, mod.SQL_PG.backfill_remaining)
    assert stamped == 2, "the breaching batch's own work is durable — it committed first"
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_atomic_mode_has_no_lock_hold_tripwire_to_fire(pg_migration_db, monkeypatch):
    """The NEGATIVE, and it is the assertion that forbids a fiction.

    Atomic mode's hold ends when Alembic's transaction commits — in ``env.py``,
    after ``upgrade()`` has already returned — so the revision can neither observe
    "first row lock through commit" nor raise on it. An atomic upgrade with
    ``EST_MAX_LOCK_HOLD_MS`` patched absurdly low must therefore SUCCEED.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        ids = _seed_stale(conn, 3, user_id=950_200)

    monkeypatch.setattr(mod, "EST_MAX_LOCK_HOLD_MS", 0)
    clock = mod._RunClock()
    stall = mod._AtomicStall(clock, n_stale=3, n_repair=0, projected_ms=1.0)
    migration_guard.migration_stall_probe.reset()
    with eng.begin() as conn:
        _run_phase(conn, "backfill", env=mod.ATOMIC_ENV, clock=clock, stall=stall, batch_size=1)
        _run_phase(conn, "repair", env=mod.ATOMIC_ENV, clock=clock, stall=stall)
        mod._assert_fail_closed(conn, mod.SQL_PG, clock, mod.ATOMIC_ENV)
    with eng.connect() as conn:
        for sid in ids:
            assert _cached(conn, sid) == (INTACT_ACCURACY, 1)
    migration_guard.migration_stall_probe.reset()
    eng.dispose()


# ---------------------------------------------------------------------------
# 8. Atomic mode: the residual stall budget bounds a SUM of lock waits.
# ---------------------------------------------------------------------------


@pg_gate_plugin.pg_gate
def test_pg_atomic_cumulative_lock_waits_breach_the_residual_budget(
    pg_migration_db, monkeypatch
):
    """The test a per-wait cap passes and a budget fails.

    Every individual wait comes in UNDER ``ATOMIC_LOCK_WAIT_MS``, so a design that
    bounded only the per-acquisition wait would see nothing wrong — PostgreSQL
    documents that ``lock_timeout`` applies separately to each acquisition. Their SUM
    is what extends a hold already open over every row locked so far, and that is
    what the residual budget bounds.

    One SQLSTATE is deliberately NOT pinned. Once the residual budget falls below
    ``ATOMIC_LOCK_WAIT_MS`` the two armed timeouts COINCIDE (``lock_timeout =
    min(cap, remaining)``), so whether the terminal breach surfaces as 57014 or
    55P03 is a race, and a test that pinned it would be pinning the race rather than
    the budget. What is pinned is the budget: the armed values decrease across
    batches, the breach happens at the remaining budget rather than at a fresh
    allowance, no INDIVIDUAL wait exceeded the cap, nothing was stamped, and every
    row lock came off.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        ids = _seed_stale(conn, 4, user_id=950_210)

    hold_ms = 300  # comfortably under ATOMIC_LOCK_WAIT_MS = 1000
    assert hold_ms < mod.ATOMIC_LOCK_WAIT_MS
    budget_ms = 800
    stop = threading.Event()

    # EVERY row is locked up front, on its OWN connection, and the locks are released
    # on a STAGGERED schedule. Holding them one at a time from a single connection
    # would race the runner's own progression through the id order and could let it
    # slip past uncontended; locking all of them first makes each wait deterministic.
    holders = [eng.connect() for _ in ids]
    for holder_conn, sid in zip(holders, sorted(ids, key=str)):
        holder_conn.execute(
            text("SELECT id FROM game_sessions WHERE id = CAST(:i AS uuid) FOR NO KEY UPDATE")
            .bindparams(i=str(sid))
        )

    def _release_on_schedule():
        for holder_conn in holders:
            if stop.wait(hold_ms / 1000):
                return
            with contextlib.suppress(Exception):
                holder_conn.rollback()

    # Shrink the residual work budget so the SUM of the waits cannot fit it, without
    # touching MAX_WRITER_STALL_MS: the teardown reserve is the other lever, and it
    # is the one the design names.
    monkeypatch.setattr(
        mod, "MARGINED_MS_ATOMIC_TEARDOWN_FIXED", mod.MAX_WRITER_STALL_MS - budget_ms
    )
    clock = mod._RunClock()
    stall = mod._AtomicStall(clock, n_stale=4, n_repair=0, projected_ms=1.0)
    migration_guard.migration_stall_probe.reset()

    holder = threading.Thread(target=_release_on_schedule)
    holder.start()
    try:
        with eng.connect() as conn, _Trace(conn) as trace:
            trans = conn.begin()
            with pytest.raises(mod.DeadlineExceeded) as exc:
                # batch_size=1, so each batch is one lock-bearing guarded UPDATE and
                # the waits accumulate inside ONE hold.
                _run_phase(
                    conn, "backfill", env=mod.ATOMIC_ENV, batch_size=1,
                    clock=clock, stall=stall,
                )
            trans.rollback()
    finally:
        stop.set()
        holder.join()
        for holder_conn in holders:
            with contextlib.suppress(Exception):
                holder_conn.rollback()
            holder_conn.close()

    assert stall.work_budget_ms is not None and stall.work_budget_ms <= budget_ms
    message = str(exc.value)
    assert "phase=backfill" in message
    sqlstate = re.search(r"sqlstate=(\S+)", message).group(1)
    assert sqlstate in ("57014", "55P03"), message

    # The armed values draw on ONE budget: they decrease across the batches.
    armed = trace.armed_before("ghostreplay:guarded_update")
    assert len(armed) >= 2, armed
    assert armed == sorted(armed, reverse=True), armed
    assert all(a <= stall.work_budget_ms for a in armed), (armed, stall.work_budget_ms)

    with eng.connect() as conn:
        remaining, _ = mod.remaining_scan(conn, mod.SQL_PG.backfill_remaining)
    assert remaining == 4, "the rolled-back atomic transaction must have stamped nothing"
    with eng.connect() as probe:
        probe.execute(text("SELECT set_config('lock_timeout', '1000ms', true)"))
        probe.execute(
            text(
                "SELECT id FROM game_sessions WHERE id = ANY(CAST(:ids AS uuid[])) "
                "FOR NO KEY UPDATE"
            ).bindparams(ids=[str(i) for i in ids])
        )
        probe.rollback()
    migration_guard.migration_stall_probe.reset()
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_the_same_waits_complete_cleanly_under_a_budget_that_absorbs_them(
    pg_migration_db, monkeypatch
):
    """The companion NEGATIVE: without it the test above proves a flaky timeout
    rather than a budget."""
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        ids = _seed_stale(conn, 4, user_id=950_220)

    stop = threading.Event()

    def _serial_holder():
        with eng.connect() as holder:
            for sid in ids:
                if stop.is_set():
                    return
                holder.execute(
                    text(
                        "SELECT id FROM game_sessions WHERE id = CAST(:i AS uuid) "
                        "FOR NO KEY UPDATE"
                    ).bindparams(i=str(sid))
                )
                time.sleep(0.25)
                holder.rollback()

    clock = mod._RunClock()
    stall = mod._AtomicStall(clock, n_stale=4, n_repair=0, projected_ms=1.0)
    migration_guard.migration_stall_probe.reset()
    holder = threading.Thread(target=_serial_holder)
    holder.start()
    time.sleep(0.05)
    try:
        with eng.begin() as conn:
            _run_phase(
                conn, "backfill", env=mod.ATOMIC_ENV, batch_size=1, clock=clock, stall=stall
            )
    finally:
        stop.set()
        holder.join()

    with eng.connect() as conn:
        assert mod.remaining_scan(conn, mod.SQL_PG.backfill_remaining) == (0, [])
    migration_guard.migration_stall_probe.reset()
    eng.dispose()


# ---------------------------------------------------------------------------
# 9. The env.py stall probe.
# ---------------------------------------------------------------------------


@pg_gate_plugin.pg_gate
def test_pg_the_stall_probe_measures_through_the_commit(pg_migration_db, monkeypatch):
    """The probe's value must EXCEED the elapsed time to the RETURN of upgrade().

    That is the whole point: it includes the commit, and no measurement taken inside
    the revision could have. It is the ONLY empirical check on atomic mode's
    projection, and it is read from ``env.py``'s existing ``finally`` around
    ``context.begin_transaction()`` — which fires exactly when COMMIT (or ROLLBACK)
    returns, i.e. when the row locks are released, on BOTH paths.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        _seed_stale(conn, 3, user_id=950_230)
    monkeypatch.setenv(mod.ENV_MODE, "atomic")

    migration_guard.migration_stall_probe.reset()
    recorded: dict = {}
    real_record = migration_guard._MigrationStallProbe.record_first_row_lock

    def _spy_record(self, ts, **kw):
        recorded.setdefault("ts", ts)
        recorded.update(kw)
        return real_record(self, ts, **kw)

    reported: dict = {}
    real_report = migration_guard._MigrationStallProbe.report

    def _spy_report(self):
        reported["observed_ms"] = (
            (time.monotonic() - self._first_row_lock_at) * 1000
            if self._first_row_lock_at is not None
            else None
        )
        reported["max_stall_ms"] = self._max_stall_ms
        reported["projected_stall_ms"] = self._projected_stall_ms
        return real_report(self)

    monkeypatch.setattr(
        migration_guard._MigrationStallProbe, "record_first_row_lock", _spy_record
    )
    monkeypatch.setattr(migration_guard._MigrationStallProbe, "report", _spy_report)

    command.upgrade(_alembic_config(), REVISION)

    assert recorded.get("ts") is not None, "the revision never anchored the probe"
    assert recorded["max_stall_ms"] == mod.MAX_WRITER_STALL_MS
    assert recorded["projected_stall_ms"] > 0
    assert reported.get("observed_ms") is not None, "env.py never reported"
    # The report happened AFTER the revision returned, so the observed hold covers
    # the commit the revision could not see.
    assert reported["observed_ms"] > 0
    assert reported["max_stall_ms"] == mod.MAX_WRITER_STALL_MS
    migration_guard.migration_stall_probe.reset()
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_the_stall_probe_still_reports_on_a_failing_upgrade(
    pg_migration_db, monkeypatch
):
    """``env.py``'s ``finally`` fires on the ROLLBACK path too, which is the moment
    the row locks are released there."""
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        _seed_stale(conn, 2, user_id=950_240)
        # A row the backfill's predicate cannot reach (version 2) but the coverage
        # assertion counts (IS DISTINCT FROM 1): a REAL, unpatched failure at the
        # fail-closed assertion, after the backfill took its row locks.
        _seed_pg_session(
            conn, uuid.uuid4(), status="ended", mode="normal", drill_state=None,
            version=2, accuracy=50, plies=INTACT_PLIES, user_id=950_241,
        )
    monkeypatch.setenv(mod.ENV_MODE, "atomic")

    migration_guard.migration_stall_probe.reset()
    reported: list = []
    real_report = migration_guard._MigrationStallProbe.report

    def _spy_report(self):
        reported.append(self._first_row_lock_at)
        return real_report(self)

    monkeypatch.setattr(migration_guard._MigrationStallProbe, "report", _spy_report)

    with pytest.raises(RuntimeError, match="phase=assert coverage"):
        command.upgrade(_alembic_config(), REVISION)

    assert reported and reported[0] is not None, "the probe did not report on the failure path"
    with eng.connect() as conn:
        # Rolled back: nothing stamped, and the version pointer did not advance.
        assert mod.remaining_scan(conn, mod.SQL_PG.backfill_remaining)[0] == 2
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == (
            PREVIOUS_HEAD
        )
    migration_guard.migration_stall_probe.reset()
    eng.dispose()


# ---------------------------------------------------------------------------
# 10. Relation growth with both populations unchanged.
# ---------------------------------------------------------------------------


def _grow(conn, n, *, user_id):
    """Growth consisting ONLY of correctly stamped version-1 ended-visible rows.

    By construction in NEITHER population — which is exactly the shape Release A
    writes for the whole interval between sizing and deploy, and exactly why a guard
    that rechecks only the populations is blind to it.
    """
    for _ in range(n):
        _seed_pg_session(
            conn, uuid.uuid4(), status="ended", mode="normal", drill_state=None,
            version=1, accuracy=INTACT_ACCURACY, plies=INTACT_PLIES, user_id=user_id,
        )


@pg_gate_plugin.pg_gate
def test_pg_growth_in_neither_population_still_moves_the_growth_factors(
    pg_migration_db, monkeypatch
):
    """The dimension probe is what makes the recheck a recheck.

    Both populations stay BIT-FOR-BIT what they were while the relations grow, and
    the derived factors must move — otherwise "the runner rechecks the bound against
    the live populations" is a recheck of the two numbers that cannot have changed.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        _seed_stale(conn, 2, user_id=950_250)
        _seed_repair(conn, 1, user_id=950_251)

    clock = mod._RunClock()
    with eng.connect() as conn:
        conn.execute(text("ANALYZE game_sessions"))
        conn.execute(text("ANALYZE session_moves"))
        conn.commit()
        before = mod.probe_growth(conn, mod.SQL_PG, clock)
        n_stale_before = mod._count_population(
            conn, mod.SQL_PG.backfill_population_count, clock,
            lock_wait_ms=mod.ATOMIC_LOCK_WAIT_MS,
        )
        n_repair_before = mod._count_population(
            conn, mod.SQL_PG.repair_population_count, clock,
            lock_wait_ms=mod.ATOMIC_LOCK_WAIT_MS,
        )
        conn.rollback()

    # Shrink the frozen dimensions to this database's own size, so the factors are
    # 1.0 before the growth and measurable after it. (Patching the SIZED_* literals
    # is the only way to make a 5-row test database resemble a sized snapshot.)
    monkeypatch.setattr(mod, "SIZED_SESSIONS_BYTES", before[2]["sessions_bytes"])
    monkeypatch.setattr(mod, "SIZED_MOVES_BYTES", before[2]["moves_bytes"])
    monkeypatch.setattr(mod, "SIZED_TOTAL_ROWS", max(1, int(before[2]["total_rows"] or 1)))
    monkeypatch.setattr(mod, "SIZED_M_TOTAL", max(1, int(before[2]["m_total"] or 1)))

    # Growth big enough to move the factors well clear of 1.0, small enough that the
    # frozen bound still fits — the "growth is checked, not forbidden" half.
    with eng.begin() as conn:
        _grow(conn, 10, user_id=950_252)
        conn.execute(text("ANALYZE game_sessions"))
        conn.execute(text("ANALYZE session_moves"))

    with eng.connect() as conn:
        g_moves, g_sessions, dims = mod.probe_growth(conn, mod.SQL_PG, clock)
        n_stale_after = mod._count_population(
            conn, mod.SQL_PG.backfill_population_count, clock,
            lock_wait_ms=mod.ATOMIC_LOCK_WAIT_MS,
        )
        n_repair_after = mod._count_population(
            conn, mod.SQL_PG.repair_population_count, clock,
            lock_wait_ms=mod.ATOMIC_LOCK_WAIT_MS,
        )
        conn.rollback()

    assert (n_stale_after, n_repair_after) == (n_stale_before, n_repair_before)
    assert g_moves > 1.0, dims
    assert g_sessions > 1.0, dims

    # And those factors are what the two guards consume: the atomic projection
    # rejects, and — with no projection to help it — so does batch mode's runtime
    # scan-budget recheck, once the growth is large enough.
    monkeypatch.setenv(mod.ENV_MODE, "atomic")
    huge = 10_000.0
    with pytest.raises(mod.MigrationError, match="projected writer stall"):
        mod.bind_mode(
            "postgresql", n_stale=n_stale_after, n_repair=n_repair_after,
            g_moves=huge, g_sessions=huge,
        )
    with pytest.raises(mod.MigrationError, match="live scan budget"):
        mod.assert_runtime_scan_budget(g_moves=huge, g_sessions=1.0)
    with pytest.raises(mod.MigrationError, match="live scan budget"):
        mod.assert_runtime_scan_budget(g_moves=1.0, g_sessions=huge)
    # Growth within the margin still admits the run.
    mod.assert_runtime_scan_budget(g_moves=g_moves, g_sessions=g_sessions)
    assert mod.bind_mode(
        "postgresql", n_stale=n_stale_after, n_repair=n_repair_after,
        g_moves=g_moves, g_sessions=g_sessions,
    ) is mod.ATOMIC_ENV
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_a_cancelled_dimension_probe_arrives_as_the_frozen_template(
    pg_migration_db, monkeypatch
):
    """Armed means cancellable, and cancellable means it must be converted.

    ``probe_growth`` arms the probe like every other scan-bearing statement, so
    PostgreSQL can cancel it — and an unconverted 57014 leaves the operator reading
    ``psycopg2.errors.QueryCanceled`` with no phase, no elapsed and no deadline,
    from a statement that reads nothing but the catalog. The probe is O(1), so a
    cancellation here means the revision clock is already spent before the runner
    started, which is exactly what ``phase=validate`` says.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)

    # Slow at the SQL layer, under the real arm. One row out, so the uncorrelated
    # subquery sleeps once.
    slow = mod.SQL_PG._replace(
        dimension_probe=(
            f"SELECT d.*, (SELECT pg_sleep(2)) AS _slow "
            f"FROM ({mod.DIMENSION_PROBE_SQL}) d"
        )
    )
    monkeypatch.setattr(mod, "SCAN_STMT_TIMEOUT_MS", 300)

    with eng.connect() as conn:
        conn.commit()
        with pytest.raises(mod.DeadlineExceeded) as exc:
            mod.probe_growth(conn, slow, mod._RunClock())
        conn.rollback()

    message = str(exc.value)
    assert "phase=validate" in message
    assert "sqlstate=57014" in message
    assert "did not converge" in message
    eng.dispose()


# ---------------------------------------------------------------------------
# 11. Backfill interleavings, split by mode.
#
# Selection differs by mode, so one interleaving story cannot cover both. Atomic
# mode selects with a plain SELECT and takes its first row lock at the guarded
# UPDATE. Per-batch mode takes FOR NO KEY UPDATE at selection time and holds it
# until the batch commits — and the Release-A /moves hook needs that same lock. So
# a live writer cannot write and commit while a paused per-batch runner owns the
# row, and live-first interleaving is reachable only in atomic mode.
# ---------------------------------------------------------------------------


def _hook_write(eng, sid, *, accuracy, repair_grid=False):
    """What the guarded /moves hook does: take the row lock, then write version 1.

    ``repair_grid`` additionally inserts the missing ply, so the hook's recompute is
    over an INTACT grid — the case where both the stale and the fresh value are
    version 1 and only the re-read can tell them apart.
    """
    with eng.begin() as conn:
        conn.execute(
            text("SELECT id FROM game_sessions WHERE id = CAST(:i AS uuid) FOR NO KEY UPDATE")
            .bindparams(i=str(sid))
        )
        if repair_grid:
            conn.execute(
                text("DELETE FROM session_moves WHERE session_id = CAST(:i AS uuid)")
                .bindparams(i=str(sid))
            )
            for move_number, color, eval_cp in INTACT_PLIES:
                conn.execute(
                    text(
                        "INSERT INTO session_moves (session_id, move_number, color, move_san, "
                        "fen_after, eval_cp) VALUES (CAST(:s AS uuid), :m, :c, 'e4', 'fen', :e)"
                    ).bindparams(s=str(sid), m=move_number, c=color, e=eval_cp)
                )
        conn.execute(
            text(
                "UPDATE game_sessions SET player_accuracy = :a, "
                "player_accuracy_algo_version = 1 WHERE id = CAST(:i AS uuid)"
            ).bindparams(a=accuracy, i=str(sid))
        )


@pg_gate_plugin.pg_gate
def test_pg_atomic_backfill_yields_to_a_hook_that_wrote_first(
    pg_migration_db, monkeypatch
):
    """Atomic, live-hook first: the row drops out of ``RETURNING``.

    The runner loads an unlocked stale snapshot and pauses. The hook takes the row
    lock, stamps version 1 and commits. At READ COMMITTED the guarded UPDATE
    rechecks the stale-version predicate AFTER acquiring the row lock, so that id is
    ABSENT from ``RETURNING`` and the live value survives.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        [sid] = _seed_stale(conn, 1, user_id=950_260)

    hook_value = 61
    assert hook_value != INTACT_ACCURACY
    real_apply = mod._Runner._apply
    admitted: list[int] = []

    def _pause_then_apply(self, state, results):
        _hook_write(eng, sid, accuracy=hook_value)  # the hook wins the race
        stamped = real_apply(self, state, results)
        admitted.append(stamped)
        return stamped

    monkeypatch.setattr(mod._Runner, "_apply", _pause_then_apply)
    clock = mod._RunClock()
    with eng.begin() as conn:
        _run_phase(conn, "backfill", env=mod.ATOMIC_ENV, batch_size=10, clock=clock)

    assert admitted == [0], "the guarded predicate should have admitted no row"
    with eng.connect() as conn:
        assert _cached(conn, sid) == (hook_value, 1)
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_per_batch_selection_skips_a_row_a_hook_holds_and_the_hook_wins(
    pg_migration_db, monkeypatch
):
    """Per-batch, hook first: SKIP LOCKED demonstrably skips, and success comes from
    the REMAINING COUNT rather than from a zero-row pass.

    Already proven end to end by
    ``test_pg_skipped_rows_converge_across_passes_and_a_zero_row_pass_is_never_success``;
    this pins the SELECTION property on its own, so a regression that stopped
    skipping (or started blocking) fails here with a much smaller surface.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        ids = _seed_stale(conn, 3, user_id=950_270)
    held = ids[0]

    holder = eng.connect()
    try:
        holder.execute(
            text("SELECT id FROM game_sessions WHERE id = CAST(:i AS uuid) FOR NO KEY UPDATE")
            .bindparams(i=str(held))
        )
        with eng.connect() as conn:
            conn.commit()
            trans = conn.begin()
            state = mod._BatchState(deadline=time.monotonic() + 5)
            runner = mod._Runner(
                conn, mod.SQL_PG, clock=mod._RunClock(), env=mod.BATCH_ENV,
                batch_size=10, stall=None,
            )
            started = time.monotonic()
            page = runner._select_page(state, None)
            elapsed_ms = (time.monotonic() - started) * 1000
            trans.rollback()
        selected = {str(r.id) for r in page}
        assert str(held) not in selected, "SKIP LOCKED did not skip the held row"
        assert len(selected) == 2
        # It SKIPPED rather than waited: nowhere near the lock-wait cap.
        assert elapsed_ms < mod.BATCH_LOCK_WAIT_MS
    finally:
        holder.rollback()
        holder.close()
        eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_per_batch_migration_first_blocks_the_writer_until_the_batch_commits(
    pg_migration_db, monkeypatch
):
    """Per-batch, migration first: the writer BLOCKS, and the hook still wins after.

    The runner selects with ``FOR NO KEY UPDATE SKIP LOCKED`` and pauses before
    commit. A post-end writer for that session must not complete inside a bounded
    observation window; after the batch commits it completes promptly and its own
    value is the final state at version 1.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        [sid] = _seed_stale(conn, 1, user_id=950_280)

    writer_done = threading.Event()
    hook_value = 71
    assert hook_value != INTACT_ACCURACY
    start_writer = threading.Event()

    def _writer():
        start_writer.wait(30)
        _hook_write(eng, sid, accuracy=hook_value)
        writer_done.set()

    thread = threading.Thread(target=_writer)
    thread.start()

    real_apply = mod._Runner._apply

    def _hold_then_apply(self, state, results):
        stamped = real_apply(self, state, results)
        start_writer.set()
        # The batch has NOT committed yet; the writer must not get through.
        assert not writer_done.wait(1.0), "the writer completed while the batch held the lock"
        return stamped

    monkeypatch.setattr(mod._Runner, "_apply", _hold_then_apply)
    with eng.connect() as conn:
        conn.commit()
        _run_phase(conn, "backfill", env=mod.BATCH_ENV, batch_size=10)
    # The batch committed; the writer completes promptly.
    assert writer_done.wait(15), "the writer never completed after the batch committed"
    thread.join()

    with eng.connect() as conn:
        assert _cached(conn, sid) == (hook_value, 1)
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_atomic_repair_re_read_skips_a_grid_the_hook_already_repaired(
    pg_migration_db, monkeypatch
):
    """THE interleaving the stale-version guard cannot cover.

    Both the stale and the fresh value are version 1, so the version never advances
    and the guard is blind. Safety comes from the RE-READ alone: atomic selection
    takes no row lock, so the window between "the materialized candidate set says X
    is broken" and "the runner locks X" is genuinely open to a writer. The runner
    locks, re-reads in a FRESH statement (which under READ COMMITTED sees the state
    as of after the grant), sees an intact grid, and SKIPS the UPDATE.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        [sid] = _seed_repair(conn, 1, user_id=950_290)

    hook_value = 81
    assert hook_value not in (INTACT_ACCURACY, BROKEN_UNGUARDED_ACCURACY)
    real_repair_one = mod._Runner._repair_one
    nulled: list[int] = []

    def _hook_first_then_repair(self, state, candidate):
        # The candidate set already says "broken". The hook now inserts the missing
        # ply, recomputes a CORRECT accuracy over an intact grid, stamps version 1
        # and commits — all before the runner takes its lock.
        _hook_write(eng, candidate, accuracy=hook_value, repair_grid=True)
        rows = real_repair_one(self, state, candidate)
        nulled.append(rows)
        return rows

    monkeypatch.setattr(mod._Runner, "_repair_one", _hook_first_then_repair)
    with eng.begin() as conn:
        _run_phase(conn, "repair", env=mod.ATOMIC_ENV, batch_size=10)

    assert nulled == [0], "the repair UPDATE must have affected zero rows"
    with eng.connect() as conn:
        # The hook's CORRECT number, not NULL.
        assert _cached(conn, sid) == (hook_value, 1)
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_the_repair_nulls_a_broken_row_when_no_hook_intervenes(
    pg_migration_db, monkeypatch
):
    """The CONVERSE, and without it every assertion above would pass for a repair
    that never fires."""
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        [sid] = _seed_repair(conn, 1, user_id=950_300)
    with eng.begin() as conn:
        _run_phase(conn, "repair", env=mod.ATOMIC_ENV, batch_size=10)
    with eng.connect() as conn:
        assert _cached(conn, sid) == (None, 1)
    eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_per_batch_repair_selection_already_holds_the_lock_the_hook_needs(
    pg_migration_db, monkeypatch
):
    """Per-batch mode's repair race is NOT the atomic one, and it is easy to get wrong.

    ``REPAIR_SELECT_SQL`` also carries ``FOR NO KEY UPDATE ... SKIP LOCKED``, so a
    candidate is locked the moment it is SELECTED and the explicit per-session lock
    that follows is a no-op re-acquisition of a lock the runner already holds. The
    hook therefore cannot repair and commit between selection and that lock: it
    BLOCKS. Requiring the atomic ordering in both modes would be requiring an
    interleaving per-batch mode's own selection makes impossible, and a test written
    to it would pass only by never actually racing.
    """
    url = pg_migration_db
    eng = _at_previous_head(url, monkeypatch)
    with eng.begin() as conn:
        [sid] = _seed_repair(conn, 1, user_id=950_310)

    hook_done = threading.Event()
    hook_value = 91

    def _hook():
        _hook_write(eng, sid, accuracy=hook_value, repair_grid=True)
        hook_done.set()

    thread = threading.Thread(target=_hook)
    real_repair_one = mod._Runner._repair_one
    observed: list[bool] = []

    def _selection_holds_it(self, state, candidate):
        # Selection has already returned, so the runner holds the row lock. The hook
        # starts now and must NOT get through before the batch commits.
        thread.start()
        observed.append(hook_done.wait(1.0))
        return real_repair_one(self, state, candidate)

    monkeypatch.setattr(mod._Runner, "_repair_one", _selection_holds_it)
    with eng.connect() as conn:
        conn.commit()
        _run_phase(conn, "repair", env=mod.BATCH_ENV, batch_size=10)
    assert observed == [False], "the hook completed while the repair selection held the lock"
    assert hook_done.wait(15), "the hook never completed after the batch committed"
    thread.join()

    with eng.connect() as conn:
        # The hook arrived second and still wins: it recomputed over the repaired
        # grid and wrote its own correct value at version 1.
        assert _cached(conn, sid) == (hook_value, 1)
    eng.dispose()
