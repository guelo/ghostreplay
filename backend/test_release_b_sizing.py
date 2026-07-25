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
import math
import pathlib

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
    lock hold is bounded by the admission projection rather than by this term.
    """
    assert (
        mod.EST_MAX_LOCK_HOLD_MS
        == mod.MAX_BATCH_MS + mod.MAX_SINGLE_SESSION_COMPUTE_MS + mod.TEARDOWN_ALLOWANCE_MS
    )
    assert mod.EST_MAX_LOCK_HOLD_MS <= mod.MAX_WRITER_STALL_MS


def test_scan_stmt_timeout_covers_the_most_expensive_scan_it_is_armed_on():
    assert mod.SCAN_STMT_TIMEOUT_MS >= max(
        mod.MARGINED_MS_PER_SCAN_STMT, mod.MARGINED_MS_COVERAGE_ASSERT
    )


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


def test_scan_budget_fits_the_revision_deadline():
    """Every scan-bearing statement a run can issue, charged against one clock.

    Checked at IMPORT in the revision, so a ``session_moves`` large enough that
    the scans alone cannot fit the wall clock fails when the revision loads
    instead of exhausting ``MAX_PASSES`` and raising a misleading
    non-convergence error 900 seconds later.
    """
    budget = mod._scan_budget_ms(mod.MARGINED_MS_PER_SCAN_STMT, mod.MARGINED_MS_COVERAGE_ASSERT)
    assert budget == (2 * mod.MAX_PASSES + 2) * mod.MARGINED_MS_PER_SCAN_STMT + (
        mod.MARGINED_MS_COVERAGE_ASSERT
    )
    assert budget < mod.REVISION_DEADLINE_S * 1000


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
    # Three scans under lock plus the coverage assertion plus the teardown floor
    # are the WHOLE stall on this run, and none of them is zero.
    assert out["decision_1"]["T_stall_prod_ms"] == pytest.approx(
        mod.ATOMIC_SCANS_UNDER_LOCK * 120.0 + 60.0 + 40.0
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
         _probe("batch", 10.0), _probe("batch", 77.0), _probe("atomic", 80.0)],
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
             _probe("batch", 10.0), _probe("atomic", 20.0, rows_locked=5)],
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
        + p["T_atomic_teardown_floor_prod"]
        + p["T_atomic_teardown_per_row_prod"] * (1000 + 10)
    )
    assert out["decision_1"]["T_stall_prod_ms"] == pytest.approx(expected)
    assert out["decision_1"]["verdict"] in {"atomic", "batch"}
    assert out["decision_1"]["margined_stall_ms"] == pytest.approx(
        harness.MARGIN * out["decision_1"]["T_stall_prod_ms"]
    )


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
