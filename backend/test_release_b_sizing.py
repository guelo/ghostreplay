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
from decimal import Decimal
from fractions import Fraction

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

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
    # The backfill's OWN game_sessions work. The convergence count is PROVISIONAL
    # until g-b-size-derive-backfill-terms times it directly, but declared,
    # positive and load-bearing now: the scan budget and the atomic projection both
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


def _measurement(**over):
    """A minimal but complete atomic measurement, with everything nonzero."""
    base = {
        "kind": "atomic",
        "batch_size": 500,
        "repair_batch_size": 200,
        "dimensions_before": {
            "total_rows": 1000,
            "sessions_bytes": 2_000_000,
            "m_total": 60_000,
            "moves_bytes": 8_000_000,
        },
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
    base = {
        "kind": "sweep_domain",
        "artifact": "synthetic-sweep-domain",
        "sessions_synthesized": False,
        "dimensions_before": dimensions or _measurement()["dimensions_before"],
        "populations_before": {"n_stale": 1000},
        "points": list(points if points is not None else _SWEEP_DEFAULT_POINTS),
    }
    base.update(over)
    return base


def _probe(scope, unlock_max, *, trials=harness.MIN_CANCEL_TRIALS, rows_locked=100_000):
    return {
        "kind": "cancel_probe",
        "scope": scope,
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

_SWEEP_ARTIFACT = _BACKEND_DIR.parent / "docs" / "sizing" / "sweep_batch_domain_20260725.json"

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

    The glob stands in for ``derive``'s own selection, which these tests cannot
    reach: ``derive`` picks its sweep inputs by ``kind == "sweep_domain"`` and
    refuses the pre-schema shape outright, but getting that far needs the
    atomic/batch/probe measurements, and those were never committed
    (``g-b-size-measurement-json``). So both selection rules are ASSERTED here
    rather than assumed. An artifact that quietly stopped matching them would be
    fitted by every test below while the real derivation ignored it — a
    disagreement between the evidence the suite checks and the evidence the
    constants come from, which is the one thing this file exists to prevent.
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

    # The copy's basis, exact and agreeing with its own recorded factor.
    n_copy = harness.sweep_copy_growth_factor(
        doc["dimensions_before"], _FROZEN_BASIS, artifact="shipped"
    )
    assert n_copy == Fraction(1222, 661)
    assert float(n_copy) == pytest.approx(
        float(doc["frozen_basis"]["growth_factor_for_this_copy"])
    )

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
    assert fit["a"] == Fraction(16_170_721_723, 677_525_000)
    assert fit["b"] == Fraction(1_414_007, 8_200_000)
    # The line TOUCHES its worst points, which is what "least conservative" means
    # — and one of them is run B's, the only evidence at 4 pages.
    active = {(c["run"], c["pages"]) for c in fit["active_constraints"]}
    assert active == {("B", 4), ("C", 824)}
    # Over BOTH bases now. The endpoint artifact added ten points, all of them
    # covered and none of them active, so the objective's total over-charge grows
    # while the line itself does not move — see
    # test_the_endpoint_basis_enters_the_fit_without_moving_it.
    assert fit["sum_overcharge_ms"] == pytest.approx(276.580163, abs=1e-5)


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
    # The two tightest points are the ones the LP made active — run C at 824 pages
    # and run B's outlier at 4 — so the margin is genuinely being spent on
    # variance rather than on fit error.
    slacks.sort()
    assert {(run, pages) for _, run, pages in slacks[:2]} == {("C", 824), ("B", 4)}


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
    """
    measured_max_pages = max(p.pages for p in _shipped_sweep_points())
    assert measured_max_pages == mod.IMPORT_WORST_CASE_SWEEP_PAGES == 6_001

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
    assert "MEASURED TO THE ENDPOINT" in declaration
    assert isinstance(tree, ast.Module)  # the source parsed; the split is over real code


def test_the_endpoint_basis_enters_the_fit_without_moving_it():
    """The second basis is a MEASUREMENT, not a re-basing of the first.

    ``gr_p2_sweep6000`` is its own copy with its own ``dimensions_before``, and its
    points enter the same LP through their own ``N_copy`` alongside
    ``gr_p1_sweep``'s. Nothing is rebased, dropped or merged — which is exactly
    what makes "the fit did not move" a finding rather than a construction.

    Its ``N_copy`` is the clamp: the copy is LARGER than the frozen basis on both
    axes (8,538 rows / 14,008,320 bytes against 6,000 / 10,010,624), so
    ``max(1, ...)`` binds at 1 and its timings enter undiscounted. That is what
    makes it admissible where the fixture-scale linearity probe is not — a copy
    whose relation sits far BELOW the basis would have its scan coefficient
    multiplied by a factor with no measurement behind it.
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

    # And that guard in `derive`'s own code, over the pair as an input set. The
    # full derivation cannot run — the atomic/batch/probe artifacts were never
    # committed (`g-b-size-measurement-json`) — so what this pins is that the ONLY
    # thing stopping it is those missing runs, not anything about these two.
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
    assert {p.n_copy for p in baseline} == {Fraction(1222, 661)}
    assert all(p.steers and p.trials >= harness.MIN_SWEEP_TRIALS for p in endpoint)
    assert max(p.pages for p in endpoint) == mod.IMPORT_WORST_CASE_SWEEP_PAGES

    # The same vertex, from the baseline alone and from both bases together.
    alone = harness.solve_sweep_envelope(baseline)
    both = harness.solve_sweep_envelope(baseline + endpoint)
    assert (both["a"], both["b"]) == (alone["a"], alone["b"])
    assert both["coverage_points"] == 34 and both["objective_points"] == 22
    assert both["max_pages"] == 6_001

    # And the frozen pair covers 3x the endpoint's own maximum, which is the claim
    # the whole bead exists to make: 6,001 pages priced by measurement.
    worst = max(endpoint, key=lambda p: p.pages)
    modelled = (
        mod.MARGINED_MS_BACKFILL_SWEEP_SCAN / float(worst.n_copy)
        + mod.MARGINED_US_BACKFILL_SWEEP_PER_PAGE * worst.pages / 1000
    )
    assert modelled >= harness.MARGIN * float(worst.max_ms)


def test_legacy_maxima_constrain_the_fit_without_steering_it():
    """A published maximum is a measurement, and a bound has to cover it.

    Trial count decides whether a point may STEER a fit, never whether the bound
    may sit below a number a host actually produced. Run B's 3-trial points carry
    no retained trials, so they are accepted, land in the coverage set only — and
    ``(4 pages, 13.60 ms)`` is nonetheless an ACTIVE constraint of the shipped
    solution, and the only evidence at that page count.
    """
    points = _shipped_sweep_points()
    with_b = harness.solve_sweep_envelope(points)
    assert with_b["coverage_points"] == 34
    assert with_b["objective_points"] == 22
    assert any(c["run"] == "B" and not c["steers"] for c in with_b["active_constraints"])

    # Dropping run B changes the solution — the proof that it constrains rather
    # than decorates.
    without_b = harness.solve_sweep_envelope([p for p in points if p.run != "B"])
    assert (without_b["a"], without_b["b"]) != (with_b["a"], with_b["b"])
    assert without_b["a"] < with_b["a"]


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
    """
    assert mod.MAX_BATCH_SIZE == 1_000
    assert harness.resolve_sweep_batch_sizes([1000, 1, mod.MAX_BATCH_SIZE, 1]) == [1, 1000]
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
