"""Constant and derivation tests for Release B's sizing (g-b-size-derive).

Revision ``20260719_01``'s admission constants are frozen literals produced by a
measured run. This file gates them. It proves three different kinds of thing, and
the difference matters:

**Arithmetic over frozen literals.** ``EST_MAX_LOCK_HOLD_MS <=
MAX_WRITER_STALL_MS``, ``BATCH_LOCK_WAIT_MS < MAX_BATCH_MS``, the scan budget,
the zero-batch boundary. These prove the constants are internally consistent and
nothing more — a comparison of literals cannot enforce a production observation,
and no green run of this file is evidence that a row lock is never held for 30
seconds. What backs the estimates at run time is g-b-runtime-envelope's compute
watchdog, its armed SQL timeouts, and its observed-lock-hold tripwire.

**Structure of the derivation.** The harness's Phase 2 arithmetic is exercised
directly on synthetic measurement dictionaries, because the failure modes it has
to survive — a population that is zero, a scan term that a population-scaled
model would lose, a µs constant "tidied" into ms, a batch size the formula
admitted but no run ever demonstrated — are cheap to construct and impossible to
observe from a single real sizing run.

**Provenance.** A constant whose measured input was never recorded is
unfalsifiable, so the runbook is parsed and required to carry the numbers the
constants were frozen from.
"""

from __future__ import annotations

import ast
import json
import math
import pathlib
import re
from decimal import Decimal
from fractions import Fraction

import pg_gate_plugin
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from scripts import phase3_cancellation_probe as probe
from scripts import phase3_fixture_guard as probe_guard
from scripts import size_accuracy_backfill as harness

_BACKEND_DIR = pathlib.Path(__file__).resolve().parent
_RUNBOOK = _BACKEND_DIR.parent / "docs" / "release_b_runbook.md"

REVISION = "20260719_01"


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return cfg


mod = ScriptDirectory.from_config(_alembic_config()).get_revision(REVISION).module


# ---------------------------------------------------------------------------
# The frozen literals exist and are internally consistent.
# ---------------------------------------------------------------------------

MEASURED_CONSTANTS = (
    "MARGINED_MS_PER_ROW",
    "MARGINED_MS_PER_REPAIR_ROW",
    "MARGINED_MS_PER_SCAN_STMT",
    "MARGINED_MS_COVERAGE_ASSERT",
    # The backfill's OWN game_sessions work. The convergence count is measured
    # directly as of g-b-size-derive-backfill-terms and gated against the committed
    # artifacts by test_frozen_backfill_remaining_covers_every_committed_measurement
    # — which is where its tightness lives, since it covers its worst measurement
    # with no integer headroom. The scan budget and the atomic projection both
    # charge it, and a zero would price a growing relation at nothing.
    "MARGINED_MS_BACKFILL_REMAINING",
    # The selection sweep, as TWO constants rather than one scalar: a relation
    # component in ms and a per-page component in µs. There is no scalar here on
    # purpose — a scalar is what something would go on to price a sweep with.
    "MARGINED_MS_BACKFILL_SWEEP_SCAN",
    "MARGINED_US_BACKFILL_SWEEP_PER_PAGE",
    "SCAN_STMT_TIMEOUT_MS",
    "MAX_SINGLE_SESSION_COMPUTE_MS",
    "TEARDOWN_ALLOWANCE_MS",
    "MARGINED_MS_ATOMIC_TEARDOWN_FIXED",
    "MARGINED_US_ATOMIC_TEARDOWN_PER_ROW",
    "B_TESTED",
    "R_TESTED",
)

SIZED_DIMENSIONS = (
    "SIZED_TOTAL_ROWS",
    "SIZED_SESSIONS_BYTES",
    "SIZED_M_TOTAL",
    "SIZED_MOVES_BYTES",
)


@pytest.mark.parametrize("name", MEASURED_CONSTANTS)
def test_measured_constants_are_declared_and_positive(name):
    value = getattr(mod, name)
    assert isinstance(value, int) and value > 0, f"{name} must be a positive int, got {value!r}"


@pytest.mark.parametrize("name", SIZED_DIMENSIONS)
def test_sized_relation_dimensions_are_declared_and_positive(name):
    """The dimensions the two scan constants were measured against.

    Without them the scan constants are unfalsifiable at run time: nothing
    downstream can tell whether the relations they price still exist at that
    size, and g-b-runtime-envelope's growth factors have nothing to divide by.
    A dimension recorded only in the runbook cannot be divided by anything.
    """
    value = getattr(mod, name)
    assert isinstance(value, int) and value > 0, f"{name} must be a positive int, got {value!r}"


def test_batch_lock_wait_is_strictly_below_the_batch_deadline():
    """Otherwise a lock wait could only ever surface as a statement timeout.

    At or above MAX_BATCH_MS the statement deadline always fires first, so
    BATCH_LOCK_WAIT_MS would never be the reason anything was cancelled and the
    setting would be decorative.
    """
    assert mod.BATCH_LOCK_WAIT_MS < mod.MAX_BATCH_MS


def test_atomic_lock_wait_equals_the_batch_lock_wait():
    assert mod.ATOMIC_LOCK_WAIT_MS == mod.BATCH_LOCK_WAIT_MS


def test_est_max_lock_hold_fits_the_writer_stall_bound():
    """Proves the ARITHMETIC OF THE ADMISSION ESTIMATE and nothing else.

    ``EST_MAX_LOCK_HOLD_MS`` is a margined empirical estimate of one per-batch
    batch's lock hold, not a hard bound. This assertion is a comparison of frozen
    literals: it cannot enforce a production observation, and a green suite here
    is NOT a proof that a row lock is never held longer than
    ``MAX_WRITER_STALL_MS``. It also says nothing about atomic mode, whose single
    lock hold is bounded by the admission projection and the residual stall
    deadline rather than by this term.
    """
    assert mod.EST_MAX_LOCK_HOLD_MS == mod.MAX_BATCH_MS + mod.TEARDOWN_ALLOWANCE_MS
    assert mod.EST_MAX_LOCK_HOLD_MS <= mod.MAX_WRITER_STALL_MS


def test_est_max_lock_hold_has_no_single_session_compute_addend():
    """Adding the per-session ceiling here would DOUBLE-COUNT it.

    ``MAX_BATCH_MS`` is batch-wide over SQL *and* Python, because the compute
    watchdog is armed to ``min(MAX_SINGLE_SESSION_COMPUTE_MS, batch remaining,
    revision remaining, atomic remaining)`` — so no session's compute can push the
    batch past its deadline, and the compute is already inside ``MAX_BATCH_MS``.
    ``MAX_SINGLE_SESSION_COMPUTE_MS`` survives only as that watchdog ceiling.
    """
    assert mod.MAX_SINGLE_SESSION_COMPUTE_MS > 0  # still declared, still armed
    assert (
        mod.EST_MAX_LOCK_HOLD_MS
        != mod.MAX_BATCH_MS + mod.MAX_SINGLE_SESSION_COMPUTE_MS + mod.TEARDOWN_ALLOWANCE_MS
    )


def test_scan_stmt_timeout_covers_the_most_expensive_scan_it_is_armed_on():
    assert mod.SCAN_STMT_TIMEOUT_MS >= max(
        mod.MARGINED_MS_PER_SCAN_STMT,
        mod.MARGINED_MS_COVERAGE_ASSERT,
        mod.MARGINED_MS_BACKFILL_REMAINING,
    )


def test_scan_stmt_timeout_must_cover_the_backfill_convergence_scan_specifically():
    """The backfill convergence scan, pinned on its own.

    The two convergence scans are priced by DIFFERENT terms, and only one of them
    needed adding to the maximum. ``REPAIR_REMAINING_SQL`` wraps the ply detector,
    so it scans ``session_moves`` and is already one of the four complete
    statements behind ``MARGINED_MS_PER_SCAN_STMT`` (scaled by ``G_moves``).
    ``BACKFILL_REMAINING_SQL`` scans ``game_sessions`` on an unindexed predicate,
    so it is priced by ``MARGINED_MS_BACKFILL_REMAINING`` and scales by
    ``G_sessions`` — by NEITHER population, which is what lets relation growth
    alone push it past the other two terms. Both go through
    ``_Runner._arm_scan``, so ``SCAN_STMT_TIMEOUT_MS`` is the cap armed on both,
    and a value below the backfill term arms that scan with less time than it
    measurably needs. The resulting 57014 arrives through the exhaustion template
    as ``did not converge`` — a self-inflicted cancellation misreported as the
    migration's own non-convergence.
    """
    inflated_remaining = mod.SCAN_STMT_TIMEOUT_MS + 1
    assert not (
        mod.SCAN_STMT_TIMEOUT_MS
        >= max(
            mod.MARGINED_MS_PER_SCAN_STMT,
            mod.MARGINED_MS_COVERAGE_ASSERT,
            inflated_remaining,
        )
    )


def test_derived_scan_stmt_timeout_covers_the_backfill_convergence_scan():
    """The DERIVATION, not just the frozen constant.

    The frozen numbers happen to satisfy the invariant above, so the invariant
    test passes whether or not ``derive()`` charges the backfill convergence scan.
    This drives the harness with a measurement where that scan is the most
    expensive armed statement and asserts the emitted constant covers it.
    """
    slow = _measurement()
    slow["scans"]["backfill_remaining"]["max_ms"] = 4000.0
    derived = harness.derive([slow] + _complete()[1:], None)["constants"]
    assert derived["MARGINED_MS_BACKFILL_REMAINING"] > derived["MARGINED_MS_PER_SCAN_STMT"]
    assert derived["SCAN_STMT_TIMEOUT_MS"] >= derived["MARGINED_MS_BACKFILL_REMAINING"]


def test_scan_stmt_timeout_must_cover_the_coverage_assertion_specifically():
    """The coverage side, pinned on its own.

    ``SCAN_STMT_TIMEOUT_MS`` is armed on ``COVERAGE_ASSERT_SQL`` too, and that
    statement scans a different relation from the other four. A timeout that
    covered ``MARGINED_MS_PER_SCAN_STMT`` but sat below
    ``MARGINED_MS_COVERAGE_ASSERT`` would arm the coverage assertion with less
    time than its own measured cost — a self-inflicted cancellation, on the one
    statement whose failure is read as "the cache must not serve".
    """
    inflated_coverage = mod.SCAN_STMT_TIMEOUT_MS + 1
    assert not (
        mod.SCAN_STMT_TIMEOUT_MS >= max(mod.MARGINED_MS_PER_SCAN_STMT, inflated_coverage)
    )


def _budget(**kw):
    """The import-time shape: the declared worst-case page count unless overridden."""
    kw.setdefault("pages", mod.IMPORT_WORST_CASE_SWEEP_PAGES)
    return mod._scan_budget_ms(
        mod.MARGINED_MS_PER_SCAN_STMT, mod.MARGINED_MS_COVERAGE_ASSERT, **kw
    )


def test_scan_budget_fits_the_revision_deadline():
    """Every scan-bearing statement a run can issue, charged against one clock.

    Checked at IMPORT in the revision, so a ``session_moves`` large enough that
    the scans alone cannot fit the wall clock fails when the revision loads
    instead of exhausting ``MAX_PASSES`` and raising a misleading
    non-convergence error 900 seconds later. The sweep is charged at the DECLARED
    worst case — the whole sized relation stale at the smallest admitted batch —
    because module load has no population and no resolved batch size.
    """
    budget = _budget()
    assert budget == (
        (2 * mod.MAX_PASSES + 2) * mod.MARGINED_MS_PER_SCAN_STMT
        + mod.MARGINED_MS_COVERAGE_ASSERT
        + mod.MAX_PASSES
        * (
            mod.backfill_sweep_ms(pages=mod.IMPORT_WORST_CASE_SWEEP_PAGES)
            + mod.MARGINED_MS_BACKFILL_REMAINING
        )
    )
    assert budget < mod.REVISION_DEADLINE_S * 1000


def test_scan_budget_charges_the_backfills_own_game_sessions_work():
    """The last group is MANDATORY, and a budget without it is an underestimate.

    The backfill's keyset SELECTION SWEEP and ``BACKFILL_REMAINING_SQL`` both
    filter ``game_sessions`` on ``player_accuracy_algo_version IS NULL OR < 1``,
    which NO index covers (``app/models.py:188``, ``app/models.py:224``). So each
    is O(G_sessions) and not O(N_stale) — every version-1 row Release A stamps
    between sizing and deploy grows the scanned relation while shrinking the
    population — and per-batch mode can run up to ``MAX_PASSES`` backfill passes.
    A budget that counted only the ``session_moves`` detectors and the coverage
    assertion priced that relation-scaled work at zero.
    """
    with_terms = _budget()
    without_terms = _budget(
        margined_ms_backfill_sweep_scan=0,
        margined_us_backfill_sweep_per_page=0,
        margined_ms_backfill_remaining=0,
    )
    assert with_terms > without_terms
    assert with_terms - without_terms == mod.MAX_PASSES * (
        mod.backfill_sweep_ms(pages=mod.IMPORT_WORST_CASE_SWEEP_PAGES)
        + mod.MARGINED_MS_BACKFILL_REMAINING
    )


def test_scan_budget_moves_with_the_page_count():
    """The defect this bead exists to fix, at the budget end.

    Same constants, same relations, same populations — only the batch size
    differs, and it moves the sweep's page count by a factor of
    ``MAX_BATCH_SIZE``. A budget that could not see that charged the same number
    for 7 pages and for 6,001.
    """
    n_stale = mod.SIZED_TOTAL_ROWS
    at_min = _budget(
        pages=mod.backfill_sweep_pages(n_stale=n_stale, batch_size=mod.MIN_ADMITTED_BATCH)
    )
    at_max = _budget(
        pages=mod.backfill_sweep_pages(n_stale=n_stale, batch_size=mod.MAX_BATCH_SIZE)
    )
    assert at_min > at_max
    assert at_min - at_max == pytest.approx(
        mod.MAX_PASSES
        * mod.MARGINED_US_BACKFILL_SWEEP_PER_PAGE
        / 1000
        * (
            mod.backfill_sweep_pages(n_stale=n_stale, batch_size=mod.MIN_ADMITTED_BATCH)
            - mod.backfill_sweep_pages(n_stale=n_stale, batch_size=mod.MAX_BATCH_SIZE)
        )
    )


def test_scan_budget_scales_each_relations_terms_by_its_own_growth_factor():
    """``session_moves`` terms by ``G_moves``, ``game_sessions`` terms by
    ``G_sessions`` — and, inside the sweep, the SCAN component ONLY.

    Growing only one relation must move only that relation's terms. A budget that
    scaled the ``session_moves`` detectors and left the backfill's own
    ``game_sessions`` sweep at its frozen value would be blind to exactly the
    growth that a correctly-stamped version-1 row produces.

    The load-bearing half is the second assertion: ``g_sessions`` reaches the
    sweep's relation-scan component and STOPS. The per-page term is statement
    startup, which a larger relation does not make more expensive, so a future
    "simplification" that multiplies the whole sweep by ``g_sessions`` fails here
    — asserted as an exact difference rather than an inequality, because the
    over-charging version is also strictly larger and an inequality would pass.
    """
    base = _budget()
    moves_only = _budget(g_moves=2.0)
    sessions_only = _budget(g_sessions=2.0)
    assert moves_only - base == (2 * mod.MAX_PASSES + 2) * mod.MARGINED_MS_PER_SCAN_STMT
    assert sessions_only - base == pytest.approx(
        mod.MARGINED_MS_COVERAGE_ASSERT
        + mod.MAX_PASSES
        * (mod.MARGINED_MS_BACKFILL_SWEEP_SCAN + mod.MARGINED_MS_BACKFILL_REMAINING)
    )
    # And explicitly NOT the whole sweep: doubling g_sessions must not double the
    # per-page contribution.
    doubled_whole_sweep = (
        mod.MARGINED_US_BACKFILL_SWEEP_PER_PAGE * mod.IMPORT_WORST_CASE_SWEEP_PAGES / 1000
    )
    assert sessions_only - base != pytest.approx(
        mod.MARGINED_MS_COVERAGE_ASSERT
        + mod.MAX_PASSES
        * (
            mod.MARGINED_MS_BACKFILL_SWEEP_SCAN
            + doubled_whole_sweep
            + mod.MARGINED_MS_BACKFILL_REMAINING
        )
    )


# ---------------------------------------------------------------------------
# The sweep model: the page formula, the µs unit, and the frozen pair against
# the evidence it was fitted to.
# ---------------------------------------------------------------------------


def test_sweep_pages_formula():
    """``ceil(n_stale / batch_size) + 1``, with the two boundaries that bite.

    The ``+1`` is the empty page that terminates the sweep. ``n_stale = 0`` is ONE
    page and never zero — a run with nothing to backfill still issues the first
    page, gets nothing back and stops — and the import-time worst case is the whole
    sized relation at the smallest admitted batch.
    """
    assert mod.backfill_sweep_pages(n_stale=0, batch_size=mod.MAX_BATCH_SIZE) == 1
    assert mod.backfill_sweep_pages(n_stale=0, batch_size=mod.MIN_ADMITTED_BATCH) == 1
    assert mod.backfill_sweep_pages(n_stale=1, batch_size=mod.MAX_BATCH_SIZE) == 2
    assert mod.backfill_sweep_pages(n_stale=1, batch_size=mod.MIN_ADMITTED_BATCH) == 2
    assert mod.backfill_sweep_pages(n_stale=999, batch_size=500) == 3
    assert mod.backfill_sweep_pages(n_stale=1000, batch_size=500) == 3
    assert mod.backfill_sweep_pages(n_stale=1001, batch_size=500) == 4
    assert (
        mod.backfill_sweep_pages(
            n_stale=mod.SIZED_TOTAL_ROWS, batch_size=mod.MIN_ADMITTED_BATCH
        )
        == mod.SIZED_TOTAL_ROWS + 1
        == mod.IMPORT_WORST_CASE_SWEEP_PAGES
    )


def test_sweep_per_page_term_is_denominated_in_microseconds():
    """The µs analogue of the teardown-per-row unit pin.

    The measured slope is ~0.52 ms/page. Rounding a sub-millisecond slope up to an
    integer millisecond nearly doubles it and manufactures ~2.9 s of phantom stall
    at the 6,001-page worst case, which is enough to reject atomic runs that are
    fine. ``backfill_sweep_ms`` divides by 1000 at exactly one call site, and this
    is what makes a "tidy-up" into milliseconds fail loudly: a value of 518
    contributes 0.518 ms per page, not 518.
    """
    assert mod.MARGINED_US_BACKFILL_SWEEP_PER_PAGE > 100  # sub-ms, so µs-denominated
    one_page = mod.backfill_sweep_ms(pages=1, scan_ms=0, per_page_us=518)
    assert one_page == pytest.approx(0.518)
    assert mod.backfill_sweep_ms(pages=1000, scan_ms=0, per_page_us=518) == pytest.approx(518.0)
    # And the scan component is NOT divided by 1000: it is already milliseconds.
    assert mod.backfill_sweep_ms(pages=0, scan_ms=72, per_page_us=518) == pytest.approx(72.0)


def test_sweep_scan_component_takes_g_sessions_and_the_per_page_component_does_not():
    """The split, pinned at the function that applies it.

    The scan component is a relation read and scales with ``game_sessions``
    exactly as every other ``game_sessions`` term does. The per-page component is
    statement startup — parse, plan, execute, round-trip — and is indifferent to
    relation size. Applying ``g_sessions`` to it over-charges; applying nothing to
    the scan component under-charges.
    """
    pages = 1_000
    base = mod.backfill_sweep_ms(pages=pages)
    grown = mod.backfill_sweep_ms(pages=pages, g_sessions=2.0)
    assert grown - base == pytest.approx(mod.MARGINED_MS_BACKFILL_SWEEP_SCAN)
    assert grown != pytest.approx(2 * base)


def test_runner_pass_limits_do_not_exceed_the_charged_pass_bound():
    """Otherwise the scan budget charges fewer scans than a run can issue.

    ``MAX_PASSES`` is what the invariant prices two scans per pass against. A
    per-phase limit above it would let a run execute scans the budget never paid
    for, which makes the import-time check an underestimate rather than a bound.
    """
    assert mod.BACKFILL_MAX_PASSES <= mod.MAX_PASSES
    assert mod.REPAIR_MAX_PASSES <= mod.MAX_PASSES


def test_admitted_batch_sizes_are_positive_and_never_exceed_what_was_tested():
    """No admitted batch exceeds a size sizing actually demonstrated.

    ``B_formula`` is derived from a MEAN per-row cost, so it can name a batch
    size larger than anything that was ever run. Admitting it would mean the
    deployment's maximum batch was never demonstrated to fit the deadline, which
    is precisely what ``MAX_BATCH_SIZE`` exists to prevent.
    """
    assert mod.MAX_BATCH_SIZE >= 1
    assert mod.REPAIR_BATCH_SIZE >= 1
    assert mod.DEFAULT_BATCH_SIZE == mod.MAX_BATCH_SIZE
    assert mod.MAX_BATCH_SIZE <= mod.B_TESTED
    assert mod.MAX_BATCH_SIZE <= mod.MAX_BATCH_MS // mod.MARGINED_MS_PER_ROW
    assert mod.REPAIR_BATCH_SIZE <= mod.R_TESTED
    assert mod.REPAIR_BATCH_SIZE <= mod.MAX_BATCH_MS // mod.MARGINED_MS_PER_REPAIR_ROW


# ---------------------------------------------------------------------------
# The zero-batch boundary, both sides and both formulas.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("constant", ["MARGINED_MS_PER_ROW", "MARGINED_MS_PER_REPAIR_ROW"])
def test_zero_batch_boundary_is_admissible_at_equality(constant):
    """At ``per_row == MAX_BATCH_MS`` the formula yields 1 and the runner runs.

    A one-row batch then consumes exactly the margined budget — which the 3x
    margin already inside the per-row constant covers. Rejecting the boundary
    would reject a configuration that is by construction within budget.
    """
    assert (
        mod._admitted_batch_size(mod.MAX_BATCH_MS, 10_000, constant=constant) == 1
    )


@pytest.mark.parametrize("constant", ["MARGINED_MS_PER_ROW", "MARGINED_MS_PER_REPAIR_ROW"])
def test_zero_batch_boundary_raises_above_it_rather_than_clamping_to_one(constant):
    """One row over budget must RE-SIZE, not silently become "batch size 1".

    Clamping would admit a batch whose single row is projected to overrun
    ``MAX_BATCH_MS`` and call that the minimum — a re-sizing decision disguised
    as a default, violating the exact budget the constant exists to enforce.
    """
    with pytest.raises(mod.MigrationError) as exc:
        mod._admitted_batch_size(mod.MAX_BATCH_MS + 1, 10_000, constant=constant)
    assert constant in str(exc.value)
    assert "re-size before deploying" in str(exc.value)


def test_admitted_batch_size_takes_the_minimum_of_formula_and_tested():
    # formula = 5000 // 5 = 1000, tested = 40 -> tested binds.
    assert mod._admitted_batch_size(5, 40, constant="MARGINED_MS_PER_ROW") == 40
    # formula = 5000 // 2500 = 2, tested = 40 -> formula binds.
    assert mod._admitted_batch_size(2500, 40, constant="MARGINED_MS_PER_ROW") == 2


# ---------------------------------------------------------------------------
# The harness measures the SHIPPED statements, by import identity.
# ---------------------------------------------------------------------------


def test_harness_loads_the_shipped_revision_module():
    assert harness.REVISION == REVISION
    assert harness.mod.__file__ == mod.__file__


#: Statements the harness reads DIRECTLY off the revision module.
#: ``LOAD_MOVES_PG`` is deliberately absent — it is reached through ``SQL_PG``,
#: which is stronger; see the bundle test below. ``REPAIR_PREDICATE_SQL`` is a
#: shared fragment rather than a statement (no marker, never executed alone) and
#: is reached transitively inside the statements that embed it.
MEASURED_STATEMENTS = [
    "POPULATION_PREDICATE_SQL",
    "PLY_DETECTOR_SQL",
    "PLY_DETECTOR_ONE_PG",
    "SELECT_BATCH_FIRST_PG",
    "SELECT_BATCH_PG",
    "SELECT_BATCH_FIRST_LOCKED_PG",
    "SELECT_BATCH_LOCKED_PG",
    "UPDATE_SQL_PG",
    "REPAIR_POPULATE_SQL",
    "REPAIR_SELECT_FIRST_PG",
    "REPAIR_SELECT_PG",
    "REPAIR_LOCK_PG",
    "REPAIR_UPDATE_PG",
    "REPAIR_REMAINING_SQL",
    "SOUNDNESS_ASSERT_SQL",
    "COVERAGE_ASSERT_SQL",
]


def _harness_mod_attribute_reads() -> set[str]:
    """Every ``mod.<NAME>`` the harness reads, collected from its AST."""
    tree = ast.parse(pathlib.Path(harness.__file__).read_text())
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "mod"
    }


@pytest.mark.parametrize("attr", MEASURED_STATEMENTS)
def test_harness_reaches_every_measured_statement_through_the_revision(attr):
    """Reached through the loaded revision, never restated in the harness.

    Asserted structurally rather than with ``is``: Alembic's ``ScriptDirectory``
    loads the revision file afresh per call, so the harness's module object and
    this file's are distinct objects over the same source and their string
    constants are never the same object. Object identity would therefore fail for
    a harness that is behaving perfectly — so what is asserted is the property
    that actually matters: the name is READ OFF the revision module, and its
    value is the revision's.

    A harness that restated the statements would keep measuring the old ones
    after the revision changed, and the drift would be invisible: both copies
    would still be valid SQL and both would still return a number.
    """
    assert harness.mod.__file__ == mod.__file__
    assert attr in _harness_mod_attribute_reads()
    assert getattr(harness.mod, attr) == getattr(mod, attr)


def test_harness_loads_moves_through_the_revisions_own_bundle_and_loader():
    """``LOAD_MOVES_PG`` is reached through ``SQL_PG``, not read off the module.

    That is stronger than a direct read, not weaker: the revision's rule is that
    the runner never names a statement constant directly, and the harness obeys
    the same rule by calling ``mod._load_moves(conn, mod.SQL_PG, ids)``. So it
    cannot pick the SQLite bind form by accident, and it exercises the array-bind
    path the migration actually uses rather than a hand-rolled equivalent.
    """
    reads = _harness_mod_attribute_reads()
    assert "_load_moves" in reads
    assert "SQL_PG" in reads
    assert mod.SQL_PG.load_moves == mod.LOAD_MOVES_PG


def test_harness_defines_no_statement_of_its_own_that_carries_a_revision_marker():
    """The one way a restated copy could hide: same marker, harness-local text.

    Every shipped statement's first token is a ``/* ghostreplay:<name> */``
    marker, and the listener tests and the Phase 3c probe identify statements by
    it. A harness-local constant carrying one of those markers would be measured
    and reported as though it were the shipped statement. The harness's own DDL
    (the parking trigger, the synthesis statements) carries no marker, so this
    stays clean without exempting anything.
    """
    tree = ast.parse(pathlib.Path(harness.__file__).read_text())
    offenders = [
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and "/* ghostreplay:" in node.value.value
    ]
    assert offenders == []


def test_harness_prices_the_four_complete_scan_bearing_statements():
    """Not the bare detector, which no code path executes alone.

    What executes is the detector wrapped in a statement that also joins and
    filters ``game_sessions`` and then counts or inserts, and every one of those
    costs strictly more. Pricing ``MARGINED_MS_PER_SCAN_STMT`` from the bare
    detector would under-size every scan the migration actually issues.
    """
    stmts = harness.scan_bearing_statements()
    assert set(stmts) == {
        "repair_populate",
        "repair_remaining",
        "soundness_assert",
        "repair_population_count",
    }
    assert stmts["repair_populate"] == mod.REPAIR_POPULATE_SQL
    assert stmts["repair_remaining"] == mod.REPAIR_REMAINING_SQL
    assert stmts["soundness_assert"] == mod.SOUNDNESS_ASSERT_SQL
    # The pre-flight population count is DERIVED from the shipped statement, not
    # restated: same plan, its own marker.
    assert stmts["repair_population_count"] != mod.REPAIR_REMAINING_SQL
    assert "ghostreplay:repair_population_count" in stmts["repair_population_count"]
    assert stmts["repair_population_count"].replace(
        "ghostreplay:repair_population_count", "ghostreplay:repair_remaining"
    ) == mod.REPAIR_REMAINING_SQL


def _scans(populate, remaining, soundness, count, detector, coverage=800.0):
    return {
        "repair_populate": {"max_ms": populate},
        "repair_remaining": {"max_ms": remaining},
        "soundness_assert": {"max_ms": soundness},
        "repair_population_count": {"max_ms": count},
        "_diagnostic_bare_detector": {"max_ms": detector},
        "coverage_assert": {"max_ms": coverage},
    }


def test_scan_stmt_max_is_the_maximum_across_the_four_and_ignores_the_coverage_assert():
    """The coverage assertion scans a different relation and has its own constant."""
    scans = _scans(10.0, 40.0, 25.0, 30.0, detector=9.0, coverage=800.0)
    assert harness.scan_stmt_max_ms(scans) == 40.0
    assert harness.scan_plan_inversion(scans) is False


def test_scan_stmt_max_absorbs_the_bare_detector_when_the_plan_inverts():
    """When the planner serves the complete statements with an early-terminating
    join, they can read a fraction of ``session_moves`` while a bare detector
    reads all of it — so the "diagnostic lower bound" is not a lower bound and
    the four measurements are not pricing a full scan. Freezing the smaller
    number would under-size the one term the atomic stall projection cannot
    revalidate at run time, in the direction that admits atomic mode.
    """
    scans = _scans(10.0, 40.0, 25.0, 30.0, detector=900.0)
    assert harness.scan_stmt_max_ms(scans) == 900.0
    assert harness.scan_plan_inversion(scans) is True


# ---------------------------------------------------------------------------
# Phase 2 derivation.
# ---------------------------------------------------------------------------


def _bases(dimensions, populations=None, *, pre=None, legacy=False):
    """The two labelled readings the harness attaches to every artifact.

    Derived from the artifact's own ``dimensions_before`` by default, so the
    substitution guard passes and a test that wants to trip it has to say so.
    ``pre`` supplies a DIFFERENT pre-synthesis reading — the shape a real
    synthesis leaves — and ``legacy`` drops it entirely.
    """
    dims = {k: v for k, v in dimensions.items() if k in harness.DIMENSION_KEYS}
    return {
        "timing_basis": harness.TIMING_BASIS,
        "dimension_bases": {
            "pre_synthesis": (
                {"status": harness.LEGACY_UNRECORDED}
                if legacy
                else {"status": "measured", **(pre or dims)}
            ),
            "post_synthesis": {"status": "measured", **dims, "populations": populations or {}},
        },
    }


def _measurement(**over):
    """A minimal but complete atomic measurement, with everything nonzero."""
    dims = {
        "total_rows": 1000,
        "sessions_bytes": 2_000_000,
        "m_total": 60_000,
        "moves_bytes": 8_000_000,
    }
    base = {
        "kind": "atomic",
        "batch_size": 500,
        "repair_batch_size": 200,
        "dimensions_before": dims,
        **_bases(dims),
        "populations_before": {
            "n_stale": 1000,
            "m_moves": 60_000,
            "n_broken_audit": 10,
            "n_repair": 10,
        },
        "scans": {
            "repair_populate": {"max_ms": 100.0},
            "repair_remaining": {"max_ms": 120.0},
            "soundness_assert": {"max_ms": 110.0},
            "repair_population_count": {"max_ms": 115.0},
            "coverage_assert": {"max_ms": 60.0},
            # The backfill's convergence count: one statement per PASS, priced by
            # its own constant and scaled by r_sessions — NOT part of "the maximum
            # across the four complete session_moves statements". The SWEEP is not
            # here at all: it is a domain, and it arrives as its own measurement.
            "backfill_remaining": {"max_ms": 70.0},
        },
        "teardown_point": "full",
        "validated_in_run": True,
        "teardown": {"commit_ms": 300.0, "n_mutated": 1010},
        "timings": {
            "single_session_compute": {"total_ms": 2000.0, "max_ms": 8.0},
            "guarded_update": {"total_ms": 400.0, "max_ms": 20.0},
            "load_moves": {"total_ms": 300.0, "max_ms": 15.0},
            "select_batch": {"total_ms": 100.0, "max_ms": 5.0},
            "repair_per_candidate": {"median_ms": 2.0, "max_ms": 4.0},
        },
    }
    base.update(over)
    return base


def _empty_point(commit_ms=40.0, *, validated=True):
    """The atomic teardown FLOOR, from its own run against its own restore."""
    return {
        "kind": "atomic",
        "teardown_point": "empty",
        "validated_in_run": validated,
        "teardown": {"commit_ms": commit_ms, "n_mutated": 0},
        "dimensions_before": _measurement()["dimensions_before"],
        **_bases(_measurement()["dimensions_before"]),
        "populations_before": {"n_stale": 0, "m_moves": 0, "n_broken_audit": 0, "n_repair": 0},
        "scans": _measurement()["scans"],
        "timings": {},
    }


def _batch_measurement(
    size,
    max_batch_ms,
    *,
    repair_size=200,
    max_repair_batch_ms=100.0,
    rows=None,
    repair_rows=None,
    commit_ms=12.0,
    repair_commit_ms=1.0,
):
    return {
        "kind": "batch",
        "batch_size": size,
        "repair_batch_size": repair_size,
        "dimensions_before": _measurement()["dimensions_before"],
        **_bases(_measurement()["dimensions_before"]),
        "timings": {
            "single_batch": {"max_ms": max_batch_ms},
            # The ACTUAL page cardinality, which defaults to the requested size.
            "single_batch_rows": {"max_ms": float(size if rows is None else rows)},
            "batch_commit": {"max_ms": commit_ms},
            "single_repair_batch": {"max_ms": max_repair_batch_ms},
            "single_repair_batch_rows": {
                "max_ms": float(repair_size if repair_rows is None else repair_rows)
            },
            "repair_batch_commit": {"max_ms": repair_commit_ms},
        },
    }


def _sweep_point(pages, max_ms, *, batch_size=None, retained=True, run="D", trials=None):
    """One sweep-domain point.

    ``max_ms`` is a STRING on purpose. The fit refuses a raw binary float — by the
    time one exists the decimal a producer wrote is already gone — so a fixture
    that writes ``155.0007`` as a Python float is testing a path the shipped
    intake does not have.
    """
    n = trials if trials is not None else (harness.MIN_SWEEP_TRIALS if retained else 3)
    pt = {
        "run": run,
        "batch_size": batch_size,
        "pages": pages,
        "trials_per_point": n,
        "raw_trials_retained": retained,
        "max_ms": max_ms,
    }
    if retained:
        # The maximum LAST, over n-1 smaller trials, so max(trials_ms) is the
        # recorded maximum and the fit is re-derivable from the trials themselves.
        low = str(Decimal(max_ms) / 2)
        pt["trials_ms"] = [low] * (n - 1) + [max_ms]
    return pt


#: A two-point sweep domain whose LP solution is exact and easy to state:
#: ``A + b = 10.1`` and ``A + 11b = 11.1`` give ``b = 0.1``, ``A = 10.0``.
_SWEEP_DEFAULT_POINTS = [_sweep_point(1, "10.1"), _sweep_point(11, "11.1")]


def _sweep_domain(points=None, *, dimensions=None, **over):
    # Merged onto the default four rather than replacing them: a real basis is
    # always all four dimensions, and these overrides only ever care about the two
    # the growth factor divides.
    dims = {**_measurement()["dimensions_before"], **(dimensions or {})}
    base = {
        "kind": "sweep_domain",
        "artifact": "synthetic-sweep-domain",
        "sessions_synthesized": False,
        "dimensions_before": dims,
        **_bases(dims, {"n_stale": 1000}),
        "populations_before": {"n_stale": 1000},
        "points": list(points if points is not None else _SWEEP_DEFAULT_POINTS),
    }
    base.update(over)
    return base


def _probe(scope, unlock_max, *, trials=harness.MIN_CANCEL_TRIALS, rows_locked=100_000):
    return {
        "kind": "cancel_probe",
        "scope": scope,
        "dimensions_before": _measurement()["dimensions_before"],
        **_bases(_measurement()["dimensions_before"]),
        "trials": trials,
        "rows_locked": rows_locked,
        "cancel_to_unlock_ms": {"max": unlock_max, "median": unlock_max / 2, "min": 1.0},
        "rollback_only_teardown_ms": {"max": unlock_max / 10, "median": unlock_max / 20},
    }


#: A production database with BOTH populations empty — the fully-stamped,
#: clean-audit state. Legitimate, and distinct from an empty SNAPSHOT, which is
#: an unsynthesized measurement and is rejected outright.
_ZERO_PRODUCTION = {
    "dimensions": {
        "total_rows": 1000,
        "sessions_bytes": 2_000_000,
        "m_total": 60_000,
        "moves_bytes": 8_000_000,
    },
    "populations": {"n_stale": 0, "m_moves": 0, "n_broken_audit": 0, "n_repair": 0},
}


def _complete(*extra):
    """A derivation input set that satisfies every mandatory-evidence rule."""
    return [
        _measurement(),
        _empty_point(),
        _batch_measurement(500, 400.0),
        _probe("batch", 10.0),
        _probe("atomic", 20.0),
        _sweep_domain(),
        *extra,
    ]


def test_derivation_freezes_the_scan_terms_from_the_maximum_of_the_four():
    out = harness.derive(_complete(), None)
    # max of the four complete statements is repair_remaining at 120ms; the
    # coverage assertion is priced separately at 60ms.
    assert out["projected_ms"]["T_scan_stmt_snap"] == 120.0
    assert out["constants"]["MARGINED_MS_PER_SCAN_STMT"] == 360
    assert out["constants"]["MARGINED_MS_COVERAGE_ASSERT"] == 180


def test_scan_terms_survive_a_run_where_both_populations_are_zero():
    """The failure this whole timing model exists to prevent.

    A clean audit with a small stale set is the shape a population-scaled model
    scores at nearly zero and wrongly admits into atomic mode. The scan and
    coverage terms divide by no count, so they must come out unchanged and
    nonzero — and they must still be charged to the stall.
    """
    out = harness.derive(_complete(), _ZERO_PRODUCTION)
    assert out["projected_ms"]["T_backfill_prod"] == 0.0
    assert out["projected_ms"]["T_repair_prod"] == 0.0
    assert out["constants"]["MARGINED_MS_PER_SCAN_STMT"] > 0
    assert out["constants"]["MARGINED_MS_COVERAGE_ASSERT"] > 0
    assert out["constants"]["MARGINED_MS_BACKFILL_SWEEP_SCAN"] > 0
    assert out["constants"]["MARGINED_US_BACKFILL_SWEEP_PER_PAGE"] > 0
    assert out["constants"]["MARGINED_MS_BACKFILL_REMAINING"] > 0
    # N_stale = 0 is ONE sweep page — the empty-teardown shape — so the sweep is
    # still CHARGED and still nonzero: A + b x 1 = 10.0 + 0.1.
    assert out["projected_ms"]["T_backfill_sweep_pages_prod"] == 1
    assert out["projected_ms"]["T_backfill_sweep_prod"] == pytest.approx(10.1)
    # Three session_moves scans under lock, the coverage assertion, the backfill's
    # own selection sweep and convergence count, and the teardown floor are the
    # WHOLE stall on this run — and not one of them is zero.
    assert out["decision_1"]["T_stall_prod_ms"] == pytest.approx(
        mod.ATOMIC_SCANS_UNDER_LOCK * 120.0
        + 60.0
        + mod.BACKFILL_SELECT_SWEEPS_UNDER_LOCK * 10.1
        + mod.BACKFILL_REMAINING_UNDER_LOCK * 70.0
        + 40.0
    )


def test_atomic_teardown_has_a_floor_even_when_nothing_was_mutated():
    """An atomic transaction that mutated nothing still commits."""
    out = harness.derive(_complete(), _ZERO_PRODUCTION)
    assert out["constants"]["MARGINED_MS_ATOMIC_TEARDOWN_FIXED"] == 120  # 3 * 40ms


def test_atomic_teardown_slope_is_floored_at_zero():
    """A negative slope is measurement noise, not a discount."""
    m = _measurement()
    m["teardown"] = {"commit_ms": 5.0, "n_mutated": 1010}  # below the empty run
    out = harness.derive([m] + _complete()[1:], None)
    assert out["projected_ms"]["T_atomic_teardown_per_row_prod"] == 0.0


def test_atomic_teardown_per_row_is_denominated_in_microseconds():
    """Rounding a sub-ms marginal cost up to a whole ms would add a phantom
    second of projected stall per thousand rows and reject atomic runs that are
    fine. (260ms - 0) over 1010 rows is ~0.257ms/row: as an integer millisecond
    that is 1, which is a ~4x inflation before the margin is even applied.
    """
    out = harness.derive(_complete(), None)
    slope_ms = out["projected_ms"]["T_atomic_teardown_per_row_prod"]
    assert slope_ms < 1.0
    assert out["constants"]["MARGINED_US_ATOMIC_TEARDOWN_PER_ROW"] == math.ceil(
        harness.MARGIN * 1000.0 * slope_ms
    )
    assert out["constants"]["MARGINED_US_ATOMIC_TEARDOWN_PER_ROW"] > 100


def test_teardown_allowance_takes_the_larger_of_commit_and_cancel_to_unlock():
    """Locks are held until whichever one returns, and the deadline-breach path
    ends in a cancellation followed by a rollback, not in a commit."""
    out = harness.derive(
        [
            _measurement(),
            _empty_point(),
            _batch_measurement(500, 1200.0, commit_ms=30.0),
            _probe("batch", 250.0),
            _probe("atomic", 250.0),
            _sweep_domain(),
        ],
        None,
    )
    assert out["constants"]["TEARDOWN_ALLOWANCE_MS"] == 750  # 3 * 250, not 3 * 30


def test_teardown_allowance_covers_the_repair_phases_commits_too():
    """A repair batch holds row locks until ITS commit returns.

    Reading only ``batch_commit`` let a slow repair commit hide behind a fast
    backfill commit, and the reported maximum was the fast one.
    """
    out = harness.derive(
        [
            _measurement(),
            _empty_point(),
            _batch_measurement(500, 400.0, commit_ms=1.0, repair_commit_ms=100.0),
            _probe("batch", 2.0),
            _probe("atomic", 2.0),
            _sweep_domain(),
        ],
        None,
    )
    assert out["projected_ms"]["max_batch_commit_ms_observed"] == 100.0
    assert out["constants"]["TEARDOWN_ALLOWANCE_MS"] == 300  # 3 * 100


def test_derivation_refuses_to_freeze_teardown_without_a_cancel_probe():
    """A run that measured only COMMIT must not silently produce the constant.

    The old fall-through scored a missing probe as 0.0, so ``max(commit, 0)``
    quietly became the frozen input — a plausible small integer with no breach
    measurement behind it, which is exactly the sizing failure the design names.
    """
    with pytest.raises(SystemExit, match="probe-scope batch"):
        harness.derive(
            [_measurement(), _empty_point(), _batch_measurement(500, 400.0),
             _probe("atomic", 20.0)],
            None,
        )
    with pytest.raises(SystemExit, match="probe-scope atomic"):
        harness.derive(
            [_measurement(), _empty_point(), _batch_measurement(500, 400.0),
             _probe("batch", 10.0)],
            None,
        )


def test_derivation_rejects_an_under_trialled_cancel_probe():
    """A maximum over three samples is not the claim a maximum over twenty is."""
    with pytest.raises(SystemExit, match=">= 20"):
        harness.derive(
            [_measurement(), _empty_point(), _batch_measurement(500, 400.0),
             _probe("batch", 10.0, trials=3), _probe("atomic", 20.0)],
            None,
        )


def test_derivation_combines_repeated_probes_of_one_scope_by_maximum():
    out = harness.derive(
        [_measurement(), _empty_point(), _batch_measurement(500, 400.0),
         _probe("batch", 10.0), _probe("batch", 77.0), _probe("atomic", 80.0),
         _sweep_domain()],
        None,
    )
    assert out["projected_ms"]["max_batch_cancel_to_unlock_ms_observed"] == 77.0


def test_derivation_rejects_a_batch_probe_smaller_than_the_largest_admitted_batch():
    """TEARDOWN_ALLOWANCE_MS bounds ONE per-batch-mode batch — of EITHER phase.

    ``REPAIR_BATCH_SIZE`` divides by a cheaper per-row cost, so it can exceed
    ``MAX_BATCH_SIZE``; a probe sized to the backfill's batch then measures the
    breach path on a smaller transaction than the largest one the runner will
    admit.
    """
    with pytest.raises(SystemExit, match="largest admitted batch"):
        harness.derive(
            [
                _measurement(),
                _empty_point(),
                _batch_measurement(500, 400.0, repair_size=2000, max_repair_batch_ms=100.0),
                _probe("batch", 10.0, rows_locked=500),
                _probe("atomic", 20.0),
                _sweep_domain(),
            ],
            None,
        )


def test_empty_point_must_have_mutated_nothing():
    """A run that stamped rows has a commit that flushed them.

    Its COMMIT is then not the population-independent floor, and the slope
    derived against it is short by exactly those rows.
    """
    e = _empty_point()
    e["teardown"] = {"commit_ms": 40.0, "n_mutated": 5}
    with pytest.raises(SystemExit, match="n_mutated=5"):
        harness.derive([_measurement(), e] + _complete()[2:], None)


def test_empty_point_mutation_count_must_be_present_not_merely_falsy():
    """Absent evidence is not evidence of zero — and it reads as falsy.

    A truthiness check accepts a measurement that never recorded what it
    mutated, which is the one fact the teardown floor depends on.
    """
    e = _empty_point()
    e["teardown"] = {"commit_ms": 40.0}  # n_mutated omitted entirely
    with pytest.raises(SystemExit, match="n_mutated=None"):
        harness.derive([_measurement(), e] + _complete()[2:], None)


@pytest.mark.parametrize("n_mutated", [0, None])
def test_full_point_must_have_mutated_something(n_mutated):
    """A full point that mutated nothing IS the empty point.

    ``(full - empty) / n_mutated`` then divides by zero, and the guarded form
    of that division yields a slope of 0.0 that looks exactly like a clean
    measurement of "commit does not scale with rows".
    """
    m = _measurement()
    m["teardown"] = {"commit_ms": 300.0}
    if n_mutated is not None:
        m["teardown"]["n_mutated"] = n_mutated
    with pytest.raises(SystemExit, match="teardown SLOPE"):
        harness.derive([m] + _complete()[1:], None)


@pytest.mark.parametrize("n_mutated", [True, False])
def test_empty_point_mutation_count_must_be_an_integer_not_a_bool(n_mutated):
    """JSON ``false`` compares equal to 0, so a value test alone admits it.

    A bool carries no row count; accepting one would let the teardown floor be
    proven by a field that never held a population.
    """
    e = _empty_point()
    e["teardown"] = {"commit_ms": 40.0, "n_mutated": n_mutated}
    with pytest.raises(SystemExit, match=f"n_mutated={n_mutated!r}"):
        harness.derive([_measurement(), e] + _complete()[2:], None)


@pytest.mark.parametrize("n_mutated", [True, False])
def test_full_point_mutation_count_must_be_an_integer_not_a_bool(n_mutated):
    """JSON ``true`` is an int greater than 0, so a value test alone admits it.

    ``(full - empty) / True`` is a division by one row — a slope inflated by
    the whole difference between the two points.
    """
    m = _measurement()
    m["teardown"] = {"commit_ms": 300.0, "n_mutated": n_mutated}
    with pytest.raises(SystemExit, match="teardown SLOPE"):
        harness.derive([m] + _complete()[1:], None)


def test_derivation_rejects_an_atomic_probe_smaller_than_the_transaction_it_bounds():
    with pytest.raises(SystemExit, match="smaller transaction"):
        harness.derive(
            [_measurement(), _empty_point(), _batch_measurement(500, 400.0),
             _probe("batch", 10.0), _probe("atomic", 20.0, rows_locked=5),
             _sweep_domain()],
            None,
        )


def test_batch_size_is_bounded_by_what_was_actually_demonstrated():
    """A candidate whose observed maximum fails the 3x rule is not eligible."""
    measurements = [
        _measurement(),
        _empty_point(),
        _batch_measurement(500, 400.0),  # 3 * 400 = 1200 <= 5000: passes
        _batch_measurement(4000, 2000.0),  # 3 * 2000 = 6000 > 5000: fails
        _probe("batch", 10.0),
        _probe("atomic", 20.0),
        _sweep_domain(),
    ]
    out = harness.derive(measurements, None)
    assert out["batch_sizing"]["B_tested"] == 500
    assert out["constants"]["MAX_BATCH_SIZE"] <= 500
    tried = {
        c["requested_size"]: c["passes_3x"] for c in out["batch_sizing"]["backfill_candidates"]
    }
    assert tried == {500: True, 4000: False}


def test_tested_size_is_the_page_actually_executed_not_the_limit_requested():
    """A population smaller than the requested size never exercises that size.

    The first recorded Phase 1 run asked for a repair batch of 301 against a
    population of 300 and froze ``R_TESTED = 301`` — a size nothing ever ran.
    """
    out = harness.derive(
        [
            _measurement(),
            _empty_point(),
            _batch_measurement(4000, 400.0, rows=120, repair_size=301, repair_rows=300),
            _probe("batch", 10.0),
            _probe("atomic", 20.0),
            _sweep_domain(),
        ],
        None,
    )
    assert out["batch_sizing"]["B_tested"] == 120
    assert out["batch_sizing"]["R_tested"] == 300
    candidate = out["batch_sizing"]["backfill_candidates"][0]
    assert candidate["requested_size"] == 4000
    assert candidate["demonstrated_size"] == 120
    assert candidate["reached_requested_size"] is False


def test_derivation_fails_when_no_batch_candidate_passed_rather_than_using_the_formula():
    """Falling back to the formula would hand the admitted maximum to a number
    with nothing empirical behind it — the state ``min(formula, tested)`` exists
    to make impossible."""
    with pytest.raises(SystemExit, match="backfill batch sizing has nothing demonstrated"):
        harness.derive(
            [
                _measurement(),
                _empty_point(),
                _batch_measurement(4000, 9_000.0),  # 3 * 9000 > 5000
                _probe("batch", 10.0),
                _probe("atomic", 20.0),
                _sweep_domain(),
            ],
            None,
        )


def test_relation_growth_scales_the_scan_terms_and_nothing_else():
    """The scan terms are the only ones a population recount cannot revalidate.

    Doubling ``session_moves`` while every population stays put must double the
    scan term — and must leave the per-row constants alone, because those ARE
    revalidated by the population counts at run time.
    """
    m = _measurement()
    prod = {
        "dimensions": {
            "total_rows": 1000,
            "sessions_bytes": 2_000_000,
            "m_total": 120_000,
            "moves_bytes": 16_000_000,
        },
        "populations": m["populations_before"],
    }
    base = harness.derive([m] + _complete()[1:], None)
    grown = harness.derive([m] + _complete()[1:], prod)
    assert grown["scaling"]["r_moves"] == 2.0
    assert grown["scaling"]["r_sessions"] == 1.0
    assert (
        grown["constants"]["MARGINED_MS_PER_SCAN_STMT"]
        == 2 * base["constants"]["MARGINED_MS_PER_SCAN_STMT"]
    )
    assert (
        grown["constants"]["MARGINED_MS_COVERAGE_ASSERT"]
        == base["constants"]["MARGINED_MS_COVERAGE_ASSERT"]
    )
    assert (
        grown["constants"]["MARGINED_MS_PER_REPAIR_ROW"]
        == base["constants"]["MARGINED_MS_PER_REPAIR_ROW"]
    )
    # The backfill's own terms scan game_sessions, which did NOT grow here.
    for name in (
        "MARGINED_MS_BACKFILL_SWEEP_SCAN",
        "MARGINED_US_BACKFILL_SWEEP_PER_PAGE",
        "MARGINED_MS_BACKFILL_REMAINING",
    ):
        assert grown["constants"][name] == base["constants"][name], name


def test_relation_growth_of_game_sessions_alone_scales_the_backfills_own_terms():
    """The other relation, and the leak a ``session_moves``-only model misses.

    ``game_sessions`` doubles while ``session_moves`` stands still — the exact
    shape a month of correctly-stamped version-1 sessions with no new plies would
    NOT produce, but a month of ordinary traffic would. The coverage assertion AND
    both backfill terms must double; the ``session_moves`` scan term must not.
    """
    m = _measurement()
    prod = {
        "dimensions": {
            "total_rows": 2000,
            "sessions_bytes": 4_000_000,
            "m_total": 60_000,
            "moves_bytes": 8_000_000,
        },
        "populations": m["populations_before"],
    }
    base = harness.derive([m] + _complete()[1:], None)
    grown = harness.derive([m] + _complete()[1:], prod)
    assert grown["scaling"]["r_sessions"] == 2.0
    assert grown["scaling"]["r_moves"] == 1.0
    for name in ("MARGINED_MS_COVERAGE_ASSERT", "MARGINED_MS_BACKFILL_REMAINING"):
        assert grown["constants"][name] == 2 * base["constants"][name], name
    assert (
        grown["constants"]["MARGINED_MS_PER_SCAN_STMT"]
        == base["constants"]["MARGINED_MS_PER_SCAN_STMT"]
    )
    # The SWEEP splits, and this is the whole point of splitting it. A bigger
    # game_sessions means a bigger relation WALK, so the scan coefficient doubles;
    # it does not make STARTING a statement more expensive, so the per-page slope
    # is bit-for-bit unchanged. The doubling is exact rather than approximate
    # because the fit is solved in frozen-basis coordinates: substituting
    # A' = A / N_copy leaves the LP in (A', b) identical, so the solution moves by
    # exactly N_copy on A and by nothing at all on b.
    assert grown["projected_ms"]["sweep_scan_coeff_frozen_basis_ms"] == pytest.approx(
        2 * base["projected_ms"]["sweep_scan_coeff_frozen_basis_ms"]
    )
    assert (
        grown["projected_ms"]["sweep_envelope_per_page_ms_exact"]
        == base["projected_ms"]["sweep_envelope_per_page_ms_exact"]
    )
    assert (
        grown["constants"]["MARGINED_MS_BACKFILL_SWEEP_SCAN"]
        == 2 * base["constants"]["MARGINED_MS_BACKFILL_SWEEP_SCAN"]
    )
    assert (
        grown["constants"]["MARGINED_US_BACKFILL_SWEEP_PER_PAGE"]
        == base["constants"]["MARGINED_US_BACKFILL_SWEEP_PER_PAGE"]
    )


def test_decision_1_rejects_atomic_when_only_the_backfills_own_terms_breach():
    """Small ``N_stale``, small ``session_moves``, LARGE ``game_sessions``.

    The shape a formula that prices only the ``session_moves`` scans scores as
    nearly free: the backfill's unindexed selection sweep and its convergence
    count must walk the whole of a large ``game_sessions`` under every row lock the
    backfill took. If the two BACKFILL terms were dropped, this run would be
    admitted into atomic mode.
    """
    m = _measurement()
    m["timings"]["single_session_compute"] = {"total_ms": 2.0, "max_ms": 2.0}
    for name in ("repair_populate", "repair_remaining", "soundness_assert",
                 "repair_population_count"):
        m["scans"][name] = {"max_ms": 1.0}
    m["scans"]["coverage_assert"] = {"max_ms": 1.0}
    m["scans"]["backfill_remaining"] = {"max_ms": 6_000.0}
    # The sweep arrives as a DOMAIN now, not a scalar scan entry: two points whose
    # LP solution is a ~6 s relation walk plus a 0.01 ms/page slope.
    expensive_sweep = _sweep_domain([_sweep_point(1, "6000.0"), _sweep_point(11, "6000.1")])
    free_sweep = _sweep_domain([_sweep_point(1, "0.0"), _sweep_point(11, "0.0")])
    prod = {
        "dimensions": m["dimensions_before"],
        "populations": {"n_stale": 1, "m_moves": 60, "n_broken_audit": 0, "n_repair": 0},
    }
    rest = [x for x in _complete()[1:] if x.get("kind") != "sweep_domain"]
    out = harness.derive([m] + rest + [expensive_sweep], prod)
    assert out["projected_ms"]["T_repair_prod"] == 0.0
    assert out["decision_1"]["verdict"] == "batch"
    # And the reason really is those two terms: zeroing them admits the same run.
    without = dict(m)
    without["scans"] = dict(m["scans"])
    without["scans"]["backfill_remaining"] = {"max_ms": 0.0}
    assert (
        harness.derive([without] + rest + [free_sweep], prod)["decision_1"]["verdict"] == "atomic"
    )


def test_decision_1_charges_every_term_of_the_stall():
    """Backfill + repair + scans under lock + coverage + teardown floor + slope.

    Dropping any single term must change the verdict arithmetic, which is why
    the sum is asserted against the explicit formula rather than against a
    recorded number.
    """
    out = harness.derive(_complete(), None)
    p = out["projected_ms"]
    expected = (
        p["T_backfill_prod"]
        + p["T_repair_prod"]
        + mod.ATOMIC_SCANS_UNDER_LOCK * p["T_scan_stmt_prod"]
        + p["T_coverage_assert_prod"]
        + mod.BACKFILL_SELECT_SWEEPS_UNDER_LOCK * p["T_backfill_sweep_prod"]
        + mod.BACKFILL_REMAINING_UNDER_LOCK * p["T_backfill_remaining_prod"]
        + p["T_atomic_teardown_floor_prod"]
        + p["T_atomic_teardown_per_row_prod"] * (1000 + 10)
    )
    assert out["decision_1"]["T_stall_prod_ms"] == pytest.approx(expected)
    assert out["decision_1"]["verdict"] in {"atomic", "batch"}
    assert out["decision_1"]["margined_stall_ms"] == pytest.approx(
        harness.MARGIN * out["decision_1"]["T_stall_prod_ms"]
    )


def test_decision_1_reports_both_ends_of_the_admitted_batch_range():
    """A verdict is a property of a CONFIGURATION, not of a database.

    The headline verdict is at ``DEFAULT_BATCH_SIZE``, the configuration a deploy
    actually runs. ``MIN_ADMITTED_BATCH`` is reported beside it, because the sweep
    is ``ceil(N_stale / batch_size) + 1`` pages and a verdict that flips between
    the two ends of the range an operator may set is a fact they need BEFORE
    choosing an override, not after.
    """
    out = harness.derive(_complete(), None)["decision_1"]
    assert out["batch_size_assumed"] == 500  # min(B_formula 555, B_tested 500)
    assert out["batch_size_min_admitted"] == mod.MIN_ADMITTED_BATCH == 1
    # 1,000 stale rows: 3 pages at the default, 1,001 at the floor.
    assert out["sweep_pages"] == 3
    assert out["sweep_pages_min_batch"] == 1_001
    assert out["T_stall_prod_ms_min_batch"] > out["T_stall_prod_ms"]
    assert out["margined_stall_ms_min_batch"] == pytest.approx(
        harness.MARGIN * out["T_stall_prod_ms_min_batch"]
    )
    assert out["verdict_min_batch"] in {"atomic", "batch"}


def test_decision_1_rejects_atomic_when_only_the_scan_terms_breach_the_bound():
    """The clean-audit, large-``session_moves`` shape.

    ``N_repair = 0`` and a tiny stale population, so a population-scaled model
    scores this run at nearly zero. It is inadmissible, and the reason is the
    three scans held across every row lock the backfill took.
    """
    m = _measurement()
    # The SNAPSHOT keeps both populations — they are what the per-row constants
    # are measured from. PRODUCTION is the clean-audit, near-empty one.
    m["timings"]["single_session_compute"] = {"total_ms": 2.0, "max_ms": 2.0}
    m["timings"]["guarded_update"] = {"total_ms": 1.0, "max_ms": 1.0}
    m["timings"]["load_moves"] = {"total_ms": 1.0, "max_ms": 1.0}
    m["timings"]["select_batch"] = {"total_ms": 1.0, "max_ms": 1.0}
    for name in ("repair_populate", "repair_remaining", "soundness_assert",
                 "repair_population_count"):
        m["scans"][name] = {"max_ms": 4_000.0}
    prod = {
        "dimensions": m["dimensions_before"],
        "populations": {"n_stale": 1, "m_moves": 60, "n_broken_audit": 0, "n_repair": 0},
    }
    out = harness.derive([m] + _complete()[1:], prod)
    assert out["projected_ms"]["T_repair_prod"] == 0.0
    assert out["decision_1"]["verdict"] == "batch"


def test_derivation_requires_an_atomic_measurement():
    with pytest.raises(SystemExit):
        harness.derive([_batch_measurement(500, 400.0)], None)


def test_derivation_requires_a_separate_empty_point_run():
    """One measurement point cannot yield a teardown floor AND a slope."""
    with pytest.raises(SystemExit, match="teardown_point == 'empty'"):
        harness.derive(
            [_measurement(), _batch_measurement(500, 400.0),
             _probe("batch", 10.0), _probe("atomic", 20.0)],
            None,
        )


def test_empty_point_must_have_executed_validate():
    """A second pass in the same process has already validated the constraint.

    Its COMMIT then flushes no catalog change, and subtracting it from a full
    point that DID validate charges VALIDATE's own commit cost to the per-row
    slope, where it does not belong. The floor needs a FRESH restore.
    """
    with pytest.raises(SystemExit, match="did not execute VALIDATE"):
        harness.derive(
            [_measurement(), _empty_point(validated=False), _batch_measurement(500, 400.0),
             _probe("batch", 10.0), _probe("atomic", 20.0)],
            None,
        )


@pytest.mark.parametrize("population", ["n_stale", "n_repair"])
def test_derivation_rejects_an_unsynthesized_snapshot_population(population):
    """An empty SNAPSHOT population is not a zero branch — it is a missing
    measurement, and continuing fabricates the per-row constant from a fallback
    instead of deriving it. The legitimate zero branches are on the PRODUCTION
    side; see test_production_zero_populations_keep_the_measured_per_row_constants.
    """
    m = _measurement()
    m["populations_before"] = dict(m["populations_before"], **{population: 0})
    with pytest.raises(SystemExit, match="snapshot has no"):
        harness.derive([m] + _complete()[1:], None)


def test_production_zero_populations_keep_the_measured_per_row_constants():
    """The TERM drops out of Decision 1; the CONSTANT is still declared.

    The runtime guard multiplies the per-row constants by the LIVE counts, which
    may have grown since the audit — so a production population of zero must not
    erase the number the snapshot measured. The old code divided a projected
    total by a production count of zero and fell through to a fabricated 1.
    """
    out = harness.derive(_complete(), _ZERO_PRODUCTION)
    assert out["projected_ms"]["T_backfill_prod"] == 0.0
    assert out["projected_ms"]["T_repair_prod"] == 0.0
    # 2000+400+300+100 = 2800ms over 1000 stale sessions = 2.8ms/row, x3 = 9.
    assert out["constants"]["MARGINED_MS_PER_ROW"] == 9
    assert out["constants"]["MARGINED_MS_PER_REPAIR_ROW"] == 6


def test_production_zero_populations_do_not_project_the_snapshots_row_work():
    """``_ratio`` must read a production zero as zero, not as "not recorded".

    Treating 0 as missing returned a scaling of 1.0 and projected the entire
    snapshot backfill onto a database with no stale rows at all.
    """
    assert harness._ratio(0, 5700) == 0.0
    assert harness._ratio(None, 5700) == 1.0


# ---------------------------------------------------------------------------
# The sweep envelope: the LP, its bases, its intake, and the frozen pair against
# the evidence on disk.
# ---------------------------------------------------------------------------

_SIZING_DIR = _BACKEND_DIR.parent / "docs" / "sizing"

_SWEEP_ARTIFACT = _SIZING_DIR / "sweep_batch_domain_20260725.json"

#: The endpoint basis (``g-b-sweep-endpoint-measure``), measured on a row-cloned
#: copy out to ``IMPORT_WORST_CASE_SWEEP_PAGES``.
_SWEEP_ENDPOINT_ARTIFACT = _SWEEP_ARTIFACT.parent / "sweep_batch_domain_endpoint_20260725.json"

#: The basis the shipped constants are frozen against — the revision's own
#: ``SIZED_*``, not any measuring copy's.
_FROZEN_BASIS = {
    "total_rows": mod.SIZED_TOTAL_ROWS,
    "sessions_bytes": mod.SIZED_SESSIONS_BYTES,
}


def _shipped_sweep_artifacts() -> list[tuple[str, dict]]:
    """Every sweep-domain artifact on disk, through the EXACT-decimal intake.

    The glob has to agree with ``derive``'s own selection, which picks its sweep
    inputs by ``kind == "sweep_domain"`` and refuses the pre-schema shape
    outright. Both rules are ASSERTED here rather than assumed: an artifact that
    quietly stopped matching them would be fitted by every test below while the
    real derivation ignored it — a disagreement between the evidence the suite
    checks and the evidence the constants come from, which is the one thing this
    file exists to prevent.

    That the two agree is no longer only asserted rule-by-rule. The
    atomic/batch/probe measurements are on disk now
    (``g-b-size-measurement-json``), so ``derive`` runs end to end over the
    committed set and names the sweep inputs it actually selected — checked
    against this glob in
    ``test_the_committed_derivation_selects_exactly_the_shipped_sweep_artifacts``.
    """
    paths = sorted(_SWEEP_ARTIFACT.parent.glob("sweep_*.json"))
    assert paths, f"no sweep-domain artifact under {_SWEEP_ARTIFACT.parent}"
    docs = [(p.name, harness._load_measurement_json(str(p))) for p in paths]
    for name, doc in docs:
        assert doc.get("kind") == "sweep_domain", f"{name}: derive would not select this"
        assert "dimensions_of_this_copy" not in doc, f"{name}: pre-schema shape"
    return docs


def _shipped_sweep_points() -> list[harness.SweepPoint]:
    out = []
    for name, doc in _shipped_sweep_artifacts():
        out.extend(harness.sweep_points(doc, _FROZEN_BASIS, artifact=name))
    return out


def test_shipped_sweep_artifact_matches_the_derive_schema():
    """The migrated artifact validates, and nothing was lost migrating it.

    It predates the schema ``derive`` requires: keyed by top-level ``runs`` and
    ``dimensions_of_this_copy``, with no ``kind`` and no ``dimensions_before``.
    Offered as it stood it would have matched nothing, and the run carrying the
    only retained trials on record would have been invisible.
    """
    doc = harness._load_measurement_json(str(_SWEEP_ARTIFACT))
    assert doc["kind"] == "sweep_domain"
    assert doc["sessions_synthesized"] is False
    points = harness.sweep_points(doc, _FROZEN_BASIS, artifact=_SWEEP_ARTIFACT.name)

    # Both runs survived, with their retention flags intact.
    assert len(points) == 24
    assert {p.run for p in points} == {"B", "C"}
    assert all(not p.steers for p in points if p.run == "B")
    assert all(p.steers for p in points if p.run == "C")
    assert {p.trials for p in points if p.run == "B"} == {3}
    assert {p.trials for p in points if p.run == "C"} == {7}

    # The copy's basis, exact, RECOMPUTED against the live frozen basis.
    n_copy = harness.sweep_copy_growth_factor(
        doc["dimensions_before"], _FROZEN_BASIS, artifact="shipped"
    )
    assert n_copy == Fraction(750, 661)

    # And NOT the factor the artifact carries, which is a different number for a
    # good reason. `frozen_basis.growth_factor_for_this_copy` is what this copy's
    # factor was against the basis in force WHEN THE RUN WAS TAKEN — the retired
    # 6,000 / 10,010,624 — and it is provenance, not an input: `derive` recomputes
    # from `dimensions_before` against whatever basis it is freezing. The 2026-07-27
    # re-freeze is what made the two differ, and the evidence was deliberately not
    # rewritten to match: an artifact records what a host did on a date, and
    # back-dating its arithmetic to a basis that did not exist yet would destroy the
    # one property the committed set has.
    retired_basis = {
        "total_rows": doc["frozen_basis"]["SIZED_TOTAL_ROWS"],
        "sessions_bytes": doc["frozen_basis"]["SIZED_SESSIONS_BYTES"],
    }
    assert retired_basis == {"total_rows": 6_000, "sessions_bytes": 10_010_624}
    assert float(
        harness.sweep_copy_growth_factor(
            doc["dimensions_before"], retired_basis, artifact="shipped"
        )
    ) == pytest.approx(float(doc["frozen_basis"]["growth_factor_for_this_copy"]))

    # The record of why a least-squares fit is not a cost model is still there.
    assert "withdrawn" in doc["fits"]
    assert "4.101 + 0.15424 * pages" in doc["fits"]["withdrawn"]["line"]


def test_frozen_sweep_model_matches_the_published_envelope():
    """The derivation is REPRODUCIBLE from the evidence on disk.

    Which is exactly what the withdrawn OLS line was not: it was computed over
    twelve maxima, published beside an eight-row table, copied into the bead as
    eleven, and its raw trials had not been kept — so no reader could re-derive
    it, and the two published forms disagreed.
    """
    fit = harness.solve_sweep_envelope(_shipped_sweep_points())
    assert mod.MARGINED_MS_BACKFILL_SWEEP_SCAN == math.ceil(harness.MARGIN * fit["a"])
    assert mod.MARGINED_US_BACKFILL_SWEEP_PER_PAGE == math.ceil(
        harness.MARGIN * fit["b"] * 1000
    )
    # Exact rationals, not floats: the vertex is picked at the last digits.
    assert fit["a"] == Fraction(35_354_079_496_799_386_439, 1_498_500_000_000_000_000)
    assert fit["b"] == Fraction(244_848_624_992_300_761, 1_498_500_000_000_000_000)
    # The line TOUCHES its worst points, which is what "least conservative" means.
    # Both are the ENDPOINT copy's, at the two ends of its domain — see
    # test_the_endpoint_basis_now_determines_the_fit for why that changed at the
    # re-freeze and why it is a finding rather than a regression.
    active = {(c["run"], c["pages"]) for c in fit["active_constraints"]}
    assert active == {("", 7), ("", 6_001)}
    assert fit["sum_overcharge_ms"] == pytest.approx(238.370280, abs=1e-5)


def test_frozen_sweep_model_covers_every_retained_measurement():
    """The invariant, carried as a test rather than as a table.

    For every measured point, de-normalized back onto the copy it actually ran on,
    the SHIPPED integers still cover ``3x`` what was measured there. True by
    construction — the LP covers every point in frozen-basis coordinates and both
    literals are ``ceil``-ed UP from ``MARGIN x`` the solution — which is the
    reason to assert it: a frozen model whose margined value drops below 3x a
    measurement it was fitted to has silently stopped being conservative, and
    nothing else in the revision would notice.

    Ranges over every point of every artifact on disk, each through ITS OWN
    ``N_copy``. A second measuring copy does not get a second invariant.
    """
    points = _shipped_sweep_points()
    assert points
    slacks = []
    for p in points:
        # backfill_sweep_ms at the point's own basis: g_sessions is 1 at the frozen
        # basis, so carrying the model back to the copy divides the SCAN component
        # by N_copy and leaves the per-page component alone.
        modelled = (
            mod.MARGINED_MS_BACKFILL_SWEEP_SCAN / float(p.n_copy)
            + mod.MARGINED_US_BACKFILL_SWEEP_PER_PAGE * p.pages / 1000
        )
        required = harness.MARGIN * float(p.max_ms)
        assert modelled >= required, (p.artifact, p.run, p.pages, modelled, required)
        slacks.append((modelled - required, p.run, p.pages))
    # The tightest point is one the LP made active — the endpoint copy at 7 pages —
    # so the margin is genuinely being spent on variance rather than on fit error.
    # Only the first, not the first two: the OTHER active constraint is the same
    # copy's 6,001-page endpoint, and at 6,001 pages the ceil() rounding of a
    # per-page slope is worth 5.1 ms of slack while at 7 pages it is worth 0.2 ms.
    # An active constraint is tight in the EXACT LP; the shipped integers are
    # rounded up from it, and that rounding is worth more where the page count is
    # larger. Ranking the shipped model's slack is therefore not the same as
    # listing the LP's active set, and asserting it were would pin an accident.
    slacks.sort()
    assert (slacks[0][1], slacks[0][2]) == ("", 7)
    assert {(c["run"], c["pages"]) for c in harness.solve_sweep_envelope(points)[
        "active_constraints"
    ]} == {("", 7), ("", 6_001)}


# ---------------------------------------------------------------------------
# The committed derivation set: `--derive`, end to end, over evidence on disk.
#
# `g-b-size-measurement-json`. Every number `--derive` consumes used to survive
# as a TRANSCRIPTION in the runbook, and a transcription cannot be re-run: a
# derivation error affecting a non-sweep constant was detectable only by
# re-reading arithmetic in prose. The sweep pair was the single exception —
# `solve_sweep_envelope` is a pure function of artifacts that WERE committed, so
# `test_frozen_sweep_model_matches_the_published_envelope` can re-derive it.
#
# The measurements below give every other term the same property. What they do
# NOT give it is the §3 literals the revision ships: those were measured on
# PostgreSQL 15.18 against a locally synthesized 6,000-row snapshot that no
# longer exists, and no run on any other fixture can return them. So the claim
# these tests make is the one the evidence supports — this derivation is
# reproducible, and its basis is not the shipped basis — and
# `test_the_committed_derivation_is_recorded_not_applied` is where the difference
# is pinned rather than glossed.
# ---------------------------------------------------------------------------

#: The measurement set `--derive` consumes, as the repo-relative paths the
#: runbook's §8 command passes it. The paths are load-bearing: `main` labels each
#: artifact with the path it was given, so the emitted provenance — and therefore
#: the committed output below — is a function of these strings as well as of the
#: files' contents.
#:
#: ONE RUN's manifest, not a listing of `docs/sizing/`. Phase 3 re-measures into
#: the same directory, and its artifacts belong in a set and a `derived_*.json`
#: of their own rather than appended here: the atomic and batch timings have to
#: come from a single fixture state for the frozen basis to mean anything. The
#: sweep domains are the exception the harness already handles — they are fitted
#: on their own copies' bases via `N_copy`, which is why the 2026-07-25 pair sits
#: in a 2026-07-26 derivation without mixing readings.
_COMMITTED_DERIVATION_SET = (
    "docs/sizing/atomic_full_20260726.json",
    "docs/sizing/atomic_empty_20260726.json",
    "docs/sizing/batch_b100_r200_20260726.json",
    "docs/sizing/batch_b250_r500_20260726.json",
    "docs/sizing/batch_b500_r1000_20260726.json",
    "docs/sizing/batch_b1000_r2000_20260726.json",
    "docs/sizing/cancel_probe_batch_20260726.json",
    "docs/sizing/cancel_probe_atomic_20260726.json",
    "docs/sizing/sweep_batch_domain_20260725.json",
    "docs/sizing/sweep_batch_domain_endpoint_20260725.json",
)

_COMMITTED_DERIVED = _SIZING_DIR / "derived_20260726.json"

#: What a `docs/sizing/` filename's leading token declares its `kind` to be. The
#: sweep artifacts predate the convention and keep their `sweep_` prefix, which is
#: exactly why one is needed: `_shipped_sweep_artifacts` selects them by globbing
#: `sweep_*.json`, so any artifact whose name could match that glob without being
#: a sweep domain would enter the fit.
_ARTIFACT_NAME_KINDS = {
    "sweep_": "sweep_domain",
    "atomic_": "atomic",
    "batch_": "batch",
    "cancel_probe_": "cancel_probe",
}


def _committed_measurements() -> list[dict]:
    """The committed set through `main`'s own intake, labelled the way it labels."""
    out = []
    for rel in _COMMITTED_DERIVATION_SET:
        doc = harness._load_measurement_json(str(_BACKEND_DIR.parent / rel))
        doc.setdefault("artifact", rel)
        out.append(doc)
    return out


def test_the_committed_measurement_set_re_derives_its_published_output():
    """`--derive` runs end to end from the repo, and returns what is committed.

    The check the bead exists for. `derive` is pure arithmetic over its inputs, so
    the committed measurements and the committed output are a closed pair: edit an
    artifact, change a formula, or reorder the inputs, and this fails. Compared as
    the SERIALIZED payload `main` writes, not field by field, because a
    field-by-field comparison silently ignores whatever it forgot to look at —
    and the terms most worth catching a change in are the ones no assertion here
    thought to name.

    It cannot fail closed on an edit to a SHIPPED literal, and no test can: the
    §3 constants were measured on PostgreSQL 15.18 against a snapshot that no
    longer exists. What it does fail closed on is this table drifting from the
    evidence behind it, which is the property §7's transcription never had.
    """
    fresh = json.dumps(harness.derive(_committed_measurements(), None), indent=2, default=str)
    assert fresh + "\n" == _COMMITTED_DERIVED.read_text()


def test_the_committed_derivation_selects_exactly_the_shipped_sweep_artifacts():
    """`derive`'s own selection, against the glob every sweep test fits over.

    `_shipped_sweep_artifacts` globs `sweep_*.json` and asserts the two rules
    `derive` selects by; this asserts the selections THEMSELVES agree, over the
    real input set. An artifact that matched the glob but not `kind`, or reached
    `derive` from somewhere the glob does not look, would leave the suite fitting
    a different domain than the derivation does.
    """
    out = harness.derive(_committed_measurements(), None)
    selected = [b["artifact"] for b in out["projected_ms"]["sweep_copy_growth_factors"]]
    assert selected == [f"docs/sizing/{name}" for name, _ in _shipped_sweep_artifacts()]
    # And every OTHER committed artifact reached the derivation too — the set is
    # the whole set, not the subset that happens to be fitted.
    assert [b["artifact"] for b in out["scaling"]["measurement_bases"]] == list(
        _COMMITTED_DERIVATION_SET
    )


#: Every name `--derive` is required to emit under `constants`, BY IDENTITY. This
#: is the contract between the derivation and the revision, and it is written out
#: rather than read off `derive`'s return value on purpose: a set derived from the
#: thing under test cannot notice that thing dropping a member.
_DERIVED_CONSTANT_NAMES = (
    "SIZED_TOTAL_ROWS",
    "SIZED_SESSIONS_BYTES",
    "SIZED_M_TOTAL",
    "SIZED_MOVES_BYTES",
    "MARGINED_MS_PER_ROW",
    "MARGINED_MS_PER_REPAIR_ROW",
    "MARGINED_MS_PER_SCAN_STMT",
    "MARGINED_MS_COVERAGE_ASSERT",
    "MARGINED_MS_BACKFILL_REMAINING",
    "MARGINED_MS_BACKFILL_SWEEP_SCAN",
    "MARGINED_US_BACKFILL_SWEEP_PER_PAGE",
    "SCAN_STMT_TIMEOUT_MS",
    "MAX_SINGLE_SESSION_COMPUTE_MS",
    "TEARDOWN_ALLOWANCE_MS",
    "MARGINED_MS_ATOMIC_TEARDOWN_FIXED",
    "MARGINED_US_ATOMIC_TEARDOWN_PER_ROW",
    "MAX_BATCH_SIZE",
    "DEFAULT_BATCH_SIZE",
    "REPAIR_BATCH_SIZE",
    "EST_MAX_LOCK_HOLD_MS",
)


def test_the_committed_derivation_is_applied_whole_with_the_basis_it_arrived_with():
    """A term and the basis it was measured against have to move together — APPLIED.

    This test is the inverse of the one it replaces. Until 2026-07-27 it asserted
    that the committed 18.4 derivation was RECORDED and not applied, and that the
    shipped literals were still the 15.18 ones: `SIZED_TOTAL_ROWS == 4_184 !=
    mod.SIZED_TOTAL_ROWS`. `g-b-sizing-harness`'s Phase 2 re-freeze applied it, so
    that `!=` became false by design and the test had to be rewritten rather than
    repaired. The claim it makes now is the stronger one and the one the whole
    arrangement was built to reach: EVERY shipped literal equals what `--derive`
    returns over evidence committed to this repo, and they are all from the SAME
    derivation.

    Whole, not row by row, and that is the entire point. The failure this guards is
    not a wrong number, it is a MIXED basis — a scan term taken from one run beside
    a dimension from another, each defensible alone and jointly describing no
    database that ever existed. `docs/sizing/derived_20260726.json` is one output of
    one `--derive` over one fixture state, so applying it whole is the only
    application that cannot be mixed, and comparing the whole constants block is the
    only comparison that notices a row nobody thought to name.

    The sweep pair still makes the point sharpest, because it is the one term whose
    two published values came from the SAME two artifacts and the same LP: 72 / 518
    at 6,000 / 10,010,624 and 71 / 491 at 4,184 / 6,144,000, no new measurement
    between them. What ships now is the second, and `test_frozen_sweep_model_
    matches_the_published_envelope` re-derives it from the artifacts on every run.
    """
    out = harness.derive(_committed_measurements(), None)
    consts, basis = out["constants"], out["scaling"]["frozen_basis"]

    atomic_full = harness._load_measurement_json(
        str(_BACKEND_DIR.parent / _COMMITTED_DERIVATION_SET[0])
    )
    assert basis["source"] == f"atomic snapshot {_COMMITTED_DERIVATION_SET[0]}"
    assert basis["reading"] == "post_synthesis"
    assert basis["dimensions"] == {
        k: int(atomic_full["dimensions_before"][k]) for k in harness.DIMENSION_KEYS
    }

    # The basis, applied — the atomic full point's own post-synthesis reading, which
    # is what its statements were timed against.
    assert (mod.SIZED_TOTAL_ROWS, mod.SIZED_SESSIONS_BYTES) == (4_184, 6_144_000)
    assert (mod.SIZED_M_TOTAL, mod.SIZED_MOVES_BYTES) == (130_676, 45_817_856)
    assert basis["dimensions"] == {
        "total_rows": mod.SIZED_TOTAL_ROWS,
        "sessions_bytes": mod.SIZED_SESSIONS_BYTES,
        "m_total": mod.SIZED_M_TOTAL,
        "moves_bytes": mod.SIZED_MOVES_BYTES,
    }

    # THE KEY SET FIRST, and pinned to a literal rather than to whatever `derive`
    # happened to return. Iterating `consts` alone is FORWARD-ONLY: it compares the
    # names the derivation emits today, so a term that disappears from `derive`
    # — and from the regenerated JSON with it — stops being compared, silently, and
    # the module literal it used to pin is then free to drift. Nothing downstream
    # notices, because `test_measured_constants_are_declared_and_positive` only
    # asserts the module's literals are declared and positive; it does not know
    # what any of them should equal. So a dropped name has to fail HERE.
    assert set(consts) == set(_DERIVED_CONSTANT_NAMES), sorted(
        set(consts) ^ set(_DERIVED_CONSTANT_NAMES)
    )

    # And every term with it, compared as the WHOLE block. A derived name with no
    # counterpart on the module is a derivation that grew a constant the revision
    # never adopted.
    derived = {name: getattr(mod, name) for name in _DERIVED_CONSTANT_NAMES}
    assert derived == consts, {
        k: (consts[k], derived[k]) for k in consts if consts[k] != derived[k]
    }

    # B_TESTED / R_TESTED are NOT in `constants` — they live under `batch_sizing`,
    # because they are the sizing DECISION's inputs rather than the revision's
    # admission arithmetic. The whole-block comparison above therefore cannot reach
    # them, and until they are bound explicitly they are the two shipped literals
    # with no committed counterpart at all. `min(formula, tested)` is what makes
    # `MAX_BATCH_SIZE` mean "a size sizing demonstrated", so an unpinned `B_TESTED`
    # is exactly the term that could raise the admitted batch past what any run
    # ever exercised.
    assert (mod.B_TESTED, mod.R_TESTED) == (
        out["batch_sizing"]["B_tested"],
        out["batch_sizing"]["R_tested"],
    )
    assert (out["batch_sizing"]["B_bound_by"], out["batch_sizing"]["R_bound_by"]) == (
        "tested",
        "tested",
    ), "both batch sizes are fixture-bound, not deadline-bound (g-b-fixture-moves-clone)"

    # Spelled out for the rows a reader will actually look for, so a diff of this
    # file shows what the re-freeze moved rather than only that it moved something.
    assert (mod.MARGINED_MS_BACKFILL_SWEEP_SCAN, mod.MARGINED_US_BACKFILL_SWEEP_PER_PAGE) == (
        71,
        491,
    )
    assert (mod.MARGINED_MS_PER_SCAN_STMT, mod.SCAN_STMT_TIMEOUT_MS) == (171, 171)
    assert (mod.MARGINED_MS_COVERAGE_ASSERT, mod.MARGINED_MS_BACKFILL_REMAINING) == (4, 4)
    assert (mod.MAX_BATCH_SIZE, mod.REPAIR_BATCH_SIZE) == (646, 1_000)
    assert (mod.B_TESTED, mod.R_TESTED) == (646, 1_000)
    assert mod.TEARDOWN_ALLOWANCE_MS == 6 and mod.EST_MAX_LOCK_HOLD_MS == 5_006


#: The kinds that run a scan block, and so the cohort every `game_sessions` scan
#: term is measured over. The cancel probes time a lock release and record no
#: scans; the sweep domains are a page-count measurement and time nothing
#: standalone.
_SCAN_BEARING_KINDS = frozenset({"atomic", "batch"})

#: That cohort, BY IDENTITY. A derivation names its own inputs
#: (`_COMMITTED_DERIVATION_SET`); this names which of them are entitled to price a
#: `game_sessions` scan, so the two cannot drift into meaning different files.
_SCAN_BEARING_ARTIFACTS = (
    "docs/sizing/atomic_full_20260726.json",
    "docs/sizing/atomic_empty_20260726.json",
    "docs/sizing/batch_b100_r200_20260726.json",
    "docs/sizing/batch_b250_r500_20260726.json",
    "docs/sizing/batch_b500_r1000_20260726.json",
    "docs/sizing/batch_b1000_r2000_20260726.json",
)


def _committed_game_sessions_scan_points(scan: str) -> list[tuple[str, Fraction, Fraction]]:
    """`(artifact, max_ms, N_copy)` for every committed point entitled to time `scan`.

    `N_copy` is `game_sessions`' factor and not `session_moves`': both statements
    read through here filter `game_sessions` on the unindexed version predicate and
    never touch `session_moves`, so each is carried onto the frozen basis by the
    same relation it scans. Per point, against the copy each one actually ran on —
    the six copies share 4,184 rows and their relations differ by 13%.

    The cohort is selected by `kind` and asserted by IDENTITY, not by size. A count
    is satisfied by a substitution: a batch point that quietly stops timing the
    statement, against a cancel probe that starts timing one, leaves six of
    something while changing which six — and if the substitute does not itself
    breach, every assertion downstream of it still passes. So membership is pinned
    three ways: the cohort is exactly `_SCAN_BEARING_ARTIFACTS`, every member timed
    `scan`, and nothing outside the cohort carries it. The last one matters most,
    because a cancel probe with a `scans` block is not a cheap extra reading — it
    is a measurement of a run that never executed a scan block at all.

    Shared by the two gates below ON PURPOSE. `BACKFILL_REMAINING_SQL` was inferred
    from `COVERAGE_ASSERT_SQL` precisely because they are the same shape against
    the same relation, so pricing them by two different normalizations here would
    make the pair of results incomparable — and the whole finding in
    `g-b-coverage-assert-18` is that one of them clears the check and the other
    does not.
    """
    assert set(_SCAN_BEARING_ARTIFACTS) <= set(_COMMITTED_DERIVATION_SET)
    docs = _committed_measurements()

    eligible = [doc for doc in docs if doc.get("kind") in _SCAN_BEARING_KINDS]
    assert tuple(doc["artifact"] for doc in eligible) == _SCAN_BEARING_ARTIFACTS

    missing = [doc["artifact"] for doc in eligible if scan not in (doc.get("scans") or {})]
    assert not missing, f"scan-bearing artifacts that did not time {scan}: {missing}"
    intruders = [
        doc["artifact"]
        for doc in docs
        if doc.get("kind") not in _SCAN_BEARING_KINDS and scan in (doc.get("scans") or {})
    ]
    assert not intruders, f"artifacts that run no scan block yet time {scan}: {intruders}"

    return [
        (
            doc["artifact"],
            Fraction(doc["scans"][scan]["max_ms"]),
            harness.sweep_copy_growth_factor(
                doc["dimensions_before"], _FROZEN_BASIS, artifact=doc["artifact"]
            ),
        )
        for doc in eligible
    ]


def _committed_backfill_remaining_points() -> list[tuple[str, Fraction, Fraction]]:
    return _committed_game_sessions_scan_points("backfill_remaining")


def _committed_coverage_assert_points() -> list[tuple[str, Fraction, Fraction]]:
    return _committed_game_sessions_scan_points("coverage_assert")


def _demanded(points: list[tuple[str, Fraction, Fraction]]) -> dict[str, int]:
    """The integer millisecond each point demands, on the frozen basis."""
    return {
        name: math.ceil(harness.MARGIN * max_ms * n_copy) for name, max_ms, n_copy in points
    }


def test_frozen_backfill_remaining_covers_every_committed_measurement():
    """`MARGINED_MS_BACKFILL_REMAINING = 4` is MEASURED, and it is exactly tight.

    The term shipped PROVISIONAL: the run that produced every other constant
    predates the discovery that the backfill's own `game_sessions` work is
    relation-scaled, so it never timed `BACKFILL_REMAINING_SQL` and the literal was
    inferred from `COVERAGE_ASSERT_SQL` — the same shape against the same relation.
    `g-b-size-derive-backfill-terms` timed it directly on PostgreSQL 18.4, and this
    is that measurement carried as a gate rather than as a table: six points, each
    divided onto the frozen basis by its own copy's `N_copy`, all covered by the
    shipped literal.

    The inference landed on the right integer at the 15.18 basis (6) and lands on
    the right integer at the re-frozen 18.4 basis (4), which is why this asserts
    EQUALITY at the worst point rather than mere coverage. What the re-freeze
    changed is the amount of room behind that equality: at the retired basis the
    worst point sat 1.7% below the rounding boundary, and here it sits 9.5% below
    it, because the copy the statement ran on IS the basis now and its
    `N_copy` is 1 rather than 1.629. The term that is one rounding step from
    under-charging is no longer this one — see the sibling test.
    """
    points = _committed_backfill_remaining_points()
    demanded = _demanded(points)
    for name, ms in demanded.items():
        assert mod.MARGINED_MS_BACKFILL_REMAINING >= ms, (name, ms)

    worst_name, worst_ms, worst_n_copy = max(points, key=lambda p: p[1] * p[2])
    assert worst_name == "docs/sizing/atomic_full_20260726.json"
    assert demanded[worst_name] == mod.MARGINED_MS_BACKFILL_REMAINING == 4

    # `ceil(3 * x) <= 4` iff `x <= 4/3`, so 1.333… ms at the frozen basis is the
    # rounding boundary the literal sits against. Exact rationals THROUGHOUT,
    # because the whole claim is about where a value falls relative to that
    # boundary: an approximate comparison would admit exactly the evidence drift
    # the tightness is asserted against.
    normalized = worst_ms * worst_n_copy
    assert normalized < Fraction(4, 3)
    assert normalized == Fraction(603_604_014_031_589, 500_000_000_000_000)  # ~1.207208
    # This copy IS the frozen basis, so it is carried by nothing. That is the whole
    # difference between the two readings of this term, and pinning it as an
    # equality with 1 keeps a future re-basing from passing here unnoticed.
    assert worst_n_copy == Fraction(1)


def test_frozen_coverage_assert_now_covers_its_own_measurement():
    """`MARGINED_MS_COVERAGE_ASSERT` survives the sibling's check — CLOSED, via (a).

    This asserted the opposite until 2026-07-27, and it was green because the breach
    was real (`g-b-coverage-assert-18`): at the 15.18 basis the same pairing over
    the same six artifacts put `COVERAGE_ASSERT_SQL` at 2.104080 ms, demanding 7
    against a shipped 6, with three of the six points demanding 7. The term
    `MARGINED_MS_BACKFILL_REMAINING` was INFERRED FROM under-charged itself while
    the borrowed term qualified.

    The old docstring named two legitimate exits: (a) re-freeze the literals and
    `SIZED_*` together from one committed set, or (b) patch the single literal to 7
    at the shipped basis and call it a patch. `g-b-sizing-harness`'s Phase 2
    re-freeze took (a). At the 18.4 basis the statement's own copy IS the basis, so
    its 1.291375 ms is carried by nothing and demands `ceil(3 x 1.291375) = 4` —
    which is what ships. Nothing about the measurement changed; the basis did.

    Why the inverted test is kept rather than deleted. The finding it recorded was
    never "6 is too small" — it was that a term and its basis have to move together,
    demonstrated on the one term where inheriting across a re-basing would have been
    wrong in BOTH directions (7 demanded at the old basis, 4 at the new). Deleting
    it would leave the re-freeze's most load-bearing case unguarded, and a future
    re-basing that carried 4 across onto a hotter basis would pass silently. So the
    pairing stays, pointed at the state that now holds.

    The tightness moved with it, and that is the live fact worth keeping: this term,
    not its sibling, is now the one nearest its rounding boundary — 3.1% below,
    against the sibling's 9.5%.
    """
    points = _committed_coverage_assert_points()
    demanded = _demanded(points)
    breaching = sorted(n for n, ms in demanded.items() if ms > mod.MARGINED_MS_COVERAGE_ASSERT)
    assert breaching == []
    # Every one of the six, not merely the worst — the old finding was that three of
    # them breached, so "none of them does" is the statement that retires it.
    assert set(demanded.values()) == {mod.MARGINED_MS_COVERAGE_ASSERT} == {4}

    worst_name, worst_ms, worst_n_copy = max(points, key=lambda p: p[1] * p[2])
    assert worst_name == "docs/sizing/atomic_full_20260726.json"
    assert demanded[worst_name] == mod.MARGINED_MS_COVERAGE_ASSERT == 4

    # `ceil(3 * x) <= 4` iff `x <= 4/3` — the boundary the sibling term sits 9.5%
    # under and this one sits 3.1% under. Exact rationals, same reason as there: the
    # value is pinned as the rational the committed artifact actually carries, not
    # as a float within a tolerance, so evidence drift is a failure rather than a
    # round.
    normalized = worst_ms * worst_n_copy
    assert normalized < Fraction(4, 3)
    assert normalized == Fraction(1_291_374_966_967_851, 1_000_000_000_000_000)  # ~1.291375
    assert worst_n_copy == Fraction(1)

    # This term is now the tighter of the two against the same boundary, which is
    # the fact a future re-measure has to be checked against. Asserted as an
    # ordering rather than as two percentages so it cannot drift into agreeing with
    # a stale comment.
    sibling_worst = max(_committed_backfill_remaining_points(), key=lambda p: p[1] * p[2])
    assert normalized > sibling_worst[1] * sibling_worst[2]

    # The armed timeout still covers the term's own demand by a wide margin, so no
    # statement is armed below its measured cost and there is no self-cancellation
    # path — the property that kept the old breach a qualification defect rather
    # than a live hazard, and that has to keep holding after the re-freeze moved
    # both numbers.
    assert mod.SCAN_STMT_TIMEOUT_MS >= demanded[worst_name]
    assert _budget() < mod.REVISION_DEADLINE_S * 1000


def test_docs_sizing_holds_measurement_artifacts_named_for_their_kind():
    """The naming convention, enforced rather than described.

    `docs/sizing/` is globbed by prefix — `sweep_*.json` is how the sweep domain
    is selected — so a filename is a selector and not a label. Every measurement
    there must declare the kind its name claims, and that holds over the whole
    directory rather than over one generation's set: Phase 3 measures into this
    same directory, and a convention checked only against the files that exist
    today stops being checked the moment it is used.

    Which artifacts a given derivation stands on is a separate question, answered
    by `_COMMITTED_DERIVATION_SET` and not by what happens to be on disk. All this
    asserts about that is that the inputs it names are still here.
    """
    on_disk = sorted(p.name for p in _SIZING_DIR.glob("*.json"))
    named = {pathlib.PurePosixPath(rel).name for rel in _COMMITTED_DERIVATION_SET}
    missing = (named | {_COMMITTED_DERIVED.name}) - set(on_disk)
    assert not missing, (
        f"the committed derivation names artifacts that are gone: {sorted(missing)}"
    )

    for name in on_disk:
        if name.startswith("derived_"):
            # A derivation's OUTPUT rather than a measurement, so it has no `kind`
            # to agree with. Asserted, not assumed: otherwise `derived_` becomes a
            # prefix under which a measurement can sit outside the convention.
            assert "kind" not in harness._load_measurement_json(str(_SIZING_DIR / name)), (
                f"{name} carries a `kind` — a measurement named as a derivation output"
            )
            continue
        prefixes = [p for p in _ARTIFACT_NAME_KINDS if name.startswith(p)]
        assert prefixes, f"{name}: no kind prefix — it cannot be selected by name"
        # Longest match: `cancel_probe_` starts with no other prefix, but a future
        # `batch_domain_` would sit under `batch_`.
        prefix = max(prefixes, key=len)
        doc = harness._load_measurement_json(str(_SIZING_DIR / name))
        assert doc["kind"] == _ARTIFACT_NAME_KINDS[prefix], (
            f"{name} declares kind {doc['kind']!r}, its name declares "
            f"{_ARTIFACT_NAME_KINDS[prefix]!r}"
        )


def test_measured_sweep_domain_reaches_the_page_count_the_budget_charges():
    """The domain is MEASURED to the endpoint, and the constant says so.

    It was not always: the pair was frozen from a domain that stopped at 1,647
    pages and was linearly extrapolated to the 6,001 the import-time budget
    evaluates, with the gap declared as an assumption here and in the docstring.
    ``g-b-sweep-endpoint-measure`` measured the endpoint on a production-shaped
    copy, so the assertion inverts: the evidence must now REACH
    ``IMPORT_WORST_CASE_SWEEP_PAGES``, and a docstring still declaring an
    extrapolation would be describing a state that no longer holds.

    The page count is the whole claim. A domain that stops short of the budget's
    own worst case cannot bound it, whichever direction the prose leans.

    The relation is ``>=``, not ``==``, and the re-freeze is why it has to be. The
    two were equal by construction when the endpoint run was commissioned: it was
    aimed at ``IMPORT_WORST_CASE_SWEEP_PAGES``, which was 6,001 at the 6,000-row
    basis. Re-freezing ``SIZED_TOTAL_ROWS`` to 4,184 moved the charged worst case
    down to 4,185 while the measured domain stayed where the host actually walked
    it, at 6,001. Measuring PAST the charged endpoint is the safe direction and the
    only one an equality would have rejected.
    """
    measured_max_pages = max(p.pages for p in _shipped_sweep_points())
    assert measured_max_pages == 6_001
    assert measured_max_pages >= mod.IMPORT_WORST_CASE_SWEEP_PAGES == 4_185

    source = pathlib.Path(mod.__file__).read_text()
    tree = ast.parse(source)
    # The comment block immediately above the constant is its docstring by the
    # `#:` convention this revision uses throughout, so the declaration is looked
    # for in the source rather than in `__doc__`.
    assert "MARGINED_US_BACKFILL_SWEEP_PER_PAGE = " in source
    declaration = source.split("MARGINED_US_BACKFILL_SWEEP_PER_PAGE = ")[0]
    assert f"{measured_max_pages:,}" in declaration
    assert "IMPORT_WORST_CASE_SWEEP_PAGES" in declaration
    # The word survives only in the past tense, describing how the pair was frozen
    # and what closed the gap. What must NOT survive is the live declaration.
    assert "EXTRAPOLATION, NAMED AS ONE" not in declaration
    assert "MEASURED PAST THE ENDPOINT" in declaration
    assert f"{mod.IMPORT_WORST_CASE_SWEEP_PAGES:,}" in declaration
    assert isinstance(tree, ast.Module)  # the source parsed; the split is over real code


def test_the_endpoint_basis_now_determines_the_fit():
    """The second basis is a MEASUREMENT, not a re-basing of the first — and at the
    re-frozen basis it is the one that BINDS.

    ``gr_p2_sweep6000`` is its own copy with its own ``dimensions_before``, and its
    points enter the same LP through their own ``N_copy`` alongside
    ``gr_p1_sweep``'s. Nothing is rebased, dropped or merged.

    Its ``N_copy`` is the clamp, and the clamp is why the roles swapped. The copy is
    LARGER than the frozen basis on both axes — 8,538 rows / 14,008,320 bytes — so
    ``max(1, ...)`` binds at 1 and its timings enter undiscounted, at BOTH the
    retired basis and this one. That makes its demand on ``a`` BASIS-INDEPENDENT:
    23.592979 ms, i.e. 71, either way. ``gr_p1_sweep``'s is not — its factor fell
    from 1.848714 to 1.134644 when the basis moved, and a baseline-only fit falls
    with it, from 23.867343 (72) to 14.648533 (44).

    So at the retired basis the baseline demanded MORE than the endpoint and the
    joint fit was the baseline's line, with the endpoint covered and inactive; at
    this one the baseline demands less and the joint fit IS the endpoint's line.
    This test used to be called ``…enters_the_fit_without_moving_it`` and asserted
    the first arrangement. Same two artifacts, same solver, no new measurement — the
    only thing that changed is the basis, which is the rule this whole file is built
    around, observed rather than argued.

    It also retires the reading that the endpoint run merely confirmed an
    extrapolation. It is now load-bearing: drop it and the frozen pair drops to
    44 / 518, below 3x what a host produced at 6,001 pages.
    """
    # The two fields `derive` keys on, pinned at the DOCUMENT rather than inferred
    # from the points it yields. `kind` is how `derive` selects a sweep input;
    # `sessions_synthesized` is the record that this copy's game_sessions was grown
    # by row cloning, the provenance that confines it to the sweep and nothing else
    # and that `derive` enforces against every other kind. Lose either quietly and
    # the fit asserted below stays green while the real derivation either skips the
    # artifact or forgets what it is.
    endpoint_doc = harness._load_measurement_json(str(_SWEEP_ENDPOINT_ARTIFACT))
    assert endpoint_doc.get("kind") == "sweep_domain"
    assert endpoint_doc.get("sessions_synthesized") is True

    # And that guard in `derive`'s own code, over the pair ALONE as an input set:
    # what stops a sweeps-only derivation is the missing atomic run, never
    # anything about these two. `sessions_synthesized` confines the endpoint copy
    # to the sweep domain, and the sweep domain is where it is being offered.
    with pytest.raises(SystemExit) as excinfo:
        harness.derive([doc for _, doc in _shipped_sweep_artifacts()], None)
    assert "--mode atomic" in str(excinfo.value)
    assert "sessions_synthesized" not in str(excinfo.value)

    by_artifact: dict[str, list[harness.SweepPoint]] = {}
    for p in _shipped_sweep_points():
        by_artifact.setdefault(p.artifact, []).append(p)
    endpoint = by_artifact[_SWEEP_ENDPOINT_ARTIFACT.name]
    baseline = by_artifact[_SWEEP_ARTIFACT.name]

    assert {p.n_copy for p in endpoint} == {Fraction(1)}
    assert {p.n_copy for p in baseline} == {Fraction(750, 661)}
    assert all(p.steers and p.trials >= harness.MIN_SWEEP_TRIALS for p in endpoint)
    assert max(p.pages for p in endpoint) > mod.IMPORT_WORST_CASE_SWEEP_PAGES

    # The joint vertex is the ENDPOINT's, and the baseline no longer reaches it.
    alone = harness.solve_sweep_envelope(baseline)
    endpoint_alone = harness.solve_sweep_envelope(endpoint)
    both = harness.solve_sweep_envelope(baseline + endpoint)
    assert (both["a"], both["b"]) == (endpoint_alone["a"], endpoint_alone["b"])
    assert (both["a"], both["b"]) != (alone["a"], alone["b"])
    assert both["a"] > alone["a"]
    assert both["coverage_points"] == 34 and both["objective_points"] == 22
    assert both["max_pages"] == 6_001

    # What the two fits are worth as SHIPPED integers, which is the form the claim
    # "load-bearing" has to be made in — a vertex that moved by less than a
    # millisecond would round to the same pair and change nothing.
    assert (
        math.ceil(harness.MARGIN * both["a"]),
        math.ceil(harness.MARGIN * both["b"] * 1000),
    ) == (mod.MARGINED_MS_BACKFILL_SWEEP_SCAN, mod.MARGINED_US_BACKFILL_SWEEP_PER_PAGE) == (71, 491)
    assert (
        math.ceil(harness.MARGIN * alone["a"]),
        math.ceil(harness.MARGIN * alone["b"] * 1000),
    ) == (44, 518)

    # And the frozen pair covers 3x the endpoint's own maximum, which is the claim
    # the whole bead exists to make: 6,001 pages priced by measurement.
    worst = max(endpoint, key=lambda p: p.pages)
    modelled = (
        mod.MARGINED_MS_BACKFILL_SWEEP_SCAN / float(worst.n_copy)
        + mod.MARGINED_US_BACKFILL_SWEEP_PER_PAGE * worst.pages / 1000
    )
    assert modelled >= harness.MARGIN * float(worst.max_ms)


def test_legacy_maxima_enter_the_coverage_set_without_steering_the_fit():
    """A published maximum is a measurement, and a bound has to cover it.

    Trial count decides whether a point may STEER a fit, never whether the bound may
    sit below a number a host actually produced. Run B's 3-trial points carry no
    retained trials, so they are accepted and land in the COVERAGE set only: 34
    coverage constraints against 22 in the objective, the difference being exactly
    run B's twelve.

    At the retired basis run B also happened to BIND — ``(4 pages, 13.60 ms)`` was an
    active constraint of the solution, and this test asserted that. It no longer is,
    and nothing about run B changed. Its copy's ``N_copy`` fell from 1.848714 to
    1.134644 at the re-freeze, so every demand it makes on ``a`` fell by the same
    factor, while the endpoint copy's stayed clamped at 1. The distinction the test
    exists for is unaffected: coverage-only points are still CHECKED, and a bound
    that dropped below one would still fail — see
    ``test_frozen_sweep_model_covers_every_retained_measurement``, which walks every
    point of every artifact including these.
    """
    points = _shipped_sweep_points()
    with_b = harness.solve_sweep_envelope(points)
    assert with_b["coverage_points"] == 34
    assert with_b["objective_points"] == 22
    b_points = [p for p in points if p.run == "B"]
    assert len(b_points) == 12 and not any(p.steers for p in b_points)

    # Not active, and not binding: dropping them leaves the vertex where it was.
    assert not any(c["run"] == "B" for c in with_b["active_constraints"])
    without_b = harness.solve_sweep_envelope([p for p in points if p.run != "B"])
    assert (without_b["a"], without_b["b"]) == (with_b["a"], with_b["b"])

    # Still covered, which is the part that was never about steering. Every run-B
    # maximum sits under the shipped model carried back onto run B's own copy.
    for p in b_points:
        modelled = (
            mod.MARGINED_MS_BACKFILL_SWEEP_SCAN / float(p.n_copy)
            + mod.MARGINED_US_BACKFILL_SWEEP_PER_PAGE * p.pages / 1000
        )
        assert modelled >= harness.MARGIN * float(p.max_ms), (p.pages, modelled)


def test_under_trialled_objective_run_is_refused():
    """The floor is scoped to evidence that STEERS the fit.

    A point claiming retained trials with fewer than ``MIN_SWEEP_TRIALS`` of them
    is a hard failure naming the point and the floor. The SAME point with
    ``raw_trials_retained: false`` is accepted as coverage-only — because refusing
    it outright would discard the only measurement at 4 pages and let the shipped
    bound sit below a maximum a host actually produced.
    """
    thin = _sweep_point(5, "20.0", trials=harness.MIN_SWEEP_TRIALS - 1)
    with pytest.raises(SystemExit, match=f"MIN_SWEEP_TRIALS \\({harness.MIN_SWEEP_TRIALS}\\)"):
        harness.sweep_points(
            _sweep_domain(_SWEEP_DEFAULT_POINTS + [thin]), _FROZEN_BASIS, artifact="thin"
        )
    coverage_only = dict(thin, raw_trials_retained=False)
    coverage_only.pop("trials_ms")
    points = harness.sweep_points(
        _sweep_domain(_SWEEP_DEFAULT_POINTS + [coverage_only]), _FROZEN_BASIS, artifact="ok"
    )
    assert [p.steers for p in points] == [True, True, False]


def test_lp_takes_the_A_zero_boundary_when_no_pair_intersection_is_feasible():
    """Both axes are load-bearing vertices, not defensive padding.

    With points at (10 pages, 1.0 ms) and (1000 pages, 1000.0 ms) the single
    pair-intersection has ``A < 0``. A solver that only enumerated pair
    intersections and the ``b = 0`` boundary would return the flat line at an
    over-charge of 999 ms and call it "least conservative"; the true optimum is
    ``(A = 0, b = 1)`` at 9 ms.
    """
    points = harness.sweep_points(
        _sweep_domain([_sweep_point(10, "1.0"), _sweep_point(1000, "1000.0")]),
        _measurement()["dimensions_before"],  # N_copy = 1
        artifact="axis",
    )
    fit = harness.solve_sweep_envelope(points)
    assert (fit["a"], fit["b"]) == (Fraction(0), Fraction(1))
    assert fit["sum_overcharge_ms"] == pytest.approx(9.0)


def test_lp_takes_the_b_zero_boundary_when_the_tie_break_prefers_a_flat_line():
    """The mirror image: one point, two vertices, identical over-charge.

    With a single measurement every line through it is equally conservative, so
    the tie-break decides — and it picks the SMALLER SLOPE, i.e. the flat line,
    rather than a per-page term one point cannot possibly evidence.
    """
    points = harness.sweep_points(
        _sweep_domain([_sweep_point(10, "5.0")]),
        _measurement()["dimensions_before"],
        artifact="flat",
    )
    fit = harness.solve_sweep_envelope(points)
    assert (fit["a"], fit["b"]) == (Fraction(5), Fraction(0))
    assert fit["sum_overcharge_ms"] == pytest.approx(0.0)


def test_sweep_fit_combines_two_copies_on_their_own_bases():
    """The failure mode the per-point ``N_copy`` exists to make impossible.

    Two copies of the same relation at different sizes. Their points are only
    jointly satisfiable in FROZEN-BASIS coordinates, because the leaner copy
    (``N_copy = 2``) is the one carrying the expensive measurements — and the
    leanest copy is always the one demanding the largest factor.

    Fitting raw ``(a, b)`` across both and multiplying ``a`` by ONE shared factor
    afterwards prices every point through whichever copy happened to be chosen,
    and here that BREACHES the very measurement it was fitted to.
    """
    frozen = {"total_rows": 6000, "sessions_bytes": 12_000_000}
    lean = _sweep_domain(  # N_copy = 2
        [_sweep_point(1, "30.0"), _sweep_point(101, "40.0")],
        dimensions={"total_rows": 3000, "sessions_bytes": 6_000_000},
    )
    bloated = _sweep_domain(  # N_copy = 1
        [_sweep_point(1, "10.0"), _sweep_point(101, "20.0")],
        dimensions={"total_rows": 6000, "sessions_bytes": 12_000_000},
    )
    points = harness.sweep_points(lean, frozen, artifact="lean") + harness.sweep_points(
        bloated, frozen, artifact="bloated"
    )
    assert {float(p.n_copy) for p in points} == {1.0, 2.0}

    fit = harness.solve_sweep_envelope(points)
    assert fit["a"] == Fraction(299, 5)  # 59.8
    assert fit["b"] == Fraction(1, 10)
    # Every point covered through ITS OWN basis.
    for p in points:
        assert fit["a"] / p.n_copy + fit["b"] * p.pages >= p.max_ms, (p.artifact, p.pages)

    # The counterfactual: the same LP solved basis-blind gives a raw intercept of
    # 29.9, and carrying it across with the BLOATED copy's factor of 1 leaves the
    # lean copy's 30.0 ms point uncovered by more than half.
    blind = harness.solve_sweep_envelope([p._replace(n_copy=Fraction(1)) for p in points])
    shared_basis_a = blind["a"] * Fraction(1)
    assert shared_basis_a == Fraction(299, 10)  # 29.9
    lean_point = next(p for p in points if p.artifact == "lean" and p.pages == 1)
    assert shared_basis_a / lean_point.n_copy + blind["b"] * 1 < lean_point.max_ms


def test_derive_parses_measurement_decimals_exactly():
    """A decimal timing must reach the fit as the number it was written as.

    ``155.0007`` has no exact binary float. A plain ``json.loads`` turns it into
    the nearest one BEFORE anything downstream can see it, and
    ``Fraction(<that float>)`` is then the exact value of a different number — at
    precisely the digits where the LP picks between vertices, and where a 68 ns
    transcription error has already broken one published bound on this data.
    """
    doc = harness._load_measurement_json(str(_SWEEP_ARTIFACT))
    points = harness.sweep_points(doc, _FROZEN_BASIS, artifact="exact")
    binding = next(p for p in points if p.pages == 824 and p.run == "C")
    assert binding.max_ms == Fraction(Decimal("155.0007"))
    assert binding.max_ms != Fraction(155.0007)  # what a plain loader would produce

    # Re-reading the same file reproduces the same solution, bit for bit.
    again = harness.sweep_points(
        harness._load_measurement_json(str(_SWEEP_ARTIFACT)), _FROZEN_BASIS, artifact="exact"
    )
    first, second = harness.solve_sweep_envelope(points), harness.solve_sweep_envelope(again)
    assert (first["a"], first["b"]) == (second["a"], second["b"])
    assert first["active_constraints"] == second["active_constraints"]

    # And the plain loader is refused rather than silently accepted.
    plain = json.loads(_SWEEP_ARTIFACT.read_text())
    with pytest.raises(SystemExit, match="_load_measurement_json"):
        harness.sweep_points(plain, _FROZEN_BASIS, artifact="plain")


def test_derive_requires_a_sweep_domain_and_accepts_several():
    """Zero is a hard failure; more than one is the expected shape."""
    without = [m for m in _complete() if m.get("kind") != "sweep_domain"]
    with pytest.raises(SystemExit, match="--mode sweep-domain"):
        harness.derive(without, None)

    # Two artifacts, each its own basis, both entering the same fit.
    second = _sweep_domain(
        [_sweep_point(3, "10.5", run="E"), _sweep_point(203, "31.0", run="E")],
        dimensions={"total_rows": 500, "sessions_bytes": 1_000_000},
        artifact="second-copy",
    )
    out = harness.derive(without + [_sweep_domain(), second], None)
    assert out["projected_ms"]["sweep_domain_points"] == 4
    bases = {b["artifact"]: b for b in out["projected_ms"]["sweep_copy_growth_factors"]}
    assert set(bases) == {"synthetic-sweep-domain", "second-copy"}
    assert bases["synthetic-sweep-domain"]["N_copy_exact"] == "1"
    assert bases["second-copy"]["N_copy_exact"] == "2"


def test_derive_rejects_a_row_cloned_copy_offered_as_anything_but_a_sweep_domain():
    """Clones carry no ``session_moves`` rows.

    So a copy grown by ``--synthesize-sessions`` can price the sweep and nothing
    else: every ``session_moves``-scaled term and the whole repair population on
    it are meaningless.
    """
    m = _measurement(sessions_synthesized=True)
    with pytest.raises(SystemExit, match="sessions_synthesized"):
        harness.derive([m] + _complete()[1:], None)
    # The same flag on the sweep domain itself is exactly what it is for.
    harness.derive(
        [x for x in _complete() if x.get("kind") != "sweep_domain"]
        + [_sweep_domain(sessions_synthesized=True)],
        None,
    )


def test_derive_names_the_migration_rather_than_finding_zero_artifacts():
    """The pre-schema shape matches nothing, and silence is the wrong failure.

    Offered as it stood, the one artifact carrying retained trials would have been
    invisible and the derivation would have reported "no sweep-domain
    measurement" about a file that is nothing but a sweep domain.
    """
    legacy = {
        "what": "the pre-migration shape",
        "dimensions_of_this_copy": {"n_stale": 1646, "g_sessions": 4184},
        "runs": [{"id": "B", "points": []}],
    }
    with pytest.raises(SystemExit, match="pre-schema shape"):
        harness.derive(_complete() + [legacy], None)


# ---------------------------------------------------------------------------
# Measurement provenance: which reading, recorded and checked.
#
# The harness synthesizes its populations BEFORE the measured pass, so the
# relation the timed statements ran against is the post-synthesis one. That is
# the CORRECT basis for those timings. The defect was that it was the only
# reading recorded and nothing said which one it was — while SIZED_*, the basis
# the runtime divides every scan term by, is frozen from it.
# ---------------------------------------------------------------------------


def test_the_harness_records_both_readings_and_names_the_one_it_timed_against():
    """Both readings on every artifact, and an explicit selector between them."""
    pre = {"total_rows": 1000, "sessions_bytes": 2_000_000,
           "m_total": 61_000, "moves_bytes": 8_000_000}
    m = _measurement(**_bases(_measurement()["dimensions_before"], pre=pre))
    bases = harness.measurement_bases(m, artifact="synthesized")

    assert bases["timing_basis"] == "post_synthesis"
    assert bases["timings_paired_with"] == m["dimensions_before"]
    assert bases["pre_synthesis_recorded"] is True
    # What the synthesis moved, in the shape the production restore showed:
    # session_moves DOWN by the plies synthesize_repair deleted, and nothing
    # else claimed to have changed.
    assert bases["synthesis_delta"]["m_total"] == -1_000
    assert bases["synthesis_delta"]["total_rows"] == 0


def test_derive_fails_closed_on_an_artifact_with_no_machine_readable_basis():
    """Unlabelled is REFUSED, not assumed. Checked for every kind, not just sweeps.

    A basis is never inferred — not from prose, not from a filename, not from a
    sibling copy, and not by inverting the frozen-basis calculation.
    """
    for kind, drop in (
        ("atomic", _measurement()),
        ("sweep_domain", _sweep_domain()),
        ("batch", _batch_measurement(500, 400.0)),
        ("cancel_probe", _probe("batch", 10.0)),
    ):
        unlabelled = {k: v for k, v in drop.items()
                      if k not in ("timing_basis", "dimension_bases")}
        others = [m for m in _complete() if m.get("kind") != kind]
        with pytest.raises(SystemExit, match="no machine-readable measurement basis"):
            harness.derive(others + [unlabelled], None)


def test_derive_refuses_a_basis_substituted_for_the_one_the_statements_timed_against():
    """The failure mode strictly worse than the defect itself.

    Swapping the recorded basis to the pre-synthesis reading while leaving the
    timings alone reads as a correction and is not one: it divides a statement
    timed against 6,144,000 bytes by a 4,096,000-byte basis.

    It is not refused for being optimistic. The error runs in both directions —
    ``N_copy`` is ``frozen / copy``, so substituting a sweep copy's own reading
    downward over-charges that point while substituting the FROZEN basis downward
    under-charges every point (and over-charges the run-time ``live / SIZED``).
    The guard holds because a timing and its basis move together, either way.
    """
    m = _measurement()
    swapped = dict(m["dimension_bases"]["post_synthesis"])
    swapped["sessions_bytes"] = 4_096_000  # the pre-synthesis reading, timings untouched
    m["dimension_bases"] = {**m["dimension_bases"], "post_synthesis": swapped}
    with pytest.raises(SystemExit, match="basis has been SUBSTITUTED"):
        harness.derive([m] + _complete()[1:], None)


def test_derive_refuses_a_substituted_basis_on_a_sweep_artifact_too():
    """Where the substitution is worth the most: N_copy is a per-point divisor."""
    sweep = _sweep_domain()
    lean = dict(sweep["dimension_bases"]["post_synthesis"])
    lean["sessions_bytes"] = 1_000_000
    sweep["dimension_bases"] = {**sweep["dimension_bases"], "post_synthesis": lean}
    without = [m for m in _complete() if m.get("kind") != "sweep_domain"]
    with pytest.raises(SystemExit, match="basis has been SUBSTITUTED"):
        harness.derive(without + [sweep], None)


def test_derive_refuses_a_timing_basis_it_does_not_measure_against():
    """``post_synthesis`` is the only basis this harness measures on.

    A timing may only be normalized by the basis of the copy it actually ran on;
    carrying it onto another is a separate, explicit step, not a field to relabel.
    """
    m = _measurement(timing_basis="pre_synthesis")
    with pytest.raises(SystemExit, match="only.*basis this harness measures against"):
        harness.derive([m] + _complete()[1:], None)


def test_derive_will_not_freeze_sized_from_a_copy_whose_displacement_is_unrecorded():
    """The one place the pre-synthesis reading gates anything.

    SIZED_* stays the POST-synthesis reading — term and basis have to move
    together — but a basis inflated by synthesis and frozen without that being
    visible leaves the runtime growth factor pinned at 1.0 across the whole gap,
    charging nothing extra the entire way.
    """
    legacy = _measurement(**_bases(_measurement()["dimensions_before"], legacy=True))
    with pytest.raises(SystemExit, match="supply the frozen SIZED_. dimensions"):
        harness.derive([legacy] + _complete()[1:], None)

    # The same input is fine once the basis comes from production facts that were
    # never synthesized: the question the gate asks does not arise.
    out = harness.derive([legacy] + _complete()[1:], _ZERO_PRODUCTION)
    assert out["constants"]["SIZED_SESSIONS_BYTES"] == 2_000_000


@pytest.mark.parametrize(
    "production",
    [
        pytest.param({}, id="empty-file"),
        pytest.param({"populations": _ZERO_PRODUCTION["populations"]}, id="populations-only"),
        pytest.param(
            {"dimensions": {"total_rows": 1000, "sessions_bytes": 2_000_000}}, id="two-of-four"
        ),
        pytest.param(
            {"dimensions": {**_ZERO_PRODUCTION["dimensions"], "m_total": 0}}, id="zero-dimension"
        ),
        pytest.param({"dimensions": _ZERO_PRODUCTION["dimensions"]}, id="no-populations"),
        pytest.param(
            {
                "dimensions": _ZERO_PRODUCTION["dimensions"],
                "populations": {"n_stale": 0, "n_repair": 0},
            },
            id="populations-missing-m_moves",
        ),
    ],
)
def test_the_production_escape_must_actually_declare_a_production_relation(production):
    """The gate above must not be waivable by a file that declares nothing.

    Passing ``--production-dimensions`` is what asserts the frozen basis is
    production fact rather than a synthesized copy. Presence of the flag used to
    be enough: any non-``None`` object turned the gate off while each missing
    block fell through to the SNAPSHOT, so an empty file froze the synthesized
    basis, skipped the check, and reported the source as
    ``--production-dimensions``. A zero dimension is refused for a different
    reason — it is the denominator ``_growth_factor`` divides by, and a zero one
    drops its ratio out entirely, pinning the factor at 1.0 however far the
    relation grows.
    """
    legacy = _measurement(**_bases(_measurement()["dimensions_before"], legacy=True))
    with pytest.raises(SystemExit, match="--production-dimensions"):
        harness.derive([legacy] + _complete()[1:], production)

    # And it is the DECLARATION that is refused, not the legacy artifact: a
    # complete one admits the same run.
    assert harness.derive([legacy] + _complete()[1:], _ZERO_PRODUCTION)


def test_a_legacy_sweep_artifact_still_constrains_the_bound_and_is_reported_as_incomplete():
    """Migrated, not discarded. The reading its timings ran against IS recorded.

    That is all the fit needs — normalization uses the basis the timing actually
    ran on. What is missing gates only the freeze of SIZED_*, which no sweep
    artifact supplies. The gap is stated in the output rather than left for a
    reader to notice.
    """
    sweep = _sweep_domain(**_bases(_measurement()["dimensions_before"],
                                   {"n_stale": 1000}, legacy=True))
    without = [m for m in _complete() if m.get("kind") != "sweep_domain"]
    out = harness.derive(without + [sweep], None)

    assert out["projected_ms"]["sweep_domain_points"] == 2
    base = out["projected_ms"]["sweep_copy_growth_factors"][0]
    assert base["pre_synthesis_recorded"] is False
    assert base["pre_synthesis_status"] == harness.LEGACY_UNRECORDED
    assert base["synthesis_delta"] is None
    assert base["timing_basis"] == "post_synthesis"


def test_derive_says_where_the_frozen_dimensions_came_from():
    """Which basis SIZED_* is, stated rather than worked out from what was passed."""
    snapshot = harness.derive(_complete(), None)["scaling"]["frozen_basis"]
    assert snapshot["reading"] == "post_synthesis"
    assert "atomic snapshot" in snapshot["source"]
    assert snapshot["dimensions"] == _measurement()["dimensions_before"]

    supplied = harness.derive(_complete(), _ZERO_PRODUCTION)["scaling"]["frozen_basis"]
    assert supplied["source"] == "--production-dimensions"
    assert supplied["reading"] == "production"


def test_derive_emits_both_readings_of_every_artifact_it_consumed():
    """The pre-synthesis reading is recorded and divided by NOWHERE.

    It is the evidence an explicit rebasing would start from, not an input to
    this derivation — so it belongs in the output and in no formula.
    """
    out = harness.derive(_complete(), None)
    emitted = out["scaling"]["measurement_bases"]
    assert len(emitted) == len(_complete())
    assert {b["kind"] for b in emitted} == {"atomic", "batch", "cancel_probe", "sweep_domain"}
    assert all(b["timing_basis"] == "post_synthesis" for b in emitted)
    assert all(b["pre_synthesis_recorded"] for b in emitted)


@pytest.mark.parametrize("path", [_SWEEP_ARTIFACT, _SWEEP_ENDPOINT_ARTIFACT])
def test_the_shipped_sweep_artifacts_carry_machine_readable_provenance(path):
    """The migration, pinned. Both are legacy on the pre-synthesis side and say so.

    Their post-synthesis reading was copied verbatim from their own
    ``dimensions_before``; nothing was reconstructed, and the equality below is
    what makes that claim checkable rather than a comment in a JSON file.
    """
    doc = harness._load_measurement_json(str(path))
    bases = harness.measurement_bases(doc, artifact=path.name)
    assert bases["timing_basis"] == "post_synthesis"
    assert bases["pre_synthesis_status"] == harness.LEGACY_UNRECORDED
    assert bases["timings_paired_with"] == {
        k: int(doc["dimensions_before"][k]) for k in harness.DIMENSION_KEYS
    }
    with pytest.raises(SystemExit, match="cannot supply"):
        harness.require_recorded_pre_synthesis(bases, purpose="supply anything")


def test_derive_reports_the_extrapolation_gap_in_its_own_output():
    """The measured ceiling beside the page count the budget evaluates.

    Stated in the emitted JSON rather than only in prose, because the budget is
    read from the JSON and the prose is not.
    """
    out = harness.derive(_complete(), None)["invariants"]
    gap = out["sweep_domain_max_pages_vs_import_worst_case"]
    assert gap["measured_max_pages"] == 11  # the synthetic domain's largest point
    assert gap["import_worst_case_pages"] == 1_001  # 1,000 sized rows at batch 1
    assert gap["extrapolated"] is True
    assert out["sweep_envelope_covers_every_measured_point"] is True
    assert out["scan_budget_sweep_pages"] == 1_001


def test_sweep_batch_size_list_is_deduplicated_and_range_checked():
    """``MAX_BATCH_SIZE`` is DERIVED, so an explicit duplicate of it is reachable.

    Measuring one point twice would enter the fit as two constraints at the same
    page count, double-weighting one host reading in the objective.

    Written against ``mod.MAX_BATCH_SIZE`` rather than against its value. It was
    spelled ``1_000`` here until the 2026-07-27 re-freeze moved it to 646, at which
    point the literal made this test fail for a reason that had nothing to do with
    deduplication — and the range check below would have started rejecting its own
    fixture.
    """
    assert harness.resolve_sweep_batch_sizes(
        [mod.MAX_BATCH_SIZE, 1, mod.MAX_BATCH_SIZE, 1]
    ) == [1, mod.MAX_BATCH_SIZE]
    assert harness.resolve_sweep_batch_sizes(None) == sorted(
        set(harness.DEFAULT_SWEEP_BATCH_SIZES)
    )
    with pytest.raises(SystemExit, match="resolve_batch_size admits"):
        harness.resolve_sweep_batch_sizes([mod.MAX_BATCH_SIZE + 1])
    with pytest.raises(SystemExit, match="resolve_batch_size admits"):
        harness.resolve_sweep_batch_sizes([mod.MIN_ADMITTED_BATCH - 1])


def test_sweep_generation_refuses_a_point_whose_trials_disagree_on_the_page_count():
    """A fit point is a PAIR, and both halves must come from the same sweep.

    Taking ``max(walked)`` and ``max(durations)`` independently builds a point no
    trial ever produced — the largest page count from one trial beside the slowest
    time from another. That is not merely imprecise: a higher page count with a
    lower cost RELAXES ``A / N + b x pages >= max_ms``, so the fictitious pair
    under-constrains the bound, in the one direction a bound must not move.

    Refused rather than averaged or majority-voted. Nothing mutates during an
    unlocked sweep, so a spread means a concurrent writer on a copy that is
    supposed to be disposable — and the per-trial counts are not retained in the
    artifact, so this is the only place it could ever be seen.
    """
    assert harness.agreed_sweep_pages([9, 9, 9], formula=9, batch_size=5) == 9
    with pytest.raises(SystemExit, match="population moved under it"):
        harness.agreed_sweep_pages([9, 10, 9], formula=9, batch_size=5)


def test_sweep_generation_refuses_a_page_count_the_formula_did_not_predict():
    """The formula is what every consumer prices from, so a disagreement is not
    local to the point that found it: the atomic projection and the import-time
    budget are both derived from ``ceil(n / b) + 1``, and if the runner's real
    paging differs then both are wrong by the same amount."""
    with pytest.raises(SystemExit, match="backfill_sweep_pages predicts"):
        harness.agreed_sweep_pages([10, 10, 10], formula=9, batch_size=5)


def test_sweep_domain_refuses_an_under_trialled_run_at_generation():
    """Enforced where the evidence is PRODUCED, not only where it is consumed.

    Everything the harness emits from here on is objective-eligible; the
    coverage-only path exists for the one legacy artifact that predates the rule.
    """
    with pytest.raises(SystemExit, match=str(harness.MIN_SWEEP_TRIALS)):
        harness.run_sweep_domain(
            None, batch_sizes=[1], trials=harness.MIN_SWEEP_TRIALS - 1, synthesized=False
        )


# ---------------------------------------------------------------------------
# Provenance. A measured constant whose input was never recorded is
# unfalsifiable, and the runbook is the only place those inputs live.
# ---------------------------------------------------------------------------


def _runbook() -> str:
    assert _RUNBOOK.exists(), f"{_RUNBOOK} must exist: it is the constants' only provenance"
    return _RUNBOOK.read_text()


@pytest.mark.parametrize("name", MEASURED_CONSTANTS + SIZED_DIMENSIONS)
def test_every_frozen_constant_is_named_in_the_runbook(name):
    assert name in _runbook()


@pytest.mark.parametrize(
    "marker",
    [
        # The cancel-to-unlock input, so a sizing run that measured only COMMIT
        # cannot silently produce TEARDOWN_ALLOWANCE_MS.
        "max_batch_cancel_to_unlock_ms",
        # The narrower rollback-only metric, recorded BESIDE it and named as
        # what it is rather than as the writer-felt tail.
        "rollback_only_teardown_ms",
        # Atomic teardown at TWO measurement points: one cannot give a floor and
        # a slope, so a floor-only or slope-only run cannot produce the pair.
        "T_atomic_teardown_empty",
        "T_atomic_teardown_full",
        "N_mut_snap",
        # Which bound won for each batch size, and every candidate tried.
        "B_formula",
        "B_tested",
        "R_formula",
        "R_tested",
        # The execution-mode verdict is executable, not advisory.
        "GHOSTREPLAY_ACCURACY_BACKFILL_MODE",
        # The sweep is FITTED rather than read off a maximum, so its provenance is
        # the fit: both coefficients, the measured page ceiling the extrapolation
        # starts from, and the basis every point was carried on.
        "sweep_scan_coeff_frozen_basis_ms",
        "sweep_envelope_per_page_ms",
        "sweep_domain_max_pages",
        "sweep_copy_growth_factors",
    ],
)
def test_runbook_records_the_measured_inputs(marker):
    assert marker in _runbook()


# ---------------------------------------------------------------------------
# The synthesis sample floor.
# ---------------------------------------------------------------------------


def test_repair_synthesis_enforces_the_contract_sample_floor():
    """K = 300 against 6,000 eligible sessions is what the first run did."""
    with pytest.raises(SystemExit, match="below the contract floor of 1000"):
        harness.check_repair_sample_size(300, 6_000)
    harness.check_repair_sample_size(harness.MIN_SYNTHESIZED_REPAIR, 6_000)
    harness.check_repair_sample_size(3_000, 6_000)


def test_repair_synthesis_floor_falls_back_to_the_whole_eligible_set():
    """"...or the whole set if smaller" — a snapshot with 40 eligible sessions
    cannot produce 1,000 candidates, and demanding it would make the harness
    unusable on a small restore rather than safer."""
    harness.check_repair_sample_size(40, 40)
    with pytest.raises(SystemExit, match="below the contract floor of 40"):
        harness.check_repair_sample_size(39, 40)


# ---------------------------------------------------------------------------
# Phase 3 — QUALIFICATION evidence
#
# Phase 2 derives the constants; Phase 3 runs the SHIPPED revision with them
# armed and breaks it on purpose. The artifacts under docs/sizing/phase3/ are the
# machine-readable record of those runs, taken by
# `scripts/phase3_cancellation_probe.py`. They are committed for the same reason
# the measurement set is: a qualification recorded only as prose cannot be
# re-checked, and the two silent-success failure modes this probe hit — a
# transaction-snapshotted `pg_stat_activity` that polls a process table from
# before the runner existed, and a park that outlives its own batch budget so the
# run dies of `statement_timeout` under the same SQLSTATE a cancel raises — both
# look exactly like clean results from the outside.
# ---------------------------------------------------------------------------

_PHASE3_DIR = _SIZING_DIR / "phase3"

#: The committed qualification runs, BY IDENTITY and by what each one is FOR.
_PHASE3_RUNS = {
    "run_3a_production_shaped_20260727.json": "none",
    "run_3a_populated_20260727.json": "none",
    "run_3c_cancel_backfill_batch_20260727.json": "batch",
    "run_3c_cancel_repair_batch_20260727.json": "repair",
    "run_3c_cancel_atomic_20260727.json": "atomic",
}


def _phase3(name: str) -> dict:
    return json.loads((_PHASE3_DIR / name).read_text())


def _phase3_cancels() -> list[tuple[str, dict]]:
    return [(n, _phase3(n)) for n, m in _PHASE3_RUNS.items() if m != "none"]


def test_committed_phase3_runs_are_the_named_set_at_the_shipped_constants():
    """The evidence is exactly these runs, and they are evidence about THESE constants.

    Both halves matter. Selecting by glob would let a stray artifact join the
    cohort and satisfy a coverage check that no qualified run actually satisfies.
    And a qualification run is only evidence about the constants it ran against —
    every artifact records the four that bound its transactions, so raising
    `REPAIR_BATCH_SIZE` without re-running Phase 3 fails here rather than leaving a
    stale JSON quietly vouching for a batch size nothing ever cancelled.
    """
    assert {p.name for p in _PHASE3_DIR.glob("*.json")} == set(_PHASE3_RUNS)
    for name, expected_mode in _PHASE3_RUNS.items():
        doc = _phase3(name)
        assert (doc["kind"], doc["mode"], doc["revision"]) == ("phase3_run", expected_mode, "20260719_01")
        assert doc["valid"] is True and doc["invalid_reasons"] == [], name
        assert doc["down_revision"] == mod.down_revision, name
        # The database each run RESOLVED to, which is what the probe records — with
        # `--url` the name typed on the command line need not be the one measured.
        # It came through the fence, so it is a disposable copy by construction.
        assert probe_guard.DISPOSABLE_RE.fullmatch(doc["database"]), (name, doc["database"])
        assert doc["constants"] == {
            "MAX_BATCH_SIZE": mod.MAX_BATCH_SIZE,
            "REPAIR_BATCH_SIZE": mod.REPAIR_BATCH_SIZE,
            "TEARDOWN_ALLOWANCE_MS": mod.TEARDOWN_ALLOWANCE_MS,
            "MAX_WRITER_STALL_MS": mod.MAX_WRITER_STALL_MS,
        }, name
        # One frozen state across all five, so the runs compose — and that has to
        # cover EVERY binding, not the file-level ones only. A cancel run taken
        # under a different synthesis or a different ANALYZE would otherwise sit in
        # the cohort agreeing on the revision and disagreeing on the fixture that
        # produced its numbers, which is exactly the composition this cohort claims.
        reference = _phase3("run_3a_populated_20260727.json")
        for key in ("frozen_files", "frozen_fingerprints", "frozen_symbols"):
            assert doc[key] == reference[key], (name, key)


def test_phase3_evidence_is_invalidated_by_a_behavioural_edit_to_what_it_ran():
    """The recorded fingerprints are compared to the LIVE files, or they mean nothing.

    Agreeing with each other only proves the five runs were taken together. What
    makes a qualification expire is the code moving underneath it, and that needs a
    comparison against the tree as it is now. Without this, a behavioural edit to
    the runner leaves every Phase 3 test green so long as the four constants happen
    to be unchanged — which is precisely the case the runbook claims invalidates the
    runs.

    Compared on the SEMANTIC fingerprint, not the content digest. The digest moves
    when a docstring is reflowed, so gating on it would demand a full Phase 3 re-run
    for edits that cannot affect a measurement, and a gate that fires on prose is a
    gate people learn to override. The fingerprint is the parsed AST with docstrings
    stripped: comments never reach it, `ast.dump` drops line and column numbers, and
    anything that changes a statement, a literal or an expression changes it.

    The CONTROLLER is in the set, not just the revision. It decides when the gates
    hold and what gets written, so a probe that cancels at the wrong moment produces
    a wrong number in the one way nothing downstream can detect.

    So are the revision's FROZEN IMPORTS. `20260719_01` pins `app.accuracy_v1` and
    `app.accuracy_rows_v1` rather than importing `app.accuracy`, and every per-row
    cost these runs measured is that algorithm executing — the batch sizes are
    literally quotients of it. A set naming the revision but not what it imports
    would stay green through an edit that changes both what the migration computes
    and how long a batch holds its rows. And the guard is in the set because it
    computes these fingerprints: a protocol that does not cover its own comparator
    can be weakened by editing the comparator.

    The SEEDING path is bound too, and counts are why. `populations_before` and
    `dimensions_before` do not identify a fixture: `synthesize_repair`'s own
    docstring records that taking candidates by `ORDER BY id` instead of
    `ORDER BY md5(id)` produces exactly K candidates and deletes exactly K plies —
    identical populations, identical relation sizes — while selecting the K lowest
    ids, which lets a merge join terminate a few percent into `session_moves` and
    measures every scan-bearing statement at a fraction of its real cost. Same
    numbers, different rows, different plans.

    That one is bound PER SYMBOL rather than per file. `size_accuracy_backfill.py`
    is ~2,500 lines of Phase 1 and Phase 2 machinery of which only the synthesis
    functions decide what a fixture is; fingerprinting the whole module would expire
    every qualification run whenever `derive` changed, and a gate that fires on
    unrelated edits is one people learn to override.
    """
    recorded = _phase3("run_3a_populated_20260727.json")["frozen_fingerprints"]
    assert set(recorded) == {
        "alembic/versions/20260719_01_backfill_session_player_accuracy.py",
        "app/accuracy_v1.py",
        "app/accuracy_rows_v1.py",
        "app/migration_guard.py",
        "alembic/env.py",
        "scripts/phase3_cancellation_probe.py",
        "scripts/phase3_fixture_guard.py",
        "scripts/phase3_seed_populations.py",
        "scripts/phase3_prepare.py",
    }
    # The preparer is Python, and it is in the set for that reason. It chooses the
    # template, decides that stamping happens after the clone and before seeding,
    # and reconstructs the pre-revision state — and `ast.parse` has nothing to say
    # about the shell script it used to be, so a component that cannot be
    # fingerprinted cannot be part of what expires these runs.
    assert not (_BACKEND_DIR / "scripts/phase3_prepare.sh").exists()
    live = {rel: probe_guard.semantic_fingerprint(_BACKEND_DIR / rel) for rel in recorded}
    assert live == recorded, (
        "a behaviour-bearing file changed since Phase 3 was taken: "
        f"{sorted(k for k in recorded if live[k] != recorded[k])}. "
        "Re-run the Phase 3 qualification (docs/release_b_runbook.md §10)."
    )

    symbols = _phase3("run_3a_populated_20260727.json")["frozen_symbols"]
    assert set(symbols) == {
        f"scripts/size_accuracy_backfill.py::{n}"
        for n in (
            "MIN_SYNTHESIZED_REPAIR",
            "analyze_after_synthesis",
            "check_repair_sample_size",
            "synthesize_repair",
            "synthesize_stale",
        )
    }
    live_symbols = {
        f"{rel}::{name}": digest
        for rel, names in probe.FROZEN_SYMBOLS.items()
        for name, digest in probe_guard.symbol_fingerprint(_BACKEND_DIR / rel, names).items()
    }
    assert live_symbols == symbols, (
        "the synthesis that built the Phase 3 fixture changed: "
        f"{sorted(k for k in symbols if live_symbols.get(k) != symbols[k])}. "
        "Re-run the Phase 3 qualification (docs/release_b_runbook.md §10)."
    )


def test_the_fixture_digest_binds_the_migrations_own_input_columns():
    """The digest's column lists are checked against the revision's SQL, not a memory.

    A fixture digest is only "which rows" for the columns it covers, and it is wrong
    in both directions. TOO NARROW and it agrees while the algorithm's input differs:
    `player_color` decides which side's plies are scored and `eval_cp` / `eval_mate`
    ARE the scores, so a copy with flipped colours or different eval density computes
    different accuracies, at a different cost, under an identical digest. TOO BROAD
    and it expires runs for nothing: `move_san` was in this list and is read by no
    statement in the revision.

    So the lists are derived from the revision's OWN SQL and compared for EQUALITY,
    in both directions and over every statement it holds — not a subset check against
    the two `SELECT`s I happened to remember. Editing what the revision touches
    without re-scoping the digest fails here.
    """
    from app.models import GameSession, SessionMove

    def selected(sql: str) -> list[str]:
        body = sql.split("SELECT", 1)[1].split("FROM", 1)[0]
        return [c.strip() for c in body.split(",")]

    def referenced(sql: str, table) -> set[str]:
        """Columns of `table` this statement NAMES — as tokens, not as substrings.

        `"player_accuracy" in sql` is satisfied by a statement that only mentions
        `player_accuracy_algo_version`, which is how a containment check quietly
        stops being a check. String literals are stripped first, so a value like
        `'converted'` cannot be mistaken for an identifier.
        """
        tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", re.sub(r"'[^']*'", " ", sql)))
        return tokens & {c.name for c in table.__table__.columns}

    # EVERY SQL constant the revision defines, not a chosen three. The population
    # predicate, the repair predicate, the coverage assertion, the ply detector, the
    # remaining-count queries and the updates all decide which rows exist, which are
    # in a population, and what gets written — and any of them can start reading a
    # column the digest does not cover.
    statements = [
        v
        for name, v in vars(mod).items()
        if isinstance(v, str)
        and not name.startswith("__")
        and re.search(r"\b(SELECT|UPDATE|INSERT|DELETE|WHERE|FROM)\b", v)
    ]
    assert len(statements) > 20, len(statements)
    touched_sessions = set().union(*(referenced(s, GameSession) for s in statements))
    touched_moves = set().union(*(referenced(s, SessionMove) for s in statements))

    # Keys are excluded from the comparison because the digest binds them separately,
    # as the leading term of each tuple rather than as an input — and `id` is a column
    # of BOTH tables, so a bare token cannot be attributed. Excluded is not unchecked:
    # `_digest_sql` takes the key as an argument, and the moves key is what puts a
    # ply's OWNERSHIP inside the digest. Keyed by `session_id`, re-parenting plies to
    # another session changes it; keyed by the surrogate `session_moves.id`, that move
    # is invisible while every column list above stays correct.
    assert "SELECT id::text || '|' || " in probe_guard._SESSIONS_DIGEST_SQL
    assert "FROM game_sessions" in probe_guard._SESSIONS_DIGEST_SQL
    assert "SELECT session_id::text || '|' || " in probe_guard._MOVES_DIGEST_SQL
    assert "FROM session_moves" in probe_guard._MOVES_DIGEST_SQL
    assert touched_sessions - {"id"} == set(probe_guard.SESSION_INPUT_COLUMNS), (
        "the revision's SQL and the fixture digest disagree about which session "
        "columns are inputs: "
        f"only in SQL {sorted(touched_sessions - {'id'} - set(probe_guard.SESSION_INPUT_COLUMNS))}, "
        f"only in digest {sorted(set(probe_guard.SESSION_INPUT_COLUMNS) - touched_sessions)}"
    )
    assert touched_moves - {"id", "session_id"} == set(probe_guard.MOVE_INPUT_COLUMNS), (
        "the revision's SQL and the fixture digest disagree about which move columns "
        "are inputs: "
        f"only in SQL {sorted(touched_moves - {'id', 'session_id'} - set(probe_guard.MOVE_INPUT_COLUMNS))}, "
        f"only in digest {sorted(set(probe_guard.MOVE_INPUT_COLUMNS) - touched_moves)}"
    )

    # And the payload projections specifically — the rows the algorithm is actually
    # handed. These are the two statements whose cost the sizing was measured from.
    assert selected(mod.SELECT_BATCH_FIRST_PG) == ["id", "player_color", "pgn"]
    assert selected(mod.LOAD_MOVES_PG) == [
        "session_id",
        "move_number",
        "color",
        "eval_cp",
        "eval_mate",
    ]
    assert set(selected(mod.LOAD_MOVES_PG)) - {"session_id"} <= set(
        probe_guard.MOVE_INPUT_COLUMNS
    )
    assert set(selected(mod.SELECT_BATCH_FIRST_PG)) - {"id"} <= set(
        probe_guard.SESSION_INPUT_COLUMNS
    )

    # Both predicates, by name, so neither can be dropped from the sweep unnoticed:
    # the repair population is a different filter over the same table and was once
    # missing from this test entirely.
    assert referenced(mod.POPULATION_PREDICATE_SQL, GameSession) == {
        "status",
        "session_mode",
        "drill_state",
        "player_accuracy_algo_version",
    }
    assert referenced(mod.REPAIR_PREDICATE_SQL, GameSession) == {
        "status",
        "session_mode",
        "drill_state",
        "player_accuracy_algo_version",
        "player_accuracy",
    }

    # And nothing the migration never reads. `move_san` is the one that was here.
    for unread in ("move_san", "fen_after", "fen_before", "classification", "eval_delta"):
        assert unread not in probe_guard.MOVE_INPUT_COLUMNS
        assert unread not in probe_guard._SESSIONS_DIGEST_SQL + probe_guard._MOVES_DIGEST_SQL
        assert not any(unread in s for s in statements), unread


_DIGEST_FIXTURE_SESSION = (
    "INSERT INTO game_sessions (id, user_id, started_at, ended_at, status, engine_elo, "
    "is_rated, player_color, pgn, session_mode, drill_state, player_accuracy, "
    "player_accuracy_algo_version) VALUES (CAST(:id AS uuid), 970001, :ts, :ts, 'ended', "
    "1500, true, :color, :pgn, 'normal', NULL, :accuracy, :version)"
)
_DIGEST_FIXTURE_MOVE = (
    "INSERT INTO session_moves (session_id, move_number, color, move_san, fen_after, "
    "eval_cp, eval_mate) VALUES (CAST(:sid AS uuid), :mn, :c, :san, 'fen', :cp, :mate)"
)


@pg_gate_plugin.pg_gate
def test_the_fixture_digest_moves_for_every_input_and_for_nothing_else(
    pg_migration_db, monkeypatch
):
    """The binding, exercised against a real database one column at a time.

    Asserting the column lists (above) says the digest is *scoped* correctly. It
    does not say the SQL built from them actually varies with those columns — a
    projection can name a column and still be blind to it, and this digest is the
    only thing standing between "same counts" and "same fixture".

    Both directions are the test. Every bound column must move it, or the fixture
    claim is weaker than it reads. No unbound column may move it, or the artifacts
    expire on edits that cannot change a measurement — and an expiry that fires on
    noise is one people learn to override.
    """
    from alembic import command
    from sqlalchemy import create_engine, text

    monkeypatch.setenv("DATABASE_URL", pg_migration_db)
    command.upgrade(_alembic_config(), "20260718_01")
    eng = create_engine(pg_migration_db)

    sid = "aaaaaaaa-0000-4000-8000-00000000000f"
    ts = __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc)
    with eng.begin() as c:
        c.execute(
            text(_DIGEST_FIXTURE_SESSION),
            {"id": sid, "ts": ts, "color": "white", "pgn": "1. e4 e5", "accuracy": 55.5,
             "version": 1},
        )
        for mn, color, cp in ((1, "white", 10), (1, "black", -20), (2, "white", 30)):
            c.execute(
                text(_DIGEST_FIXTURE_MOVE),
                {"sid": sid, "mn": mn, "c": color, "san": "e4", "cp": cp, "mate": None},
            )

    def digest() -> dict:
        with eng.connect() as c:
            return probe_guard.fixture_digest(c)

    def mutate(sql: str, **params) -> dict:
        with eng.begin() as c:
            c.execute(text(sql), params)
        return digest()

    base = digest()
    assert base["sessions_rows"] == 1 and base["moves_rows"] == 3

    # `session_mode` and `drill_state` cannot be moved one at a time against the
    # shipped schema: `ck_game_sessions_mode_drill_state` refuses a `drill_state` on a
    # normal session, and `ck_game_sessions_drill_rating_boundary` refuses a drill row
    # without the rest of one. Moving BOTH together would prove NEITHER — a digest
    # that had stopped encoding `session_mode` would still follow `drill_state`, and
    # every assertion would pass. So the two CHECKs come off this copy for the
    # duration and each column is moved alone. The definitions are read back rather
    # than restated so they cannot drift, they are restored below, and the database is
    # created per test and thrown away. The alternative is a gate blind to one of the
    # three columns that decide ended-visible.
    drill_checks = ("ck_game_sessions_mode_drill_state", "ck_game_sessions_drill_rating_boundary")
    with eng.begin() as c:
        check_defs = {
            name: c.execute(
                text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
                {"n": name},
            ).scalar()
            for name in drill_checks
        }
        assert all(check_defs.values()), check_defs
        for name in drill_checks:
            c.execute(text(f"ALTER TABLE game_sessions DROP CONSTRAINT {name}"))

    # Every bound input, one at a time, each restored before the next so the
    # movement observed is attributable to that column alone.
    bound = [
        ("UPDATE game_sessions SET player_color = 'black'",
         "UPDATE game_sessions SET player_color = 'white'"),
        ("UPDATE game_sessions SET pgn = '1. d4 d5'", "UPDATE game_sessions SET pgn = '1. e4 e5'"),
        ("UPDATE game_sessions SET player_accuracy = 61.25",
         "UPDATE game_sessions SET player_accuracy = 55.5"),
        ("UPDATE game_sessions SET player_accuracy_algo_version = 2",
         "UPDATE game_sessions SET player_accuracy_algo_version = 1"),
        # `status` ALONE. Clearing `ended_at` alongside it — which the schema does not
        # require — would have made the movement attributable to either column, and a
        # digest that needlessly encoded `ended_at` would have passed. `ended_at` is
        # mutated on its own below and must NOT move it.
        ("UPDATE game_sessions SET status = 'active'",
         "UPDATE game_sessions SET status = 'ended'"),
        # With `status`, the three columns that decide ended-VISIBLE, and so decide
        # the population size the whole sizing is scaled by.
        ("UPDATE game_sessions SET session_mode = 'drill'",
         "UPDATE game_sessions SET session_mode = 'normal'"),
        ("UPDATE game_sessions SET drill_state = 'converted'",
         "UPDATE game_sessions SET drill_state = NULL"),
        ("UPDATE session_moves SET eval_cp = 999 WHERE move_number = 2",
         "UPDATE session_moves SET eval_cp = 30 WHERE move_number = 2"),
        ("UPDATE session_moves SET eval_mate = 3 WHERE move_number = 2",
         "UPDATE session_moves SET eval_mate = NULL WHERE move_number = 2"),
        ("UPDATE session_moves SET move_number = 7 WHERE move_number = 2",
         "UPDATE session_moves SET move_number = 2 WHERE move_number = 7"),
        ("UPDATE session_moves SET color = 'black' WHERE move_number = 2",
         "UPDATE session_moves SET color = 'white' WHERE move_number = 2"),
    ]
    for change, undo in bound:
        assert mutate(change, ts=ts) != base, change
        assert mutate(undo, ts=ts) == base, undo

    with eng.begin() as c:
        for name, definition in check_defs.items():
            c.execute(text(f"ALTER TABLE game_sessions ADD CONSTRAINT {name} {definition}"))
    assert digest() == base

    # EVERY bound column, or the above is a sample rather than a proof. A column
    # added to the digest without an exercise here would otherwise be asserted about
    # statically and never moved. This counts what the statements NAME; what proves
    # the digest encodes each one is that each statement above moved it ALONE.
    covered = {c for change, _ in bound for c in re.findall(r"(?:SET|,)\s*(\w+)\s*=", change)}
    unexercised = (
        set(probe_guard.SESSION_INPUT_COLUMNS) | set(probe_guard.MOVE_INPUT_COLUMNS)
    ) - covered
    assert not unexercised, sorted(unexercised)

    # The KEYS, behaviourally. The moves digest is keyed by `session_id`, which is what
    # puts a ply's OWNERSHIP inside the digested tuple: re-parenting a ply to another
    # session changes it. Keyed by the surrogate `session_moves.id` instead, the same
    # move is invisible while every column list stays correct — so the keys are checked
    # rather than merely excluded from the column comparison.
    other_sid = "aaaaaaaa-0000-4000-8000-00000000001f"
    with eng.begin() as c:
        c.execute(
            text(_DIGEST_FIXTURE_SESSION),
            {"id": other_sid, "ts": ts, "color": "white", "pgn": "1. e4 e5",
             "accuracy": 55.5, "version": 1},
        )
    with_two = digest()
    assert with_two["sessions_rows"] == 2 and with_two["moves_rows"] == 3
    reparented = mutate(
        "UPDATE session_moves SET session_id = CAST(:other AS uuid) "
        "WHERE move_number = 2 AND color = 'white'",
        other=other_sid,
    )
    assert reparented["moves_rows"] == 3
    assert reparented["moves_digest"] != with_two["moves_digest"]
    assert mutate(
        "UPDATE session_moves SET session_id = CAST(:sid AS uuid) "
        "WHERE session_id = CAST(:other AS uuid)",
        sid=sid, other=other_sid,
    ) == with_two
    # And the sessions digest is keyed by `id`. That two rows digest differently from
    # one is multiplicity, not the key — it would hold with `id` dropped from the
    # tuple entirely. What proves the key is moving ONLY the id: the second session is
    # move-free and identical to the first in every bound column, so re-keying it
    # holds the row count and every input constant and leaves nothing else to move.
    third_sid = "aaaaaaaa-0000-4000-8000-00000000002f"
    rekeyed = mutate(
        "UPDATE game_sessions SET id = CAST(:new AS uuid) WHERE id = CAST(:other AS uuid)",
        new=third_sid, other=other_sid,
    )
    assert rekeyed["sessions_rows"] == 2 and rekeyed["moves_rows"] == 3
    assert rekeyed["sessions_digest"] != with_two["sessions_digest"]
    assert mutate(
        "UPDATE game_sessions SET id = CAST(:other AS uuid) WHERE id = CAST(:new AS uuid)",
        new=third_sid, other=other_sid,
    ) == with_two
    assert mutate(
        "DELETE FROM game_sessions WHERE id = CAST(:other AS uuid)", other=other_sid
    ) == base

    # And the eval columns specifically: an accuracy is COMPUTED from these, so a
    # fixture that differs only in eval density scores differently at a different
    # cost — the case a count-based identity calls identical.
    assert mutate("UPDATE session_moves SET eval_cp = NULL")["moves_digest"] != base["moves_digest"]
    assert mutate(
        "UPDATE session_moves SET eval_cp = 10 WHERE move_number = 1 AND color = 'white'"
    ) != base
    with eng.begin() as c:
        c.execute(text("UPDATE session_moves SET eval_cp = -20 WHERE color = 'black'"))
        c.execute(text("UPDATE session_moves SET eval_cp = 30 WHERE move_number = 2"))
    assert digest() == base

    # A deleted ply is what `synthesize_repair` does, and it must be visible.
    assert mutate("DELETE FROM session_moves WHERE move_number = 2")["moves_rows"] == 2
    with eng.begin() as c:
        c.execute(
            text(_DIGEST_FIXTURE_MOVE),
            {"sid": sid, "mn": 2, "c": "white", "san": "e4", "cp": 30, "mate": None},
        )
    assert digest() == base

    # Nothing the migration never reads. `move_san` was in this digest once.
    for change in (
        "UPDATE session_moves SET move_san = 'd4'",
        "UPDATE session_moves SET fen_after = 'other'",
        "UPDATE session_moves SET classification = 'blunder'",
        "UPDATE game_sessions SET engine_elo = 2400",
        "UPDATE game_sessions SET started_at = now()",
        # The migration reads `status`, never the timestamp beside it.
        "UPDATE game_sessions SET ended_at = NULL",
    ):
        assert mutate(change) == base, change
    eng.dispose()


def test_the_symbol_binding_refuses_to_cover_nothing():
    """A fingerprint gate that silently covers nothing is worse than no gate.

    Renaming or moving a bound symbol is exactly the edit that would turn its
    binding off, and the artifact would go on carrying a `frozen_symbols` block that
    still looks like coverage. So the helper raises instead of recording partial
    coverage — which is also what makes the set assertion above meaningful.
    """
    harness_path = _BACKEND_DIR / "scripts/size_accuracy_backfill.py"
    with pytest.raises(SystemExit, match="cannot fingerprint"):
        probe_guard.symbol_fingerprint(harness_path, ("synthesize_repair", "no_such_symbol"))

    # And it is per-symbol in the sense that matters: the same module holds `derive`
    # and the whole Phase 2 machinery, none of which is bound here.
    bound = probe_guard.symbol_fingerprint(
        harness_path, probe.FROZEN_SYMBOLS["scripts/size_accuracy_backfill.py"]
    )
    assert "derive" not in bound


def test_the_five_runs_measured_one_fixture_and_can_prove_which():
    """"Same fixture" is an OBSERVATION across the five runs, not a claim about them.

    Counts cannot carry it. `synthesize_repair` documents the case in its own
    docstring: `ORDER BY id` in place of `ORDER BY md5(id)` yields exactly K
    candidates and exactly K deleted plies — `populations_before` identical,
    `dimensions_before` identical — while selecting the K lowest ids and measuring
    every scan-bearing statement at a fraction of its cost. Two fixtures can agree on
    every number these artifacts previously recorded and still be different fixtures.

    So each artifact carries content digests of the accuracy-bearing columns
    (`fixture_identity` — *which* rows) and what `phase3_prepare.py` stamped on the
    copy before anything seeded it (`fixture_provenance` — which base data). Those
    make three things checkable that were previously only assertable.
    """
    docs = {name: _phase3(name) for name in _PHASE3_RUNS}
    clean = "run_3a_production_shaped_20260727.json"

    # 1. All five copies were cloned from ONE base. Five separate `CREATE DATABASE
    #    ... TEMPLATE` runs, five separate stamps, one set of digests.
    provenances = {n: d["fixture_provenance"] for n, d in docs.items()}
    for name, prov in provenances.items():
        assert prov is not None, f"{name} ran on a copy phase3_prepare.py never stamped"
        assert prov["template"] == "gr_p3_base", name
    base = provenances[clean]
    for name, prov in provenances.items():
        assert (prov["sessions_digest"], prov["moves_digest"]) == (
            base["sessions_digest"],
            base["moves_digest"],
        ), f"{name} was cloned from different base data than {clean}"

    # 2. The unseeded run's observed fixture IS its base — nothing touched it between
    #    the stamp and the read. That is also the digest function checking itself:
    #    two independent reads of unchanged data must agree.
    assert docs[clean]["fixture_identity"]["sessions_digest"] == base["sessions_digest"]
    assert docs[clean]["fixture_identity"]["moves_digest"] == base["moves_digest"]

    # 3. The four seeded runs agree with EACH OTHER and differ from the base. Four
    #    independently prepared copies, seeded by four separate `--repair 1000`
    #    invocations, produced byte-identical populations — which is the synthesis
    #    being deterministic, observed rather than assumed. And the moves digest
    #    moving is `synthesize_repair` having actually deleted plies: the corruption
    #    is visible in the record instead of being taken on trust.
    seeded = {n: d["fixture_identity"] for n, d in docs.items() if n != clean}
    one = next(iter(seeded.values()))
    for name, ident in seeded.items():
        assert ident == one, f"{name} seeded a different fixture than the other runs"
    assert one["sessions_digest"] != base["sessions_digest"]
    assert one["moves_digest"] != base["moves_digest"]
    seeded_k = docs["run_3a_populated_20260727.json"]["populations_before"]["n_repair"]
    assert one["moves_rows"] == base["moves_rows"] - seeded_k
    assert one["sessions_rows"] == base["sessions_rows"]


def test_phase3_cancel_evidence_covers_the_largest_admitted_transaction():
    """The Phase 3 counterpart of `derive`'s own `rows_locked` refusal.

    `TEARDOWN_ALLOWANCE_MS` is scoped to a batch of EITHER phase, and the larger is
    not the backfill's: `REPAIR_BATCH_SIZE` divides by a cheaper per-row cost, so it
    exceeds `MAX_BATCH_SIZE` (1,000 against 646). Phase 2 already refuses to freeze
    the constant unless its probe locked at least `max(MAX_BATCH_SIZE,
    REPAIR_BATCH_SIZE)` rows — `size_accuracy_backfill.derive` raises otherwise, and
    `cancel_probe_batch_20260726.json` carries `rows_locked = 1000` for exactly that
    reason.

    Phase 3 owes the same coverage against the SHIPPED revision, and for one
    revision of this work it did not have it: the only per-batch cancel took the
    backfill's 646-row guarded UPDATE, which is the SMALLER of the two admitted
    transactions. `repair` mode exists to close that, and this test is what stops it
    reopening — raise `REPAIR_BATCH_SIZE` and the committed evidence no longer
    reaches it.
    """
    # PER-BATCH-MODE runs only, and that restriction is the test. The atomic cancel
    # holds 1,646 rows — more than either batch size — so counting it here would
    # satisfy this check with the repair run deleted. It must not: TEARDOWN_
    # ALLOWANCE_MS is "of ONE PER-BATCH-MODE BATCH TRANSACTION" and the revision
    # says in as many words that it neither covers nor pretends to cover atomic
    # mode's whole-population transaction, which has its own two constants. A
    # bigger transaction of the WRONG KIND is not coverage; it is the substitution
    # failure this file guards against everywhere else.
    per_batch = [
        (n, d) for n, d in _phase3_cancels() if d["runner_mode"] == "batch"
    ]
    largest = max(mod.MAX_BATCH_SIZE, mod.REPAIR_BATCH_SIZE)
    covered = max(d["dirty_rows_at_cancel"]["value"] for _, d in per_batch)
    assert covered >= largest, f"per-batch cancel evidence reaches {covered} rows, need {largest}"

    # The atomic constants have the same obligation against their own scope, and
    # `derive` enforces it on the Phase 2 side (the atomic probe must lock at least
    # as many rows as the atomic transaction mutated). Its Phase 3 counterpart is
    # that the atomic cancel held the WHOLE population dirty, not a batch of it.
    atomic = _phase3("run_3c_cancel_atomic_20260727.json")
    pops = atomic["populations_before"]
    assert atomic["dirty_rows_at_cancel"]["value"] == pops["n_stale"] + pops["n_repair"]
    assert atomic["dirty_rows_at_cancel"]["value"] > largest

    # And the run that reaches it proves its own row count rather than asserting it.
    # A single-row `UPDATE` per candidate means an AFTER-STATEMENT park fires
    # `REPAIR_BATCH_SIZE` times, each with one more row dirty, so parking on the
    # FIRST would be evidence about a ONE-ROW transaction. The trigger counts
    # instead and publishes the count as the advisory lock's OBJID, which is what a
    # second session reads. objid == value == the batch size is that chain closed.
    repair = _phase3("run_3c_cancel_repair_batch_20260727.json")
    assert repair["dirty_rows_at_cancel"]["value"] == mod.REPAIR_BATCH_SIZE
    assert repair["park_objid"] == repair["dirty_rows_at_cancel"]["value"]
    assert repair["populations_before"]["n_repair"] >= mod.REPAIR_BATCH_SIZE


def test_every_phase3_cancel_died_of_the_cancel_and_under_the_allowance():
    """All four gates, the right SQLSTATE for the right REASON, and the number.

    `cancel_cause` is not decoration. A cancel and a `statement_timeout` breach both
    raise SQLSTATE 57014 and differ only in message text, so a park that outlives
    its batch's own budget produces a run that looks cancelled, reports a plausible
    unlock time, and is measuring the timeout path instead. It happened here at a
    2 s park. The discriminator has to be in the artifact or the trial cannot be
    audited afterwards.
    """
    for name, doc in _phase3_cancels():
        assert doc["result"] == "cancelled", name
        assert doc["gates"] == {
            "a_transactionid_xlock": True,
            "b_55P03_on_held_row": True,
            "c_statement_identity": True,
            "d_dirty_batch_advisory": True,
        }, name
        assert doc["cancel_cause"] == "user_request", name
        assert doc["pg_cancel_backend"] is True, name
        assert doc["target_pid"] != doc["canceller_pid"], name
        # The frozen constant has to cover what the shipped revision actually did.
        assert 0 < doc["cancel_to_unlock_ms"] < mod.TEARDOWN_ALLOWANCE_MS, name


def test_phase3_cancels_left_nothing_stamped_and_nothing_leaked():
    """A cancelled run is a rolled-back run, and the probe cleans up after itself.

    `alembic_version` unmoved and the CHECK still `NOT VALID` is the fail-closed
    claim: a breach mid-run leaves the database in the state the revision found it,
    so a rerun does the whole thing again. The two leak checks are about the
    HARNESS rather than the revision — a park trigger or a session-scoped advisory
    lock left behind on the copy would silently change every later run taken on it.
    """
    for name, doc in _phase3_cancels():
        t = doc["terminal"]
        assert t["alembic_version"] == doc["down_revision"] == "20260718_01", name
        assert t["check_convalidated"] is False, name
        assert (t["probe_trigger_left"], t["advisory_locks_left"]) == (0, 0), name
        # The run DIED. A cancel that landed on a backend already finishing returns
        # true, unlocks the row by committing, and exits 0.
        assert doc["alembic_returncode"] != 0, name

    # EVERY cancel's populations, against what it started with — the version stamp
    # alone does not say the data is unchanged. Each of the three has a different
    # expected relationship and all three have to be asserted, or the artifact that
    # is silent becomes the one where a durable mutation could hide.
    #
    #   atomic   ONE transaction, so nothing survives: both populations back exactly
    #            where they started. This is the load-bearing half of "fully rolled
    #            back" — an atomic run that left rows changed would otherwise pass.
    #   backfill the cancel breaks the FIRST batch of the run, so nothing had
    #            committed yet and both populations are also unchanged.
    #   repair   the cancel breaks a LATER transaction, so the backfill's batch had
    #            already committed and STAYS committed: n_stale is 0 and durable
    #            while the repair batch rolled back whole.
    #
    # Neither per-batch run demonstrates a PARTIAL batch — that needs a population
    # past one batch and is blocked on g-b-fixture-moves-clone.
    for name in (
        "run_3c_cancel_atomic_20260727.json",
        "run_3c_cancel_backfill_batch_20260727.json",
    ):
        doc = _phase3(name)
        before, t = doc["populations_before"], doc["terminal"]
        assert (t["n_stale"], t["n_repair"]) == (before["n_stale"], before["n_repair"]), name

    repair = _phase3("run_3c_cancel_repair_batch_20260727.json")
    assert repair["populations_before"]["n_stale"] == mod.MAX_BATCH_SIZE
    assert repair["terminal"]["n_stale"] == 0, "the committed backfill batch did not survive"
    assert repair["terminal"]["n_repair"] == repair["populations_before"]["n_repair"]


def test_only_a_populated_run_can_observe_the_atomic_stall():
    """The structural hole 3a found, pinned so a future reader does not re-find it.

    The design asks the production-shaped run for `observed_atomic_stall_ms`. It
    cannot supply it and no production-shaped run can: the stall probe reports from
    the FIRST ROW LOCK, and a run whose populations are both empty skips the runner
    and takes none. That is the same shape as the hole the design already names for
    3c — a clean run cancels nothing, so it cannot observe a cancellation — one
    level up. The observation needs its own populated run, which is what 3a' is.
    """
    clean = _phase3("run_3a_production_shaped_20260727.json")
    populated = _phase3("run_3a_populated_20260727.json")

    assert clean["populations_before"] == {"n_stale": 0, "n_repair": 0}
    assert any("skipping the runner" in ln for ln in clean["log"])
    assert not any("observed_atomic_stall_ms" in ln for ln in clean["log"])

    assert populated["populations_before"]["n_stale"] > 0
    stall = [ln for ln in populated["log"] if "observed_atomic_stall_ms" in ln]
    assert len(stall) == 1, populated["log"]
    observed = float(stall[0].split("observed_atomic_stall_ms=")[1].split()[0])
    projected = float(stall[0].split("projected_stall_ms=")[1].split()[0])
    assert 0 < observed < projected <= mod.MAX_WRITER_STALL_MS

    # Both `none` runs completed: stamped, validated, both populations converged.
    for doc in (clean, populated):
        assert doc["alembic_returncode"] == 0
        assert doc["terminal"]["alembic_version"] == "20260719_01"
        assert doc["terminal"]["check_convalidated"] is True
        assert (doc["terminal"]["n_stale"], doc["terminal"]["n_repair"]) == (0, 0)


def test_the_probe_discards_every_trial_that_measured_the_wrong_thing():
    """`validate()` directly, one rejection at a time.

    The "discarded, not recorded" contract used to be enforced by whoever read the
    output — the controller wrote its `--out` and exited 0 either way. Every failure
    below produces an artifact that LOOKS fine, which is the entire problem: gates
    that never held still leave `result` set and every other field populated, a
    `pg_cancel_backend` that returned false still leaves a plausible unlock time
    measured off a lock nobody was holding, and a `statement_timeout` raises the same
    SQLSTATE 57014 a cancel does. Left unenforced, any one of them lands in
    `docs/sizing/phase3/` and is indistinguishable from evidence.

    A BREACH is deliberately not in here. `cancel_to_unlock_ms` over
    `TEARDOWN_ALLOWANCE_MS` is a real finding and must be recorded loudly; this
    function only throws away trials that measured the wrong thing.

    THE CANCEL LANDING AND THE RUN DYING ARE TWO CLAIMS. A `pg_cancel_backend`
    against a backend that was already finishing returns true, the held row unlocks
    because the transaction COMMITTED, and the migration goes on to stamp
    `alembic_version` and validate the CHECK. Every cancel-side field in that trial
    is correct and the number in it is measured off a teardown that never happened.
    So the outcome is checked too, and the fabricated record below — a clean cancel
    on a run that SUCCEEDED — is the case that has to be rejected.
    """
    terminal_ok = {
        "alembic_version": "20260718_01",
        "check_convalidated": False,
        "n_stale": 0,
        "n_repair": 1000,
        "probe_trigger_left": 0,
        "advisory_locks_left": 0,
    }
    good_cancel = {
        "mode": "repair",
        "down_revision": "20260718_01",
        "gates": {"a": True},
        "pg_cancel_backend": True,
        "cancel_to_unlock_ms": 0.7,
        "cancel_cause": "user_request",
        "alembic_returncode": 1,
        "populations_before": {"n_stale": 646, "n_repair": 1000},
        "fixture_identity": {"sessions_digest": "d", "moves_digest": "d"},
        "fixture_provenance": {"template": "gr_p3_base", "sessions_digest": "b"},
        "terminal": terminal_ok,
    }
    assert probe.validate(good_cancel) == []

    # A breach is recorded, not discarded.
    breach = dict(good_cancel, cancel_to_unlock_ms=float(mod.TEARDOWN_ALLOWANCE_MS) * 10)
    assert probe.validate(breach) == []

    # The whole point, stated as one record: a cancel that looks perfect on a
    # migration that ran to completion. Nothing on the cancel side can tell.
    stamped = {
        **good_cancel,
        "alembic_returncode": 0,
        "terminal": {**terminal_ok, "alembic_version": "20260719_01", "check_convalidated": True},
    }
    reasons = probe.validate(stamped)
    assert len(reasons) == 3, reasons
    assert any("a cancelled run must fail" in r for r in reasons)
    assert any("leaves the stamp where it found it" in r for r in reasons)
    assert any("stayed NOT VALID" in r for r in reasons)

    def one(**over) -> str:
        problems = probe.validate({**good_cancel, **over})
        assert len(problems) == 1, problems
        return problems[0]

    assert "gates never all held" in one(gates={}, result="gates never all held; nothing cancelled")
    assert "pg_cancel_backend returned" in one(pg_cancel_backend=False)
    assert "never unlocked" in one(cancel_to_unlock_ms=None)
    assert "measured a timeout, not the cancel path" in one(cancel_cause="statement_timeout")
    assert "cancel_cause" in one(cancel_cause=None)
    assert "a cancelled run must fail" in one(alembic_returncode=0)
    assert "nothing to check the version stamp against" in one(down_revision=None)
    assert "leaves the stamp where it found it" in one(
        terminal={**terminal_ok, "alembic_version": "20260719_01"}
    )
    assert "stayed NOT VALID" in one(terminal={**terminal_ok, "check_convalidated": True})
    assert "trigger was left installed" in one(
        terminal={**terminal_ok, "probe_trigger_left": 1}
    )
    assert "advisory lock(s) still held" in one(
        terminal={**terminal_ok, "advisory_locks_left": 2}
    )

    # The populations, per mode — the half that says the CANCELLED TRANSACTION
    # rolled back. The stamp cannot say it: a durable partial mutation leaves
    # `alembic_version` exactly where a clean rollback does.
    assert "the terminal counts prove nothing" in one(populations_before=None)

    # A copy `phase3_prepare.py` never stamped cannot say what base data it
    # measured, and no later read can recover it — synthesis deletes plies and
    # rewrites the accuracy columns, so the copy no longer resembles its template.
    assert "not prepared by" in one(fixture_provenance=None)
    assert "the fixture is unobserved" in one(fixture_identity=None)
    assert "did not roll back whole" in one(terminal={**terminal_ok, "n_repair": 400})
    assert "did not cancel a repair-phase transaction" in one(
        terminal={**terminal_ok, "n_stale": 646}
    )

    # And the two whole-rollback modes, where BOTH populations must be untouched.
    for m in ("atomic", "batch"):
        whole = {**good_cancel, "mode": m, "terminal": {**terminal_ok, "n_stale": 646}}
        assert probe.validate(whole) == []
        landed = probe.validate({**whole, "terminal": {**terminal_ok, "n_stale": 646, "n_repair": 0}})
        assert len(landed) == 1 and "expected (646, 1000) unchanged" in landed[0], landed

    # `none` runs are judged on a different contract: they must SUCCEED and stamp.
    good_none = {
        "mode": "none",
        "alembic_returncode": 0,
        "fixture_identity": {"sessions_digest": "d"},
        "fixture_provenance": {"template": "gr_p3_base"},
        "terminal": {
            "alembic_version": "20260719_01",
            "probe_trigger_left": 0,
            "advisory_locks_left": 0,
        },
    }
    assert probe.validate(good_none) == []
    assert "expected 0" in probe.validate({**good_none, "alembic_returncode": 1})[0]
    assert "terminal alembic_version" in probe.validate(
        {**good_none, "terminal": {**good_none["terminal"], "alembic_version": "20260718_01"}}
    )[0]


def test_the_disposable_name_fence_refuses_what_it_should():
    """The fence in front of `DROP DATABASE`, and the realistic failure it catches.

    Not a deliberate misfire at production — a typo in a hand-typed database name.
    `--confirm-mutates` alone would be typed straight past that, which is why the
    naming rule exists beside it and why it is narrow rather than a blocklist.
    """
    probe_guard.check_disposable_name("gr_p3c_batch", template="gr_p3_base")

    for bad in ("ghostreplay", "postgres", "gr_p1_sweep", "gr_m_probe_batch", "GR_P3A", "gr_p3a; drop"):
        with pytest.raises(SystemExit, match="refusing to touch"):
            probe_guard.check_disposable_name(bad)

    # Fixtures other runs are restored from, even though they match the pattern.
    with pytest.raises(SystemExit, match="fixtures other runs are restored from"):
        probe_guard.check_disposable_name("gr_p3_base")
    # A target that is its own template destroys the fixture it needs.
    with pytest.raises(SystemExit, match="it is the template"):
        probe_guard.check_disposable_name("gr_p3x", template="gr_p3x")

    # Whatever survives the fence is safe to interpolate into DDL that cannot take
    # a bind parameter, which is what the shell script relies on.
    assert probe_guard.DISPOSABLE_RE.fullmatch("gr_p3c_batch")
    assert not any(c in "gr_p3c_batch" for c in " '\";\\-")


def test_neither_name_rule_admits_trailing_whitespace():
    """`$` is not the end of the string in Python — it also matches before a final
    newline, so `^gr_p3[a-z0-9_]*$` accepts `"gr_p3x\\n"`.

    Not injection: a newline cannot start a second statement inside a quoted
    identifier. But it defeats a fence that promises no whitespace at all, and the
    damage is asymmetric — the target is dropped by the FIRST statement and a
    newline-suffixed template then fails the second, leaving the operator with no
    database rather than a rejected command. Both rules are `\\A…\\Z` and both are
    applied with `fullmatch`; either alone would be sufficient.
    """
    for bad in ("gr_p3x\n", "gr_p3x\n\n", "gr_p3x ", " gr_p3x", "gr_p3x\t", "gr_p3x\r"):
        with pytest.raises(SystemExit, match="refusing to touch"):
            probe_guard.check_disposable_name(bad)
    for bad in ("gr_p3_base\n", "gr_p3_base ", "\ngr_p3_base", "gr_p3_template\n"):
        with pytest.raises(SystemExit, match="refusing to clone from"):
            probe_guard.check_template_name(bad)

    # The anchors themselves, so a future edit back to `^…$` fails here and not in
    # front of a `DROP DATABASE`.
    for rx in (probe_guard.DISPOSABLE_RE, probe_guard.TEMPLATE_RE):
        assert "$" not in rx.pattern and rx.pattern.endswith(r"\Z"), rx.pattern


def test_the_template_identifier_is_fenced_by_its_own_rule():
    """`CREATE DATABASE <target> TEMPLATE <template>` has TWO identifiers in it.

    Checking only the target leaves the other half unfenced, and the template is
    interpolated into the same statement — so a name carrying a quote reaches DDL
    that cannot take a bind parameter just as easily from that side. The rule is the
    exact inverse of the target's: a target must NOT carry a fixture suffix, a
    template MUST. That is a role check as well as a syntax one — cloning from a
    working database is a `CREATE DATABASE` against something that may be in use,
    and it silently makes whatever it copied into a fixture.

    Checked BEFORE the drop, because the drop and the create are two statements and
    a template rejected between them leaves the operator with no database at all.
    """
    probe_guard.check_pair("gr_p3c_batch", "gr_p3_base")
    probe_guard.check_template_name("gr_p3_template")

    for bad in (
        "gr_p3_base\"; drop database ghostreplay; --",  # the reason this exists
        "ghostreplay",  # a live database, not a fixture
        "gr_p3c_batch",  # matches the TARGET rule, so it is a disposable copy
        "postgres",
        "GR_P3_BASE",
    ):
        with pytest.raises(SystemExit, match="refusing to clone from"):
            probe_guard.check_template_name(bad)

    # The pair check runs the target rule too.
    with pytest.raises(SystemExit, match="refusing to touch"):
        probe_guard.check_pair("ghostreplay", "gr_p3_base")
    # And the two rules together make target == template unreachable rather than
    # merely refused: the equality check that used to catch it now never fires here,
    # because anything a template is allowed to be, a target is already refused for
    # being. The suffix rule catches it first, one step earlier.
    with pytest.raises(SystemExit, match="fixtures other runs are restored from"):
        probe_guard.check_pair("gr_p3_base", "gr_p3_base")

    assert not any(c in "gr_p3_base" for c in " '\";\\-")


def test_the_fence_records_the_database_it_resolved_to():
    """With `--url`, the name typed and the database measured can be different.

    Both can be disposable and both can pass the name check, so nothing about the
    fence catches it — and an artifact that records the positional argument then
    identifies a database it never touched. `confirm_mutates` returns what
    `current_database()` said and the helpers record THAT; `expect=` additionally
    refuses the divergence, because the positional is what the runbook command line
    and every message names.
    """

    class _Conn:
        def execute(self, stmt):
            sql = str(stmt)
            return type("R", (), {"scalar": lambda _self: "gr_p3c_batch" if "current_database" in sql else "PostgreSQL 18.4"})()

    resolved = probe_guard.confirm_mutates(_Conn(), confirmed=True, what="x")
    assert resolved == "gr_p3c_batch"
    assert probe_guard.confirm_mutates(_Conn(), confirmed=True, what="x", expect="gr_p3c_batch")

    with pytest.raises(SystemExit, match="the record of this run would name the other"):
        probe_guard.confirm_mutates(_Conn(), confirmed=True, what="x", expect="gr_p3a_clean")
    # And the fence still comes first: consent is refused on the resolved name.
    with pytest.raises(SystemExit, match="without --confirm-mutates"):
        probe_guard.confirm_mutates(_Conn(), confirmed=False, what="x")
