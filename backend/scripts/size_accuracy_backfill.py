#!/usr/bin/env python3
"""Standalone sizing harness for revision ``20260719_01`` (Release B, g-b-size-derive).

This script exists because the shipped revision **must contain no bypass**. The
migration refuses an atomic run whose projected writer stall exceeds
``MAX_WRITER_STALL_MS`` and enforces a per-batch SQL deadline; both bounds are
made of constants only a measured run can produce, and measuring *through* the
revision with those guards armed is circular — in exactly the case where batch
mode turns out to be mandatory, the guarded run aborts before producing the
number that would have proved it.

The tempting fix is an environment variable that disarms the guards for sizing.
That variable is production-reachable by definition: matching
``current_database()`` only prevents *accidental* reuse against a differently
named database, and the production database name is knowable. So there is no
switch. Measurement lives here instead, in a script that is run by hand against a
restored snapshot and is never on a deployment path.

What keeps this honest
----------------------
The harness **imports** the revision's statement constants through Alembic's
``ScriptDirectory`` rather than restating them, and it imports the same frozen
``app.accuracy_v1`` / ``app.accuracy_rows_v1`` the revision imports (by calling
the revision's own ``_accuracy_for``). A constant test in
``test_release_b_sizing.py`` asserts that import identity, so a statement that
changes in the revision cannot keep a stale twin alive here. What it does *not*
share with the revision is the loop wrapper and the guards — which is why nothing
merges on the strength of a harness run alone: **Phase 3** (downstream, in
``g-b-sizing-harness``) reruns the frozen shipped revision with its guards armed
on fresh restores.

The six kinds of work, and why they are timed apart
---------------------------------------------------
Scaling one combined number by stale-session count makes work that is
*independent of both populations* vanish from the projection whenever those
populations are small or zero — which is precisely the clean-audit shape a green
audit most invites you to admit into atomic mode. So the harness times six
things that scale with six different things:

===============================  ==========================  =================
Work                             Scales with                 Zero when?
===============================  ==========================  =================
``VALIDATE CONSTRAINT``          whole ``game_sessions``     never
Backfill row work                ``N_stale`` / its moves     ``N_stale = 0``
Repair per-candidate mutation    ``N_repair``                ``N_repair = 0``
Scan-bearing stmt over moves     whole ``session_moves``     **never**
Coverage assertion               whole ``game_sessions``     **never**
Atomic teardown                  floor **plus** per-row      **never**
===============================  ==========================  =================

The last three rows are the point of the whole file.

Subcommands
-----------
``--mode atomic``
    One transaction over the whole population, the shape atomic mode ships. Times
    ``VALIDATE``, the backfill, the repair (per-candidate mutation only, scans
    excluded), every scan-bearing statement standalone, the coverage assertion,
    and — because a single point cannot give a floor *and* a slope — atomic
    ``COMMIT`` at **two** populations: the full mutated one, and then, on the
    now-drained database, an atomic run that mutated nothing.

``--mode batch --batch-size N [--repair-batch-size R]``
    The per-batch shape, each batch its own transaction on its own connection.
    Reports the maximum single-batch duration and the maximum batch ``COMMIT``
    duration, which are what ``B_tested`` / ``R_tested`` are read from. Each
    candidate size needs its own fresh copy: the first run consumes the
    population.

``--cancel-probe``
    The breach path. Provokes a mid-statement cancellation on a **locked, fully
    dirtied** batch and measures ``cancel_to_unlock_ms`` **from a second
    connection** — cancel issuance to the moment a competing
    ``FOR NO KEY UPDATE NOWAIT`` on a row the batch held *acquires*. See
    "Why cancel-to-unlock and not teardown_ms" below.

``--derive --measurement F [...] [--production-dimensions F]``
    Phase 2. Pure arithmetic over recorded measurement JSON: projects the
    snapshot numbers to production, applies the 3x margin, and emits the frozen
    constants, the batch-size provenance (every candidate tried, its observed
    maximum, and which bound won), the import-time invariants, and the Decision 1
    verdict — as JSON, for transcription into the runbook. Touches no database
    and takes no ``--url``.

Why cancel-to-unlock and not ``teardown_ms``
--------------------------------------------
The tail a live writer actually waits through on the deadline-breach path is not
"how long ``ROLLBACK`` took". PostgreSQL notices a cancellation **at the
statement's next interrupt point**, unwinds the statement, and raises to the
driver; only *then* does Python issue ``ROLLBACK``, and the row locks release
when that returns. A clock the cancelled process starts cannot contain the
interrupt latency or the unwind, because it starts after both. So the constant is
frozen from the outside measurement. The process-side ``ROLLBACK``-to-return
duration is reported beside it, named as the narrower rollback-only metric, and
is never the priced number.

Safety
------
Takes an explicit ``--url``, prints ``current_database()`` before doing anything,
and refuses to run without ``--confirm-mutates``. It rewrites the rows it
measures and installs a parking trigger for the cancel probe: point it at a
disposable restore, never at production.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import sys
import threading
import time
from typing import Any, Callable

_BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

REVISION = "20260719_01"

#: The 3x safety margin applied to every measured input before it is frozen.
#: Named once so the derivation and the runbook cannot disagree about it.
MARGIN = 3

#: Trials for the cancel probe. The acceptance contract requires >= 20.
DEFAULT_CANCEL_TRIALS = 20

#: How many times each scan-bearing statement is executed standalone. The FIRST
#: trial is the cold-ish one and is usually the maximum; the maximum across
#: trials is what gets frozen, and the median is reported beside it so a run
#: whose maximum is a single outlier is visible as one.
DEFAULT_SCAN_TRIALS = 5


def _revision_module():
    """The revision, loaded through Alembic — never re-implemented here.

    Import identity, not string equality: this is the same module object the
    migration runs, so the statements measured are the statements shipped.
    """
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(cfg).get_revision(REVISION).module


mod = _revision_module()


# ---------------------------------------------------------------------------
# The scan-bearing statements, and why there are four of them.
#
# Nothing in the run ever executes PLY_DETECTOR_SQL by itself. What executes is
# the detector wrapped in a statement that ALSO joins and filters game_sessions
# and then counts (repair_remaining, soundness_assert, the pre-flight repair
# population count) or inserts into the temp table (repair_populate) — and every
# one of those costs strictly more than the bare detector. Pricing
# MARGINED_MS_PER_SCAN_STMT from the detector alone would under-size every scan
# the migration actually issues.
#
# The pre-flight repair population count is the statement g-b-runtime-envelope's
# admission projection issues before the first row lock. Its SQL is
# REPAIR_REMAINING_SQL under its own marker: a comment changes no plan and no
# cost, but it makes the statement identifiable from pg_stat_activity, and
# pricing it as its own entry keeps "the maximum of four" honest if the runtime
# envelope later gives it a different shape. It is DERIVED from the imported
# constant rather than restated, so it cannot drift.
# ---------------------------------------------------------------------------


def repair_population_count_sql() -> str:
    # The revision now DECLARES this statement (it is a bundle field the runner
    # executes during mode binding), so the harness reads it rather than deriving
    # it — a locally derived copy could drift from the one production issues.
    return mod.REPAIR_POPULATION_COUNT_SQL


def scan_bearing_statements() -> dict[str, str]:
    """The four COMPLETE scan-bearing statements over ``session_moves``."""
    return {
        "repair_populate": mod.REPAIR_POPULATE_SQL,
        "repair_remaining": mod.REPAIR_REMAINING_SQL,
        "soundness_assert": mod.SOUNDNESS_ASSERT_SQL,
        "repair_population_count": repair_population_count_sql(),
    }


# ---------------------------------------------------------------------------
# Timing helpers.
# ---------------------------------------------------------------------------


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


class Timer:
    """Accumulates named durations in milliseconds."""

    def __init__(self) -> None:
        self.samples: dict[str, list[float]] = {}

    def record(self, name: str, ms: float) -> None:
        self.samples.setdefault(name, []).append(ms)

    def time(self, name: str, fn: Callable[[], Any]) -> Any:
        started = time.perf_counter()
        try:
            return fn()
        finally:
            self.record(name, _ms(started))

    def summary(self) -> dict[str, dict[str, float]]:
        return {
            name: {
                "n": len(vals),
                "max_ms": max(vals),
                "median_ms": statistics.median(vals),
                "min_ms": min(vals),
                "total_ms": sum(vals),
            }
            for name, vals in sorted(self.samples.items())
        }


# ---------------------------------------------------------------------------
# Dimensions and populations.
#
# The four relation dimensions are not diagnostics. They become SIZED_M_TOTAL,
# SIZED_MOVES_BYTES, SIZED_TOTAL_ROWS and SIZED_SESSIONS_BYTES in the revision,
# and they are the only way a later run can tell whether the two scan constants
# still describe the relations they were measured on. A scan cost recorded
# without the size of the thing it scanned is a number nothing downstream can
# check.
# ---------------------------------------------------------------------------

_MOVES_OF_STALE_SQL = f"""
    SELECT count(*) FROM session_moves m
    JOIN game_sessions g ON g.id = m.session_id
    WHERE {mod.POPULATION_PREDICATE_SQL}
"""

#: Gate condition 6, the writer-defect signal: every ended-visible session with a
#: broken grid, regardless of version or value. WIDER than the repair population,
#: which additionally requires version 1 AND a non-NULL accuracy — and it is the
#: narrower one that scales the repair term.
_BROKEN_AUDIT_SQL = f"""
    SELECT count(*) FROM game_sessions
    WHERE {mod.VISIBLE_ENDED_SQL}
      AND id IN ({mod.PLY_DETECTOR_SQL})
"""


def read_dimensions(conn) -> dict[str, int]:
    scalar = lambda sql: int(conn.execute(text(sql)).scalar() or 0)  # noqa: E731
    return {
        "total_rows": scalar("SELECT count(*) FROM game_sessions"),
        "sessions_bytes": scalar("SELECT pg_total_relation_size('game_sessions')"),
        "m_total": scalar("SELECT count(*) FROM session_moves"),
        "moves_bytes": scalar("SELECT pg_total_relation_size('session_moves')"),
    }


def read_populations(conn) -> dict[str, int]:
    scalar = lambda sql: int(conn.execute(text(sql)).scalar() or 0)  # noqa: E731
    # The two convergence statements return a COUNT AND up to 20 sampled ids in one
    # result set (zero rows means zero remaining), so they are read through the
    # revision's own reader rather than with .scalar() — which would return the
    # first sampled id.
    count = lambda sql: mod.remaining_scan(conn, sql)[0]  # noqa: E731
    n_stale = count(mod.BACKFILL_REMAINING_SQL)
    m_moves = scalar(_MOVES_OF_STALE_SQL)
    return {
        "n_stale": n_stale,
        "m_moves": m_moves,
        "mean_plies_per_stale": (m_moves / n_stale) if n_stale else None,
        "n_broken_audit": scalar(_BROKEN_AUDIT_SQL),
        "n_repair": count(mod.REPAIR_REMAINING_SQL),
    }


# ---------------------------------------------------------------------------
# Population synthesis.
#
# Every per-row projection divides by a population count, so each zero case needs
# an explicit rule and NO fabricated constant. A snapshot with no stale rows is
# repopulated by nulling the version across the ENTIRE ended-visible set, which
# reproduces production's real PGN and move distribution — the same rows, the
# same plies, the same parse cost. A snapshot with no broken grids gets a
# documented sample corrupted into exactly the shape Release A's unguarded hook
# left: a broken grid stamped version 1 with a non-NULL accuracy.
# ---------------------------------------------------------------------------


def synthesize_stale(conn) -> int:
    res = conn.execute(
        text(
            f"""
            UPDATE game_sessions
            SET player_accuracy_algo_version = NULL, player_accuracy = NULL
            WHERE {mod.VISIBLE_ENDED_SQL}
            """
        )
    )
    return res.rowcount


#: The contract's floor on a SYNTHESIZED repair population. A per-candidate cost
#: frozen from a handful of rows is a maximum over a handful of rows, and the
#: first Phase 1 run of this harness took K = 300 without anything objecting.
MIN_SYNTHESIZED_REPAIR = 1_000


def check_repair_sample_size(k: int, eligible: int) -> None:
    """The design's "K >= 1_000, or the whole set if smaller" rule.

    Enforced rather than left to the operator's memory: the first Phase 1 run of
    this harness took K = 300 and nothing objected. A per-candidate cost frozen
    from a handful of rows is a maximum over a handful of rows, and the constant
    it produces is multiplied by the LIVE repair count at run time.
    """
    floor = min(MIN_SYNTHESIZED_REPAIR, eligible)
    if k < floor:
        raise SystemExit(
            f"--synthesize-repair {k} is below the contract floor of {floor} "
            f"({MIN_SYNTHESIZED_REPAIR}, or the whole eligible set of {eligible} when smaller): "
            "a per-candidate cost frozen from a handful of rows is a maximum over a handful "
            "of rows"
        )


def synthesize_repair(conn, k: int) -> dict[str, int]:
    """Corrupt K ended-visible grids and stamp them version 1 / non-NULL.

    Deleting one ply is what breaks the COORDINATE grid: the remaining plies keep
    their own ``(move_number, color)`` values while ``row_number()`` shifts under
    them, so the detector's ``move_number <> i / 2 + 1`` arm fires. Sessions with
    fewer than 3 plies are skipped — removing a ply from a 1-ply session leaves
    an empty grid, and ``ply_coordinates_intact([])`` is True, so such a row would
    be corrupted without becoming a candidate.

    ``ORDER BY md5(id)``, NOT ``ORDER BY id``, and the difference silently
    changes every scan measurement in this file. The scan-bearing statements are
    ``<outer> ... WHERE id IN (<detector>)``, and PostgreSQL is free to serve
    that with a MERGE JOIN over ``game_sessions.id`` and the detector's
    ``session_id``. A merge join STOPS as soon as the outer side is exhausted —
    so a candidate set taken with ``ORDER BY id LIMIT k`` is the k LOWEST session
    ids, the join terminates a few percent into ``session_moves``, and every
    scan-bearing statement is measured at a small fraction of its real cost. It
    is a synthesis artifact that looks exactly like good news. Production's
    broken grids are spread across the id space, so the synthesized ones must be
    too; md5 gives a deterministic spread.

    ``k`` is checked against the design's "K >= 1_000, or the whole set if
    smaller" rule before anything is corrupted.
    """
    eligible = int(
        conn.execute(
            text(
                f"""
                SELECT count(*) FROM game_sessions g
                WHERE {mod.VISIBLE_ENDED_SQL}
                  AND (SELECT count(*) FROM session_moves m WHERE m.session_id = g.id) >= 3
                """
            )
        ).scalar()
        or 0
    )
    check_repair_sample_size(k, eligible)
    ids = [
        str(r[0])
        for r in conn.execute(
            text(
                f"""
                SELECT g.id FROM game_sessions g
                WHERE {mod.VISIBLE_ENDED_SQL}
                  AND (SELECT count(*) FROM session_moves m WHERE m.session_id = g.id) >= 3
                ORDER BY md5(g.id::text)
                LIMIT :k
                """
            ).bindparams(k=k)
        )
    ]
    for sid in ids:
        conn.execute(
            text(
                """
                DELETE FROM session_moves
                WHERE id = (
                  SELECT id FROM session_moves
                  WHERE session_id = CAST(:sid AS uuid)
                  ORDER BY move_number ASC,
                           CASE WHEN color = 'white' THEN 0 ELSE 1 END ASC
                  OFFSET 1 LIMIT 1
                )
                """
            ).bindparams(sid=sid)
        )
    if ids:
        conn.execute(
            text(
                f"""
                UPDATE game_sessions
                SET player_accuracy = 50, player_accuracy_algo_version = {mod.ALGO_VERSION}
                WHERE id = ANY(CAST(:ids AS uuid[]))
                """
            ).bindparams(ids=ids)
        )
    return {"k_requested": k, "k_corrupted": len(ids)}


def synthesize_stamped(conn) -> int:
    """Stamp the whole ended-visible set version 1 over INTACT grids.

    This is how the atomic teardown FLOOR gets measured on a restore whose CHECK
    is still ``NOT VALID``. The floor is defined as the teardown of an atomic run
    that mutated nothing *while VALIDATE and the scans still ran* — and the
    second pass of a single ``--mode atomic`` invocation cannot supply it,
    because the first pass already validated the constraint, so the second
    records ``validate_skipped_already_validated`` and its COMMIT flushes no
    catalog change. Subtracting that from a full point that DID validate pushes
    VALIDATE's own commit cost into the per-row slope, where it does not belong.

    So the floor is its own run against its own fresh restore: stamp everything
    version 1, which empties the backfill population, while leaving the
    constraint unvalidated. The value 100 satisfies the range CHECK the run is
    about to validate.

    Emptying the REPAIR population needs the grid, and that is why the value is
    conditional. A repair candidate is version 1 AND a non-NULL accuracy AND a
    broken grid, so stamping a broken-grid row with 100 CREATES a candidate
    instead of removing one, and the "empty" run then mutates the rows the repair
    phase nulls. A fixture with no broken grids hides this completely: the first
    Phase 1 snapshot was synthesized with intact grids throughout, so the
    unconditional form measured a genuinely empty run there and the defect only
    surfaced against a production restore, whose 10 real broken grids turned the
    floor measurement into a 10-row mutation and made ``--derive`` reject it.

    So a broken grid is stamped version 1 with a NULL accuracy — which is exactly
    what the fail-closed backfill itself writes for such a row — and an intact
    grid gets 100. Both populations end up empty for the right reason: version 1
    excludes every row from the backfill, and no row is left carrying a served
    value over a broken grid.
    """
    return conn.execute(
        text(
            f"""
            UPDATE game_sessions
            SET player_accuracy_algo_version = {mod.ALGO_VERSION},
                player_accuracy = CASE
                  WHEN id IN ({mod.PLY_DETECTOR_SQL}) THEN NULL
                  ELSE 100
                END
            WHERE {mod.VISIBLE_ENDED_SQL}
            """
        )
    ).rowcount


def analyze_after_synthesis(conn) -> None:
    """MANDATORY after any synthesis, and not a tidiness step.

    Synthesis rewrites the version column across the whole ended-visible set. If
    the planner is left holding statistics that describe the table BEFORE that
    rewrite, it estimates the repair population at ~1 row and picks a nested loop
    that re-executes the whole set-wide detector once per candidate. The measured
    number is then the cost of a plan production will never choose — off by
    orders of magnitude, and off in the direction that makes the run look
    catastrophic rather than merely slow.

    A real restore arrives with statistics that match its own contents, so this
    corrects for the synthesis and nothing else.
    """
    conn.execute(text("ANALYZE game_sessions"))
    conn.execute(text("ANALYZE session_moves"))


# ---------------------------------------------------------------------------
# Scan-bearing statement timing.
#
# Timed WHOLE and standalone, repeatedly, and reported as max/median of a SINGLE
# execution — never as a warm repeat divided by a count. REPAIR_POPULATE_SQL is
# timed against a repair population at least as large as the one it will run on,
# so its insert component is not measured against an emptier table than
# production's; the temp table is cleared between trials for the same reason a
# second INSERT into a non-empty table is not the statement we are pricing.
# ---------------------------------------------------------------------------


def time_scan_statements(conn, trials: int) -> dict[str, dict[str, float]]:
    conn.execute(text(mod.REPAIR_CANDIDATES_DDL_PG))
    out: dict[str, dict[str, float]] = {}
    for name, sql in scan_bearing_statements().items():
        durations = []
        for _ in range(trials):
            if name == "repair_populate":
                conn.execute(text(mod.REPAIR_CLEAR_SQL))
            started = time.perf_counter()
            conn.execute(text(sql))
            durations.append(_ms(started))
        out[name] = {
            "n": trials,
            "cold_ms": durations[0],
            "max_ms": max(durations),
            "median_ms": statistics.median(durations),
        }
    conn.execute(text(mod.REPAIR_CLEAR_SQL))

    # The bare detector: a DIAGNOSTIC LOWER BOUND only. It is never the priced
    # number, because no code path executes it alone.
    bare = []
    for _ in range(trials):
        started = time.perf_counter()
        conn.execute(text(f"SELECT count(*) FROM ({mod.PLY_DETECTOR_SQL}) d"))
        bare.append(_ms(started))
    out["_diagnostic_bare_detector"] = {
        "n": trials,
        "cold_ms": bare[0],
        "max_ms": max(bare),
        "median_ms": statistics.median(bare),
    }

    # The coverage assertion scans a DIFFERENT relation and scales by a DIFFERENT
    # ratio, so it is priced by its own constant rather than folded into the
    # maximum above — but it is armed with the same timeout, which is why
    # SCAN_STMT_TIMEOUT_MS has to cover it too.
    cov = []
    for _ in range(trials):
        started = time.perf_counter()
        conn.execute(text(mod.COVERAGE_ASSERT_SQL))
        cov.append(_ms(started))
    out["coverage_assert"] = {
        "n": trials,
        "cold_ms": cov[0],
        "max_ms": max(cov),
        "median_ms": statistics.median(cov),
    }

    # The BACKFILL's OWN game_sessions work, priced by its own two constants and
    # scaled by G_sessions. Scan-bearing even though neither statement touches
    # session_moves: both filter game_sessions on
    # `player_accuracy_algo_version IS NULL OR < 1`, and NO INDEX covers that
    # predicate (app/models.py:188, app/models.py:224). So each costs
    # O(G_sessions), NOT O(N_stale) — every version-1 row Release A stamps between
    # sizing and deploy grows the scanned relation while shrinking the population.
    # Omitting them priced a growing relation at zero.
    for name, sql in (("backfill_remaining", mod.BACKFILL_REMAINING_SQL),):
        durations = []
        for _ in range(trials):
            started = time.perf_counter()
            conn.execute(text(sql))
            durations.append(_ms(started))
        out[name] = {
            "n": trials,
            "cold_ms": durations[0],
            "max_ms": max(durations),
            "median_ms": statistics.median(durations),
        }

    # One full selection SWEEP: every SELECT_BATCH_* page of one pass, timed as ONE
    # unit, because that is the unit the constant prices — the atomic projection
    # charges one sweep per pass, not one page. Unlocked variants and no mutation,
    # so the sweep is repeatable: nothing it selects leaves the population.
    sweeps = []
    for _ in range(trials):
        started = time.perf_counter()
        pages = 0
        last_id: str | None = None
        while True:
            page = _select_page(conn, last_id, mod.MAX_BATCH_SIZE, locked=False)
            pages += 1
            if not page:
                break
            last_id = str(page[-1].id)
        sweeps.append(_ms(started))
    out["backfill_select_sweep"] = {
        "n": trials,
        "cold_ms": sweeps[0],
        "max_ms": max(sweeps),
        "median_ms": statistics.median(sweeps),
        "pages": pages,
    }
    return out


def scan_stmt_max_ms(scans: dict[str, dict[str, float]]) -> float:
    """The MAXIMUM across the four complete statements — not the detector.

    With ONE conservative exception, and it is not a contradiction of the rule.
    The rule rests on a claim about cost: every complete statement wraps the
    detector in a join and an aggregate, so it must cost strictly more than the
    detector alone. That claim is about a PLAN, and PostgreSQL is free to choose
    one where it is false — a merge join over ``game_sessions.id`` and the
    detector's ``session_id`` terminates as soon as the outer side is exhausted,
    so the complete statement can read a fraction of ``session_moves`` while a
    bare detector reads all of it. When that happens the "diagnostic lower bound"
    is not a lower bound and the four measurements are not pricing a full scan.

    Silently freezing the smaller number would under-size the one term the atomic
    stall projection cannot revalidate at run time, in the direction that admits
    atomic mode. So the maximum absorbs the diagnostic when the diagnostic wins,
    and ``scan_plan_inversion`` records that it did.
    """
    complete = max(scans[name]["max_ms"] for name in scan_bearing_statements())
    bare = scans.get("_diagnostic_bare_detector", {}).get("max_ms", 0.0)
    return max(complete, bare)


def scan_plan_inversion(scans: dict[str, dict[str, float]]) -> bool:
    """True when the bare detector out-cost every complete statement.

    Never a silent condition: it means the planner served the complete statements
    with an early-terminating join, so their measured cost is a property of where
    the repair candidates sit in the id space rather than of the relation. Phase
    3 has to re-check it against production's statistics.
    """
    complete = max(scans[name]["max_ms"] for name in scan_bearing_statements())
    return scans.get("_diagnostic_bare_detector", {}).get("max_ms", 0.0) > complete


# ---------------------------------------------------------------------------
# VALIDATE.
# ---------------------------------------------------------------------------


def time_validate(conn, timer: Timer) -> None:
    """Time the ``NOT VALID`` -> validated CHECK transition, if it is still pending.

    Outside the writer stall by construction: ``VALIDATE``'s SHARE UPDATE
    EXCLUSIVE does not conflict with the row writes or ``FOR NO KEY UPDATE``
    locks the /moves hook takes, and it completes before the first row lock. It
    is timed anyway because it is charged against the revision's wall clock and
    is measured on every sizing run — including a run where both populations are
    empty and there is nothing else to measure.
    """
    already = conn.execute(
        text(
            "SELECT convalidated FROM pg_constraint WHERE conname = :n"
        ).bindparams(n=mod.CHECK_NAME)
    ).scalar()
    if already is None:
        # The CHECK does not exist. Recording a zero-duration "validate" here
        # would make `validated_in_run` true on a run that never validated
        # anything — which is exactly the proof the empty teardown point rests
        # on. A restore without Release A's constraint is not a valid sizing
        # target at all, so fail rather than time a statement that did not run.
        raise SystemExit(
            f"{mod.CHECK_NAME} does not exist on this database: it is created by Release A "
            "(20260709_01). Restore to at least 20260718_01 before sizing"
        )
    if already:
        # Already validated on this copy: re-running is a no-op that would time
        # as free and misreport the cost. Say so rather than record a zero.
        timer.record("validate_skipped_already_validated", 0.0)
        return
    conn.execute(
        text("SELECT set_config('lock_timeout', :v, true)").bindparams(
            v=mod.VALIDATE_LOCK_TIMEOUT
        )
    )
    timer.time(
        "validate",
        lambda: conn.execute(
            text(f"ALTER TABLE game_sessions VALIDATE CONSTRAINT {mod.CHECK_NAME}")
        ),
    )
    conn.execute(text("SELECT set_config('lock_timeout', :v, true)").bindparams(v="1000ms"))


# ---------------------------------------------------------------------------
# Backfill, instrumented.
#
# The statements and the accuracy computation come from the revision module; only
# the loop wrapper and the clocks are local. Per-session compute is timed
# individually because MAX_SINGLE_SESSION_COMPUTE_MS is a MAXIMUM, not a mean:
# what the compute watchdog has to survive is the worst single session in the
# population, not the average one.
# ---------------------------------------------------------------------------


def _select_page(conn, last_id: str | None, batch_size: int, *, locked: bool):
    if locked:
        first_sql, next_sql = (
            mod.SELECT_BATCH_FIRST_LOCKED_PG,
            mod.SELECT_BATCH_LOCKED_PG,
        )
    else:
        first_sql, next_sql = mod.SELECT_BATCH_FIRST_PG, mod.SELECT_BATCH_PG
    if last_id is None:
        stmt = text(first_sql).bindparams(batch_size=batch_size)
    else:
        stmt = text(next_sql).bindparams(last_id=last_id, batch_size=batch_size)
    return conn.execute(stmt).all()


def _compute_batch(conn, batch, timer: Timer) -> list[tuple[Any, int | None]]:
    """Parse + validate + score one batch, timing EACH session separately."""
    ids = [r.id for r in batch]
    grouped = timer.time("load_moves", lambda: mod._load_moves(conn, mod.SQL_PG, ids))
    results = []
    for r in batch:
        started = time.perf_counter()
        acc = mod._accuracy_for(grouped.get(str(r.id), []), r.player_color, r.pgn)
        timer.record("single_session_compute", _ms(started))
        results.append((r.id, acc))
    return results


def _apply_batch(conn, results, timer: Timer) -> int:
    admitted = timer.time(
        "guarded_update",
        lambda: conn.execute(
            text(mod.UPDATE_SQL_PG).bindparams(
                ids=[str(sid) for sid, _ in results],
                accuracies=[acc for _, acc in results],
            )
        )
        .scalars()
        .all(),
    )
    return len(admitted)


def run_backfill_atomic(conn, batch_size: int, timer: Timer) -> int:
    last_id: str | None = None
    stamped = 0
    while True:
        batch = timer.time(
            "select_batch", lambda: _select_page(conn, last_id, batch_size, locked=False)
        )
        if not batch:
            return stamped
        stamped += _apply_batch(conn, _compute_batch(conn, batch, timer), timer)
        last_id = str(batch[-1].id)


def run_backfill_batched(engine, batch_size: int, timer: Timer) -> int:
    """Per-batch transactions on an INDEPENDENT connection, one per batch.

    This is the shape whose maximum single-batch duration ``B_tested`` is read
    from, so the batch clock has to start where the runtime envelope's deadline
    will start: at the beginning of the batch transaction, not at the first
    statement inside it.
    """
    last_id: str | None = None
    stamped = 0
    with engine.connect() as conn:
        while True:
            batch_started = time.perf_counter()
            trans = conn.begin()
            try:
                batch = _select_page(conn, last_id, batch_size, locked=True)
                if not batch:
                    trans.rollback()
                    return stamped
                stamped += _apply_batch(conn, _compute_batch(conn, batch, timer), timer)
                commit_started = time.perf_counter()
                trans.commit()
                timer.record("batch_commit", _ms(commit_started))
            except Exception:
                trans.rollback()
                raise
            timer.record("single_batch", _ms(batch_started))
            # The ACTUAL page cardinality, not the requested LIMIT. A run whose
            # population is smaller than --batch-size never exercises the size it
            # was asked for, and freezing that size as "demonstrated" would admit
            # a batch nothing ever ran. Recorded per page so the maximum is the
            # largest batch this run really executed.
            timer.record("single_batch_rows", float(len(batch)))
            last_id = str(batch[-1].id)


# ---------------------------------------------------------------------------
# Repair, instrumented.
#
# The per-candidate clock covers exactly lock + session-scoped re-read +
# conditional update and EXCLUDES every set-wide scan. That exclusion is what
# makes MARGINED_MS_PER_REPAIR_ROW a genuinely per-row number, and it is only
# legitimate BECAUSE the materialization is hoisted out of the batch: were the
# set-wide detector still embedded in the selection, the admissible repair batch
# would be floor((MAX_BATCH_MS - MARGINED_MS_PER_SCAN_STMT) / per_row) and would
# go negative on any session_moves large enough to matter.
# ---------------------------------------------------------------------------


def _repair_candidate(conn, sid, timer: Timer) -> int:
    started = time.perf_counter()
    conn.execute(text(mod.REPAIR_LOCK_PG).bindparams(sid=str(sid)))
    nulled = 0
    if mod._scalar(conn, mod.PLY_DETECTOR_ONE_PG, sid=str(sid)) != 0:
        nulled = conn.execute(
            text(mod.REPAIR_UPDATE_PG).bindparams(sid=str(sid))
        ).rowcount
    timer.record("repair_per_candidate", _ms(started))
    return nulled


def _repair_page(conn, last_id: str | None, repair_batch_size: int):
    if last_id is None:
        stmt = text(mod.REPAIR_SELECT_FIRST_PG).bindparams(
            repair_batch_size=repair_batch_size
        )
    else:
        stmt = text(mod.REPAIR_SELECT_PG).bindparams(
            last_id=last_id, repair_batch_size=repair_batch_size
        )
    return conn.execute(stmt).scalars().all()


def _repair_pages(conn, repair_batch_size: int):
    last_id: str | None = None
    while True:
        page = _repair_page(conn, last_id, repair_batch_size)
        if not page:
            return
        yield page
        last_id = str(page[-1])


def run_repair_atomic(conn, repair_batch_size: int, timer: Timer) -> int:
    conn.execute(text(mod.REPAIR_CANDIDATES_DDL_PG))
    conn.execute(text(mod.REPAIR_CLEAR_SQL))
    timer.time("repair_populate_inline", lambda: conn.execute(text(mod.REPAIR_POPULATE_SQL)))
    nulled = 0
    for page in _repair_pages(conn, repair_batch_size):
        for sid in page:
            nulled += _repair_candidate(conn, sid, timer)
    return nulled


def run_repair_batched(engine, repair_batch_size: int, timer: Timer) -> int:
    """Per-batch transactions. The materialization is hoisted OUT of every batch.

    The temp table is session-local, so materialization and the pages that read
    it must share one connection; only the mutating work is split into per-batch
    transactions. Each page is SELECTED INSIDE its own batch transaction, for the
    same reason the backfill's is: the runtime envelope's batch deadline starts at
    the beginning of the batch transaction, so a selection that ran outside it
    would be work the measured batch clock never saw.
    """
    nulled = 0
    with engine.connect() as conn:
        trans = conn.begin()
        conn.execute(text(mod.REPAIR_CANDIDATES_DDL_PG))
        conn.execute(text(mod.REPAIR_CLEAR_SQL))
        timer.time(
            "repair_populate_inline", lambda: conn.execute(text(mod.REPAIR_POPULATE_SQL))
        )
        trans.commit()

        last_id: str | None = None
        while True:
            batch_started = time.perf_counter()
            trans = conn.begin()
            try:
                page = _repair_page(conn, last_id, repair_batch_size)
                if not page:
                    trans.rollback()
                    return nulled
                for sid in page:
                    nulled += _repair_candidate(conn, sid, timer)
                commit_started = time.perf_counter()
                trans.commit()
                timer.record("repair_batch_commit", _ms(commit_started))
            except Exception:
                trans.rollback()
                raise
            timer.record("single_repair_batch", _ms(batch_started))
            timer.record("single_repair_batch_rows", float(len(page)))
            last_id = str(page[-1])


# ---------------------------------------------------------------------------
# Atomic teardown, at two populations.
#
# One measurement point cannot yield a floor and a slope. The atomic transaction
# that mutated NOTHING (VALIDATE and the scans still ran) gives the
# population-independent floor; the one over the full mutated population gives
# the slope. Both take the larger of COMMIT and cancel-to-unlock at their point,
# because an atomic run that breaches rolls back the whole population and that
# rollback — plus the interrupt latency in front of it — is inside the stall
# exactly as its commit would have been.
# ---------------------------------------------------------------------------


def atomic_pass(
    engine,
    *,
    batch_size: int,
    repair_batch_size: int,
    scan_trials: int,
    timer: Timer,
    label: str,
) -> dict[str, Any]:
    """One whole atomic-shaped run in ONE transaction, ending in a timed COMMIT."""
    with engine.connect() as conn:
        trans = conn.begin()
        time_validate(conn, timer)
        scans = time_scan_statements(conn, scan_trials)
        stamped = run_backfill_atomic(conn, batch_size, timer)
        nulled = run_repair_atomic(conn, repair_batch_size, timer)
        timer.time("coverage_assert_inline", lambda: conn.execute(text(mod.COVERAGE_ASSERT_SQL)))
        timer.time(
            "soundness_assert_inline", lambda: conn.execute(text(mod.SOUNDNESS_ASSERT_SQL))
        )
        commit_started = time.perf_counter()
        trans.commit()
        commit_ms = _ms(commit_started)
    timer.record(f"atomic_commit_{label}", commit_ms)
    return {
        "label": label,
        "stamped": stamped,
        "nulled": nulled,
        "n_mutated": stamped + nulled,
        "commit_ms": commit_ms,
        "scans": scans,
    }


# ---------------------------------------------------------------------------
# The cancel probe: the breach path, timed from OUTSIDE the cancelled process.
#
# Three connections. A holds a locked, fully dirtied batch and parks inside the
# statement; C issues pg_cancel_backend and starts the clock at that instant; B
# polls a competing FOR NO KEY UPDATE NOWAIT on a row A holds and stops the clock
# when it ACQUIRES. The interval B measures spans the interrupt latency, the
# statement unwind, the raise into the driver, and A's ROLLBACK — every part of
# the tail a live writer actually waits through. A's own ROLLBACK-to-return
# duration is reported beside it and is strictly contained by it.
#
# The parking trigger is what makes "fully dirtied" true and the statement
# reliably interruptible: an AFTER ... FOR EACH STATEMENT trigger fires once the
# UPDATE has written every row, so cancelling while it sleeps cancels a statement
# whose rows are already locked and dirty. It lives on the harness's disposable
# copy and never in the revision.
# ---------------------------------------------------------------------------

_PARK_TRIGGER_SQL = """
    CREATE OR REPLACE FUNCTION _gr_size_park() RETURNS trigger AS $$
    BEGIN
      PERFORM pg_sleep(%(seconds)s);
      RETURN NULL;
    END;
    $$ LANGUAGE plpgsql;
    DROP TRIGGER IF EXISTS _gr_size_park_trg ON game_sessions;
    CREATE TRIGGER _gr_size_park_trg AFTER UPDATE ON game_sessions
      FOR EACH STATEMENT EXECUTE FUNCTION _gr_size_park();
"""

_DROP_PARK_TRIGGER_SQL = """
    DROP TRIGGER IF EXISTS _gr_size_park_trg ON game_sessions;
    DROP FUNCTION IF EXISTS _gr_size_park();
"""


def _install_park_trigger(engine, seconds: float) -> None:
    with engine.begin() as conn:
        conn.execute(text(_PARK_TRIGGER_SQL % {"seconds": seconds}))


def _drop_park_trigger(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(_DROP_PARK_TRIGGER_SQL))


def _victim_ids(engine, limit: int) -> list[str]:
    """Rows the probe will dirty. Any ended-visible rows will do: the probe
    rewrites the accuracy columns to themselves, so the population is not
    consumed and the trials are repeatable."""
    with engine.connect() as conn:
        return [
            str(r[0])
            for r in conn.execute(
                text(
                    f"""
                    SELECT id FROM game_sessions
                    WHERE {mod.VISIBLE_ENDED_SQL}
                    ORDER BY id
                    LIMIT :n
                    """
                ).bindparams(n=limit)
            )
        ]


def _cancel_trial(
    engine, ids: list[str], park_seconds: float, canceller, prober_conn
) -> dict[str, float] | None:
    """One trial. Returns None if the statement finished before the cancel landed.

    ``canceller`` and ``prober_conn`` are LONG-LIVED connections owned by the
    caller, deliberately not drawn from the pool per trial. A pooled connection
    is returned to the pool when the holder's block exits, so a canceller that
    opened its own connection per trial could be handed the very backend it is
    about to cancel — and ``pg_cancel_backend`` would then cancel the cancelling
    statement instead of the holder's. The self-cancel is also checked explicitly
    below, because a silent one would look exactly like a fast unlock and would
    bias the frozen maximum DOWN.
    """
    acquired = threading.Event()
    started_holding = threading.Event()
    result: dict[str, float] = {}
    errors: list[BaseException] = []

    def holder() -> None:
        try:
            with engine.connect() as conn:
                # begin() FIRST: reading the pid would autobegin a transaction and
                # the explicit begin() below would then raise.
                trans = conn.begin()
                pid = conn.execute(text("SELECT pg_backend_pid()")).scalar()
                result["pid"] = float(pid)
                # Lock and fully dirty the batch, then park inside the statement.
                conn.execute(
                    text(
                        "SELECT id FROM game_sessions WHERE id = ANY(CAST(:ids AS uuid[])) "
                        "ORDER BY id FOR NO KEY UPDATE"
                    ).bindparams(ids=ids)
                )
                started_holding.set()
                try:
                    conn.execute(
                        text(
                            "UPDATE game_sessions SET player_accuracy = player_accuracy "
                            "WHERE id = ANY(CAST(:ids AS uuid[]))"
                        ).bindparams(ids=ids)
                    )
                except DBAPIError:
                    rollback_started = time.perf_counter()
                    trans.rollback()
                    result["teardown_ms"] = _ms(rollback_started)
                    return
                # Not cancelled in time — this trial is discarded rather than
                # recorded as a fast unlock, which would bias the maximum DOWN.
                trans.rollback()
                result["not_cancelled"] = 1.0
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    def prober() -> None:
        try:
            started_holding.wait(timeout=30)
            while not acquired.is_set():
                trans = prober_conn.begin()
                try:
                    prober_conn.execute(
                        text(
                            "SELECT id FROM game_sessions WHERE id = CAST(:sid AS uuid) "
                            "FOR NO KEY UPDATE NOWAIT"
                        ).bindparams(sid=ids[0])
                    )
                    result["unlocked_at"] = time.perf_counter()
                    trans.rollback()
                    acquired.set()
                    return
                except DBAPIError:
                    trans.rollback()
                    time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    h = threading.Thread(target=holder, daemon=True)
    p = threading.Thread(target=prober, daemon=True)
    h.start()
    p.start()

    started_holding.wait(timeout=30)
    # WAIT FOR THE PARK, never sleep towards it. started_holding fires BEFORE the
    # UPDATE is issued, so a fixed sleep races the UPDATE's write phase: on a
    # production-sized batch the cancel can land while rows are still being
    # written. The trial would still look valid — the preceding
    # SELECT ... FOR NO KEY UPDATE already locked every row, so the prober still
    # measures a real lock release — but the rollback would cover a PARTIALLY
    # dirtied batch and under-report the teardown, in the direction that shrinks
    # TEARDOWN_ALLOWANCE_MS.
    #
    # The AFTER ... FOR EACH STATEMENT trigger fires only once the UPDATE has
    # written every row, and it parks in pg_sleep. So "the backend is waiting on
    # PgSleep" is an observable proof that the batch is FULLY dirtied. Poll for
    # it; a trial that never parks is discarded rather than cancelled blind.
    target_pid = int(result.get("pid", 0))
    parked = False
    park_deadline = time.perf_counter() + park_seconds + 30.0
    while time.perf_counter() < park_deadline:
        if target_pid:
            waiting = canceller.execute(
                text(
                    "SELECT 1 FROM pg_stat_activity WHERE pid = :pid AND wait_event = 'PgSleep'"
                ).bindparams(pid=target_pid)
            ).scalar()
            if waiting:
                parked = True
                break
        if not h.is_alive():
            break
        time.sleep(0.002)
        target_pid = int(result.get("pid", 0))
    if not parked:
        acquired.set()
        h.join(timeout=60)
        p.join(timeout=60)
        return None
    canceller_pid = int(canceller.execute(text("SELECT pg_backend_pid()")).scalar())
    if target_pid in (0, canceller_pid):
        acquired.set()
        h.join(timeout=60)
        p.join(timeout=60)
        return None
    cancel_issued = time.perf_counter()
    canceller.execute(text("SELECT pg_cancel_backend(:pid)").bindparams(pid=target_pid))
    h.join(timeout=60)
    p.join(timeout=60)
    acquired.set()

    if errors:
        raise errors[0]
    if "not_cancelled" in result or "unlocked_at" not in result:
        return None
    return {
        "cancel_to_unlock_ms": (result["unlocked_at"] - cancel_issued) * 1000.0,
        "teardown_ms": result.get("teardown_ms", float("nan")),
    }


def run_cancel_probe(engine, *, batch_size: int, trials: int, park_seconds: float) -> dict[str, Any]:
    ids = _victim_ids(engine, batch_size)
    if not ids:
        raise SystemExit("cancel probe: no ended-visible rows to lock")
    _install_park_trigger(engine, park_seconds)
    samples: list[dict[str, float]] = []
    discarded = 0
    try:
        # The canceller runs AUTOCOMMIT: a cancel issued inside an open
        # transaction would leave the connection idle-in-transaction between
        # trials, holding back the snapshot horizon while the probe measures
        # lock release.
        with engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as canceller, engine.connect() as prober_conn:
            for _ in range(trials * 3):
                if len(samples) >= trials:
                    break
                trial = _cancel_trial(engine, ids, park_seconds, canceller, prober_conn)
                if trial is None:
                    discarded += 1
                    continue
                samples.append(trial)
    finally:
        _drop_park_trigger(engine)

    if len(samples) < trials:
        raise SystemExit(
            f"cancel probe: only {len(samples)} of {trials} trials landed a cancellation "
            f"({discarded} discarded); raise --park-seconds and re-run"
        )
    unlock = [s["cancel_to_unlock_ms"] for s in samples]
    teardown = [s["teardown_ms"] for s in samples if not math.isnan(s["teardown_ms"])]
    return {
        "trials": len(samples),
        "discarded": discarded,
        "rows_locked": len(ids),
        "cancel_to_unlock_ms": {
            "max": max(unlock),
            "median": statistics.median(unlock),
            "min": min(unlock),
        },
        # The narrower rollback-only metric. Reported so the two can be compared,
        # named as what it is, and NEVER the frozen input: it starts after the
        # interrupt latency has already elapsed.
        "rollback_only_teardown_ms": {
            "max": max(teardown) if teardown else None,
            "median": statistics.median(teardown) if teardown else None,
        },
    }


# ---------------------------------------------------------------------------
# Phase 2 derivation. Pure arithmetic, no database.
# ---------------------------------------------------------------------------


#: The acceptance contract's floor on cancel-probe trials. A probe with fewer is
#: rejected rather than averaged in: the frozen number is a MAXIMUM, and a
#: maximum over three samples is not the same claim as a maximum over twenty.
MIN_CANCEL_TRIALS = 20


def _ratio(prod: float | None, snap: float | None) -> float:
    """Production-over-snapshot scaling. A full restore makes this exactly 1.

    ``None`` means "not recorded" and yields 1.0 — no scaling claim. **Zero means
    zero** and is passed through, which is the whole distinction: a production
    population of 0 is a real, load-bearing measurement (the term drops out of
    Decision 1), and collapsing it to 1.0 would project the snapshot's entire row
    work onto a database that has none of it. A zero DENOMINATOR is still 1.0,
    because dividing by an empty snapshot is undefined rather than infinite — and
    ``derive`` rejects empty snapshot populations outright before it gets here.
    """
    if prod is None or not snap:
        return 1.0
    return float(prod) / float(snap)


def _probe_max_unlock_ms(probes: list[dict], scope: str) -> tuple[float, dict]:
    """The largest cancel-to-unlock across every probe of one scope.

    Fails closed on absence and on an under-trialled probe. A missing probe used
    to fall through as ``0.0``, which let a sizing run that measured only COMMIT
    silently produce ``TEARDOWN_ALLOWANCE_MS`` — exactly what the design forbids,
    and invisible in the output because the resulting constant is a plausible
    small integer. Multiple probes of the same scope are combined by MAXIMUM
    rather than first-wins, because the frozen input is a worst case.
    """
    matching = [p for p in probes if p.get("scope", "batch") == scope]
    if not matching:
        raise SystemExit(
            f"derivation needs a --cancel-probe --probe-scope {scope} measurement: "
            "the teardown constants take the larger of COMMIT and CANCEL-TO-UNLOCK, so a run "
            "that measured only commit cannot produce them"
        )
    thin = [p for p in matching if int(p.get("trials", 0)) < MIN_CANCEL_TRIALS]
    if thin:
        raise SystemExit(
            f"--probe-scope {scope} has {min(int(p.get('trials', 0)) for p in thin)} landed "
            f"trial(s); the contract requires >= {MIN_CANCEL_TRIALS}"
        )
    best = max(matching, key=lambda p: p["cancel_to_unlock_ms"]["max"])
    return best["cancel_to_unlock_ms"]["max"], best


def derive(measurements: list[dict], production: dict | None) -> dict[str, Any]:
    """Project the snapshot numbers to production and freeze the constants.

    Every projection here is stated in the design as a formula over recorded
    numbers; nothing is fitted. Where two ratios are available (row count and
    relation size) the LARGER wins, which is conservative without pretending to
    know a coefficient.
    """
    atomics = [m for m in measurements if m.get("kind") == "atomic"]
    batches = [m for m in measurements if m.get("kind") == "batch"]
    probes = [m for m in measurements if m.get("kind") == "cancel_probe"]

    # TWO atomic runs, on two restores: one over a real mutated population (the
    # slope) and one over an empty one (the floor). One measurement point cannot
    # yield both, and the empty point must be its OWN run — a second pass in the
    # same process has already validated the constraint, so its COMMIT flushes no
    # catalog change and the difference would charge VALIDATE's commit to the
    # per-row slope.
    atomic = next((m for m in atomics if m.get("teardown_point") == "full"), None)
    empty_run = next((m for m in atomics if m.get("teardown_point") == "empty"), None)
    if atomic is None:
        raise SystemExit(
            "derivation needs an --mode atomic run over a NON-EMPTY population "
            "(teardown_point == 'full')"
        )
    if empty_run is None:
        raise SystemExit(
            "derivation needs a second --mode atomic run over an EMPTY population "
            "(teardown_point == 'empty'): prepare a FRESH restore with --synthesize-stamped. "
            "One measurement point cannot yield a teardown floor and a slope"
        )
    if not empty_run.get("validated_in_run"):
        raise SystemExit(
            "the empty-point run did not execute VALIDATE (its CHECK was already validated), "
            "so its COMMIT is not the floor the full point should be measured against; "
            "use a FRESH restore"
        )
    # "Empty" and "full" are properties of what each transaction MUTATED, not of
    # what the populations looked like beforehand — and both are asserted
    # EXPLICITLY rather than by truthiness. A missing `n_mutated` is absent
    # evidence, not evidence of zero, and it reads as falsy; a full point that
    # mutated nothing is the same measurement as the empty point, and dividing by
    # its zero yields a teardown slope of 0 that looks like a clean measurement of
    # "commit does not scale with rows". The type test is `type(...) is int`, not
    # isinstance: JSON `false` compares equal to 0 and JSON `true` is an int that
    # is greater than 0, so a bool would satisfy both value tests while carrying
    # no row count at all.
    empty_mutated = empty_run["teardown"].get("n_mutated")
    if type(empty_mutated) is not int or empty_mutated != 0:
        raise SystemExit(
            f"the empty-point run reports n_mutated={empty_mutated!r}; the teardown FLOOR is the "
            "commit of a transaction that mutated NOTHING, and that has to be recorded as an "
            "explicit integer 0 rather than left absent or given as a bool"
        )
    full_mutated = atomic["teardown"].get("n_mutated")
    if type(full_mutated) is not int or full_mutated <= 0:
        raise SystemExit(
            f"the full-point run reports n_mutated={full_mutated!r}; the teardown SLOPE is "
            "(full - empty) / n_mutated and needs a real mutated population — a positive "
            "integer, not a bool — to divide by"
        )

    # Scope is load-bearing, not a label. TEARDOWN_ALLOWANCE_MS is measured on ONE
    # per-batch-mode batch transaction — of EITHER phase, so the probe has to
    # cover max(MAX_BATCH_SIZE, REPAIR_BATCH_SIZE), which is enforced below once
    # those sizes are known. The atomic teardown constants are measured on the
    # single whole-population transaction. Using one for the other under-sizes the
    # atomic stall by the whole difference between them, so an unscoped probe
    # defaults to 'batch' and is never silently promoted.
    probe_unlock_max, probe = _probe_max_unlock_ms(probes, "batch")
    atomic_unlock_max, atomic_probe = _probe_max_unlock_ms(probes, "atomic")

    snap_dims = atomic["dimensions_before"]
    snap_pops = atomic["populations_before"]
    prod = production or {}
    prod_dims = prod.get("dimensions", snap_dims)
    prod_pops = prod.get("populations", snap_pops)
    full_restore = production is None

    # The snapshot populations are what every per-row constant is MEASURED from,
    # so an empty one is not a zero branch — it is an unsynthesized snapshot, and
    # continuing would fabricate the constant from a fallback rather than derive
    # it. The zero branches that ARE legitimate are on the PRODUCTION side.
    if not snap_pops.get("n_stale"):
        raise SystemExit(
            "snapshot has no stale population: MARGINED_MS_PER_ROW cannot be measured. "
            "Re-run the atomic measurement with --synthesize-stale"
        )
    if not snap_pops.get("n_repair"):
        raise SystemExit(
            "snapshot has no repair population: MARGINED_MS_PER_REPAIR_ROW cannot be measured. "
            "Re-run the atomic measurement with --synthesize-repair K (K >= 1000, or the whole "
            "ended-visible set if it is smaller)"
        )

    r_sessions = max(
        _ratio(prod_dims.get("total_rows"), snap_dims.get("total_rows")),
        _ratio(prod_dims.get("sessions_bytes"), snap_dims.get("sessions_bytes")),
    )
    r_moves = max(
        _ratio(prod_dims.get("m_total"), snap_dims.get("m_total")),
        _ratio(prod_dims.get("moves_bytes"), snap_dims.get("moves_bytes")),
    )

    # --- row work ----------------------------------------------------------
    t = atomic["timings"]
    n_stale_snap = snap_pops["n_stale"]

    backfill_total = t.get("single_session_compute", {}).get("total_ms", 0.0) + t.get(
        "guarded_update", {}
    ).get("total_ms", 0.0) + t.get("load_moves", {}).get("total_ms", 0.0) + t.get(
        "select_batch", {}
    ).get("total_ms", 0.0)

    # PER-ROW FIRST, then multiply by the production population — never
    # "project the total, then divide by the production population".
    #
    # The division form is undefined at N_stale_prod = 0, which is a LEGITIMATE
    # production state (a fully-stamped database), and any fallback it falls
    # through to fabricates MARGINED_MS_PER_ROW out of nothing. The per-row cost
    # is a property of the SNAPSHOT measurement and survives a production
    # population of zero intact — which is exactly what the design requires: the
    # backfill TERM drops out of Decision 1 while the CONSTANT is still declared,
    # because the runtime guard multiplies it by the LIVE count, which may have
    # grown since the audit.
    per_row_snap = backfill_total / n_stale_snap
    # Sessions can get longer without getting more numerous, and a per-session
    # cost measured at the snapshot's move distribution would miss that. Clamped
    # at 1.0: shorter production games earn no discount.
    snap_plies = snap_pops.get("m_moves", 0) / n_stale_snap
    prod_plies = (
        prod_pops["m_moves"] / prod_pops["n_stale"]
        if prod_pops.get("n_stale") and prod_pops.get("m_moves") is not None
        else snap_plies
    )
    plies_growth = max(1.0, prod_plies / snap_plies) if snap_plies else 1.0
    t_backfill_prod = per_row_snap * plies_growth * float(prod_pops.get("n_stale") or 0)

    # T_repair: PER-CANDIDATE mutation only, scans excluded. Legitimately 0 when
    # the production repair population is 0 — and that is a TRUE statement rather
    # than a hidden one, because the scans it would otherwise absorb are priced
    # by their own term below.
    per_candidate_snap = t.get("repair_per_candidate", {}).get("median_ms", 0.0)
    t_repair_prod = per_candidate_snap * float(prod_pops.get("n_repair") or 0)

    # --- scan work: no population, no zero branch --------------------------
    scans = atomic["scans"]
    t_scan_stmt_snap = scan_stmt_max_ms(scans)
    t_scan_stmt_prod = t_scan_stmt_snap * r_moves
    t_coverage_snap = scans["coverage_assert"]["max_ms"]
    t_coverage_prod = t_coverage_snap * r_sessions
    # The backfill's own game_sessions work: one convergence count, and one full
    # selection sweep (all pages of one pass). Both scale by r_sessions, not by
    # either population — see time_scan_statements.
    t_backfill_remaining_snap = scans.get("backfill_remaining", {}).get("max_ms", 0.0)
    t_backfill_remaining_prod = t_backfill_remaining_snap * r_sessions
    t_backfill_sweep_snap = scans.get("backfill_select_sweep", {}).get("max_ms", 0.0)
    t_backfill_sweep_prod = t_backfill_sweep_snap * r_sessions

    # --- atomic teardown: a floor AND a slope ------------------------------
    full = atomic["teardown"]
    empty = empty_run["teardown"]
    n_mut_snap = full["n_mutated"]
    # The larger of commit and cancel-to-unlock at each point: locks are held
    # until whichever one returns, and the breach path ends in a cancellation.
    teardown_full = max(full["commit_ms"], atomic_unlock_max)
    # The EMPTY point has no cancel-to-unlock counterpart and needs none: an
    # atomic run that mutated nothing holds no row lock, so there is no lock for a
    # competing writer to wait on and the commit is the whole of its teardown.
    teardown_empty = empty["commit_ms"]
    floor_prod = teardown_empty * (1.0 if full_restore else r_sessions)
    # n_mut_snap is guaranteed positive by the full-point check above, so this is
    # a real division rather than a guarded one that quietly yields a zero slope.
    slope_prod = max(0.0, (teardown_full - teardown_empty) / n_mut_snap)

    # --- batch-scoped estimates -------------------------------------------
    max_single_session_compute = t.get("single_session_compute", {}).get("max_ms", 0.0)
    # BOTH phases' batch commits. The repair phase runs its own per-batch
    # transactions and holds row locks until they return, so a repair commit is a
    # lock-hold tail exactly as a backfill commit is. Reading only batch_commit
    # let a 100ms repair commit sit behind a 1ms backfill commit and report the
    # maximum as 1ms.
    max_batch_commit = max(
        [
            m["timings"].get(key, {}).get("max_ms", 0.0)
            for m in batches
            for key in ("batch_commit", "repair_batch_commit")
        ]
        or [0.0]
    )

    # --- the margined constants -------------------------------------------
    ceil3 = lambda x: int(math.ceil(MARGIN * x))  # noqa: E731
    margined_ms_per_row = max(1, ceil3(per_row_snap * plies_growth))
    margined_ms_per_repair_row = max(1, ceil3(per_candidate_snap))
    margined_ms_per_scan_stmt = max(1, ceil3(t_scan_stmt_prod))
    margined_ms_coverage_assert = max(1, ceil3(t_coverage_prod))
    margined_ms_backfill_remaining = max(1, ceil3(t_backfill_remaining_prod))
    margined_ms_backfill_select_sweep = max(1, ceil3(t_backfill_sweep_prod))
    max_single_session_compute_ms = max(1, ceil3(max_single_session_compute))
    teardown_allowance_ms = max(1, ceil3(max(max_batch_commit, probe_unlock_max)))
    if int(atomic_probe.get("rows_locked", 0)) < n_mut_snap:
        raise SystemExit(
            f"the atomic cancel probe locked {atomic_probe.get('rows_locked')} row(s) but the "
            f"atomic transaction mutated {n_mut_snap}; the breach path was measured on a "
            "smaller transaction than the one it is meant to bound"
        )
    margined_atomic_teardown_fixed = max(1, ceil3(floor_prod))
    margined_us_atomic_teardown_per_row = max(1, int(math.ceil(MARGIN * 1000.0 * slope_prod)))

    max_batch_ms = mod.MAX_BATCH_MS
    b_formula = max_batch_ms // margined_ms_per_row
    r_formula = max_batch_ms // margined_ms_per_repair_row

    # B_tested: the largest size actually exercised whose observed maximum single
    # batch satisfied 3 * observed <= MAX_BATCH_MS. The MIN of formula and tested
    # is the point: B_formula comes from a MEAN per-row cost and can name a batch
    # size nothing ever ran, and admitting it would mean the deployment's maximum
    # batch was never demonstrated to fit the deadline.
    def _tested(key_batch: str, key_rows: str, key_size: str, phase: str):
        """The largest batch a run ACTUALLY EXECUTED that satisfied the 3x rule.

        The demonstrated size is the observed page CARDINALITY, not the requested
        ``LIMIT``. A run whose population is smaller than the requested size never
        exercises the size it was asked for — the first Phase 1 run asked for a
        repair batch of 301 against a population of 300 — and freezing the request
        would admit a batch nothing ever ran, which is exactly what this bound
        exists to prevent.

        No passing candidate is a HARD FAILURE, not a fall-through to the
        formula. Falling back would hand the admitted maximum to a number derived
        from a MEAN per-row cost with nothing empirical behind it — the state
        ``min(formula, tested)`` was written to make impossible.
        """
        tried = []
        best: int | None = None
        for m in batches:
            observed = m["timings"].get(key_batch, {}).get("max_ms")
            if observed is None:
                continue
            rows = m["timings"].get(key_rows, {}).get("max_ms")
            demonstrated = int(rows) if rows else 0
            requested = m[key_size]
            passed = MARGIN * observed <= max_batch_ms
            tried.append({
                "requested_size": requested,
                "demonstrated_size": demonstrated,
                "observed_max_ms": observed,
                "passes_3x": passed,
                "reached_requested_size": demonstrated >= requested,
            })
            if passed and demonstrated and (best is None or demonstrated > best):
                best = demonstrated
        if best is None:
            raise SystemExit(
                f"no {phase} batch candidate both executed and satisfied "
                f"3 * observed <= MAX_BATCH_MS ({max_batch_ms}); {phase} batch sizing has "
                "nothing demonstrated to bound it"
            )
        return best, tried

    b_tested, b_tried = _tested("single_batch", "single_batch_rows", "batch_size", "backfill")
    r_tested, r_tried = _tested(
        "single_repair_batch", "single_repair_batch_rows", "repair_batch_size", "repair"
    )

    max_batch_size = min(b_formula, b_tested)
    repair_batch_size = min(r_formula, r_tested)

    # The batch-scope breach measurement has to cover the LARGEST admitted batch
    # transaction, and that is not necessarily the backfill's. TEARDOWN_ALLOWANCE_MS
    # bounds the teardown of "one per-batch-mode batch" — the repair phase's
    # batches are per-batch-mode batches too, and REPAIR_BATCH_SIZE can exceed
    # MAX_BATCH_SIZE (it divides by a cheaper per-row cost). Checked here rather
    # than at probe time because the admitted sizes are not known until now.
    largest_admitted_batch = max(max_batch_size, repair_batch_size)
    if int(probe.get("rows_locked", 0)) < largest_admitted_batch:
        raise SystemExit(
            f"the batch cancel probe locked {probe.get('rows_locked')} row(s) but the largest "
            f"admitted batch is {largest_admitted_batch} "
            f"(MAX_BATCH_SIZE={max_batch_size}, REPAIR_BATCH_SIZE={repair_batch_size}); "
            f"re-probe with --probe-scope batch --batch-size {largest_admitted_batch}"
        )

    # MAX_BATCH_MS + TEARDOWN_ALLOWANCE_MS and NOTHING MORE. There is deliberately
    # no MAX_SINGLE_SESSION_COMPUTE_MS addend: MAX_BATCH_MS is batch-wide over SQL
    # AND Python, because the per-session compute watchdog is armed to
    # min(MAX_SINGLE_SESSION_COMPUTE_MS, batch remaining, revision remaining,
    # atomic remaining) and so cannot let a session's compute pass the batch
    # deadline. Adding the per-session ceiling on top would double-count it; it
    # survives only as that watchdog ceiling.
    est_max_lock_hold_ms = max_batch_ms + teardown_allowance_ms
    # The maximum over EVERY statement _arm arms with this cap, i.e. everything
    # routed through _Runner._arm_scan. The two convergence scans are NOT priced by
    # the same term, and that is the whole reason a third argument is needed here:
    #
    #   REPAIR_REMAINING_SQL wraps the ply detector, so it scans session_moves and
    #   is already one of the four complete statements behind
    #   margined_ms_per_scan_stmt (scaled by G_moves).
    #
    #   BACKFILL_REMAINING_SQL scans game_sessions on
    #   `player_accuracy_algo_version IS NULL OR < 1`, which no index covers, so it
    #   is O(G_sessions) and priced by margined_ms_backfill_remaining instead. That
    #   is the term the maximum used to omit — and the one that relation growth
    #   alone can push above the other two, because it scales by neither population.
    #
    # Omitting it derives a timeout that cancels a statement at less than its own
    # measured cost, and the 57014 surfaces through the exhaustion template as
    # "did not converge".
    #
    # BACKFILL_SELECT_SWEEP is deliberately NOT in this maximum: the sweep is a
    # sequence of pages and each page is armed by _arm_batch_stmt against
    # MAX_BATCH_MS, so the sweep constant prices a multi-statement unit that no
    # single armed value has to cover. It belongs to the stall projection only.
    scan_stmt_timeout_ms = max(
        margined_ms_per_scan_stmt,
        margined_ms_coverage_assert,
        margined_ms_backfill_remaining,
    )

    # --- Decision 1: the writer-stall admission verdict --------------------
    t_stall_prod = (
        t_backfill_prod
        + t_repair_prod
        + mod.ATOMIC_SCANS_UNDER_LOCK * t_scan_stmt_prod
        + t_coverage_prod
        # The backfill's own game_sessions work, counted 1 and 1 because atomic
        # backfill converges in a single unlocked-selection pass. Charged
        # unconditionally, which over-charges the N_stale = 0 path (where both
        # precede the first, repair-owned, row lock) by two game_sessions scans —
        # safe in the only direction that matters.
        + mod.BACKFILL_SELECT_SWEEPS_UNDER_LOCK * t_backfill_sweep_prod
        + mod.BACKFILL_REMAINING_UNDER_LOCK * t_backfill_remaining_prod
        + floor_prod
        + slope_prod * ((prod_pops.get("n_stale") or 0) + (prod_pops.get("n_repair") or 0))
    )
    atomic_admitted = MARGIN * t_stall_prod <= mod.MAX_WRITER_STALL_MS

    return {
        "scaling": {
            "full_restore": full_restore,
            "r_sessions": r_sessions,
            "r_moves": r_moves,
            "snapshot_dimensions": snap_dims,
            "production_dimensions": prod_dims,
            "snapshot_populations": snap_pops,
            "production_populations": prod_pops,
        },
        "projected_ms": {
            "T_backfill_prod": t_backfill_prod,
            "T_repair_prod": t_repair_prod,
            "T_repair_per_candidate_snap": per_candidate_snap,
            "T_scan_stmt_snap": t_scan_stmt_snap,
            "T_scan_stmt_prod": t_scan_stmt_prod,
            "T_coverage_assert_snap": t_coverage_snap,
            "T_coverage_assert_prod": t_coverage_prod,
            "T_backfill_remaining_snap": t_backfill_remaining_snap,
            "T_backfill_remaining_prod": t_backfill_remaining_prod,
            "T_backfill_select_sweep_snap": t_backfill_sweep_snap,
            "T_backfill_select_sweep_prod": t_backfill_sweep_prod,
            "backfill_select_sweep_pages": scans.get("backfill_select_sweep", {}).get("pages"),
            "T_atomic_teardown_floor_prod": floor_prod,
            "T_atomic_teardown_per_row_prod": slope_prod,
            "N_mut_snap": n_mut_snap,
            "max_single_session_compute_ms_observed": max_single_session_compute,
            "max_batch_commit_ms_observed": max_batch_commit,
            "max_batch_cancel_to_unlock_ms_observed": probe_unlock_max,
            "max_atomic_cancel_to_unlock_ms_observed": atomic_unlock_max,
            "batch_probe_trials": probe["trials"],
            "batch_probe_rows_locked": probe["rows_locked"],
            "atomic_probe_trials": atomic_probe["trials"],
            "atomic_probe_rows_locked": atomic_probe["rows_locked"],
            "per_row_snap_ms": per_row_snap,
            "plies_growth": plies_growth,
        },
        "constants": {
            "SIZED_TOTAL_ROWS": prod_dims.get("total_rows"),
            "SIZED_SESSIONS_BYTES": prod_dims.get("sessions_bytes"),
            "SIZED_M_TOTAL": prod_dims.get("m_total"),
            "SIZED_MOVES_BYTES": prod_dims.get("moves_bytes"),
            "MARGINED_MS_PER_ROW": margined_ms_per_row,
            "MARGINED_MS_PER_REPAIR_ROW": margined_ms_per_repair_row,
            "MARGINED_MS_PER_SCAN_STMT": margined_ms_per_scan_stmt,
            "MARGINED_MS_COVERAGE_ASSERT": margined_ms_coverage_assert,
            "MARGINED_MS_BACKFILL_REMAINING": margined_ms_backfill_remaining,
            "MARGINED_MS_BACKFILL_SELECT_SWEEP": margined_ms_backfill_select_sweep,
            "SCAN_STMT_TIMEOUT_MS": scan_stmt_timeout_ms,
            "MAX_SINGLE_SESSION_COMPUTE_MS": max_single_session_compute_ms,
            "TEARDOWN_ALLOWANCE_MS": teardown_allowance_ms,
            "MARGINED_MS_ATOMIC_TEARDOWN_FIXED": margined_atomic_teardown_fixed,
            "MARGINED_US_ATOMIC_TEARDOWN_PER_ROW": margined_us_atomic_teardown_per_row,
            "MAX_BATCH_SIZE": max_batch_size,
            "DEFAULT_BATCH_SIZE": max_batch_size,
            "REPAIR_BATCH_SIZE": repair_batch_size,
            "EST_MAX_LOCK_HOLD_MS": est_max_lock_hold_ms,
        },
        "batch_sizing": {
            "B_formula": b_formula,
            "B_tested": b_tested,
            "B_bound_by": "formula" if b_formula <= b_tested else "tested",
            "backfill_candidates": b_tried,
            "R_formula": r_formula,
            "R_tested": r_tested,
            "R_bound_by": "formula" if r_formula <= r_tested else "tested",
            "repair_candidates": r_tried,
        },
        "invariants": {
            "zero_batch_backfill_ok": b_formula >= 1,
            "zero_batch_repair_ok": r_formula >= 1,
            "est_lock_hold_fits": est_max_lock_hold_ms <= mod.MAX_WRITER_STALL_MS,
            # The revision's OWN formula, over the freshly derived constants — the
            # last term (the backfill's per-pass game_sessions work) is mandatory
            # and was the gap a session_moves-only budget left.
            "scan_budget_ms": mod._scan_budget_ms(
                margined_ms_per_scan_stmt,
                margined_ms_coverage_assert,
                margined_ms_backfill_select_sweep,
                margined_ms_backfill_remaining,
            ),
            "scan_budget_limit_ms": mod.REVISION_DEADLINE_S * 1000,
            "scan_budget_ok": mod._scan_budget_ms(
                margined_ms_per_scan_stmt,
                margined_ms_coverage_assert,
                margined_ms_backfill_select_sweep,
                margined_ms_backfill_remaining,
            )
            < mod.REVISION_DEADLINE_S * 1000,
        },
        "decision_1": {
            "T_stall_prod_ms": t_stall_prod,
            "margined_stall_ms": MARGIN * t_stall_prod,
            "MAX_WRITER_STALL_MS": mod.MAX_WRITER_STALL_MS,
            "verdict": "atomic" if atomic_admitted else "batch",
            "GHOSTREPLAY_ACCURACY_BACKFILL_MODE": "atomic" if atomic_admitted else "batch",
        },
    }


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _run_atomic(engine, args) -> dict[str, Any]:
    timer = Timer()
    with engine.connect() as conn:
        dims_before = read_dimensions(conn)
        pops_before = read_populations(conn)

    # ONE pass per invocation, and the point it measures is decided by the
    # populations it found. A second pass in the same process CANNOT be the empty
    # point: the first already validated the constraint, so the second's COMMIT
    # flushes no catalog change and subtracting it from the full point would
    # charge VALIDATE's commit to the per-row slope. The floor gets its own
    # fresh restore, prepared with --synthesize-stamped.
    point = "empty" if pops_before["n_stale"] == 0 and pops_before["n_repair"] == 0 else "full"
    measured = atomic_pass(
        engine,
        batch_size=args.batch_size,
        repair_batch_size=args.repair_batch_size,
        scan_trials=args.scan_trials,
        timer=timer,
        label=point,
    )

    with engine.connect() as conn:
        dims_after = read_dimensions(conn)
        pops_after = read_populations(conn)

    return {
        "kind": "atomic",
        "batch_size": args.batch_size,
        "repair_batch_size": args.repair_batch_size,
        "dimensions_before": dims_before,
        "populations_before": pops_before,
        "dimensions_after": dims_after,
        "populations_after": pops_after,
        "scans": measured["scans"],
        "scan_stmt_max_ms": scan_stmt_max_ms(measured["scans"]),
        "scan_plan_inversion": scan_plan_inversion(measured["scans"]),
        "teardown_point": point,
        "validated_in_run": "validate" in timer.samples,
        "teardown": measured,
        "timings": timer.summary(),
    }


def _run_batch(engine, args) -> dict[str, Any]:
    timer = Timer()
    with engine.connect() as conn:
        dims_before = read_dimensions(conn)
        pops_before = read_populations(conn)
    with engine.begin() as conn:
        time_validate(conn, timer)
        scans = time_scan_statements(conn, args.scan_trials)

    stamped = run_backfill_batched(engine, args.batch_size, timer)
    nulled = run_repair_batched(engine, args.repair_batch_size, timer)

    with engine.begin() as conn:
        timer.time("coverage_assert_inline", lambda: conn.execute(text(mod.COVERAGE_ASSERT_SQL)))
        timer.time(
            "soundness_assert_inline", lambda: conn.execute(text(mod.SOUNDNESS_ASSERT_SQL))
        )
    with engine.connect() as conn:
        dims_after = read_dimensions(conn)
        pops_after = read_populations(conn)

    return {
        "kind": "batch",
        "batch_size": args.batch_size,
        "repair_batch_size": args.repair_batch_size,
        "stamped": stamped,
        "nulled": nulled,
        "dimensions_before": dims_before,
        "populations_before": pops_before,
        "dimensions_after": dims_after,
        "populations_after": pops_after,
        "scans": scans,
        "scan_stmt_max_ms": scan_stmt_max_ms(scans),
        "scan_plan_inversion": scan_plan_inversion(scans),
        "timings": timer.summary(),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="size_accuracy_backfill",
        description="Sizing harness for revision 20260719_01 (Release B).",
    )
    p.add_argument("--url", help="explicit database URL of a DISPOSABLE restore")
    p.add_argument("--mode", choices=("atomic", "batch"), help="which execution shape to time")
    p.add_argument("--cancel-probe", action="store_true", help="time the breach path")
    p.add_argument(
        "--probe-scope",
        choices=("batch", "atomic"),
        default="batch",
        help=(
            "what the probed transaction represents. 'batch' feeds TEARDOWN_ALLOWANCE_MS and "
            "must lock at least max(MAX_BATCH_SIZE, REPAIR_BATCH_SIZE) rows — the largest "
            "admitted batch of EITHER phase, which is not necessarily the backfill's; "
            "'atomic' locks the whole population (pass --batch-size <N_total>) and feeds "
            "MARGINED_MS_ATOMIC_TEARDOWN_*. They are different transactions of different "
            "sizes and neither substitutes for the other."
        ),
    )
    p.add_argument("--derive", action="store_true", help="Phase 2: freeze constants, no database")
    p.add_argument("--measurement", action="append", default=[], help="measurement JSON (--derive)")
    p.add_argument("--production-dimensions", help="production dimensions/populations JSON")
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument("--repair-batch-size", type=int, default=200)
    p.add_argument("--scan-trials", type=int, default=DEFAULT_SCAN_TRIALS)
    p.add_argument("--trials", type=int, default=DEFAULT_CANCEL_TRIALS, help="cancel-probe trials")
    p.add_argument("--park-seconds", type=float, default=1.0, help="cancel-probe park duration")
    p.add_argument("--synthesize-stale", action="store_true")
    p.add_argument("--synthesize-repair", type=int, default=0, metavar="K")
    p.add_argument(
        "--synthesize-stamped",
        action="store_true",
        help=(
            "stamp the whole ended-visible set version 1 over intact grids, emptying BOTH "
            "populations while leaving the CHECK unvalidated. Use on a FRESH restore to "
            "measure the atomic teardown FLOOR (--mode atomic then measures the empty point)."
        ),
    )
    p.add_argument("--confirm-mutates", action="store_true")
    p.add_argument("--out", help="write the JSON result here as well as to stdout")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.derive:
        measurements = [json.loads(pathlib.Path(f).read_text()) for f in args.measurement]
        production = (
            json.loads(pathlib.Path(args.production_dimensions).read_text())
            if args.production_dimensions
            else None
        )
        result = derive(measurements, production)
    else:
        if not args.url:
            raise SystemExit("--url is required (point it at a DISPOSABLE restore)")
        engine = create_engine(args.url, future=True)
        with engine.connect() as conn:
            dbname = conn.execute(text("SELECT current_database()")).scalar()
            server = conn.execute(text("SELECT version()")).scalar()
        # Printed BEFORE anything else, because this script rewrites the rows it
        # measures and the operator's last chance to notice the wrong database is
        # here.
        print(f"database: {dbname}\nserver:   {server}", file=sys.stderr)
        if not args.confirm_mutates:
            raise SystemExit(
                f"refusing to run against {dbname!r} without --confirm-mutates: this harness "
                "REWRITES the rows it measures and installs a parking trigger"
            )

        # ORDER MATTERS, and the order is stale-then-repair. Stale synthesis nulls
        # the version across the ENTIRE ended-visible set; repair synthesis then
        # stamps K of those rows version 1 with a non-NULL accuracy over a broken
        # grid — which is the shape Release A's unguarded hook left, and which
        # takes those K rows back OUT of the stale population. The two populations
        # are disjoint by construction, so this yields both at once. Running the
        # synthesis the other way round would null the repair candidates it just
        # created and silently leave N_repair at 0.
        synthesis: dict[str, Any] = {}
        if args.synthesize_stale or args.synthesize_repair or args.synthesize_stamped:
            with engine.begin() as conn:
                if args.synthesize_stamped:
                    synthesis["stamped_rows"] = synthesize_stamped(conn)
                if args.synthesize_stale:
                    synthesis["stale_rows"] = synthesize_stale(conn)
                if args.synthesize_repair:
                    synthesis["repair"] = synthesize_repair(conn, args.synthesize_repair)
                analyze_after_synthesis(conn)
                synthesis["analyzed"] = True

        if args.cancel_probe:
            result = {
                "kind": "cancel_probe",
                "scope": args.probe_scope,
                "batch_size": args.batch_size,
                **run_cancel_probe(
                    engine,
                    batch_size=args.batch_size,
                    trials=args.trials,
                    park_seconds=args.park_seconds,
                ),
            }
        elif args.mode == "atomic":
            result = _run_atomic(engine, args)
        elif args.mode == "batch":
            result = _run_batch(engine, args)
        else:
            raise SystemExit("choose one of --mode atomic, --mode batch, --cancel-probe, --derive")
        result["database"] = dbname
        result["server_version"] = server
        if synthesis:
            result["synthesis"] = synthesis

    payload = json.dumps(result, indent=2, default=str)
    print(payload)
    if args.out:
        pathlib.Path(args.out).write_text(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
