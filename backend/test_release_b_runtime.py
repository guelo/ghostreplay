"""Runtime-envelope tests for revision ``20260719_01`` (g-b-runtime-envelope).

The correctness core's own proofs live in ``test_release_b_migrations.py`` and the
PostgreSQL gate suites; this file gates the ENVELOPE around it — the one clock,
the arming rule, mode binding, the admission projection, the growth factors, the
compute watchdog, the retry contract and the stall-probe seam.

Two things about how these tests reach the code, both deliberate:

* **They call the real runner, not a re-implementation.** ``_Runner.run_phase`` is
  where the pass bound, the convergence-by-remaining-count rule, the arming and
  the watchdog live, so a test that looped over the pass functions itself would be
  asserting against a shape production never executes.
* **Constant patching means direct calls, never ``command.upgrade``.** Alembic
  loads every revision by executing the file fresh on each run
  (``spec.loader.exec_module``), so anything monkeypatched on the module object
  this file imported is discarded the moment ``command.upgrade()`` re-imports it.
  Tests that need a patched constant therefore drive the runner directly against a
  real connection; tests that need the full Alembic path (the ``env.py`` stall
  probe, the ``VALIDATE`` lock-timeout leak) do not patch constants. The same rule
  is why the Alembic-driven tests elsewhere assert on ``RuntimeError`` plus a
  message rather than on ``mod.MigrationError`` identity.
"""

from __future__ import annotations

import logging
import math
import pathlib
import signal
import threading
import time

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from app import migration_guard
from test_release_b_migrations import (
    BROKEN_PLIES,
    BROKEN_UNGUARDED_ACCURACY,
    INTACT_PLIES,
    _build_pre_b,
    _seed_session,
)

_BACKEND_DIR = pathlib.Path(__file__).resolve().parent

REVISION = "20260719_01"


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return cfg


mod = ScriptDirectory.from_config(_alembic_config()).get_revision(REVISION).module

MIGRATION_LOGGER = "alembic.runtime.migration"


class _Records(logging.Handler):
    """Capture the migration logger's records, attached DIRECTLY to it.

    Not ``caplog``: ``command.upgrade`` runs Alembic's ``fileConfig``, which
    reconfigures the ``alembic`` logger's handlers and propagation for the rest of
    the process — so whether a record reaches pytest's root handler depends on
    which tests ran before this one. Attaching to the logger the revision actually
    writes to removes that ordering dependency.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, level: int | None = None) -> list[str]:
        return [
            r.getMessage() for r in self.records if level is None or r.levelno == level
        ]

    def __enter__(self) -> _Records:
        self._logger = logging.getLogger(MIGRATION_LOGGER)
        self._previous_level = self._logger.level
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self)
        return self

    def __exit__(self, *exc) -> bool:
        self._logger.removeHandler(self)
        self._logger.setLevel(self._previous_level)
        return False


# ---------------------------------------------------------------------------
# The clock is revision-wide, and every deadline in force narrows the arm.
# ---------------------------------------------------------------------------


def test_clock_takes_every_deadline_in_force():
    clock = mod._RunClock(deadline_s=100)
    assert clock.deadlines() == [clock.revision_deadline]

    clock.atomic_deadline = clock.started + 5
    batch = clock.started + 1
    assert set(clock.deadlines(batch)) == {
        clock.revision_deadline,
        clock.atomic_deadline,
        batch,
    }
    # The remaining budget is the LEAST of them — here the batch's.
    assert clock.remaining_ms(batch) <= 1000


def test_arm_takes_the_minimum_of_the_cap_and_every_deadline(tmp_path):
    """One rule: the armed value is the least of every budget it spends from."""
    url = f"sqlite:///{tmp_path / 'arm.db'}"
    _build_pre_b(url)
    eng = create_engine(url)
    clock = mod._RunClock(deadline_s=100)
    with eng.connect() as conn:
        # The statement's own cap binds when it is the smallest.
        assert mod._arm(
            conn, clock, stmt_cap_ms=50, lock_wait_ms=1_000, phase="backfill"
        ) == 50
        # A batch remainder below the cap binds instead.
        armed = mod._arm(
            conn,
            clock,
            stmt_cap_ms=50_000,
            lock_wait_ms=1_000,
            phase="backfill",
            batch_deadline=time.monotonic() + 0.2,
        )
        assert 0 < armed <= 200
        # And so does the atomic residual budget.
        clock.atomic_deadline = time.monotonic() + 0.05
        armed = mod._arm(conn, clock, stmt_cap_ms=50_000, lock_wait_ms=1_000, phase="backfill")
        assert 0 < armed <= 50
    eng.dispose()


def test_arm_raises_the_exhaustion_template_when_the_budget_is_spent(tmp_path):
    url = f"sqlite:///{tmp_path / 'arm_spent.db'}"
    _build_pre_b(url)
    eng = create_engine(url)
    clock = mod._RunClock(deadline_s=100)
    clock.revision_deadline = time.monotonic() - 1  # already past
    with eng.connect() as conn:
        with pytest.raises(mod.DeadlineExceeded) as exc:
            mod._arm(conn, clock, stmt_cap_ms=500, lock_wait_ms=100, phase="assert")
    assert "phase=assert" in str(exc.value)
    eng.dispose()


def test_arm_is_a_no_op_on_sqlite_but_still_enforces_the_clock(tmp_path):
    """Rule 1's HARDNESS is PostgreSQL-only; rules 2 and 3 hold on both dialects.

    ``statement_timeout`` and ``lock_timeout`` are PostgreSQL GUCs, so a SQLite arm
    issues no ``set_config`` — but it still computes the remaining budget and still
    raises when it is spent, which is what leaves SQLite with best-effort
    between-statement enforcement rather than none.
    """
    url = f"sqlite:///{tmp_path / 'arm_sqlite.db'}"
    _build_pre_b(url)
    eng = create_engine(url)
    issued: list[str] = []
    clock = mod._RunClock(deadline_s=100)
    with eng.connect() as conn:
        from sqlalchemy import event

        @event.listens_for(conn, "before_cursor_execute")
        def _capture(c, cursor, statement, params, ctx, many):  # noqa: ANN001
            issued.append(statement)

        assert mod._arm(conn, clock, stmt_cap_ms=42, lock_wait_ms=1, phase="backfill") == 42
    assert issued == [], f"SQLite arm issued SQL: {issued}"
    eng.dispose()


# ---------------------------------------------------------------------------
# The frozen exhaustion template.
# ---------------------------------------------------------------------------


def test_exhaustion_template_carries_every_field_at_a_between_passes_failure():
    clock = mod._RunClock(deadline_s=900)
    msg = str(
        mod._exhausted(
            clock,
            "repair",
            remaining=7,
            passes=20,
            max_passes=20,
            first_remaining=["a", "b"],
        )
    )
    assert "phase=repair" in msg
    assert "remaining=7" in msg
    assert "passes=20/20" in msg
    assert "sqlstate=n/a" in msg
    assert "first_remaining=a,b" in msg
    assert "deadline=900s" in msg
    assert "elapsed=" in msg


@pytest.mark.parametrize("phase", ["validate", "backfill", "repair", "assert"])
def test_exhaustion_template_uses_explicit_n_a_where_a_field_cannot_exist(phase):
    """A mid-statement cancellation has NO result set to report.

    The transaction is aborted, so the same connection cannot run a diagnostic
    query until it rolls back — and re-scanning after the rollback would be the
    extra full detector scan the scan budget forbids. ``phase=validate`` and
    ``phase=assert`` are inherently the same shape: no candidate population, no
    cursor, no pass count.
    """
    msg = str(mod._exhausted(mod._RunClock(), phase, sqlstate="57014"))
    assert f"phase={phase}" in msg
    assert "remaining=n/a" in msg
    assert f"passes=n/a/{mod.MAX_PASSES}" in msg
    assert "first_remaining=n/a" in msg
    assert "sqlstate=57014" in msg


def test_a_cancellation_sqlstate_becomes_the_template_and_other_errors_do_not():
    """Only an ARMED-timeout breach is an exhaustion; everything else propagates."""
    clock = mod._RunClock()

    class _Orig:
        sqlstate = "57014"

    from sqlalchemy.exc import OperationalError

    cancelled = OperationalError("stmt", {}, _Orig())
    with pytest.raises(mod.DeadlineExceeded, match="sqlstate=57014"):
        with mod._as_exhaustion(clock, "backfill"):
            raise cancelled

    class _Other:
        sqlstate = "08006"  # connection_failure — NOT a deadline breach

    with pytest.raises(OperationalError):
        with mod._as_exhaustion(clock, "backfill"):
            raise OperationalError("stmt", {}, _Other())


def test_the_compute_watchdog_raise_becomes_the_template_with_no_sqlstate():
    """A Python raise has no SQLSTATE, and the template must say so rather than
    inventing one."""
    with pytest.raises(mod.DeadlineExceeded) as exc:
        with mod._as_exhaustion(mod._RunClock(), "backfill"):
            raise mod.ComputeWatchdogExceeded("boom")
    assert "sqlstate=n/a" in str(exc.value)
    assert "phase=backfill" in str(exc.value)


# ---------------------------------------------------------------------------
# Mode binding: parse on every dialect, apply per dialect and per population.
# ---------------------------------------------------------------------------

#: The batch size these tests hold FIXED when they are testing something else.
#: The default a deploy resolves to when nothing overrides it — so a test that
#: does not care about the sweep's page count still prices it at the shape
#: production runs, rather than at whichever end of the range happened to be
#: convenient. Tests that DO care pass their own.
_BATCH = mod.DEFAULT_BATCH_SIZE

# --- growth factors large enough to breach, DERIVED from the frozen constants ---
#
# Every one of these was a magic number until the 2026-07-27 re-freeze, and the
# re-freeze is what showed why that was wrong. They were chosen against constants
# roughly 3x larger (MARGINED_MS_PER_SCAN_STMT was 521, now 171), so when the
# constants shrank the same factors stopped breaching anything and three "the guard
# rejects this" tests failed — which is the LUCKY direction. Had they been a little
# larger they would have kept passing while testing nothing, and a guard whose
# rejection path is never exercised is indistinguishable from one that cannot
# reject. Derived from the term each test is about, so they track a re-freeze
# instead of quietly decoupling from it.
#
# Each is a SUFFICIENT factor, not the smallest one: the named term alone exceeds
# the bound at this factor, so every other term in the projection can only add to
# the breach. The tests assert admission at g = 1 beside it, which is what keeps
# "sufficient" from degenerating into "trivially large".

#: One term — ATOMIC_SCANS_UNDER_LOCK session_moves scans — over MAX_WRITER_STALL_MS.
_G_MOVES_BREACHING_THE_STALL = float(
    math.ceil(
        mod.MAX_WRITER_STALL_MS / (mod.ATOMIC_SCANS_UNDER_LOCK * mod.MARGINED_MS_PER_SCAN_STMT)
    )
    + 1
)

#: The scan BUDGET's session_moves term — (2 * MAX_PASSES + 2) scans — over the
#: revision deadline. A different bound from the stall, so a different factor.
_G_MOVES_BREACHING_THE_BUDGET = float(
    math.ceil(
        mod.REVISION_DEADLINE_S
        * 1000
        / ((2 * mod.MAX_PASSES + 2) * mod.MARGINED_MS_PER_SCAN_STMT)
    )
    + 1
)

#: The budget's game_sessions side, through the sweep's RELATION component — the
#: term that scales with g_sessions and that the session_moves factors never touch.
_G_SESSIONS_BREACHING_THE_BUDGET = float(
    math.ceil(
        mod.REVISION_DEADLINE_S * 1000 / (mod.MAX_PASSES * mod.MARGINED_MS_BACKFILL_SWEEP_SCAN)
    )
    + 1
)


def _scan_budget(*, g_moves, g_sessions, n_stale=0, batch_size=_BATCH):
    return mod.assert_runtime_scan_budget(
        n_stale=n_stale, batch_size=batch_size, g_moves=g_moves, g_sessions=g_sessions
    )


def test_unset_mode_parses_to_none_rather_than_to_atomic(monkeypatch):
    """"Unset" is not a mode. Turning it into one here is exactly how a deployment
    error becomes a silent atomic run."""
    monkeypatch.delenv(mod.ENV_MODE, raising=False)
    assert mod.resolve_mode() is None


def test_sqlite_needs_no_mode_and_refuses_batch(monkeypatch):
    monkeypatch.delenv(mod.ENV_MODE, raising=False)
    assert mod.bind_mode(
        "sqlite", n_stale=9, n_repair=1, batch_size=_BATCH, g_moves=1.0, g_sessions=1.0
    ) is mod.ATOMIC_ENV
    monkeypatch.setenv(mod.ENV_MODE, "atomic")
    assert mod.bind_mode(
        "sqlite", n_stale=9, n_repair=1, batch_size=_BATCH, g_moves=1.0, g_sessions=1.0
    ) is mod.ATOMIC_ENV
    monkeypatch.setenv(mod.ENV_MODE, "batch")
    with pytest.raises(mod.MigrationError, match="unsupported on sqlite"):
        mod.bind_mode(
            "sqlite", n_stale=9, n_repair=1, batch_size=_BATCH, g_moves=1.0, g_sessions=1.0
        )


def test_postgres_requires_the_mode_when_either_population_is_nonzero(monkeypatch):
    monkeypatch.delenv(mod.ENV_MODE, raising=False)
    # Both zero: a fresh database, a disposable migration database and the shared
    # fixture all upgrade with no configuration.
    assert mod.bind_mode(
        "postgresql", n_stale=0, n_repair=0, batch_size=_BATCH, g_moves=1.0, g_sessions=1.0
    ) is mod.ATOMIC_ENV
    for n_stale, n_repair in [(1, 0), (0, 1), (5, 5)]:
        with pytest.raises(mod.MigrationError) as exc:
            mod.bind_mode(
                "postgresql",
                n_stale=n_stale,
                n_repair=n_repair,
                batch_size=_BATCH,
                g_moves=1.0,
                g_sessions=1.0,
            )
        assert mod.ENV_MODE in str(exc.value)
        assert mod.RUNBOOK in str(exc.value)
        assert "never a silent atomic run" in str(exc.value)


def test_batch_mode_needs_no_stall_projection(monkeypatch):
    """No batch holds a row lock across another batch, and no set-wide scan holds
    one at all — so a population the atomic projection rejects still runs."""
    monkeypatch.setenv(mod.ENV_MODE, "batch")
    env = mod.bind_mode(
        "postgresql",
        n_stale=10_000_000,
        n_repair=10_000_000,
        batch_size=_BATCH,
        g_moves=50.0,
        g_sessions=50.0,
    )
    assert env is mod.BATCH_ENV
    assert env.per_batch and env.locked_selection
    assert env.max_passes == mod.MAX_PASSES


# --- the admission projection, term by term --------------------------------


def _admit(monkeypatch, **kw):
    monkeypatch.setenv(mod.ENV_MODE, "atomic")
    kw.setdefault("batch_size", _BATCH)
    return mod.bind_mode("postgresql", **kw)


def test_atomic_is_admitted_at_the_sized_shape(monkeypatch):
    """The sizing run's own populations, which the runbook records as admissible."""
    assert _admit(
        monkeypatch, n_stale=3_000, n_repair=3_000, g_moves=1.0, g_sessions=1.0
    ) is mod.ATOMIC_ENV


def test_atomic_projection_charges_the_repair_term(monkeypatch):
    with pytest.raises(mod.MigrationError, match="projected writer stall"):
        _admit(monkeypatch, n_stale=0, n_repair=1_000_000, g_moves=1.0, g_sessions=1.0)


def test_atomic_projection_charges_the_session_moves_scan_terms(monkeypatch):
    """The clean-audit shape a population-scaled formula wrongly admits.

    Small ``N_stale``, ``N_repair = 0``, huge ``session_moves``. A formula without
    the scan terms scores this at ``N_stale * MARGINED_MS_PER_ROW`` — five
    milliseconds — and admits an atomic run whose real stall is that plus three
    full ``session_moves`` scans.
    """
    naive = 1 * mod.MARGINED_MS_PER_ROW
    assert naive < mod.MAX_WRITER_STALL_MS
    with pytest.raises(mod.MigrationError, match="g_moves"):
        _admit(monkeypatch, n_stale=1, n_repair=0, g_moves=_G_MOVES_BREACHING_THE_STALL, g_sessions=1.0)


def test_atomic_projection_charges_the_backfills_own_game_sessions_terms(monkeypatch):
    """Small populations, small ``session_moves``, LARGE ``game_sessions``.

    The backfill's unindexed selection sweep and its convergence count must walk
    the whole of ``game_sessions`` under the backfill's held row locks. A formula
    that prices only the ``session_moves`` scans scores this as nearly free.
    """
    without_backfill_terms = (
        1 * mod.MARGINED_MS_PER_ROW
        + mod.ATOMIC_SCANS_UNDER_LOCK * mod.MARGINED_MS_PER_SCAN_STMT
        + mod.MARGINED_MS_COVERAGE_ASSERT * 600.0
        + mod.atomic_teardown_reserve_ms(n_stale=1, n_repair=0)
    )
    assert without_backfill_terms < mod.MAX_WRITER_STALL_MS
    with pytest.raises(mod.MigrationError, match="g_sessions"):
        _admit(monkeypatch, n_stale=1, n_repair=0, g_moves=1.0, g_sessions=600.0)


def test_atomic_projection_charges_the_teardown_terms(monkeypatch):
    """The stall ends when COMMIT RETURNS, not when the last assertion does.

    A formula that stops at the last assertion scores a huge one-shot commit as
    free, so the teardown terms have to be able to be the terms that BIND. At the
    frozen constants they never are — the measured marginal commit cost is
    0.002 ms/row against a 2 ms/row repair-mutation cost, three orders of magnitude
    apart — so the per-row teardown constant is patched to a value where it binds.
    This is a test of the FORMULA, not of the frozen numbers: what it pins is that
    the teardown terms are present and can reject a run, which is exactly what a
    formula stopping at the last assertion could not do.
    """
    monkeypatch.setattr(mod, "MARGINED_US_ATOMIC_TEARDOWN_PER_ROW", 20_000)  # 20ms/row
    n = 1_400
    without_teardown = (
        n * mod.MARGINED_MS_PER_REPAIR_ROW
        + mod.ATOMIC_SCANS_UNDER_LOCK * mod.MARGINED_MS_PER_SCAN_STMT
        + mod.MARGINED_MS_COVERAGE_ASSERT
        + mod.backfill_sweep_ms(pages=mod.backfill_sweep_pages(n_stale=0, batch_size=_BATCH))
        + mod.MARGINED_MS_BACKFILL_REMAINING
    )
    assert without_teardown < mod.MAX_WRITER_STALL_MS
    projected = mod.project_atomic_stall_ms(n_stale=0, n_repair=n, batch_size=_BATCH)
    assert projected > mod.MAX_WRITER_STALL_MS
    assert projected - without_teardown == pytest.approx(
        mod.atomic_teardown_reserve_ms(n_stale=0, n_repair=n)
    )
    with pytest.raises(mod.MigrationError, match="projected writer stall"):
        _admit(monkeypatch, n_stale=0, n_repair=n, g_moves=1.0, g_sessions=1.0)


def test_atomic_is_inadmissible_when_the_teardown_reserve_leaves_no_work_budget(monkeypatch):
    """Admission additionally requires ``ATOMIC_WORK_BUDGET_MS > 0``.

    A population whose projected teardown ALONE exceeds the stall bound cannot be
    held to a residual, because there is no residual — and the diagnostic has to
    name that rather than reporting a stall projection an operator would try to
    shave.
    """
    monkeypatch.setattr(
        mod, "MARGINED_MS_ATOMIC_TEARDOWN_FIXED", mod.MAX_WRITER_STALL_MS + 1
    )
    with pytest.raises(mod.MigrationError, match="no residual work budget"):
        _admit(monkeypatch, n_stale=1, n_repair=0, g_moves=1.0, g_sessions=1.0)


def test_atomic_projection_charges_the_growth_factors_alone(monkeypatch):
    """Populations BIT-FOR-BIT unchanged from an admitting sizing, relations grown.

    A correctly stamped version-1 session is in NEITHER population yet adds rows
    and pages to both relations, and Release A writes nothing else for the whole
    interval between sizing and deploy. So this is the case a guard that rechecks
    only the populations cannot see.
    """
    admitted = dict(n_stale=3_000, n_repair=3_000)
    assert _admit(monkeypatch, g_moves=1.0, g_sessions=1.0, **admitted) is mod.ATOMIC_ENV
    with pytest.raises(mod.MigrationError, match="projected writer stall"):
        _admit(
            monkeypatch,
            g_moves=_G_MOVES_BREACHING_THE_STALL,
            g_sessions=_G_MOVES_BREACHING_THE_STALL,
            **admitted,
        )


def test_atomic_projection_admits_growth_within_the_margin(monkeypatch):
    """The guard rejects a projection that no longer fits, not growth itself."""
    assert _admit(
        monkeypatch, n_stale=100, n_repair=100, g_moves=1.5, g_sessions=1.5
    ) is mod.ATOMIC_ENV


# --- the sweep's page count, which is the operator's to move ----------------


def test_atomic_projection_moves_with_the_resolved_batch_size():
    """The assertion that fails against a scalar sweep constant.

    Same population, same relations, same everything — only the batch size
    differs, and the sweep is ``ceil(N_stale / batch_size) + 1`` pages. A
    projection that could not see it charged one number for 7 pages and for
    6,001, and its verdicts were a property of the relation size rather than of
    the check.
    """
    n_stale = 6_000
    at_min = mod.project_atomic_stall_ms(
        n_stale=n_stale, n_repair=0, batch_size=mod.MIN_ADMITTED_BATCH
    )
    at_max = mod.project_atomic_stall_ms(
        n_stale=n_stale, n_repair=0, batch_size=mod.MAX_BATCH_SIZE
    )
    assert at_min > at_max
    # And the difference is EXACTLY the per-page term over the extra pages: the
    # relation-scan component does not move with the batch size, and the row work
    # and teardown do not either.
    extra_pages = mod.backfill_sweep_pages(
        n_stale=n_stale, batch_size=mod.MIN_ADMITTED_BATCH
    ) - mod.backfill_sweep_pages(n_stale=n_stale, batch_size=mod.MAX_BATCH_SIZE)
    assert at_min - at_max == pytest.approx(
        mod.BACKFILL_SELECT_SWEEPS_UNDER_LOCK
        * mod.MARGINED_US_BACKFILL_SWEEP_PER_PAGE
        * extra_pages
        / 1000
    )


@pytest.mark.parametrize("n_stale", [0, 1, mod.SIZED_TOTAL_ROWS])
def test_atomic_projection_charges_a_nonzero_sweep_at_every_boundary(n_stale):
    """``N_stale = 0`` is ONE page — the empty-teardown shape — not zero pages.

    A run with nothing to backfill still issues the first selection page, gets
    nothing back and stops. Pricing that at zero would be the same class of error
    as dropping the scan terms on a clean audit.
    """
    for batch_size in (mod.MIN_ADMITTED_BATCH, mod.DEFAULT_BATCH_SIZE):
        pages = mod.backfill_sweep_pages(n_stale=n_stale, batch_size=batch_size)
        assert pages >= 1
        assert mod.backfill_sweep_ms(pages=pages) > 0
        naked = mod.project_atomic_stall_ms(
            n_stale=n_stale, n_repair=0, batch_size=batch_size
        ) - mod.BACKFILL_SELECT_SWEEPS_UNDER_LOCK * mod.backfill_sweep_ms(pages=pages)
        assert naked < mod.project_atomic_stall_ms(
            n_stale=n_stale, n_repair=0, batch_size=batch_size
        )


def test_atomic_admission_rejects_at_the_batch_size_the_scalar_admitted(monkeypatch):
    """A population inside the false-admission band, from both ends.

    Admitted at ``MAX_BATCH_SIZE`` and refused at ``MIN_ADMITTED_BATCH`` — the
    same database, the same relations, one environment variable apart. The
    refusal has to name the batch size and the page count, because "lower your
    batch size" is the advice an operator reaching for this knob would otherwise
    take from a stall message.
    """
    monkeypatch.setenv(mod.ENV_MODE, "atomic")
    admitted = mod.bind_mode(
        "postgresql",
        n_stale=5_400,
        n_repair=0,
        batch_size=mod.MAX_BATCH_SIZE,
        g_moves=1.0,
        g_sessions=1.0,
    )
    assert admitted is mod.ATOMIC_ENV
    with pytest.raises(mod.MigrationError) as exc:
        mod.bind_mode(
            "postgresql",
            n_stale=5_400,
            n_repair=0,
            batch_size=mod.MIN_ADMITTED_BATCH,
            g_moves=1.0,
            g_sessions=1.0,
        )
    assert f"batch_size={mod.MIN_ADMITTED_BATCH}" in str(exc.value)
    assert f"pages={mod.backfill_sweep_pages(n_stale=5_400, batch_size=1)}" in str(exc.value)


def test_runtime_scan_budget_moves_with_batch_size_and_live_population():
    """The leak batch mode has no stall projection to catch.

    The import-time check prices the sweep at the DECLARED worst case — the sized
    relation at the smallest admitted batch. A live population past that basis,
    combined with a small override, is past the declaration, and the runtime check
    is the only thing that sees it.
    """
    # Comfortable at the default batch, at a population well past the sized basis.
    _scan_budget(n_stale=200_000, batch_size=mod.DEFAULT_BATCH_SIZE, g_moves=1.0, g_sessions=1.0)
    with pytest.raises(mod.MigrationError, match="sweep_pages"):
        _scan_budget(
            n_stale=200_000,
            batch_size=mod.MIN_ADMITTED_BATCH,
            g_moves=1.0,
            g_sessions=1.0,
        )


def test_stall_probe_projection_matches_the_admission_projection(monkeypatch):
    """The duplicate projection is now a duplicate of something with five inputs.

    ``stall_for`` re-computes what ``bind_mode`` already projected and hands it to
    the shipped stall probe as ``projected_stall_ms``. Leaving it on a signature
    that could not see the batch size would have the probe classify the observed
    stall against a projection the admission check never made — a report about a
    configuration that did not run.
    """
    monkeypatch.setenv(mod.ENV_MODE, "atomic")
    args = dict(n_stale=2_000, n_repair=300, g_moves=1.3, g_sessions=1.7)
    for batch_size in (mod.MIN_ADMITTED_BATCH, 25, mod.MAX_BATCH_SIZE):
        env = mod.bind_mode("postgresql", batch_size=batch_size, **args)
        stall = mod.stall_for(
            mod.SQL_PG, mod._RunClock(), env=env, batch_size=batch_size, **args
        )
        assert stall is not None
        assert stall.projected_ms == mod.project_atomic_stall_ms(
            batch_size=batch_size, **args
        )
    # And the two ends really are different numbers, or the assertion above would
    # hold for a stall_for that ignored its batch size entirely.
    assert mod.project_atomic_stall_ms(
        batch_size=mod.MIN_ADMITTED_BATCH, **args
    ) != mod.project_atomic_stall_ms(batch_size=mod.MAX_BATCH_SIZE, **args)


def test_teardown_reserve_divides_the_per_row_term_by_one_thousand():
    """The per-row constant is in MICROSECONDS on purpose.

    Rounding a sub-millisecond marginal commit cost up to 1 ms/row would add a
    phantom second of projected stall per thousand rows. A future "tidy-up" into
    milliseconds has to fail here rather than silently inflating every atomic
    projection by three orders of magnitude.
    """
    reserve = mod.atomic_teardown_reserve_ms(n_stale=1_000, n_repair=0)
    assert reserve == pytest.approx(
        mod.MARGINED_MS_ATOMIC_TEARDOWN_FIXED
        + 1_000 * mod.MARGINED_US_ATOMIC_TEARDOWN_PER_ROW / 1000
    )
    # And it is never zero: an atomic transaction that mutated nothing still commits.
    assert mod.atomic_teardown_reserve_ms(n_stale=0, n_repair=0) == (
        mod.MARGINED_MS_ATOMIC_TEARDOWN_FIXED
    )


# ---------------------------------------------------------------------------
# Growth factors and the runtime scan-budget recheck.
# ---------------------------------------------------------------------------


def test_growth_factor_is_the_maximum_of_both_ratios_and_clamps_at_one():
    """A shrunk relation earns no discount: the constants were measured at the
    sized dimensions and a smaller relation cannot make a frozen measurement
    smaller than it was measured to be."""
    assert mod._growth_factor(
        live_bytes=200, live_rows=100, sized_bytes=100, sized_rows=100, relation="r"
    ) == 2.0
    assert mod._growth_factor(
        live_bytes=100, live_rows=300, sized_bytes=100, sized_rows=100, relation="r"
    ) == 3.0
    assert mod._growth_factor(
        live_bytes=10, live_rows=10, sized_bytes=100, sized_rows=100, relation="r"
    ) == 1.0


def test_growth_factor_falls_back_to_bytes_when_the_relation_was_never_analyzed():
    """``reltuples = -1`` means the row estimate is meaningless, and the fallback is
    LOGGED rather than silently taken."""
    with _Records() as records:
        g = mod._growth_factor(
            live_bytes=400, live_rows=-1, sized_bytes=100, sized_rows=100, relation="session_moves"
        )
    assert g == 4.0
    assert any("no usable reltuples" in m for m in records.messages())


def test_runtime_scan_budget_raises_when_the_relations_outgrew_their_sizing():
    """Batch mode has no stall projection, so this is the check that catches it."""
    _scan_budget(g_moves=1.0, g_sessions=1.0)  # the frozen shape fits
    with pytest.raises(mod.MigrationError, match="live scan budget"):
        _scan_budget(g_moves=_G_MOVES_BREACHING_THE_BUDGET, g_sessions=1.0)
    with pytest.raises(mod.MigrationError, match="live scan budget"):
        # game_sessions ALONE — the relation the session_moves terms never touch.
        _scan_budget(g_moves=1.0, g_sessions=_G_SESSIONS_BREACHING_THE_BUDGET)


# ---------------------------------------------------------------------------
# The compute watchdog.
# ---------------------------------------------------------------------------


def test_watchdog_restores_the_previous_handler_and_pending_timer():
    """A raise mid-compute must not leave a stray timer armed for the next batch."""

    def _sentinel(signum, frame):  # pragma: no cover - never delivered
        raise AssertionError("sentinel handler fired")

    previous = signal.signal(signal.SIGALRM, _sentinel)
    try:
        with mod._ComputeWatchdog() as watchdog:
            assert watchdog.armed
            assert signal.getsignal(signal.SIGALRM) is not _sentinel
            watchdog.arm(50)
        assert signal.getsignal(signal.SIGALRM) is _sentinel
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)

        # And on the RAISE path too.
        with pytest.raises(RuntimeError):
            with mod._ComputeWatchdog() as watchdog:
                watchdog.arm(50)
                raise RuntimeError("boom")
        assert signal.getsignal(signal.SIGALRM) is _sentinel
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    finally:
        signal.signal(signal.SIGALRM, previous)


def test_watchdog_arms_per_session_to_the_least_of_the_ceiling_and_the_remainder():
    """Re-armed PER SESSION, not once around the loop.

    With the batch remainder already below ``MAX_SINGLE_SESSION_COMPUTE_MS`` the
    armed interval equals the REMAINDER — which is what lets ``MAX_BATCH_MS`` be a
    batch-wide bound over Python as well as SQL, and lets the compute term drop out
    of ``EST_MAX_LOCK_HOLD_MS``.
    """
    with mod._ComputeWatchdog() as watchdog:
        watchdog.arm(10_000)
        assert watchdog.last_armed_ms == mod.MAX_SINGLE_SESSION_COMPUTE_MS
        watchdog.arm(3)
        assert watchdog.last_armed_ms == 3
        watchdog.disarm()


def test_watchdog_fires_on_a_single_slow_session(monkeypatch):
    """A slow single SESSION, not a slow loop: the pre-session deadline check
    cannot see this, which is the whole reason the watchdog exists."""
    monkeypatch.setattr(mod, "MAX_SINGLE_SESSION_COMPUTE_MS", 5)
    with mod._ComputeWatchdog() as watchdog:
        watchdog.arm(10_000)
        with pytest.raises(mod.ComputeWatchdogExceeded):
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                pass
        watchdog.disarm()


def test_watchdog_logs_that_it_is_unarmed_off_the_main_thread(caplog):
    """``setitimer`` delivers only to the main thread, and the runner LOGS that
    rather than claiming enforcement it does not have."""
    seen: dict = {}

    def _run():
        with caplog.at_level(logging.INFO, logger="alembic.runtime.migration"):
            with mod._ComputeWatchdog() as watchdog:
                watchdog.arm(1)
                seen["armed"] = watchdog.armed

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join()
    assert seen["armed"] is False
    assert any("compute watchdog UNARMED" in r.message % r.args for r in caplog.records)


# ---------------------------------------------------------------------------
# The retry contract.
# ---------------------------------------------------------------------------


def test_backoff_schedule_is_the_frozen_one(monkeypatch):
    """0.5, 1, 2, 4, 5, 5 … No jitter; there is one runner."""
    slept: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", slept.append)
    runner = mod._Runner(
        None,
        mod.SQL_SQLITE,
        clock=mod._RunClock(deadline_s=900),
        env=mod.BATCH_ENV,
        batch_size=10,
        stall=None,
    )
    for k in range(1, 7):
        runner._backoff("backfill", k, remaining=1, sample=["x"], max_passes=20)
    assert slept == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]


def test_backoff_raises_rather_than_sleeping_past_the_deadline(monkeypatch):
    """A migration that naps through its own deadline reports the wrong cause."""
    slept: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", slept.append)
    clock = mod._RunClock(deadline_s=900)
    clock.revision_deadline = time.monotonic() + 0.1  # less than the first backoff
    runner = mod._Runner(
        None, mod.SQL_SQLITE, clock=clock, env=mod.BATCH_ENV, batch_size=10, stall=None
    )
    with pytest.raises(mod.DeadlineExceeded) as exc:
        runner._backoff("repair", 1, remaining=3, sample=["a"], max_passes=20)
    assert slept == []
    assert "remaining=3" in str(exc.value)
    assert "passes=1/20" in str(exc.value)
    assert "first_remaining=a" in str(exc.value)


def test_pass_bounds_are_per_mode():
    """Per-batch mode needs real retries because SKIP LOCKED transiently skips
    rows; atomic mode takes no SKIP LOCKED, so one sweep drains everything its
    selection can see and extra passes are not covered by the projection."""
    assert mod.BATCH_ENV.max_passes == mod.MAX_PASSES
    assert mod.ATOMIC_ENV.max_passes == 1
    assert mod.ATOMIC_MAX_PASSES == 1
    assert mod.BACKFILL_MAX_PASSES <= mod.MAX_PASSES
    assert mod.REPAIR_MAX_PASSES <= mod.MAX_PASSES


def test_atomic_mode_raises_rather_than_backing_off_into_an_unadmitted_pass(tmp_path):
    """A nonzero remaining count after atomic pass 1 is not "retry".

    For the repair it means a writer is producing broken grids WITH a non-NULL
    accuracy while the guard is supposedly live — the live-bug case. Either way the
    extra passes were never admitted by ``ATOMIC_SCANS_UNDER_LOCK`` /
    ``BACKFILL_*_UNDER_LOCK``, so the run raises with the frozen template.
    """
    url = f"sqlite:///{tmp_path / 'atomic_one_pass.db'}"
    _build_pre_b(url)
    eng = create_engine(url)
    with eng.begin() as conn:
        _seed_session(
            conn,
            "s-broken-stamped",
            plies=BROKEN_PLIES,
            accuracy=BROKEN_UNGUARDED_ACCURACY,
            version=1,
        )

    clock = mod._RunClock()
    with eng.begin() as conn:
        runner = mod._Runner(
            conn,
            mod.SQL_SQLITE,
            clock=clock,
            env=mod.ATOMIC_ENV,
            batch_size=10,
            stall=None,
        )
        # Make the repair unable to converge: the per-candidate update is a no-op,
        # so the fresh convergence count still sees the row. This is the shape a
        # live guard bug produces, and atomic mode must refuse it in one pass
        # rather than back off.
        runner.bundle = mod.SQL_SQLITE._replace(
            repair_update="/* ghostreplay:repair_update */ UPDATE game_sessions "
            "SET player_accuracy = player_accuracy WHERE id = :sid AND 1 = 0"
        )
        with mod._ComputeWatchdog() as watchdog:
            with pytest.raises(mod.DeadlineExceeded) as exc:
                runner.run_phase("repair", watchdog)
    assert "phase=repair" in str(exc.value)
    assert "passes=1/1" in str(exc.value)
    assert "remaining=1" in str(exc.value)
    assert "first_remaining=s-broken-stamped" in str(exc.value)
    eng.dispose()


def test_convergence_reads_the_count_and_the_sample_from_one_scan(tmp_path):
    """``remaining``, ``passes`` and ``first_remaining`` all come from the SAME scan.

    Fetching the sample with a separate ``SELECT ... LIMIT 20`` would be a second
    full detector scan, breaking one-scan-per-pass and the import scan budget — so
    the statement returns both, and ZERO ROWS means zero remaining.
    """
    url = f"sqlite:///{tmp_path / 'remaining.db'}"
    _build_pre_b(url)
    eng = create_engine(url)
    with eng.begin() as conn:
        for n in range(25):
            _seed_session(conn, f"s-{n:03d}", plies=INTACT_PLIES)
    with eng.connect() as conn:
        count, sample = mod.remaining_scan(conn, mod.BACKFILL_REMAINING_SQL)
        assert count == 25
        assert len(sample) == mod.REMAINING_SAMPLE_LIMIT
        assert sample == sorted(sample)  # ORDER BY id, so the sample is stable
        assert mod.remaining_scan(conn, mod.REPAIR_REMAINING_SQL) == (0, [])
    eng.dispose()


# ---------------------------------------------------------------------------
# The revision-wide deadline covers what a runner clock cannot.
# ---------------------------------------------------------------------------


def test_a_budget_that_expires_before_the_assertions_raises_phase_assert(tmp_path):
    """The case a runner-scoped clock CANNOT fail: the runner has already returned.

    Both closing assertions are armed with ``min(SCAN_STMT_TIMEOUT_MS, remaining)``
    and check the clock first, so a spent budget raises ``phase=assert`` rather
    than scanning past the deadline.
    """
    url = f"sqlite:///{tmp_path / 'assert_deadline.db'}"
    _build_pre_b(url)
    eng = create_engine(url)
    clock = mod._RunClock(deadline_s=900)
    clock.revision_deadline = time.monotonic() - 0.001
    with eng.connect() as conn:
        with pytest.raises(mod.DeadlineExceeded) as exc:
            mod._assert_fail_closed(conn, mod.SQL_SQLITE, clock, mod.ATOMIC_ENV)
    assert "phase=assert" in str(exc.value)
    eng.dispose()


def test_a_budget_that_expires_in_the_backfill_leaves_none_for_the_repair(tmp_path):
    """One clock, not a fresh one per phase."""
    url = f"sqlite:///{tmp_path / 'phase_deadline.db'}"
    _build_pre_b(url)
    eng = create_engine(url)
    with eng.begin() as conn:
        _seed_session(conn, "s-1", plies=INTACT_PLIES)
    clock = mod._RunClock(deadline_s=900)
    with eng.begin() as conn:
        runner = mod._Runner(
            conn, mod.SQL_SQLITE, clock=clock, env=mod.ATOMIC_ENV, batch_size=10, stall=None
        )
        with mod._ComputeWatchdog() as watchdog:
            runner.run_phase("backfill", watchdog)
            # The backfill spent the budget; the repair does not get a new one.
            clock.revision_deadline = time.monotonic() - 0.001
            with pytest.raises(mod.DeadlineExceeded) as exc:
                runner.run_phase("repair", watchdog)
    assert "phase=repair" in str(exc.value)
    eng.dispose()


# ---------------------------------------------------------------------------
# Atomic mode's residual stall deadline and the SHIPPED stall probe.
# ---------------------------------------------------------------------------


def test_the_stall_anchor_arms_once_and_records_on_the_shipped_singleton():
    """FIRST-LOCK-WINS, and the recorder is ``app.migration_guard``'s singleton.

    Recording on a fresh object while ``env.py`` reported from the shipped one
    would measure nothing, which is why there is no
    ``app/migration_stall_probe.py`` and no test that references one.
    """
    migration_guard.migration_stall_probe.reset()
    clock = mod._RunClock(deadline_s=900)
    projected = mod.project_atomic_stall_ms(n_stale=10, n_repair=10, batch_size=_BATCH)
    stall = mod._AtomicStall(clock, n_stale=10, n_repair=10, projected_ms=projected)
    assert clock.atomic_deadline is None

    stall.arm()
    first_anchor = stall.armed_at
    first_deadline = clock.atomic_deadline
    assert first_deadline is not None
    reserve = mod.atomic_teardown_reserve_ms(n_stale=10, n_repair=10)
    assert stall.work_budget_ms == pytest.approx(mod.MAX_WRITER_STALL_MS - reserve)
    # The residual budget, not the whole stall bound: teardown is RESERVED because
    # COMMIT/ROLLBACK are not covered by statement_timeout and the revision does
    # not even execute them.
    assert stall.work_budget_ms < mod.MAX_WRITER_STALL_MS

    time.sleep(0.01)
    stall.arm()  # a later lock must NOT move the anchor
    assert stall.armed_at == first_anchor
    assert clock.atomic_deadline == first_deadline

    probe = migration_guard.migration_stall_probe
    assert probe._first_row_lock_at == first_anchor
    assert probe._max_stall_ms == mod.MAX_WRITER_STALL_MS
    assert probe._projected_stall_ms == pytest.approx(projected)
    probe.reset()


def test_the_probe_logs_error_only_when_the_threshold_is_breached():
    """The classification the extension exists for — and it NEVER raises.

    The event that ends the measurement is the same event that releases the lock,
    so there is nothing left to prevent: evidence, not enforcement.
    """
    probe = migration_guard._MigrationStallProbe()
    probe.record_first_row_lock(
        time.monotonic() - 1.0, max_stall_ms=10.0, projected_stall_ms=5.0
    )
    with _Records() as records:
        probe.report()
    errors = records.messages(logging.ERROR)
    assert errors and "BREACHED" in errors[0]
    assert "observed_atomic_stall_ms=" in errors[0]
    assert "projected_stall_ms=5.0" in errors[0]

    # Consumed and cleared: the next run in the same process starts clean.
    with _Records() as records:
        probe.report()
    assert records.records == []

    # Within the threshold it is INFO, with the projection alongside on every run.
    probe.record_first_row_lock(
        time.monotonic(), max_stall_ms=30_000.0, projected_stall_ms=12.5
    )
    with _Records() as records:
        probe.report()
    assert records.messages(logging.ERROR) == []
    assert "projected_stall_ms=12.5" in records.messages(logging.INFO)[0]


def test_the_probe_stays_info_only_for_a_bare_timestamp():
    """The backward-compatibility seam: every other revision records only a
    timestamp and must keep its existing INFO-only behaviour."""
    probe = migration_guard._MigrationStallProbe()
    probe.record_first_row_lock(time.monotonic() - 5.0)
    with _Records() as records:
        probe.report()
    assert [r.levelno for r in records.records] == [logging.INFO]
    assert "projected_stall_ms=n/a" in records.messages()[0]
    assert "max_stall_ms=n/a" in records.messages()[0]


def test_no_separate_stall_probe_module_exists():
    """An earlier draft named ``app/migration_stall_probe.py``. It is wrong: the
    revision records on the singleton ``env.py`` already reports from."""
    assert not (_BACKEND_DIR / "app" / "migration_stall_probe.py").exists()
    assert hasattr(migration_guard, "migration_stall_probe")


def test_per_batch_mode_has_no_stall_anchor_and_atomic_mode_has_no_tripwire():
    """Two different mechanisms, each only where it can actually work.

    Per-batch mode owns its transactions, so it gets the after-the-fact lock-hold
    tripwire and needs no single-hold budget. Atomic mode's hold ends at a commit
    the revision never observes, so it gets the residual budget and NO tripwire —
    and the tripwire helper must be a no-op there rather than a fiction.
    """
    atomic = mod._Runner(
        None,
        mod.SQL_PG,
        clock=mod._RunClock(),
        env=mod.ATOMIC_ENV,
        batch_size=10,
        stall=None,
    )
    state = mod._BatchState(deadline=None)
    state.lock_hold_ms = None  # atomic mode never measures a hold
    atomic._tripwire("backfill", state)  # no raise, by construction

    batch = mod._Runner(
        None, mod.SQL_PG, clock=mod._RunClock(), env=mod.BATCH_ENV, batch_size=10, stall=None
    )
    breached = mod._BatchState(deadline=None)
    breached.rows = ["r1", "r2"]
    breached.lock_hold_ms = mod.EST_MAX_LOCK_HOLD_MS + 1
    with pytest.raises(mod.MigrationError, match="observed lock hold"):
        batch._tripwire("backfill", breached)
    within = mod._BatchState(deadline=None)
    within.lock_hold_ms = mod.EST_MAX_LOCK_HOLD_MS
    batch._tripwire("backfill", within)  # at the estimate is not over it


# ---------------------------------------------------------------------------
# Mode envelopes, structurally.
# ---------------------------------------------------------------------------


def test_atomic_mode_arms_no_statement_cap_that_could_bind_before_the_deadlines():
    """In atomic mode the armed value must EQUAL the remaining budget.

    A generic ``MAX_BATCH_MS`` cap would bind whenever more than five seconds of
    budget remained, and the atomic arming proof asserts the armed value tracks the
    residual stall budget rather than a fresh anything.
    """
    assert mod.ATOMIC_ENV.stmt_cap_ms == mod.REVISION_DEADLINE_S * 1000
    assert mod.BATCH_ENV.stmt_cap_ms == mod.MAX_BATCH_MS
    assert mod.ATOMIC_ENV.lock_wait_ms == mod.ATOMIC_LOCK_WAIT_MS
    assert mod.BATCH_ENV.lock_wait_ms == mod.BATCH_LOCK_WAIT_MS


def test_the_validate_lock_timeout_string_and_ms_form_agree():
    """One value, two renderings: ``set_config`` takes the string, ``_arm`` clamps
    the milliseconds."""
    assert mod.VALIDATE_LOCK_TIMEOUT == f"{mod.VALIDATE_LOCK_WAIT_MS // 1000}s"
    assert mod.VALIDATE_LOCK_WAIT_MS > mod.ATOMIC_LOCK_WAIT_MS


def test_the_runner_labels_itself_with_the_guards_reserved_name():
    """``RUNNER_APP_NAME`` is frozen in ``app.migration_guard`` so the runner and
    the cancellation probe agree on the string without either restating it."""
    source = (
        _BACKEND_DIR
        / "alembic"
        / "versions"
        / "20260719_01_backfill_session_player_accuracy.py"
    ).read_text()
    assert "RUNNER_APP_NAME" in source
    assert migration_guard.RUNNER_APP_NAME == "ghostreplay_accuracy_backfill"
    assert '"ghostreplay_accuracy_backfill"' not in source


def test_the_dimension_probe_reads_the_catalog_and_never_counts_rows():
    """``count(*)`` would add a full relation scan to every execution purely to
    price another one."""
    assert "pg_total_relation_size" in mod.DIMENSION_PROBE_SQL
    assert "reltuples" in mod.DIMENSION_PROBE_SQL
    assert "count(*)" not in mod.DIMENSION_PROBE_SQL
    assert mod.SQL_SQLITE.dimension_probe is None


def test_a_cancelled_dimension_probe_ARM_also_reaches_the_template(monkeypatch):
    """Re-arming is itself a cancellable statement, so it belongs inside the wrapper.

    ``_arm`` issues two ``set_config`` statements on PostgreSQL, and they run under
    whatever ``statement_timeout`` the PREVIOUS arm left in force in the same
    transaction — which ``_arm`` computes as ``min(cap, every remaining)`` and can
    legitimately be a single millisecond once the budget is nearly spent. An arm
    outside the exhaustion wrapper therefore leaks the same raw ``DBAPIError`` the
    wrapper exists to convert, one statement earlier than the query.

    Fabricated rather than provoked: cancelling a ``set_config`` for real needs the
    armed value to land in the sub-millisecond window where that statement is slow
    enough to trip, which is a race, not a test. What must hold is the conversion,
    and that is exact.
    """
    from sqlalchemy.exc import OperationalError

    class _Cancelled:
        sqlstate = "57014"

    def raising_arm(*_a, **_kw):
        raise OperationalError("SELECT set_config(...)", {}, _Cancelled())

    monkeypatch.setattr(mod, "_arm", raising_arm)
    with pytest.raises(mod.DeadlineExceeded) as exc:
        # conn is never touched: the arm raises before the probe is issued.
        mod.probe_growth(None, mod.SQL_PG, mod._RunClock())
    assert "phase=validate" in str(exc.value)
    assert "sqlstate=57014" in str(exc.value)


def test_growth_factors_are_one_off_postgresql(tmp_path):
    """No catalog to probe and no live writer to protect."""
    url = f"sqlite:///{tmp_path / 'nogrowth.db'}"
    _build_pre_b(url)
    eng = create_engine(url)
    with eng.connect() as conn:
        assert mod.probe_growth(conn, mod.SQL_SQLITE, mod._RunClock()) == (1.0, 1.0, {})
    eng.dispose()


def test_batch_size_override_is_bounded_by_what_sizing_demonstrated(monkeypatch):
    """An unbounded override would defeat the per-batch deadline by admitting a
    batch that cannot finish inside it."""
    monkeypatch.setenv(mod.ENV_BATCH, str(mod.MAX_BATCH_SIZE))
    assert mod.resolve_batch_size() == mod.MAX_BATCH_SIZE
    monkeypatch.setenv(mod.ENV_BATCH, str(mod.MAX_BATCH_SIZE + 1))
    with pytest.raises(mod.MigrationError, match=f"1..{mod.MAX_BATCH_SIZE}"):
        mod.resolve_batch_size()


def test_sqlite_upgrade_records_no_stall_and_takes_no_row_lock(tmp_path, monkeypatch):
    """SQLite is atomic-only and single-writer: there is no hold to observe.

    A NONEMPTY upgrade, which is the case that can go wrong: the empty path skips
    the runner and could not anchor a stall if it tried. Two things must hold.

    The probe must never be anchored. SQLite takes no row lock the residual budget
    can bound — its write lock is database-wide and held by the transaction
    regardless of what this revision arms — and a SQLite upgrade has no concurrent
    writer to protect, so an ``observed_atomic_stall_ms`` from here would be a
    stall measurement of a writer that does not exist.

    And the assertion has to be a SPY, not the singleton's residual state:
    ``env.py`` calls ``report()`` at the end of every upgrade, which CONSUMES and
    CLEARS the anchor. Asserting ``_first_row_lock_at is None`` afterwards passes
    whether or not the revision anchored it, so it is not an assertion about this
    at all.
    """
    from alembic import command

    url = f"sqlite:///{tmp_path / 'sqlite_no_stall.db'}"
    _build_pre_b(url)
    with create_engine(url).begin() as conn:
        _seed_session(conn, "s-1", plies=INTACT_PLIES)
        _seed_session(conn, "s-2", plies=BROKEN_PLIES, accuracy=BROKEN_UNGUARDED_ACCURACY)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv(mod.ENV_MODE, raising=False)
    migration_guard.migration_stall_probe.reset()

    anchored: list[tuple] = []
    real = migration_guard._MigrationStallProbe.record_first_row_lock

    def spy(self, ts, **kw):
        anchored.append((ts, kw))
        return real(self, ts, **kw)

    # Patched on the probe's CLASS, not on the revision module: Alembic re-imports
    # the revision file on every upgrade, but the revision resolves the probe through
    # ``app.migration_guard`` at call time and ``sys.modules`` is shared, so the
    # spy survives the re-import. And on the CLASS rather than on the singleton:
    # patching the instance leaves the original bound method behind as an instance
    # attribute when monkeypatch undoes it, shadowing the class for the rest of the
    # process (``__slots__`` now rejects it outright — see
    # test_migration_guard.py::test_the_stall_probe_singleton_cannot_be_shadowed_per_instance).
    monkeypatch.setattr(
        migration_guard._MigrationStallProbe, "record_first_row_lock", spy
    )

    cfg = _alembic_config()
    command.stamp(cfg, "20260718_01")
    command.upgrade(cfg, REVISION)

    eng = create_engine(url)
    with eng.connect() as conn:
        # The run really did work — otherwise "no stall was recorded" is vacuous.
        assert conn.execute(
            text("SELECT player_accuracy_algo_version FROM game_sessions WHERE id = 's-1'")
        ).scalar() == 1
        assert mod.remaining_scan(conn, mod.SQL_SQLITE.backfill_remaining) == (0, [])
    eng.dispose()
    assert anchored == [], f"a SQLite upgrade anchored the stall probe: {anchored}"


def test_the_residual_stall_budget_needs_both_atomic_mode_and_postgresql():
    """The gate itself, on both axes.

    ``_AtomicStall.arm()`` sets ``clock.atomic_deadline``, and ``_arm`` takes the
    minimum over every deadline in force — so constructing one on SQLite would put
    a nonempty SQLite upgrade under a ~30-second ceiling derived from PostgreSQL
    writer-stall measurements, on a database with no writer to protect, able to
    fail an upgrade that was doing nothing wrong.

    ``bind_mode`` puts SQLite on the atomic side of the mode split, so
    ``not env.per_batch`` is TRUE there: it cannot be the whole condition.
    """
    assert not mod.bind_mode(
        "sqlite", n_stale=1, n_repair=1, batch_size=_BATCH, g_moves=1.0, g_sessions=1.0
    ).per_batch

    def stall(bundle, env):
        return mod.stall_for(
            bundle,
            mod._RunClock(),
            env=env,
            n_stale=1,
            n_repair=1,
            batch_size=_BATCH,
            g_moves=1.0,
            g_sessions=1.0,
        )

    assert stall(mod.SQL_SQLITE, mod.ATOMIC_ENV) is None
    assert stall(mod.SQL_PG, mod.BATCH_ENV) is None
    assert isinstance(stall(mod.SQL_PG, mod.ATOMIC_ENV), mod._AtomicStall)


def test_the_stall_gate_leaves_the_sqlite_clock_with_one_deadline(tmp_path):
    """And the consequence, end to end on a real nonempty SQLite run."""
    url = f"sqlite:///{tmp_path / 'sqlite_no_residual.db'}"
    _build_pre_b(url)
    eng = create_engine(url)
    with eng.begin() as conn:
        _seed_session(conn, "s-1", plies=INTACT_PLIES)
    with eng.connect() as conn:
        clock = mod._RunClock()
        env = mod.bind_mode(
            "sqlite", n_stale=1, n_repair=0, batch_size=_BATCH, g_moves=1.0, g_sessions=1.0
        )
        with mod._ComputeWatchdog() as watchdog:
            mod._Runner(
                conn,
                mod.SQL_SQLITE,
                clock=clock,
                env=env,
                batch_size=500,
                stall=mod.stall_for(
                    mod.SQL_SQLITE,
                    clock,
                    env=env,
                    n_stale=1,
                    n_repair=0,
                    batch_size=_BATCH,
                    g_moves=1.0,
                    g_sessions=1.0,
                ),
            ).run_phase("backfill", watchdog)
        conn.commit()
        assert clock.atomic_deadline is None
        assert clock.deadlines() == [clock.revision_deadline]
    eng.dispose()


def test_sqlite_rechecks_the_clock_before_every_single_row_update(tmp_path):
    """SQLite arms nothing in the database, so BETWEEN statements is all there is.

    The SQLite branch of ``_apply`` issues one UPDATE per session — up to
    ``batch_size`` of them. A single check before the loop would gate the first
    write and leave the rest unchecked, so a batch could keep writing for as long
    as the loop takes after the revision deadline has passed. The check has to be
    per statement, and the proof is that an exhaustion raised on the second check
    leaves the FIRST row stamped and the rest not.
    """
    url = f"sqlite:///{tmp_path / 'sqlite_per_update.db'}"
    _build_pre_b(url)
    eng = create_engine(url)
    ids = [f"s-{i}" for i in range(4)]
    with eng.begin() as conn:
        for sid in ids:
            _seed_session(conn, sid, plies=INTACT_PLIES)

    with eng.connect() as conn:
        clock = mod._RunClock()
        runner = mod._Runner(
            conn,
            mod.SQL_SQLITE,
            clock=clock,
            env=mod.ATOMIC_ENV,
            batch_size=500,
            stall=None,
        )
        state = mod._BatchState(None)
        results = [(sid, 55.0) for sid in ids]

        # _apply is called DIRECTLY, so every _arm inside it comes from the update
        # loop and the ordinal is unambiguous. The second one expires the revision
        # deadline before delegating, so _arm itself raises — the same way it would
        # if the loop had simply run long.
        real_arm = mod._arm
        calls = {"n": 0}

        def arm(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                clock.revision_deadline = time.monotonic() - 1.0
            return real_arm(*a, **kw)

        mod._arm = arm
        try:
            with pytest.raises(mod.DeadlineExceeded) as exc:
                runner._apply(state, results)
        finally:
            mod._arm = real_arm
        conn.commit()

    assert "phase=backfill" in str(exc.value)
    assert calls["n"] == 2, "the loop kept going after the budget was spent"
    with eng.connect() as conn:
        stamped = conn.execute(
            text("SELECT count(*) FROM game_sessions WHERE player_accuracy_algo_version = 1")
        ).scalar()
    assert stamped == 1, f"expected exactly the first row to be stamped, got {stamped}"
    eng.dispose()
