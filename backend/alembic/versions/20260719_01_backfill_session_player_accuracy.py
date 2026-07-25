"""Backfill + repair cached session accuracy, fail closed (Release B).

This revision is the **correctness state machine** of Release B *and* its
production runtime envelope. It runs three phases in order — ``validate``,
``backfill``, ``repair`` — and then two fail-closed assertions that must both pass
before any cache-only read is allowed to serve.

Why a repair phase exists at all
--------------------------------
:func:`app.accuracy_v1.compute_game_accuracy` attributes plies to a mover by
INDEX PARITY (``accuracy_v1.py:200``) but takes the eval's sign from
``move.color`` (``accuracy_v1.py:133``). Hand it a row set that is not the
contiguous mainline ply-COORDINATE grid and those two axes disagree, so it
returns a silently WRONG accuracy rather than ``None``. g-22t8.6 froze that grid
as :func:`app.accuracy_rows_v1.ply_coordinates_intact` and put it in front of the
live surface — but Release A's hooks ran *unguarded*, so rows they already
stamped can carry a wrong value. The backfill's population predicate
(``version IS NULL OR version < 1``) skips exactly those rows, so they need their
own pass.

Frozen imports, never the mutable re-export
-------------------------------------------
The revision imports ``AccuracyMove``, ``compute_game_accuracy`` and
``expected_total_moves_from_pgn`` from :mod:`app.accuracy_v1`, and
``ply_color`` / ``ply_coordinates_intact`` from :mod:`app.accuracy_rows_v1` —
never through :mod:`app.accuracy`. A guard that only wrapped the live surface
would be skipped by exactly the code that writes most of these rows, and a future
accuracy v2 re-export must not silently rewrite what this revision persisted.
No live ORM model is used; every statement is migration-local SQL.

The ply-coordinate detector has THREE definitions that must agree
----------------------------------------------------------------
- :func:`app.accuracy_rows_v1.ply_coordinates_intact` (Python, the frozen one);
- ``PLY_DETECTOR_SQL`` — the SET-WIDE form, "which sessions are broken"; and
- ``PLY_DETECTOR_ONE_{PG,SQLITE}`` — the SESSION-SCOPED form.

They are not interchangeable and the difference is a cost decision.
``session_moves`` carries coordinate indexes (``uq_session_moves_session_move_color``,
``idx_session_moves_session``) but NO stored defect marker, and a window function
over ``row_number()`` is not an indexable predicate — so every execution of the
set-wide form is a full scan of ``session_moves`` plus a partition/sort, whose
cost tracks the WHOLE relation and never drops to zero. The session-scoped form
is served by the coordinate index and costs O(plies of one session). Hence:

- the repair's per-candidate re-read uses the session-scoped form, so a full
  relation scan never lands inside a candidate's row-lock hold; and
- each repair pass MATERIALIZES the set-wide detector once into a temp table
  instead of embedding it in every batch's selection, so a pass costs one scan
  rather than one scan per batch.

A persisted defect-marker column would collapse these scans to index reads. It is
deliberately out of scope: it needs its own migration, its own writer change, and
its own backfill — and it moves cost onto the live write path, which is the path
this whole release protects.

Why the repair is lock, re-read, then act — and never one set-based UPDATE
-------------------------------------------------------------------------
The backfill's guarded UPDATE is safe under READ COMMITTED because its predicate
names only columns of the TARGET ROW, so PostgreSQL's post-lock EvalPlanQual
recheck sees the fresher tuple and the row simply drops out of ``RETURNING``.
The repair's predicate depends on ``session_moves``, and an ``id IN (<detector>)``
subplan is re-evaluated under the statement's ORIGINAL snapshot. So a lock-free
set-based repair would: block on a row a /moves upload is repairing, wake up,
still see version 1 / NOT NULL on the target row, still see "broken" from its
stale subplan, and overwrite the hook's freshly-correct value with NULL. The
stale-version guard cannot catch it — both values are version 1, so the version
never advances. **The hook always wins**: lock, re-read in a fresh statement,
then act.

The version column stays 1 for a grid-rejected row. v1 attempted the computation
and its input contract rejected the inputs, which is exactly what a stamped NULL
means per ``docs/session-accuracy-versioning.md:8-13``. Bumping the version would
make the stale-version guard cover this for free, but it would misreport an
unchanged algorithm as changed and would demand a new frozen module plus the full
three-release sequence.

The runtime envelope (g-b-runtime-envelope)
-------------------------------------------
ONE CLOCK. ``REVISION_DEADLINE_S`` is revision-WIDE, taken once at the top of
:func:`upgrade` — *before* ``VALIDATE`` and before the population counts — and it
covers everything the revision executes, including both closing assertions. A
phase clock would leave three holes and every one of them is reachable: the
repair population count runs during mode binding before any runner exists, both
fail-closed assertions run after the runner returns, and a scan armed with a
fresh ``SCAN_STMT_TIMEOUT_MS`` can start at second 899 and finish past second
900. Three rules close them, in both modes:

1. every statement is armed (:func:`_arm`) with the LEAST of every deadline in
   force — its own cap, the remaining revision budget, the batch remainder inside
   a per-batch-mode batch, and in atomic mode the residual stall budget — with
   ``lock_timeout = min(the mode's lock-wait cap, that same remaining)``;
2. Python work is checked against the same clock before each pass, batch,
   session, repair candidate and closing assertion, and the compute watchdog is
   armed to the same remaining; and
3. exhaustion raises with ``phase=`` naming what was running.

Rule 1's HARDNESS is PostgreSQL-only: ``statement_timeout`` and ``lock_timeout``
are PostgreSQL GUCs, so :func:`_arm` issues its two ``set_config`` calls there and
skips them on SQLite. Rules 2 and 3 hold on both dialects, so on SQLite the same
clock is enforced best-effort BETWEEN statements — acceptable precisely because
SQLite is dev/CI-only, single-writer, atomic-only, and has no concurrent writer
whose stall the hard deadline exists to bound.

TWO MODES, both shipped. The deployment chooses which one RUNS, never which code
exists, so the per-batch runner and its PostgreSQL suite stay permanently present
and permanently gated. Atomic mode uses Alembic's single transaction and unlocked
selection; per-batch mode runs on an INDEPENDENT connection with one explicit
transaction per batch, ``FOR NO KEY UPDATE SKIP LOCKED`` selection, and a
``MAX_BATCH_MS`` batch deadline.

WHAT BOUNDS WHAT, at its true confidence:

- **Enforced by PostgreSQL:** no SQL runs past the deadline; no SINGLE lock wait
  exceeds the mode's cap; and no SUM of lock waits exceeds the budget the hold is
  spending from, because ``lock_timeout`` is armed as ``min(cap, remaining)``. The
  third is separate from the second on purpose — ``lock_timeout`` applies per
  ACQUISITION, so on its own it permits any number of just-under-cap waits.
- **Enforced where it can arm:** Python compute, by a ``signal.setitimer``
  watchdog re-armed per session. Main thread only; off it the runner LOGS that it
  is unarmed rather than claiming enforcement it does not have.
- **Estimated only:** teardown. ``COMMIT``/``ROLLBACK`` are not covered by
  ``statement_timeout``, so per-batch mode gets an after-the-fact tripwire on
  ``EST_MAX_LOCK_HOLD_MS`` and atomic mode RESERVES its teardown out of
  ``MAX_WRITER_STALL_MS`` and holds the work to the residual.

Atomic mode has NO after-the-fact lock-hold tripwire and must not pretend to: its
hold ends when Alembic's transaction commits, which happens in ``env.py`` after
:func:`upgrade` has returned. What it has instead is admission IN FRONT (the
projection of :func:`bind_mode` step 5, rechecked against live populations AND
live relation dimensions) and enforcement IN FLIGHT (the residual stall deadline
armed on every statement from ``t_stall_0`` onward), plus OBSERVATION behind both
through ``app.migration_guard.migration_stall_probe`` — which only logs.

Deviations from the plan, stated rather than hidden
---------------------------------------------------
- The ``*_locked`` selection statements are bundle FIELDS that are ``None`` on
  SQLite, alongside ``repair_lock``. The plan named only ``repair_lock`` as
  nullable, but the "runner never reads a statement constant directly" rule
  applies to the per-batch runner too, so its statements must be reachable
  through the bundle.
- ``MARGINED_MS_BACKFILL_SELECT_SWEEP`` and ``MARGINED_MS_BACKFILL_REMAINING``
  are marked PROVISIONAL: the sizing derivation that produced every other
  measured constant predates the discovery that the backfill's OWN
  ``game_sessions`` work is relation-scaled, so it never timed those two
  statements directly. They are derived from that same run's recorded
  ``game_sessions`` scan measurement — see their docstrings — and
  g-b-size-derive-backfill-terms re-measures them.

Both keyset sweeps use the plan's first-page/later-page statement pair, and that
is not interchangeable with the two shortcuts it looks like. A sentinel minimum
ID is WRONG: the nil UUID is a schema-valid session ID, so ``id > '000…0'``
excludes such a row from selection while the remaining-count query still counts
it — the pass exhausts its retries and raises with the row unstamped. A nullable
cursor predicate (``:last_id IS NULL OR id > :last_id``) is correct but
non-sargable, so PostgreSQL abandons the primary key and every page becomes a
full scan of ``game_sessions``.

Downgrade is an explicit no-op. Production rollback is a forward revert, not data
reversal.

Revision ID: 20260719_01
Revises: 20260718_01
Create Date: 2026-07-19

"""
from __future__ import annotations

import contextlib
import logging
import os
import signal
import threading
import time
from typing import Any, NamedTuple

from alembic import op
from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError

# FROZEN imports. Never `app.accuracy` — see the module docstring.
from app.accuracy_rows_v1 import ply_color, ply_coordinates_intact
from app.accuracy_v1 import (
    AccuracyMove,
    compute_game_accuracy,
    expected_total_moves_from_pgn,
)

# The runner reuses the guard's SHARED connection-observability helpers and its
# stall probe rather than re-deriving either. `migration_stall_probe` is the
# STABLE singleton env.py already imports and already reports from — recording on
# a fresh object while env.py reported from the shipped one would measure nothing.
from app.migration_guard import (
    RUNNER_APP_NAME,
    _label_connection,
    _log_backend_pid,
    migration_stall_probe,
)

revision = "20260719_01"
# 20260718_01 (the drift reconciliation) is the current single head and already
# descends from 20260709_02. Descending from 20260709_02 here would create two
# heads and make `alembic upgrade head` fail to resolve a path — the exact
# failure mode 20260718_01 was written to end. The revision ID is dated after it
# for the same reason: the ID must sort after the revision it revises, or the
# lineage reads as a branch that happens to linearize.
down_revision = "20260718_01"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# The algorithm version this revision stamps. Mirrors
# app.accuracy.ACCURACY_ALGO_VERSION, restated as a literal because a migration
# must not import a mutable constant whose value can drift under it.
ALGO_VERSION = 1

CHECK_NAME = "ck_game_sessions_player_accuracy"


class MigrationError(RuntimeError):
    """Raised when a phase cannot converge or a fail-closed assertion fails.

    Declared here rather than beside the phase functions because the sizing block
    below raises it AT IMPORT: a zero-batch or scan-budget violation has to fail
    when the revision is loaded, not 900 seconds into a run.
    """


class DeadlineExceeded(MigrationError):
    """A budget in force ran out, or a statement was cancelled at its armed value.

    Carries the frozen exhaustion template (see :func:`_exhausted`). A distinct
    class so :func:`_as_exhaustion` can pass an already-formatted exhaustion
    through instead of re-wrapping it with a second, less specific phase.
    """


class ComputeWatchdogExceeded(MigrationError):
    """SIGALRM fired: one session's Python compute passed its armed ceiling.

    Not a subclass of :class:`DeadlineExceeded`: this is the raw signal-handler
    raise, and the runner converts it into the exhaustion template with
    ``sqlstate=n/a`` (a Python raise has no SQLSTATE).
    """


# ===========================================================================
# Sizing constants (g-b-size-derive).
#
# Everything below is either a POLICY BOUND chosen once and defended here, or a
# MEASURED number frozen from a sizing run whose provenance — snapshot, date,
# raw numbers, timed SHA — is recorded in docs/release_b_runbook.md. The
# derivation that produced the measured values, and the harness that measured
# them, are backend/scripts/size_accuracy_backfill.py.
#
# Measurement is NOT reachable from here. The shipped revision contains no
# bypass, because a variable that disarms the atomic projection guard and the
# batch deadline is production-reachable by definition: matching
# current_database() only prevents ACCIDENTAL reuse against a differently named
# database, and the production database name is knowable. Sizing runs in a
# standalone harness by hand against a restored snapshot instead.
#
# The runtime envelope below CONSUMES these constants: it arms the actual SQL
# timeouts from them, computes the run-time reserve/budget terms, and enforces
# the admission projection. This block also checks, at import, the invariants
# that are pure arithmetic over frozen literals.
# ===========================================================================

# --- policy bounds ---------------------------------------------------------

#: Hard admission bound on how long the migration may hold a row lock a live
#: writer could want.
MAX_WRITER_STALL_MS = 30_000

#: The enforced per-batch deadline, measured from the start of the batch
#: transaction and covering ALL of the batch's pre-teardown work — every blocking
#: SQL statement AND the Python compute between them, because the per-session
#: compute watchdog is armed against the batch remainder. NOT by itself the
#: lock-hold bound: see EST_MAX_LOCK_HOLD_MS.
MAX_BATCH_MS = 5_000

#: The longest any statement in a batch may wait on a lock. STRICTLY LESS than
#: MAX_BATCH_MS — at or above it, a lock wait could only ever surface as a
#: statement timeout and the setting would be decorative.
BATCH_LOCK_WAIT_MS = 1_000

#: The cap on any SINGLE row-lock wait in atomic mode, armed on the migration
#: connection immediately after VALIDATE so the wait is a designed value rather
#: than VALIDATE_LOCK_TIMEOUT leaking through Alembic's single transaction.
#:
#: A per-acquisition cap and nothing more. PostgreSQL applies lock_timeout
#: separately to each acquisition, so k waits of just under this all succeed
#: while their sum is k seconds of stall no per-wait cap ever sees. What bounds
#: the sum is atomic mode's residual stall deadline, which _arm clamps
#: lock_timeout against — not this constant.
#: https://www.postgresql.org/docs/current/runtime-config-client.html#GUC-LOCK-TIMEOUT
ATOMIC_LOCK_WAIT_MS = BATCH_LOCK_WAIT_MS

#: Lock wait for VALIDATE CONSTRAINT, which runs outside any batch budget and is
#: overwritten immediately afterwards. The string form is what set_config takes;
#: the ms form is what _arm clamps against the revision budget.
VALIDATE_LOCK_WAIT_MS = 10_000
VALIDATE_LOCK_TIMEOUT = f"{VALIDATE_LOCK_WAIT_MS // 1000}s"

#: ONE revision-wide wall clock — not a phase clock and not a runner clock. Taken
#: at the top of upgrade(), ahead of VALIDATE.
REVISION_DEADLINE_S = 900

#: The pass bound the scan-budget invariant charges against, per phase. The
#: runner's own per-phase limits must not exceed it, or the invariant would be
#: charging fewer scans than a run can actually issue. A constant test asserts
#: that.
MAX_PASSES = 20

#: The MAXIMUM number of scan-bearing session_moves statements that can execute
#: after the first row lock in atomic mode. A bound, not an identity: on a run
#: with N_stale = 0 and N_repair > 0 only two do, because the first row lock is
#: then the repair's own and it falls AFTER the materialization. Excludes
#: COVERAGE_ASSERT_SQL, which is charged by its own constant.
ATOMIC_SCANS_UNDER_LOCK = 3

#: The backfill's OWN game_sessions work under atomic mode's single lock hold,
#: counted per PASS. Both are 1 because atomic backfill converges in exactly ONE
#: pass: atomic selection uses the UNLOCKED SELECT_BATCH_* variants with no SKIP
#: LOCKED, so no row is ever transiently skipped, so one sweep drains the stale
#: set and the single remaining count reads zero — the same one-pass argument
#: that pins the atomic pass bound at 1.
#:
#: Charged UNCONDITIONALLY, which over-charges the N_stale = 0 path (where the
#: sweep and the count precede the first, repair-owned, row lock and are really
#: in the health window) by exactly two game_sessions scans. Deliberate and safe
#: in the only direction that matters, identical to how ATOMIC_SCANS_UNDER_LOCK
#: over-charges that path by one session_moves scan: an over-estimate can only
#: reject an atomic run, never wrongly admit one.
BACKFILL_SELECT_SWEEPS_UNDER_LOCK = 1
BACKFILL_REMAINING_UNDER_LOCK = 1

# --- the sized relation dimensions ----------------------------------------
#
# The scan constants are the only priced terms that scale with a RELATION rather
# than with a population, and they are the only ones a population recount cannot
# revalidate. The gap is not hypothetical: a correctly stamped version-1 session
# is in NEITHER population, yet it adds rows and pages to both relations — and
# Release A is the sole production writer for the whole interval between sizing
# and deploy, so every row it writes is exactly that shape. A guard that rechecks
# only the populations is checking the one dimension that cannot move and
# ignoring the one that must. So the dimensions the scan constants were measured
# against are frozen here, and the growth factors divide by them. A dimension
# that lived only in the runbook could not be divided by anything.

SIZED_TOTAL_ROWS = 6_000
SIZED_SESSIONS_BYTES = 10_010_624
SIZED_M_TOTAL = 357_000
SIZED_MOVES_BYTES = 93_241_344

# --- measured, margined 3x -------------------------------------------------

#: Backfill cost per stale session at production's move distribution, x3.
MARGINED_MS_PER_ROW = 5

#: Per repair candidate: lock, SESSION-SCOPED re-read, conditional update, x3.
#: Excludes every set-wide scan, which is why it is a genuinely per-row number —
#: and that exclusion is legitimate only BECAUSE the materialization is hoisted
#: out of the batch.
MARGINED_MS_PER_REPAIR_ROW = 2

#: One execution of the MOST EXPENSIVE COMPLETE scan-bearing statement over
#: session_moves, x3. NOT the cost of a bare PLY_DETECTOR_SQL: nothing in the run
#: ever executes the detector alone. What executes is the detector wrapped in a
#: statement that also joins and filters game_sessions and then counts
#: (REPAIR_REMAINING_SQL, SOUNDNESS_ASSERT_SQL, the pre-flight repair population
#: count) or inserts into the temp table (REPAIR_POPULATE_SQL) — and every one of
#: those costs strictly more than the detector by itself. Sizing times all four
#: standalone and freezes the MAXIMUM; one constant prices all four because they
#: are the same relation scan plus a bounded join or aggregate.
#:
#: Scales with the whole session_moves relation, not with either population, and
#: is nonzero when both populations are zero.
MARGINED_MS_PER_SCAN_STMT = 521

#: One COVERAGE_ASSERT_SQL execution (a whole-game_sessions scan), x3. Priced
#: separately because it scans a DIFFERENT relation and scales by a DIFFERENT
#: ratio. In atomic mode it runs under every row lock the backfill took.
MARGINED_MS_COVERAGE_ASSERT = 6

#: PROVISIONAL (g-b-size-derive-backfill-terms).
#:
#: One execution of BACKFILL_REMAINING_SQL, x3. Scan-bearing even though it never
#: touches session_moves: it filters game_sessions on
#: `player_accuracy_algo_version IS NULL OR < 1`, and NO INDEX covers that
#: predicate — game_sessions carries only the user/status/mode/drill indexes
#: (app/models.py:188, app/models.py:224). So it is a full game_sessions scan of
#: O(G_sessions), NOT O(N_stale): every version-1 row Release A stamps between
#: sizing and deploy GROWS the scanned relation while SHRINKING N_stale, so a
#: correctly-stamped row raises this cost while leaving the population term
#: unchanged. Omitting it priced a growing relation at zero.
#:
#: Provisional derivation, from the recorded run rather than a direct timing:
#: COVERAGE_ASSERT_SQL is the same shape against the same relation — a count over
#: game_sessions whose predicate no index covers — and it measured 1.74 ms max at
#: the sized dimensions, so this takes the same ceil(3 * 1.74) = 6.
MARGINED_MS_BACKFILL_REMAINING = 6

#: PROVISIONAL (g-b-size-derive-backfill-terms).
#:
#: One full backfill selection SWEEP — every SELECT_BATCH_* page of one pass
#: together — x3. Each page walks primary-key order applying the same unindexed
#: version predicate, so a sweep touches ~all of game_sessions and costs
#: O(G_sessions) rather than O(N_stale), for the same reason as
#: MARGINED_MS_BACKFILL_REMAINING above.
#:
#: Provisional derivation, from the recorded run rather than a direct timing:
#: a sweep at the sized dimensions is ceil(SIZED_TOTAL_ROWS / MAX_BATCH_SIZE) + 1
#: = 7 pages (the +1 is the empty page that terminates the sweep), and each page
#: is priced at a WHOLE game_sessions scan — the worst case for an unindexed
#: filter — using the recorded 1.74 ms coverage-scan measurement:
#: ceil(3 * 7 * 1.74) = 37. Pricing every page at a full scan is deliberately
#: conservative; a direct measurement can only lower it.
MARGINED_MS_BACKFILL_SELECT_SWEEP = 37

#: The per-statement cap for EVERY scan-bearing statement: the repair population
#: count, REPAIR_POPULATE_SQL, REPAIR_REMAINING_SQL, SOUNDNESS_ASSERT_SQL, the
#: backfill population count / convergence count, AND COVERAGE_ASSERT_SQL. It
#: must therefore cover the most expensive of them, not merely the cheapest —
#: arming a statement with a timeout below its own measured cost is a
#: self-inflicted cancellation.
#:
#: A CAP, not the armed value: what _arm arms is min(SCAN_STMT_TIMEOUT_MS, every
#: deadline in force), so a scan starting late in the revision's clock gets only
#: what is left rather than a fresh allowance. These statements are not inside a
#: BATCH budget and MAX_BATCH_MS must never be armed on them.
SCAN_STMT_TIMEOUT_MS = 521

#: Margined worst-case Python compute for ONE session (parse + validate +
#: score). A maximum, not a mean: what the compute watchdog has to survive is the
#: worst single session in the population.
#:
#: The watchdog's per-session CEILING, and nothing else. It is NOT an addend of
#: EST_MAX_LOCK_HOLD_MS: the watchdog is armed to min(this, batch remaining,
#: revision remaining, atomic remaining), so no session's compute can push a
#: batch past MAX_BATCH_MS and MAX_BATCH_MS already covers every session's
#: compute. Adding it there would double-count.
MAX_SINGLE_SESSION_COMPUTE_MS = 79

#: Margined worst-case teardown of ONE PER-BATCH-MODE BATCH TRANSACTION, taken as
#: the larger of observed commit and observed CANCEL-TO-UNLOCK, because locks are
#: held until whichever one has finished.
#:
#: Cancel-to-unlock, NOT teardown_ms. The tail a writer actually waits through on
#: the breach path is not "how long ROLLBACK took": PostgreSQL notices the
#: cancellation at the statement's next interrupt point, unwinds the statement,
#: raises to the driver, Python then issues ROLLBACK, and the row locks release
#: when that returns. A clock the cancelled process starts begins AFTER the
#: interrupt latency and the unwind have already elapsed and cannot contain them,
#: so freezing this from teardown_ms would under-size it by exactly the part that
#: is hardest to predict. The measured input is instead the interval from cancel
#: issuance to the moment a competing FOR NO KEY UPDATE NOWAIT on a held row
#: ACQUIRES, observed from a second session.
#:
#: Its scope is exactly a BATCH — but a batch of EITHER phase, and the larger of
#: the two is not necessarily the backfill's. REPAIR_BATCH_SIZE divides by a
#: cheaper per-row cost, so it can exceed MAX_BATCH_SIZE (it does here: 2500
#: against 1000), and the repair phase's per-batch transactions hold row locks
#: until their own commits return. The breach path is therefore measured on a
#: transaction of at least max(MAX_BATCH_SIZE, REPAIR_BATCH_SIZE) rows, which
#: the sizing derivation enforces rather than trusts.
#:
#: It neither covers nor pretends to cover the teardown of atomic mode's single
#: whole-population transaction — that has its own two constants below.
TEARDOWN_ALLOWANCE_MS = 7

#: Margined teardown of an atomic transaction that mutated NO rows: the COMMIT
#: (or ROLLBACK) of a run that still executed VALIDATE and the scans. The
#: population-independent floor of atomic teardown, and never zero — an atomic
#: transaction that mutated nothing still commits.
MARGINED_MS_ATOMIC_TEARDOWN_FIXED = 2

#: Margined marginal teardown cost per MUTATED ROW in the atomic transaction, in
#: MICROSECONDS. Denominated in µs on purpose: the marginal cost of one more
#: dirty row at commit is far below a millisecond, and rounding it up to an
#: integer millisecond would add a phantom second of projected stall per thousand
#: rows and make atomic mode inadmissible on populations it comfortably handles.
#: :func:`atomic_teardown_reserve_ms` divides it by 1000; a constant test pins
#: that, so a future "tidy-up" into milliseconds fails rather than silently
#: inflating every atomic projection by three orders of magnitude.
MARGINED_US_ATOMIC_TEARDOWN_PER_ROW = 2

#: The largest backfill batch sizing ACTUALLY DEMONSTRATED — the largest size
#: exercised in Phase 1 whose observed maximum single-batch duration satisfied
#: 3 * observed <= MAX_BATCH_MS. Frozen beside the formula bound because the
#: formula comes from a MEAN per-row cost and can name a batch size nothing ever
#: ran; admitting that would mean the deployment's maximum batch was never
#: demonstrated to fit the deadline, which is precisely what MAX_BATCH_SIZE
#: exists to prevent.
B_TESTED = 1_000
R_TESTED = 3_000


def _admitted_batch_size(margined_ms_per_row: int, tested: int, *, constant: str) -> int:
    """min(formula, tested), with the zero-batch invariant checked rather than clamped.

    B_formula = floor(MAX_BATCH_MS / MARGINED_MS_PER_ROW) must be at least 1,
    which is exactly the condition MARGINED_MS_PER_ROW <= MAX_BATCH_MS. The
    boundary is ADMISSIBLE: at equality B_formula is 1 and a one-row batch
    consumes exactly the margined budget, which the 3x margin already inside the
    per-row constant covers.

    Above the boundary this RAISES rather than clamping to 1. Clamping would
    silently violate the very budget the constant exists to enforce: it would
    admit a batch whose single row is projected to overrun MAX_BATCH_MS and call
    that "the minimum batch size", which is a re-sizing decision disguised as a
    default.

    A pure function, not an inline expression, so the boundary can be pinned from
    both sides without reloading the module.
    """
    if margined_ms_per_row > MAX_BATCH_MS:
        raise MigrationError(
            f"{constant} ({margined_ms_per_row}) exceeds MAX_BATCH_MS ({MAX_BATCH_MS}); "
            "re-size before deploying"
        )
    return min(MAX_BATCH_MS // margined_ms_per_row, tested)


def _scan_budget_ms(
    margined_ms_per_scan_stmt: int,
    margined_ms_coverage_assert: int,
    margined_ms_backfill_select_sweep: int = MARGINED_MS_BACKFILL_SELECT_SWEEP,
    margined_ms_backfill_remaining: int = MARGINED_MS_BACKFILL_REMAINING,
    *,
    g_moves: float = 1.0,
    g_sessions: float = 1.0,
) -> float:
    """Every scan-bearing statement a run can issue, charged against one clock.

    Three groups, because they scan two different relations and scale by two
    different growth factors:

    * ``session_moves`` — two scans per pass (REPAIR_POPULATE_SQL and
      REPAIR_REMAINING_SQL) for each of both phases' MAX_PASSES, plus the two
      one-off scans every run pays (the pre-flight repair population count and
      SOUNDNESS_ASSERT_SQL). Scaled by ``g_moves``.
    * ``game_sessions``, the coverage assertion. Scaled by ``g_sessions``.
    * ``game_sessions``, THE BACKFILL'S OWN WORK — its keyset selection sweep and
      its convergence count, per pass. MANDATORY and easy to miss: both filter
      game_sessions on the UNINDEXED version predicate, so each is O(G_sessions)
      and not O(N_stale), and per-batch mode can run up to MAX_PASSES backfill
      passes. A budget that counted only the session_moves detectors and the
      coverage assertion priced the backfill's own relation-scaled work at zero.

    The one game_sessions scan NOT charged here is the pre-flight STALE
    population count, which is one extra BACKFILL_REMAINING_SQL beyond the
    MAX_PASSES this formula prices. It is absorbed by the 3x margin already
    inside each constant, and charging it would change a bound the acceptance
    contract pins verbatim.

    At the default factors of 1.0 this is the IMPORT-time form over frozen
    literals; with live factors it is the RUNTIME form. The two are not
    redundant: a literal check cannot see that the relations grew after sizing.
    """
    return (
        (2 * MAX_PASSES + 2) * margined_ms_per_scan_stmt * g_moves
        + margined_ms_coverage_assert * g_sessions
        + MAX_PASSES
        * (margined_ms_backfill_select_sweep + margined_ms_backfill_remaining)
        * g_sessions
    )


#: EST_MAX_LOCK_HOLD_MS is the MARGINED EMPIRICAL ADMISSION ESTIMATE of how long
#: one PER-BATCH-MODE BATCH can hold a row lock. It is not a hard bound and is
#: not named as though it were. A constant test asserts it is <=
#: MAX_WRITER_STALL_MS, which is arithmetic over frozen literals and proves that
#: the ESTIMATE fits the budget — nothing more. It says nothing about atomic
#: mode, whose single lock hold is bounded by the admission projection and the
#: residual stall deadline instead.
#:
#: MAX_BATCH_MS + TEARDOWN_ALLOWANCE_MS and NOTHING MORE. There is deliberately
#: no MAX_SINGLE_SESSION_COMPUTE_MS addend: MAX_BATCH_MS is batch-wide over SQL
#: AND Python, because the per-session compute watchdog is armed against the
#: batch remainder and so cannot let compute run past the batch deadline. The one
#: part MAX_BATCH_MS does not cover is teardown, which statement_timeout does not
#: cover either. If this fails its invariant, MAX_BATCH_MS is the knob, not the
#: stall bound.
EST_MAX_LOCK_HOLD_MS = MAX_BATCH_MS + TEARDOWN_ALLOWANCE_MS

#: The largest batch any override may request, AND the largest batch sizing
#: actually demonstrated. Derived at import so the zero-batch invariant is
#: checked here rather than discovered at run time.
MAX_BATCH_SIZE = _admitted_batch_size(
    MARGINED_MS_PER_ROW, B_TESTED, constant="MARGINED_MS_PER_ROW"
)
DEFAULT_BATCH_SIZE = MAX_BATCH_SIZE
REPAIR_BATCH_SIZE = _admitted_batch_size(
    MARGINED_MS_PER_REPAIR_ROW, R_TESTED, constant="MARGINED_MS_PER_REPAIR_ROW"
)

# The scan-budget invariant, over frozen literals. A relation large enough that
# the scans ALONE cannot fit the revision's wall clock fails loudly HERE instead
# of exhausting MAX_PASSES and raising a misleading non-convergence error 900
# seconds later. The RUNTIME form of this check, over the LIVE relation
# dimensions, runs before the first row lock in BOTH modes.
if _scan_budget_ms(MARGINED_MS_PER_SCAN_STMT, MARGINED_MS_COVERAGE_ASSERT) >= (
    REVISION_DEADLINE_S * 1000
):
    raise MigrationError(
        "20260719_01 scan budget "
        f"{_scan_budget_ms(MARGINED_MS_PER_SCAN_STMT, MARGINED_MS_COVERAGE_ASSERT):.0f}ms "
        f"does not fit REVISION_DEADLINE_S ({REVISION_DEADLINE_S}s); re-size before deploying"
    )

# Per-phase pass bounds, per MODE.
#
# Per-batch mode needs real retries: SKIP LOCKED transiently skips rows a
# concurrent writer holds, and a skipped row is not complete merely because the
# cursor passed it. It is charged MAX_PASSES by the scan-budget invariant, so
# MAX_PASSES is exactly what it may spend.
BACKFILL_MAX_PASSES = MAX_PASSES
REPAIR_MAX_PASSES = MAX_PASSES
# Atomic mode gets ONE pass per phase. Atomic selection takes no SKIP LOCKED, so
# no row is ever transiently skipped and one sweep drains everything its
# selection can see. A nonzero remaining count after pass 1 therefore does not
# mean "retry" — for the repair it means a writer is producing broken grids WITH
# a non-NULL accuracy while the guard is supposedly live (the live-bug case), and
# for the backfill it means new ended-visible work arrived mid-run. Either way,
# extra passes are not covered by ATOMIC_SCANS_UNDER_LOCK /
# BACKFILL_*_UNDER_LOCK, so raise rather than back off into passes the stall
# projection never admitted.
ATOMIC_MAX_PASSES = 1

CANDIDATES_TABLE = "_accuracy_repair_candidates"

#: The sample of remaining ids every convergence scan returns alongside its
#: count, for the exhaustion template's ``first_remaining``. Bounded so the
#: diagnostic cannot become a second result set worth streaming.
REMAINING_SAMPLE_LIMIT = 20


# ---------------------------------------------------------------------------
# Shared SQL fragments (not statements: no marker, never executed alone).
# ---------------------------------------------------------------------------

# The migration necessarily freezes a SQL copy of the visibility predicate that
# application code centralizes in app/session_contracts.py's
# visible_session_filter() / is_visible_game_session(). Never filter on
# ended_at: it is nullable, and a malformed ended-visible row must still be
# backfilled. The population-parity matrix under test_release_b_pg_matrix.py is
# what actually holds the two definitions together.
VISIBLE_ENDED_SQL = """
    status = 'ended'
    AND (session_mode = 'normal' OR drill_state = 'converted')
"""

POPULATION_PREDICATE_SQL = (
    VISIBLE_ENDED_SQL
    + """
    AND (
      player_accuracy_algo_version IS NULL
      OR player_accuracy_algo_version < 1
    )
"""
)

# The repair population. Disjoint from POPULATION_PREDICATE_SQL by construction
# (version = 1 versus version IS NULL OR < 1), so the repair can never undo the
# backfill's own work, and a backfilled grid-rejected row is already value-NULL
# and therefore not a candidate.
REPAIR_PREDICATE_SQL = (
    VISIBLE_ENDED_SQL
    + f"""
    AND player_accuracy_algo_version = {ALGO_VERSION}
    AND player_accuracy IS NOT NULL
"""
)

# SET-WIDE detector: "which sessions are broken", across the whole table. A full
# scan of session_moves plus a partition/sort, every time — see the docstring.
# Integer division floors on both PostgreSQL and SQLite, matching the validator's
# `i // 2 + 1`. A session with no rows never appears in `ordered` and is
# therefore not broken, matching ply_coordinates_intact([]) is True.
PLY_DETECTOR_SQL = """
    WITH ordered AS (
      SELECT session_id, move_number, color,
             row_number() OVER (
               PARTITION BY session_id
               ORDER BY move_number ASC, CASE WHEN color = 'white' THEN 0 ELSE 1 END ASC
             ) - 1 AS i
      FROM session_moves
    )
    SELECT DISTINCT session_id
    FROM ordered
    WHERE move_number <> i / 2 + 1
       OR color <> CASE WHEN i % 2 = 0 THEN 'white' ELSE 'black' END
"""

# SESSION-SCOPED detector: the same property with the filter pushed INSIDE the
# CTE, so the window function runs over one session's plies rather than over the
# whole relation. Served by uq_session_moves_session_move_color.
_PLY_DETECTOR_ONE_TEMPLATE = """/* ghostreplay:ply_detector_one */
    WITH ordered AS (
      SELECT move_number, color,
             row_number() OVER (
               ORDER BY move_number ASC, CASE WHEN color = 'white' THEN 0 ELSE 1 END ASC
             ) - 1 AS i
      FROM session_moves
      WHERE session_id = {sid}
    )
    SELECT count(*) AS broken
    FROM ordered
    WHERE move_number <> i / 2 + 1
       OR color <> CASE WHEN i % 2 = 0 THEN 'white' ELSE 'black' END
"""

PLY_DETECTOR_ONE_PG = _PLY_DETECTOR_ONE_TEMPLATE.format(sid="CAST(:sid AS uuid)")
PLY_DETECTOR_ONE_SQLITE = _PLY_DETECTOR_ONE_TEMPLATE.format(sid=":sid")


def _remaining_sql(*, marker: str, detector: str) -> str:
    """The count AND up to 20 sampled ids, in ONE dialect-neutral statement.

    Never a bare count. The exhaustion template's ``remaining``, ``passes`` and
    ``first_remaining`` fields all have to come from the SAME scan the pass
    already paid for: fetching the sample with a separate ``SELECT ... LIMIT 20``
    would be a SECOND full detector scan, breaking "one remaining scan per pass"
    and the import scan-budget bound alike.

    A windowed count over the detector result, so it is portable to PostgreSQL
    AND SQLite (both support ``count(*) OVER ()``; no ``array_agg``, which would
    drag in exactly the dialect split the shared statement bundle exists to
    avoid). ZERO RETURNED ROWS MEANS ZERO REMAINING — convergence — so one
    statement serves both the convergence check and the diagnostic sample.
    """
    return (
        f"/* ghostreplay:{marker} */\n"
        f"    WITH remaining AS ({detector}\n    )\n"
        "    SELECT id, count(*) OVER () AS remaining\n"
        "    FROM remaining\n"
        "    ORDER BY id\n"
        f"    LIMIT {REMAINING_SAMPLE_LIMIT}\n"
    )


# ---------------------------------------------------------------------------
# Statements. Every statement's FIRST TOKEN is a distinct marker comment.
#
# A SQL comment changes no plan and no semantics, and it makes the statement
# identifiable FROM ANOTHER SESSION: pg_stat_activity.query reports the text a
# backend is currently executing, so a marker turns "which statement is this
# backend running" from a guess about whitespace into an exact match. The
# cancellation probe and the listener tests depend on that.
#
# Every statement that binds a value against a UUID-typed column exists as a
# _PG / _SQLITE pair and has NO singular name, so there is no unsuffixed
# constant a caller could reach for and get wrong. CAST(:x AS uuid) is a syntax
# error on SQLite; an untyped bind against a uuid column on PostgreSQL is
# `uuid = text`, for which no operator exists.
# ---------------------------------------------------------------------------

# The FIRST page omits the cursor predicate entirely; every later page adds
# `id > <cursor>`. Two statements, not one with a nullable cursor.
#
# A `(:last_id IS NULL OR id > :last_id)` predicate would be one statement, but
# the OR makes it non-sargable, so PostgreSQL stops using the primary key for the
# range scan — turning every page of the keyset sweep into a full scan of
# game_sessions. And a sentinel "minimum ID" is not merely inelegant, it is
# WRONG: the nil UUID is a schema-valid session ID, so `id > '000...0'` silently
# excludes such a row from selection while the remaining-count query still sees
# it, and the backfill exhausts its passes and raises with the row unstamped.
def _select_batch_sql(*, marker: str, cursor: str | None, locked: bool) -> str:
    where = f"id > {cursor}\n      AND " if cursor else ""
    tail = "\n    FOR NO KEY UPDATE SKIP LOCKED" if locked else ""
    return (
        f"/* ghostreplay:{marker} */\n"
        "    SELECT id, player_color, pgn\n"
        "    FROM game_sessions\n"
        f"    WHERE {where}{POPULATION_PREDICATE_SQL.strip()}\n"
        "    ORDER BY id\n"
        f"    LIMIT :batch_size{tail}\n"
    )


SELECT_BATCH_FIRST_PG = _select_batch_sql(marker="select_batch_first", cursor=None, locked=False)
SELECT_BATCH_FIRST_SQLITE = SELECT_BATCH_FIRST_PG  # binds no UUID: identical text
SELECT_BATCH_PG = _select_batch_sql(
    marker="select_batch", cursor="CAST(:last_id AS uuid)", locked=False
)
SELECT_BATCH_SQLITE = _select_batch_sql(marker="select_batch", cursor=":last_id", locked=False)

# Per-batch mode's variants. PostgreSQL-only by construction: SQLite is
# single-writer and has neither FOR NO KEY UPDATE nor SKIP LOCKED.
SELECT_BATCH_FIRST_LOCKED_PG = _select_batch_sql(
    marker="select_batch_first_locked", cursor=None, locked=True
)
SELECT_BATCH_LOCKED_PG = _select_batch_sql(
    marker="select_batch_locked", cursor="CAST(:last_id AS uuid)", locked=True
)

# One bind, one plan shape, typed for the same reason the guarded update's arrays
# are. An expanding IN on PostgreSQL would reintroduce per-batch-size plan churn
# AND the untyped-comparison hazard.
LOAD_MOVES_PG = """/* ghostreplay:load_moves */
    SELECT session_id, move_number, color, eval_cp, eval_mate
    FROM session_moves
    WHERE session_id = ANY(CAST(:ids AS uuid[]))
    ORDER BY session_id, move_number ASC, CASE WHEN color = 'white' THEN 0 ELSE 1 END ASC
"""

LOAD_MOVES_SQLITE = """/* ghostreplay:load_moves */
    SELECT session_id, move_number, color, eval_cp, eval_mate
    FROM session_moves
    WHERE session_id IN :ids
    ORDER BY session_id, move_number ASC, CASE WHEN color = 'white' THEN 0 ELSE 1 END ASC
"""

# ONE server statement, every bind explicitly typed.
#
# Why not `FROM (VALUES (:id1, :acc1), ...)`, the obvious form: it is a latent,
# data-dependent type error and the data that triggers it is entirely
# legitimate. A batch in which EVERY session's computed accuracy is NULL is a
# normal outcome (evaluations missing, or grids the validator rejects) and the
# backfill is REQUIRED to stamp all of them version 1 with a NULL value. With
# untyped binds every entry in that VALUES column is `unknown`; PostgreSQL
# resolves a VALUES column by the UNION/CASE rules, and an all-unknown column
# resolves to `text`. The statement then assigns text into an integer column and
# fails — on the all-NULL batch ONLY, so it survives every fixture that happens
# to contain one scorable game. The same rule bites the ID column whenever the
# driver sends it untyped: `g.id = v.id` becomes `uuid = text`.
#
#   https://www.postgresql.org/docs/current/queries-values.html
#   https://www.postgresql.org/docs/current/typeconv-union-case.html
#
# The casts make the column types a property of the STATEMENT rather than of the
# batch's data, so an all-NULL batch, an all-scored batch and a mixed batch all
# compile to the same plan. Also: one statement means one statement_timeout,
# which is what the armed batch deadline needs — a driver executemany may expand
# into several server statements and a single armed timeout would then bound none
# of them.
UPDATE_SQL_PG = f"""/* ghostreplay:guarded_update */
    UPDATE game_sessions AS g
    SET player_accuracy = v.accuracy,
        player_accuracy_algo_version = {ALGO_VERSION}
    FROM unnest(
           CAST(:ids AS uuid[]),
           CAST(:accuracies AS integer[])
         ) AS v(id, accuracy)
    WHERE g.id = v.id
      AND (
        g.player_accuracy_algo_version IS NULL
        OR g.player_accuracy_algo_version < {ALGO_VERSION}
      )
    RETURNING g.id
"""

# SQLite has no live writer to stall and no array types; the runner issues this
# once per session and reads rowcount. The guard clause is identical, so the
# stale-version semantics are the same.
UPDATE_SQL_SQLITE = f"""/* ghostreplay:guarded_update */
    UPDATE game_sessions
    SET player_accuracy = :accuracy,
        player_accuracy_algo_version = {ALGO_VERSION}
    WHERE id = :sid
      AND (
        player_accuracy_algo_version IS NULL
        OR player_accuracy_algo_version < {ALGO_VERSION}
      )
"""

# The backfill's convergence scan: count AND sampled ids in one statement. A full
# game_sessions scan (the version predicate is unindexed), which is why it is
# priced by MARGINED_MS_BACKFILL_REMAINING and scaled by G_sessions.
BACKFILL_REMAINING_SQL = _remaining_sql(
    marker="backfill_remaining",
    detector=f"""
      SELECT id FROM game_sessions
      WHERE {POPULATION_PREDICATE_SQL}""",
)

# The pre-flight STALE population count. Same SQL as the convergence scan under
# its own marker: a comment changes no plan and no cost, but it makes mode
# binding's count distinguishable from a per-pass convergence read in
# pg_stat_activity and in the listener tests.
BACKFILL_POPULATION_COUNT_SQL = BACKFILL_REMAINING_SQL.replace(
    "ghostreplay:backfill_remaining", "ghostreplay:backfill_population_count", 1
)

# --- repair phase ----------------------------------------------------------

REPAIR_CANDIDATES_DDL_PG = f"""/* ghostreplay:repair_candidates_ddl */
    CREATE TEMP TABLE IF NOT EXISTS {CANDIDATES_TABLE} (id uuid PRIMARY KEY)
"""

REPAIR_CANDIDATES_DDL_SQLITE = f"""/* ghostreplay:repair_candidates_ddl */
    CREATE TEMP TABLE IF NOT EXISTS {CANDIDATES_TABLE} (id text PRIMARY KEY)
"""

# DELETE FROM, not TRUNCATE. SQLite has no TRUNCATE, and the SQLite migration
# tests are the ones that seed an already-stamped broken-grid row and require the
# repair phase to null it — a TRUNCATE in the shared path would make the repair
# untestable on the only dialect the migration suite runs on by default. The cost
# is nil: at most N_repair bare keys in a session-local temp table.
REPAIR_CLEAR_SQL = f"""/* ghostreplay:repair_clear */
    DELETE FROM {CANDIDATES_TABLE}
"""

# The one set-wide detector scan per repair pass.
REPAIR_POPULATE_SQL = f"""/* ghostreplay:repair_populate */
    INSERT INTO {CANDIDATES_TABLE} (id)
    SELECT id
    FROM game_sessions
    WHERE {REPAIR_PREDICATE_SQL}
      AND id IN ({PLY_DETECTOR_SQL})
"""


# Same first-page / later-page split, for the same two reasons.
def _repair_select_sql(*, marker: str, cursor: str | None, locked: bool) -> str:
    where = f"\n    WHERE c.id > {cursor}" if cursor else ""
    tail = "\n    FOR NO KEY UPDATE OF g SKIP LOCKED" if locked else ""
    return (
        f"/* ghostreplay:{marker} */\n"
        "    SELECT g.id\n"
        f"    FROM {CANDIDATES_TABLE} c\n"
        "    JOIN game_sessions g ON g.id = c.id"
        f"{where}\n"
        "    ORDER BY c.id\n"
        f"    LIMIT :repair_batch_size{tail}\n"
    )


REPAIR_SELECT_FIRST_PG = _repair_select_sql(
    marker="repair_select_first", cursor=None, locked=False
)
REPAIR_SELECT_FIRST_SQLITE = REPAIR_SELECT_FIRST_PG  # binds no UUID: identical text
REPAIR_SELECT_PG = _repair_select_sql(
    marker="repair_select", cursor="CAST(:last_id AS uuid)", locked=False
)
REPAIR_SELECT_SQLITE = _repair_select_sql(marker="repair_select", cursor=":last_id", locked=False)
REPAIR_SELECT_FIRST_LOCKED_PG = _repair_select_sql(
    marker="repair_select_first_locked", cursor=None, locked=True
)
REPAIR_SELECT_LOCKED_PG = _repair_select_sql(
    marker="repair_select_locked", cursor="CAST(:last_id AS uuid)", locked=True
)

# The same lock the /moves writer takes (app/row_locks.py:11, session.py:1115).
# In per-batch mode the row is already locked from selection; this explicit lock
# is what makes ATOMIC mode safe, where selection is unlocked and the repair
# would otherwise run with no lock at all. SQLite is single-writer and has no
# counterpart — SQL_SQLITE.repair_lock is None rather than a neutered statement,
# so "skip the lock" is a structural fact of the bundle rather than a branch
# someone can forget to write.
REPAIR_LOCK_PG = """/* ghostreplay:repair_lock */
    SELECT id FROM game_sessions WHERE id = CAST(:sid AS uuid) FOR NO KEY UPDATE
"""

_REPAIR_UPDATE_TEMPLATE = f"""/* ghostreplay:repair_update */
    UPDATE game_sessions SET player_accuracy = NULL
    WHERE id = {{sid}}
      AND player_accuracy_algo_version = {ALGO_VERSION}
      AND player_accuracy IS NOT NULL
"""

REPAIR_UPDATE_PG = _REPAIR_UPDATE_TEMPLATE.format(sid="CAST(:sid AS uuid)")
REPAIR_UPDATE_SQLITE = _REPAIR_UPDATE_TEMPLATE.format(sid=":sid")

# A FRESH set-wide detector count, not a count over the materialized candidate
# table. Counting the temp table would be circular: it would report what the pass
# already knew and could never observe a row that broke DURING the pass.
# Freshness is what the convergence check is FOR, which is why this scan is
# priced rather than optimized away.
REPAIR_REMAINING_SQL = _remaining_sql(
    marker="repair_remaining",
    detector=f"""
      SELECT id FROM game_sessions
      WHERE {REPAIR_PREDICATE_SQL}
        AND id IN ({PLY_DETECTOR_SQL})""",
)

# The pre-flight REPAIR population count, issued during mode binding before any
# row is touched. Same SQL as the convergence scan under its own marker, for the
# same reason as the backfill's: it is one scan-bearing statement charged to the
# health window rather than to the writer stall, and pricing/identifying it
# separately keeps "the maximum of four" honest.
REPAIR_POPULATION_COUNT_SQL = REPAIR_REMAINING_SQL.replace(
    "ghostreplay:repair_remaining", "ghostreplay:repair_population_count", 1
)

# --- relation dimensions ---------------------------------------------------

# Four catalog lookups, O(1) each, costing nothing measurable. NEVER count(*):
# that would add a full relation scan to every execution purely to price another
# one. pg_total_relation_size is exact; pg_class.reltuples is an ESTIMATE and is
# -1 when the relation has never been analyzed, in which case the byte ratio
# stands alone and the fallback is logged.
DIMENSION_PROBE_SQL = """/* ghostreplay:dimension_probe */
    SELECT
      pg_total_relation_size('game_sessions') AS sessions_bytes,
      pg_total_relation_size('session_moves') AS moves_bytes,
      (SELECT reltuples FROM pg_class WHERE oid = 'game_sessions'::regclass) AS total_rows,
      (SELECT reltuples FROM pg_class WHERE oid = 'session_moves'::regclass) AS m_total
"""

# --- fail-closed assertions ------------------------------------------------

# Coverage: every ended-visible session was ATTEMPTED. Checks the VERSION, not
# the value, because a computed NULL is valid and must pass. Keys on status and
# mode only, so a malformed row with NULL ended_at is still covered.
COVERAGE_ASSERT_SQL = f"""/* ghostreplay:coverage_assert */
    SELECT count(*) FROM game_sessions
    WHERE {VISIBLE_ENDED_SQL}
      AND player_accuracy_algo_version IS DISTINCT FROM {ALGO_VERSION}
"""

# Ply-coordinate soundness: no served value was computed over a broken grid.
# This one MUST check the value, because a repaired row is still version 1 and
# the coverage assertion above passes whether or not the repair ran. It cannot be
# replaced by a count over the repair phase's materialized candidate set — that
# set is stale by construction, and catching a row that broke DURING the
# migration is the entire reason this assertion exists.
SOUNDNESS_ASSERT_SQL = f"""/* ghostreplay:soundness_assert */
    SELECT count(*) FROM game_sessions
    WHERE {REPAIR_PREDICATE_SQL}
      AND id IN ({PLY_DETECTOR_SQL})
"""


# ---------------------------------------------------------------------------
# One bundle, selected once.
# ---------------------------------------------------------------------------


class StatementBundle(NamedTuple):
    """Every statement the runner may execute, already resolved for one dialect.

    The runner NEVER reads a statement constant directly. ``upgrade()`` calls
    :func:`bundle_for` once and threads the result through every phase, so a
    statement cannot be picked from the wrong dialect: there is no code path that
    names a ``_PG`` constant at all outside ``SQL_PG``'s own construction.
    """

    dialect: str
    select_batch_first: str
    select_batch: str
    select_batch_first_locked: str | None
    select_batch_locked: str | None
    load_moves: str
    update_sql: str
    backfill_remaining: str
    backfill_population_count: str
    ply_detector_one: str
    repair_candidates_ddl: str
    repair_clear: str
    repair_populate: str
    repair_select_first: str
    repair_select: str
    repair_select_first_locked: str | None
    repair_select_locked: str | None
    repair_lock: str | None
    repair_update: str
    repair_remaining: str
    repair_population_count: str
    dimension_probe: str | None
    coverage_assert: str
    soundness_assert: str


#: Bundle fields that are legitimately ``None`` off PostgreSQL. Everything else
#: must be non-None in BOTH bundles — that is what stops a bundle from silently
#: omitting a statement the other has.
PG_ONLY_FIELDS = frozenset({
    "select_batch_first_locked",
    "select_batch_locked",
    "repair_select_first_locked",
    "repair_select_locked",
    "repair_lock",
    "dimension_probe",
})

#: Bundle fields whose text is identical on both dialects BECAUSE they bind no
#: UUID — asserted to be the SAME OBJECT in both bundles, so "identical" is
#: identity rather than two copies that can drift.
DIALECT_NEUTRAL_FIELDS = frozenset({
    "select_batch_first",
    "repair_select_first",
    "backfill_remaining",
    "backfill_population_count",
    "repair_clear",
    "repair_populate",
    "repair_remaining",
    "repair_population_count",
    "coverage_assert",
    "soundness_assert",
})

SQL_PG = StatementBundle(
    dialect="postgresql",
    select_batch_first=SELECT_BATCH_FIRST_PG,
    select_batch=SELECT_BATCH_PG,
    select_batch_first_locked=SELECT_BATCH_FIRST_LOCKED_PG,
    select_batch_locked=SELECT_BATCH_LOCKED_PG,
    load_moves=LOAD_MOVES_PG,
    update_sql=UPDATE_SQL_PG,
    backfill_remaining=BACKFILL_REMAINING_SQL,
    backfill_population_count=BACKFILL_POPULATION_COUNT_SQL,
    ply_detector_one=PLY_DETECTOR_ONE_PG,
    repair_candidates_ddl=REPAIR_CANDIDATES_DDL_PG,
    repair_clear=REPAIR_CLEAR_SQL,
    repair_populate=REPAIR_POPULATE_SQL,
    repair_select_first=REPAIR_SELECT_FIRST_PG,
    repair_select=REPAIR_SELECT_PG,
    repair_select_first_locked=REPAIR_SELECT_FIRST_LOCKED_PG,
    repair_select_locked=REPAIR_SELECT_LOCKED_PG,
    repair_lock=REPAIR_LOCK_PG,
    repair_update=REPAIR_UPDATE_PG,
    repair_remaining=REPAIR_REMAINING_SQL,
    repair_population_count=REPAIR_POPULATION_COUNT_SQL,
    dimension_probe=DIMENSION_PROBE_SQL,
    coverage_assert=COVERAGE_ASSERT_SQL,
    soundness_assert=SOUNDNESS_ASSERT_SQL,
)

SQL_SQLITE = StatementBundle(
    dialect="sqlite",
    select_batch_first=SELECT_BATCH_FIRST_SQLITE,
    select_batch=SELECT_BATCH_SQLITE,
    select_batch_first_locked=None,
    select_batch_locked=None,
    load_moves=LOAD_MOVES_SQLITE,
    update_sql=UPDATE_SQL_SQLITE,
    backfill_remaining=BACKFILL_REMAINING_SQL,
    backfill_population_count=BACKFILL_POPULATION_COUNT_SQL,
    ply_detector_one=PLY_DETECTOR_ONE_SQLITE,
    repair_candidates_ddl=REPAIR_CANDIDATES_DDL_SQLITE,
    repair_clear=REPAIR_CLEAR_SQL,
    repair_populate=REPAIR_POPULATE_SQL,
    repair_select_first=REPAIR_SELECT_FIRST_SQLITE,
    repair_select=REPAIR_SELECT_SQLITE,
    repair_select_first_locked=None,
    repair_select_locked=None,
    repair_lock=None,
    repair_update=REPAIR_UPDATE_SQLITE,
    repair_remaining=REPAIR_REMAINING_SQL,
    repair_population_count=REPAIR_POPULATION_COUNT_SQL,
    dimension_probe=None,
    coverage_assert=COVERAGE_ASSERT_SQL,
    soundness_assert=SOUNDNESS_ASSERT_SQL,
)


def bundle_for(dialect_name: str) -> StatementBundle:
    if dialect_name == "postgresql":
        return SQL_PG
    if dialect_name == "sqlite":
        return SQL_SQLITE
    raise MigrationError(f"20260719_01 supports postgresql and sqlite, not {dialect_name!r}")


# ---------------------------------------------------------------------------
# Environment. EXACTLY two variables, and neither can disable an admission
# guard. A test parses this module with `ast` and collects every os.environ /
# os.getenv name it reads; a third variable fails the suite. Measurement lives in
# a separate harness, never in a deployment code path.
# ---------------------------------------------------------------------------

ENV_MODE = "GHOSTREPLAY_ACCURACY_BACKFILL_MODE"
ENV_BATCH = "GHOSTREPLAY_ACCURACY_BACKFILL_BATCH"

VALID_MODES = ("atomic", "batch")

RUNBOOK = "docs/release_b_runbook.md"


def resolve_mode() -> str | None:
    """PARSE the execution mode: ``.strip()`` then an EXACT, case-sensitive match.

    Parsing happens on EVERY dialect; :func:`bind_mode` is what APPLIES the
    result per dialect and per population. Returns ``None`` when the variable is
    unset — "unset" is not a mode, and turning it into one here is exactly how a
    deployment error becomes a silent atomic run.

    Case is not folded on purpose — a deployment variable should be exactly what
    the runbook says, and silently normalizing ``ATOMIC`` hides a service-config
    value nobody actually reviewed. ``ATOMIC``, ``ATOMIC `` (strips to
    ``ATOMIC``), ``Batch``, a typo, and a blank-but-set string all raise, on
    every dialect, before any row is touched.
    """
    raw = os.environ.get(ENV_MODE)
    if raw is None:
        return None
    mode = raw.strip()
    if mode not in VALID_MODES:
        raise MigrationError(
            f"{ENV_MODE}={raw!r} is not one of {VALID_MODES} (exact, case-sensitive)"
        )
    return mode


def resolve_batch_size() -> int:
    """The backfill batch-size override, validated before any row is touched.

    An UNBOUNDED override would defeat the per-batch deadline by admitting a
    batch that cannot finish inside it, so the range is exactly
    ``1..MAX_BATCH_SIZE``. There is no repair-size override.
    """
    raw = os.environ.get(ENV_BATCH)
    if raw is None:
        return DEFAULT_BATCH_SIZE
    try:
        size = int(raw.strip())
    except ValueError as exc:
        raise MigrationError(f"{ENV_BATCH}={raw!r} is not an integer") from exc
    if not 1 <= size <= MAX_BATCH_SIZE:
        raise MigrationError(f"{ENV_BATCH}={raw!r} is outside 1..{MAX_BATCH_SIZE}")
    return size


# ---------------------------------------------------------------------------
# One clock, and the arming rule that makes it a bound rather than a hope.
# ---------------------------------------------------------------------------


class _RunClock:
    """The revision-wide monotonic budget, plus atomic mode's residual stall one.

    Created at the TOP of :func:`upgrade`, before ``VALIDATE`` and before the
    population counts, because three things a runner-scoped clock cannot cover
    are reachable: the repair population count runs during mode binding before
    any runner exists, both fail-closed assertions run after the runner returns,
    and a scan armed with a fresh ``SCAN_STMT_TIMEOUT_MS`` can start at second
    899 and finish past second 900.
    """

    def __init__(self, deadline_s: int = REVISION_DEADLINE_S) -> None:
        self.started = time.monotonic()
        self.deadline_s = deadline_s
        self.revision_deadline = self.started + deadline_s
        #: Set once, at ``t_stall_0`` — the instant immediately BEFORE atomic
        #: mode's first lock-bearing statement. ``None`` in per-batch mode and
        #: before the first row lock in atomic mode.
        self.atomic_deadline: float | None = None

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started

    def deadlines(self, batch_deadline: float | None = None) -> list[float]:
        """Every deadline in force right now, in no particular order."""
        live = [self.revision_deadline]
        if self.atomic_deadline is not None:
            live.append(self.atomic_deadline)
        if batch_deadline is not None:
            live.append(batch_deadline)
        return live

    def remaining_ms(self, batch_deadline: float | None = None) -> int:
        now = time.monotonic()
        return min(int((d - now) * 1000) for d in self.deadlines(batch_deadline))

    def check(self, phase: str, batch_deadline: float | None = None) -> None:
        """Rule 2: Python work is checked against the same clock, and raises."""
        if self.remaining_ms(batch_deadline) <= 0:
            raise _exhausted(self, phase)


def _exhausted(
    clock: _RunClock,
    phase: str,
    *,
    remaining: int | None = None,
    passes: int | None = None,
    max_passes: int = MAX_PASSES,
    sqlstate: str | None = None,
    first_remaining: list[str] | None = None,
) -> DeadlineExceeded:
    """The ONE frozen exhaustion template, with explicit ``n/a`` where a field
    cannot exist.

    ``phase`` names what was RUNNING when the budget ran out — four values, not
    two, because the deadline covers validation and the closing assertions too
    and an operator who reads ``phase=assert`` learns something a
    ``phase=repair`` template could not have told them. ``elapsed`` and
    ``deadline`` come from the monotonic clock and are ALWAYS present.

    The other three fields exist only when the failure is a BETWEEN-PASSES
    checkpoint with a fresh result set to report, and are ``n/a`` otherwise. That
    is not cosmetic:

    * they are read from the pass's OWN count-plus-sampled-ids convergence scan
      (see :func:`_remaining_sql`), never from a second diagnostic query, which
      would be another full detector scan; and
    * a mid-statement cancellation (57014 / 55P03) or a watchdog raise leaves the
      transaction ABORTED, so the same connection cannot run a diagnostic query
      at all until it rolls back — and re-scanning after the rollback is the
      extra scan the budget forbids. ``sqlstate`` carries the code instead
      (``n/a`` for the watchdog, which is a Python raise with no SQLSTATE).

    ``phase=validate`` and ``phase=assert`` are likewise inherently
    ``remaining=n/a passes=n/a``: validation and the closing assertions have no
    candidate population, no cursor and no pass count.
    """
    ids = ",".join(str(i) for i in first_remaining) if first_remaining else "n/a"
    return DeadlineExceeded(
        "20260719_01 accuracy backfill did not converge: "
        f"phase={phase} "
        f"remaining={'n/a' if remaining is None else remaining} "
        f"passes={'n/a' if passes is None else passes}/{max_passes} "
        f"elapsed={clock.elapsed_s():.1f}s "
        f"deadline={clock.deadline_s}s "
        f"sqlstate={sqlstate or 'n/a'} "
        f"first_remaining={ids}"
    )


#: The SQLSTATEs a breach of an ARMED timeout surfaces as. 57014 is
#: query_canceled (statement_timeout); 55P03 is lock_not_available
#: (lock_timeout).
CANCELLED_SQLSTATES = frozenset({"57014", "55P03"})


def _sqlstate_of(exc: BaseException) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


@contextlib.contextmanager
def _as_exhaustion(clock: _RunClock, phase: str):
    """Convert an armed-timeout cancellation or a watchdog raise into the template.

    Deliberately does NOT attempt any diagnostic query: the transaction is
    aborted by the time we get here, and re-scanning after a rollback would be
    the extra full detector scan the scan budget forbids. So
    ``remaining``/``passes``/``first_remaining`` stay ``n/a`` and ``sqlstate``
    carries the code.
    """
    try:
        yield
    except (DeadlineExceeded, ComputeWatchdogExceeded) as exc:
        if isinstance(exc, ComputeWatchdogExceeded):
            raise _exhausted(clock, phase) from exc
        raise
    except DBAPIError as exc:
        sqlstate = _sqlstate_of(exc)
        if sqlstate in CANCELLED_SQLSTATES:
            raise _exhausted(clock, phase, sqlstate=sqlstate) from exc
        raise


def _arm(
    conn,
    clock: _RunClock,
    *,
    stmt_cap_ms: int,
    lock_wait_ms: int,
    phase: str,
    batch_deadline: float | None = None,
) -> int:
    """Arm ``statement_timeout`` and ``lock_timeout`` to the LEAST of every budget
    the statement is spending from. Returns the armed ``statement_timeout``.

    ``statement_timeout`` RESTARTS for every statement, so a fixed
    ``statement_timeout = MAX_BATCH_MS`` bounds each statement individually and
    bounds the BATCH at nothing at all: the locking SELECT, the move load and the
    guarded UPDATE could each consume nearly the full allowance and the batch
    could hold row locks for roughly three times its budget. So the armed value
    is the remaining budget, re-derived immediately before each blocking
    statement, and ``SET LOCAL`` may be reissued inside the same transaction —
    each issue replaces the previous value, so the allowance narrows
    MONOTONICALLY as the unit of work spends it.

    ``lock_timeout`` is clamped to the SAME remaining, and that clamp is what
    makes a SUM of lock waits bounded rather than merely a single one.
    PostgreSQL applies ``lock_timeout`` separately to each lock acquisition, so
    on its own the cap permits any number of just-under-cap waits, each of which
    extends a hold already open over every row locked so far. Within a per-batch
    batch the sum cannot exceed the batch remainder; in atomic mode, which has
    ONE hold for the whole run, it cannot exceed the residual stall budget.
    https://www.postgresql.org/docs/current/runtime-config-client.html#GUC-STATEMENT-TIMEOUT

    DIALECT SCOPE. Both GUCs exist only on PostgreSQL, so the two ``set_config``
    calls are issued there and skipped on SQLite — which makes rule 1 an exact,
    PostgreSQL-only guarantee (a cancellation at the armed value) and leaves
    SQLite with the between-statement enforcement of rules 2 and 3. That is the
    right trade: per-batch mode is PostgreSQL-only, atomic mode's residual
    deadline protects a PostgreSQL row-lock hold, and SQLite is dev/CI-only,
    single-writer, and has no concurrent writer whose stall to bound. (A sqlite3
    progress handler could add mid-statement interruption; it is deliberately not
    adopted, because it would buy enforcement only on the one dialect with
    nothing to protect, at the cost of a dialect-specific callback the shared
    path would have to carry.)
    """
    remaining_ms = min(
        [stmt_cap_ms] + [
            int((d - time.monotonic()) * 1000) for d in clock.deadlines(batch_deadline)
        ]
    )
    if remaining_ms <= 0:
        raise _exhausted(clock, phase)
    if conn.dialect.name == "postgresql":
        conn.execute(
            text("SELECT set_config('statement_timeout', :v, true)").bindparams(
                v=f"{remaining_ms}ms"
            )
        )
        conn.execute(
            text("SELECT set_config('lock_timeout', :v, true)").bindparams(
                v=f"{min(lock_wait_ms, remaining_ms)}ms"
            )
        )
    return remaining_ms


# ---------------------------------------------------------------------------
# The compute watchdog.
# ---------------------------------------------------------------------------


class _ComputeWatchdog:
    """``signal.setitimer`` around EACH SESSION's compute, not once around the loop.

    ``statement_timeout`` cannot interrupt Python, so a deadline CHECK before
    each session bounds overshoot at one session's compute only if that session's
    compute takes about what sizing said it would. Nothing in a pre-loop check
    stops a pathological PGN, a GC pause or a noisy-neighbour CPU stall from
    running long.

    Armed to ``min(MAX_SINGLE_SESSION_COMPUTE_MS, batch remaining, revision
    remaining, atomic remaining)`` — the same minima :func:`_arm` draws on, plus
    the per-session ceiling. Two properties follow and both matter: because the
    batch remainder is one of the minima, NO SESSION'S COMPUTE CAN PUSH THE BATCH
    PAST ``MAX_BATCH_MS`` (which is what lets ``MAX_BATCH_MS`` be a batch-wide
    bound over SQL *and* Python, and lets the compute term drop out of
    ``EST_MAX_LOCK_HOLD_MS``); and because the per-session ceiling is also a
    minimum, one pathological session is caught before it eats the budget later
    sessions in the batch need.

    ITS LIMIT, STATED RATHER THAN BURIED: ``setitimer`` delivers only to the MAIN
    THREAD, and a handler runs only between bytecode instructions. In production
    the migration is the main thread of the Alembic process, so the watchdog
    arms. The PostgreSQL guard-lock tests drive Alembic in a worker thread, where
    it cannot; there the runner LOGS that the watchdog is unarmed and falls back
    to the per-session deadline check alone — which, off the main thread only,
    permits one session's compute to overshoot the batch deadline before the next
    check catches it. A C-level call that never yields to the interpreter would
    also defeat it. The watchdog narrows the gap; it does not close it.

    The previous SIGALRM handler and any pending ``setitimer`` are saved before
    the first arm and restored in ``__exit__``, so a raise mid-compute never
    leaves a stray timer armed for the next batch or the next test.
    """

    def __init__(self) -> None:
        self.armed = False
        self.last_armed_ms: int | None = None
        self._prev_handler: Any = None
        self._prev_timer: tuple[float, float] | None = None

    def __enter__(self) -> _ComputeWatchdog:
        if threading.current_thread() is not threading.main_thread():
            logger.info(
                "20260719_01: compute watchdog UNARMED (not the main thread); "
                "falling back to the per-session deadline check alone"
            )
            return self
        self._prev_handler = signal.signal(signal.SIGALRM, self._fire)
        # setitimer returns the PREVIOUS (value, interval) — which is how the
        # pending timer is captured and restored.
        self._prev_timer = signal.setitimer(signal.ITIMER_REAL, 0.0)
        self.armed = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self.armed:
            return False
        # Restoring the saved (value, interval) IS the disarm when nothing was
        # pending — the saved pair is (0.0, 0.0) in that case.
        signal.setitimer(signal.ITIMER_REAL, *(self._prev_timer or (0.0, 0.0)))
        signal.signal(signal.SIGALRM, self._prev_handler)
        self.armed = False
        return False

    @staticmethod
    def _fire(signum, frame) -> None:  # pragma: no cover - exercised via SIGALRM
        raise ComputeWatchdogExceeded(
            "20260719_01: single-session compute exceeded its armed ceiling "
            f"(<= {MAX_SINGLE_SESSION_COMPUTE_MS}ms and <= every deadline in force)"
        )

    def arm(self, remaining_ms: int) -> None:
        interval_ms = min(MAX_SINGLE_SESSION_COMPUTE_MS, remaining_ms)
        self.last_armed_ms = interval_ms
        if not self.armed:
            return
        # A non-positive interval would DISARM the timer, which is the opposite of
        # what a spent budget should do — so the deadline check that precedes this
        # is what raises, and here the floor is one millisecond.
        signal.setitimer(signal.ITIMER_REAL, max(interval_ms, 1) / 1000.0)

    def disarm(self) -> None:
        if self.armed:
            signal.setitimer(signal.ITIMER_REAL, 0.0)


# ---------------------------------------------------------------------------
# Relation dimensions, growth factors, and the admission arithmetic.
# ---------------------------------------------------------------------------


def _growth_factor(
    *, live_bytes: float, live_rows: float | None, sized_bytes: int, sized_rows: int, relation: str
) -> float:
    """``max(1.0, byte ratio, row ratio)`` for one relation.

    CLAMPED AT 1.0: a shrunk relation earns no discount, because the constants
    were measured at the sized dimensions and a smaller relation cannot make a
    frozen measurement smaller than it was measured to be.

    ``reltuples = -1`` means the relation has never been analyzed, so the row
    ratio is meaningless; the byte ratio then stands alone and the fallback is
    logged rather than silently taken.
    """
    ratios = [live_bytes / sized_bytes] if sized_bytes else []
    if live_rows is not None and live_rows >= 0 and sized_rows:
        ratios.append(live_rows / sized_rows)
    else:
        logger.info(
            "20260719_01: %s has no usable reltuples (%s); the byte ratio stands alone",
            relation,
            live_rows,
        )
    return max([1.0] + ratios)


def probe_growth(conn, bundle: StatementBundle, clock: _RunClock) -> tuple[float, float, dict]:
    """Derive ``G_moves`` and ``G_sessions`` from the LIVE relation dimensions.

    The populations are counted live; the relations the scans actually READ are
    not, unless something measures them. Every session Release A stamps correctly
    is version 1 and in NEITHER population, so a database can add a month of
    traffic — and a month of scan cost — while both counts stand still. These two
    factors are how the recheck sees that. Without them, "the runner rechecks the
    bound against the live populations" is a recheck of the two numbers that
    cannot have changed.

    Off PostgreSQL there is no catalog to probe and no live writer to protect, so
    both factors are 1.0.
    """
    if bundle.dimension_probe is None:
        return 1.0, 1.0, {}
    # Armed, therefore cancellable, therefore wrapped: an armed statement whose
    # cancellation is not converted raises a raw DBAPIError, and the operator reads
    # "psycopg2 QueryCanceled" instead of the phase, elapsed and deadline the frozen
    # template exists to give them. The probe reads only the catalog, so a 57014
    # here means the revision clock is already spent, not that the probe is slow —
    # which is precisely what phase=validate reports.
    #
    # THE ARM IS INSIDE THE WRAPPER, not merely the query. `_arm` issues two
    # `set_config` statements on PostgreSQL, and they run under whatever
    # `statement_timeout` the PREVIOUS arm left in force in this transaction — a
    # value that can legitimately be a single millisecond once the budget is nearly
    # spent, because `_arm` arms `min(cap, every remaining)`. So re-arming is itself
    # cancellable, and an arm outside the wrapper is the same raw-DBAPIError leak
    # one statement earlier. Every other `_arm` call site in this revision is inside
    # an `_as_exhaustion` block for this reason (`run_phase` wraps the whole phase).
    with _as_exhaustion(clock, "validate"):
        _arm(
            conn,
            clock,
            stmt_cap_ms=SCAN_STMT_TIMEOUT_MS,
            lock_wait_ms=ATOMIC_LOCK_WAIT_MS,
            phase="validate",
        )
        row = conn.execute(text(bundle.dimension_probe)).one()
    dims = {
        "sessions_bytes": int(row.sessions_bytes or 0),
        "moves_bytes": int(row.moves_bytes or 0),
        "total_rows": float(row.total_rows) if row.total_rows is not None else None,
        "m_total": float(row.m_total) if row.m_total is not None else None,
    }
    g_sessions = _growth_factor(
        live_bytes=dims["sessions_bytes"],
        live_rows=dims["total_rows"],
        sized_bytes=SIZED_SESSIONS_BYTES,
        sized_rows=SIZED_TOTAL_ROWS,
        relation="game_sessions",
    )
    g_moves = _growth_factor(
        live_bytes=dims["moves_bytes"],
        live_rows=dims["m_total"],
        sized_bytes=SIZED_MOVES_BYTES,
        sized_rows=SIZED_M_TOTAL,
        relation="session_moves",
    )
    return g_moves, g_sessions, dims


def atomic_teardown_reserve_ms(*, n_stale: int, n_repair: int) -> float:
    """The atomic transaction's teardown, scaled against the LIVE mutation count.

    A floor plus a slope, because atomic mode commits the entire population in
    ONE transaction and that commit is inside the stall by definition — the stall
    ends when it RETURNS. ``TEARDOWN_ALLOWANCE_MS`` cannot stand in for it: that
    constant is measured on batch transactions bounded at ``MAX_BATCH_SIZE``
    rows.

    Reserved rather than armed, because ``COMMIT`` and ``ROLLBACK`` are not
    covered by ``statement_timeout`` and run after the revision's last statement
    — the revision does not even execute them, ``env.py`` does. So they are
    subtracted from the budget up front and the WORK is held to the residual.

    The per-row term is in MICROSECONDS and is divided by 1000 exactly here.
    """
    return MARGINED_MS_ATOMIC_TEARDOWN_FIXED + (n_stale + n_repair) * (
        MARGINED_US_ATOMIC_TEARDOWN_PER_ROW / 1000
    )


def project_atomic_stall_ms(
    *, n_stale: int, n_repair: int, g_moves: float = 1.0, g_sessions: float = 1.0
) -> float:
    """The FULL projected atomic writer stall — mutation, scans and teardown alike.

    Every term is present because atomic mode holds every row lock — taken by the
    backfill's guarded UPDATE and by the repair's ``FOR NO KEY UPDATE`` alike —
    until the single Alembic transaction COMMITS, and everything that executes
    between the first of those locks and the return of that commit is inside the
    stall: the remaining batches, the repair's per-candidate work, the
    scan-bearing statements, both closing assertions, AND the commit itself.

    The SCAN terms are what make the projection honest when there is little or
    nothing to mutate. ``N_repair = 0`` zeroes the repair MUTATION term and
    NOTHING ELSE: the repair phase still materializes an empty candidate set and
    still counts zero remaining, and the soundness assertion still scans, all
    while the backfill's row locks are held. A formula without them scores such a
    run at ``N_stale * MARGINED_MS_PER_ROW`` and admits an atomic run whose real
    stall is that plus three full ``session_moves`` scans — the exact case where
    a clean audit lulls an operator into atomic mode on the largest possible move
    table.

    The two BACKFILL terms close the same leak on the OTHER relation: the
    backfill's keyset selection sweep and its convergence count both filter
    ``game_sessions`` on the unindexed version predicate, so both cost
    O(G_sessions) and not O(N_stale) — a correctly-stamped row RAISES their cost
    while leaving ``N_stale`` unchanged.

    The GROWTH FACTORS are what make the scan terms honest between sizing and
    deploy, for the reason given in :func:`probe_growth`.
    """
    return (
        n_stale * MARGINED_MS_PER_ROW
        + n_repair * MARGINED_MS_PER_REPAIR_ROW
        + ATOMIC_SCANS_UNDER_LOCK * MARGINED_MS_PER_SCAN_STMT * g_moves
        + MARGINED_MS_COVERAGE_ASSERT * g_sessions
        + BACKFILL_SELECT_SWEEPS_UNDER_LOCK * MARGINED_MS_BACKFILL_SELECT_SWEEP * g_sessions
        + BACKFILL_REMAINING_UNDER_LOCK * MARGINED_MS_BACKFILL_REMAINING * g_sessions
        + atomic_teardown_reserve_ms(n_stale=n_stale, n_repair=n_repair)
    )


class _ModeEnv(NamedTuple):
    """Everything that differs between the two execution envelopes.

    The deployment chooses which one RUNS; both ship, so the per-batch runner and
    its PostgreSQL suite stay permanently present and permanently gated instead
    of conditional on a sizing outcome.
    """

    name: str
    #: Per-batch transactions on an INDEPENDENT connection, with a MAX_BATCH_MS
    #: deadline and an after-the-fact lock-hold tripwire per batch.
    per_batch: bool
    #: FOR NO KEY UPDATE SKIP LOCKED at selection time.
    locked_selection: bool
    lock_wait_ms: int
    #: Per-phase pass bound.
    max_passes: int
    #: The own-cap for a NON-scan statement. In per-batch mode that is the batch
    #: deadline; in atomic mode there is no per-statement cap at all, so the cap
    #: is set where it can never bind and the deadlines in force do the work —
    #: which is what makes the armed value equal to the remaining budget.
    stmt_cap_ms: int


ATOMIC_ENV = _ModeEnv(
    name="atomic",
    per_batch=False,
    locked_selection=False,
    lock_wait_ms=ATOMIC_LOCK_WAIT_MS,
    max_passes=ATOMIC_MAX_PASSES,
    stmt_cap_ms=REVISION_DEADLINE_S * 1000,
)

BATCH_ENV = _ModeEnv(
    name="batch",
    per_batch=True,
    locked_selection=True,
    lock_wait_ms=BATCH_LOCK_WAIT_MS,
    max_passes=MAX_PASSES,
    stmt_cap_ms=MAX_BATCH_MS,
)


def bind_mode(
    dialect: str, *, n_stale: int, n_repair: int, g_moves: float, g_sessions: float
) -> _ModeEnv:
    """APPLY the parsed mode per dialect and per population.

    Every raise here happens BEFORE the first row is touched, so a misconfigured
    deploy fails the migration and leaves data untouched.

    1. Off PostgreSQL: unset means ``atomic``; an explicitly set ``atomic`` is
       accepted; ``batch`` raises as unsupported, because per-batch mode requires
       PostgreSQL row locks. SQLite has no concurrent live writer to stall, so no
       mode is REQUIRED there.
    2. On PostgreSQL with BOTH populations zero: no mode is required, so a fresh
       database, a disposable migration database and a dev SQLite file all
       upgrade with no configuration.
    3. On PostgreSQL with a nonzero population (either one): the variable is
       REQUIRED. Unset or empty raises, naming the accepted values and the
       runbook. There is NO default — an unset variable is a deployment error,
       never a silent atomic run.
    4. ``atomic`` on PostgreSQL: the full stall projection must fit
       ``MAX_WRITER_STALL_MS``, with the scan terms rescaled to the relations as
       they are NOW rather than as sizing found them, and the residual work
       budget must be positive.
    5. ``batch``: admitted by the zero-batch and scan-budget invariants; no stall
       projection is needed, because no batch holds a row lock across another
       batch and no set-wide scan holds one at all.
    """
    requested = resolve_mode()

    if dialect != "postgresql":
        if requested == "batch":
            raise MigrationError(
                f"{ENV_MODE}=batch is unsupported on {dialect}: per-batch mode requires "
                "PostgreSQL row locks (FOR NO KEY UPDATE SKIP LOCKED)"
            )
        return ATOMIC_ENV

    if requested is None:
        if n_stale == 0 and n_repair == 0:
            logger.info(
                "20260719_01: both populations are empty; no execution mode is required"
            )
            return ATOMIC_ENV
        raise MigrationError(
            f"{ENV_MODE} is required on postgresql when either population is nonzero "
            f"(n_stale={n_stale} n_repair={n_repair}); set it to one of {VALID_MODES} "
            f"— see {RUNBOOK}. There is no default: an unset variable is a deployment "
            "error, never a silent atomic run"
        )

    if requested == "batch":
        return BATCH_ENV

    projected = project_atomic_stall_ms(
        n_stale=n_stale, n_repair=n_repair, g_moves=g_moves, g_sessions=g_sessions
    )
    reserve = atomic_teardown_reserve_ms(n_stale=n_stale, n_repair=n_repair)
    work_budget = MAX_WRITER_STALL_MS - reserve
    if work_budget <= 0:
        raise MigrationError(
            f"{ENV_MODE}=atomic is inadmissible: the projected teardown reserve alone "
            f"({reserve:.0f}ms) leaves no residual work budget inside MAX_WRITER_STALL_MS "
            f"({MAX_WRITER_STALL_MS}ms) at n_stale={n_stale} n_repair={n_repair}; "
            f"run per-batch mode — see {RUNBOOK}"
        )
    if projected > MAX_WRITER_STALL_MS:
        raise MigrationError(
            f"{ENV_MODE}=atomic is inadmissible: projected writer stall {projected:.0f}ms "
            f"exceeds MAX_WRITER_STALL_MS ({MAX_WRITER_STALL_MS}ms) at "
            f"n_stale={n_stale} n_repair={n_repair} g_moves={g_moves:.3f} "
            f"g_sessions={g_sessions:.3f} (live relations against SIZED_MOVES_BYTES="
            f"{SIZED_MOVES_BYTES} SIZED_M_TOTAL={SIZED_M_TOTAL} SIZED_SESSIONS_BYTES="
            f"{SIZED_SESSIONS_BYTES} SIZED_TOTAL_ROWS={SIZED_TOTAL_ROWS}); "
            f"run per-batch mode — see {RUNBOOK}"
        )
    logger.info(
        "20260719_01: atomic admitted projected_stall_ms=%.0f teardown_reserve_ms=%.0f "
        "work_budget_ms=%.0f max_stall_ms=%d",
        projected,
        reserve,
        work_budget,
        MAX_WRITER_STALL_MS,
    )
    return ATOMIC_ENV


def assert_runtime_scan_budget(*, g_moves: float, g_sessions: float) -> None:
    """The scan-budget invariant again, over the LIVE relations, in BOTH modes.

    A literal check at import cannot see that the relations grew after sizing,
    and a ``game_sessions`` that has outgrown its sizing — even entirely via
    correctly-stamped version-1 rows that leave ``N_stale`` untouched — breaks the
    batch runner's clock exactly as an outgrown ``session_moves`` does. Batch mode
    has no stall projection to catch that, so this is the check that does.
    """
    budget = _scan_budget_ms(
        MARGINED_MS_PER_SCAN_STMT,
        MARGINED_MS_COVERAGE_ASSERT,
        g_moves=g_moves,
        g_sessions=g_sessions,
    )
    limit = REVISION_DEADLINE_S * 1000
    if budget >= limit:
        raise MigrationError(
            f"20260719_01 live scan budget {budget:.0f}ms does not fit REVISION_DEADLINE_S "
            f"({REVISION_DEADLINE_S}s) at g_moves={g_moves:.3f} g_sessions={g_sessions:.3f}; "
            f"the relations have outgrown their sizing — re-size before deploying "
            f"(see {RUNBOOK})"
        )


# ---------------------------------------------------------------------------
# Atomic mode's residual stall deadline, and the shipped stall probe.
# ---------------------------------------------------------------------------


class _AtomicStall:
    """Arms atomic mode's residual stall deadline at ``t_stall_0``, once.

    A PER-WAIT CAP DOES NOT BOUND A SUM OF WAITS. ``lock_timeout`` is applied
    separately to each lock acquisition, so a run whose every wait comes in at
    999 ms under a 1,000 ms cap never trips it even once — and each of those waits
    is added to a hold the migration is ALREADY keeping open over every row it has
    locked so far. Atomic mode issues one lock-bearing statement per backfill
    batch and one per repair candidate, so neither the count of acquisitions nor
    the sum they can hide is small. ``T_stall_proj`` contains no lock-wait term
    and could not usefully contain one: it would have to guess how contended
    production is at the moment of the deploy. So the sum is bounded directly.

    ``t_stall_0`` is taken BEFORE the statement, not when the lock is granted.
    The migration cannot observe the grant, and a deadline anchored there would
    exclude the wait for the FIRST lock — the one wait a first-lock anchor
    structurally cannot see. Taking the anchor one instant early over-measures
    the stall by that wait, which is conservative in the only direction that
    matters, and it is the same anchor the ``env.py`` stall probe records, so the
    projection, the deadline and the observation all measure the same interval.
    """

    def __init__(self, clock: _RunClock, *, n_stale: int, n_repair: int, projected_ms: float):
        self.clock = clock
        self.n_stale = n_stale
        self.n_repair = n_repair
        self.projected_ms = projected_ms
        self.armed_at: float | None = None
        self.work_budget_ms: float | None = None

    def arm(self) -> None:
        """Call immediately before EVERY lock-bearing statement; acts once."""
        if self.armed_at is not None:
            return
        t_stall_0 = time.monotonic()
        reserve = atomic_teardown_reserve_ms(n_stale=self.n_stale, n_repair=self.n_repair)
        self.work_budget_ms = MAX_WRITER_STALL_MS - reserve
        self.armed_at = t_stall_0
        self.clock.atomic_deadline = t_stall_0 + self.work_budget_ms / 1000.0
        # The SHIPPED singleton (app/migration_guard.py), not a new module: env.py
        # already imports it and already calls report() from the finally around
        # context.begin_transaction() — which runs precisely when COMMIT returns on
        # the success path and ROLLBACK returns on the failure path, i.e. the moment
        # the row locks are actually released. Recording on a fresh object while
        # env.py reported from that one would measure nothing.
        migration_stall_probe.record_first_row_lock(
            t_stall_0,
            max_stall_ms=MAX_WRITER_STALL_MS,
            projected_stall_ms=self.projected_ms,
        )
        logger.info(
            "20260719_01: atomic residual stall deadline armed work_budget_ms=%.0f "
            "teardown_reserve_ms=%.0f",
            self.work_budget_ms,
            reserve,
        )


def stall_for(
    bundle: StatementBundle,
    clock: _RunClock,
    *,
    env: "_ModeEnv",
    n_stale: int,
    n_repair: int,
    g_moves: float,
    g_sessions: float,
) -> _AtomicStall | None:
    """The residual stall budget, or ``None`` when there is no stall to bound.

    TWO conditions, not one. Per-batch mode is excluded because it has no single
    hold to bound — its holds are per batch, bounded by the batch deadline and
    proven after the fact by the lock-hold tripwire.

    PostgreSQL is required for the rest, and the dialect check is not cosmetic.
    SQLite takes no row lock this budget can shorten: its write lock is
    database-wide and held for the whole transaction no matter what the runner
    arms. And a SQLite upgrade has no concurrent writer to protect — it is a dev
    database or a test. Gating on ``env.per_batch`` alone would put every nonempty
    SQLite upgrade under a ~30-second deadline derived from PostgreSQL
    writer-stall measurements, able to fail an upgrade that was doing nothing
    wrong, and would report an ``observed_atomic_stall_ms`` for a hold that is not
    a row lock — a stall measurement of a writer that does not exist.
    """
    if env.per_batch or bundle.dialect != "postgresql":
        return None
    return _AtomicStall(
        clock,
        n_stale=n_stale,
        n_repair=n_repair,
        projected_ms=project_atomic_stall_ms(
            n_stale=n_stale, n_repair=n_repair, g_moves=g_moves, g_sessions=g_sessions
        ),
    )


# ---------------------------------------------------------------------------
# Phases.
# ---------------------------------------------------------------------------


def _validate_check(conn, clock: _RunClock) -> None:
    """Step 0: validate the Release-A ``NOT VALID`` CHECK.

    Validation takes SHARE UPDATE EXCLUSIVE on ``game_sessions`` and scans the
    whole table. That lock does not conflict with ROW EXCLUSIVE or ROW SHARE, so
    it does NOT block the /moves hook's writes or its FOR NO KEY UPDATE row
    locks; it blocks other DDL and autovacuum. SQLite skips this step because its
    CHECK was created validated.

    Armed against the REVISION deadline only — the atomic stall budget does not
    exist yet (no row lock has been taken) and there is no batch. A budget that
    expires here raises with ``phase=validate``, which is the proof that the clock
    started BEFORE validation rather than at runner start.
    """
    if conn.dialect.name != "postgresql":
        return
    started = time.monotonic()
    with _as_exhaustion(clock, "validate"):
        _arm(
            conn,
            clock,
            stmt_cap_ms=REVISION_DEADLINE_S * 1000,
            lock_wait_ms=VALIDATE_LOCK_WAIT_MS,
            phase="validate",
        )
        conn.execute(text(f"ALTER TABLE game_sessions VALIDATE CONSTRAINT {CHECK_NAME}"))
    # MANDATORY, and not decoration. set_config(..., true) is SET LOCAL: it lasts
    # until the end of the TRANSACTION, not the statement, and alembic/env.py
    # opens exactly one transaction around run_migrations(). Without this reset,
    # every subsequent blocking statement on this connection — the backfill's
    # guarded UPDATEs and the repair's FOR NO KEY UPDATE — would silently inherit
    # VALIDATE_LOCK_TIMEOUT, a value chosen for a DDL lock wait and never
    # reviewed as a row-lock wait. Resetting to 0 (wait forever) was the other
    # option and is the wrong one: an unbounded row-lock wait inside the atomic
    # transaction is precisely the writer-stall hazard, and it is unbounded while
    # we ourselves hold every row lock taken so far.
    conn.execute(
        text("SELECT set_config('lock_timeout', :v, true)").bindparams(
            v=f"{ATOMIC_LOCK_WAIT_MS}ms"
        )
    )
    logger.info("20260719_01: VALIDATE elapsed_ms=%d", int((time.monotonic() - started) * 1000))


def _scalar(conn, sql: str, **binds) -> int:
    """A single integer out of a single-value statement.

    Kept as a module-level helper rather than folded into the runner because the
    sizing harness replays the revision's OWN statements through it — a harness
    that re-implemented the read would be measuring its own code.
    """
    stmt = text(sql)
    if binds:
        stmt = stmt.bindparams(**binds)
    return int(conn.execute(stmt).scalar() or 0)


def remaining_scan(conn, sql: str) -> tuple[int, list[str]]:
    """Read a convergence scan's count AND its sampled ids out of ONE result set.

    ZERO RETURNED ROWS MEANS ZERO REMAINING — see :func:`_remaining_sql`. Public
    because the sizing harness reads the same statements the runner does.
    """
    rows = conn.execute(text(sql)).all()
    if not rows:
        return 0, []
    return int(rows[0].remaining), [str(r.id) for r in rows]


def _load_moves(conn, bundle: StatementBundle, ids: list) -> dict:
    """All of a batch's plies in ONE ordered query, grouped by session.

    Module-level for the same reason as :func:`_scalar`: the sizing harness loads
    moves through the revision's own bundle and loader, so the cost it measures is
    the cost the migration pays.
    """
    values = [str(i) for i in ids]
    if bundle.dialect == "postgresql":
        # ONE array bind, one plan shape — never an expanding IN, which would
        # reintroduce per-batch-size plan churn and the untyped-comparison hazard.
        stmt = text(bundle.load_moves).bindparams(ids=values)
    else:
        stmt = text(bundle.load_moves).bindparams(
            bindparam("ids", value=values, expanding=True)
        )
    grouped: dict = {}
    for row in conn.execute(stmt):
        grouped.setdefault(str(row.session_id), []).append(row)
    return grouped


def _accuracy_for(rows, player_color: str, pgn: str | None) -> int | None:
    """Validate the coordinate grid, THEN compute. Never the other way round.

    Mirrors ``app.accuracy.game_accuracy_for_rows``'s contract without importing
    it: this is the frozen path, and a guard that only wrapped the live surface
    would be skipped by exactly this code.
    """
    if not ply_coordinates_intact(rows):
        return None
    return compute_game_accuracy(
        # ply_color, not a local re-implementation: the row set the guard
        # validated and the move list the algorithm scores must read colour by
        # the SAME rule.
        [
            AccuracyMove(color=ply_color(row), eval_cp=row.eval_cp, eval_mate=row.eval_mate)
            for row in rows
        ],
        player_color=player_color,
        expected_total_moves=expected_total_moves_from_pgn(pgn),
    )


class _BatchState:
    """Bookkeeping for one unit of work, and for what it held while doing it."""

    __slots__ = ("deadline", "lock_started", "rows", "teardown_ms", "lock_hold_ms")

    def __init__(self, deadline: float | None) -> None:
        self.deadline = deadline
        #: Monotonic instant immediately before the LOCKING selection was issued —
        #: conservative by exactly the grant latency. ``None`` until a lock-bearing
        #: statement ran, and left ``None`` on an empty page (nothing was locked).
        self.lock_started: float | None = None
        self.rows: list = []
        self.teardown_ms: float = 0.0
        self.lock_hold_ms: float | None = None


class _Runner:
    """Both phases, under whichever transaction/deadline envelope the mode binds.

    In atomic mode ``self.conn`` IS the migration connection and there are no
    explicit transactions: Alembic owns the one transaction, batches bound memory
    and round trips but are not durability checkpoints, and a failure rolls back
    all batch stamps and all repairs. In per-batch mode ``self.conn`` is the
    runner's own independent connection and every batch runs in its own explicit
    transaction, so earlier committed batches stay durable across a failure and
    the rerun is idempotent.
    """

    def __init__(
        self,
        conn,
        bundle: StatementBundle,
        *,
        clock: _RunClock,
        env: _ModeEnv,
        batch_size: int,
        stall: _AtomicStall | None,
    ) -> None:
        self.conn = conn
        self.bundle = bundle
        self.clock = clock
        self.env = env
        self.batch_size = batch_size
        self.stall = stall

    # --- arming helpers ---------------------------------------------------

    def _arm_batch_stmt(self, state: _BatchState, phase: str) -> int:
        """A statement inside the unit of work: capped by the mode's own cap."""
        return _arm(
            self.conn,
            self.clock,
            stmt_cap_ms=self.env.stmt_cap_ms,
            lock_wait_ms=self.env.lock_wait_ms,
            phase=phase,
            batch_deadline=state.deadline,
        )

    def _arm_scan(self, phase: str) -> int:
        """A SCAN-BEARING statement: capped by SCAN_STMT_TIMEOUT_MS, never by a
        batch budget — these statements are not inside one."""
        return _arm(
            self.conn,
            self.clock,
            stmt_cap_ms=SCAN_STMT_TIMEOUT_MS,
            lock_wait_ms=self.env.lock_wait_ms,
            phase=phase,
        )

    # --- transaction envelopes -------------------------------------------

    @contextlib.contextmanager
    def _unit(self, phase: str):
        """One batch: its transaction (per-batch mode), its deadline, its clocks.

        On ANY breach — a Python deadline check, the compute watchdog's SIGALRM,
        or a statement PostgreSQL cancels at its armed timeout — the batch ROLLS
        BACK, releasing every row lock it holds and discarding that batch's work,
        then logs ERROR with the batch's row IDs and elapsed time and raises. The
        enforcement is PRE-COMMIT: nothing waits until after the lock was released
        to notice it was held too long.
        """
        started = time.monotonic()
        state = _BatchState(
            deadline=started + MAX_BATCH_MS / 1000.0 if self.env.per_batch else None
        )
        if not self.env.per_batch:
            yield state
            self._log_batch(phase, state, started)
            return
        trans = self.conn.begin()
        try:
            yield state
        except BaseException:
            # A failed ROLLBACK must not replace the exception that actually
            # failed the batch — the SQL cancellation or watchdog error is the
            # cause an operator needs, and a secondary teardown failure on an
            # already-broken connection tells them nothing.
            with contextlib.suppress(Exception):
                _timed_teardown(state, trans.rollback)
            self._finish_hold(state)
            self._log_batch(phase, state, started, level=logging.ERROR)
            # The original exception propagates; the lock-hold tripwire is NOT
            # armed on this path, because masking that cause would hide it.
            raise
        _timed_teardown(state, trans.commit)
        self._finish_hold(state)
        self._log_batch(phase, state, started)
        self._tripwire(phase, state)

    @contextlib.contextmanager
    def _own_transaction(self):
        """A statement that must run in its OWN closed transaction in per-batch mode.

        The per-pass materialization and each convergence read. In per-batch mode
        the materialization commits BEFORE the batch loop's first ``begin()``
        (that ordering is the contract a listener asserts) and a convergence read
        closes its transaction so it never lingers across a backoff sleep. In
        atomic mode there is nothing to open: the statement runs inside Alembic's
        single transaction, on the migration connection, with the backfill's row
        locks already held — which is exactly what ATOMIC_SCANS_UNDER_LOCK prices,
        and committing here would destroy the atomicity that is the mode's whole
        point.
        """
        if not self.env.per_batch:
            yield
            return
        with self.conn.begin():
            yield

    def _finish_hold(self, state: _BatchState) -> None:
        if state.lock_started is not None:
            state.lock_hold_ms = (time.monotonic() - state.lock_started) * 1000.0

    def _tripwire(self, phase: str, state: _BatchState) -> None:
        """The observed-lock-hold tripwire. PER-BATCH MODE ONLY, and NOT a bound.

        It fires after teardown, it cannot un-hold a lock, and it is not the
        deadline mechanism. What it buys is that the NEXT batch does not repeat
        the breach, that the migration fails instead of grinding through a
        thousand more over-budget batches, and that the breach becomes recorded
        evidence that the constants are wrong rather than an invisible production
        stall. That is the honest ceiling of what a Python-side observation can
        do — and it is only available because per-batch mode owns its
        transactions and so gets to watch each one end.

        Atomic mode gets NO such tripwire and the revision must not pretend to arm
        one: its hold ends when Alembic's transaction commits, after upgrade() has
        already returned, so there is no per-batch commit to observe and no code of
        the revision's still running when the locks are released.
        """
        if state.lock_hold_ms is None or state.lock_hold_ms <= EST_MAX_LOCK_HOLD_MS:
            return
        logger.error(
            "20260719_01: phase=%s batch EXCEEDED EST_MAX_LOCK_HOLD_MS "
            "lock_hold_ms=%.0f est_max_lock_hold_ms=%d teardown_ms=%.1f rows=%s",
            phase,
            state.lock_hold_ms,
            EST_MAX_LOCK_HOLD_MS,
            state.teardown_ms,
            ",".join(str(r) for r in state.rows),
        )
        raise MigrationError(
            f"20260719_01 phase={phase} observed lock hold {state.lock_hold_ms:.0f}ms exceeded "
            f"EST_MAX_LOCK_HOLD_MS ({EST_MAX_LOCK_HOLD_MS}ms) over {len(state.rows)} row(s); "
            f"the frozen constants no longer describe this database — re-size ({RUNBOOK})"
        )

    def _log_batch(
        self, phase: str, state: _BatchState, started: float, level: int = logging.INFO
    ) -> None:
        # rows=<n> is CONTRACT, not a diagnostic: the sizing qualification's
        # full-size and partial-batch pass condition is read from it.
        # teardown_ms IS a diagnostic — it is the rollback-only component and
        # cannot contain the interrupt latency in front of it, which is why the
        # cancellation probe measures cancel_to_unlock_ms from a second session
        # instead.
        logger.log(
            level,
            "20260719_01: phase=%s batch rows=%d elapsed_ms=%d lock_hold_ms=%s teardown_ms=%.1f",
            phase,
            len(state.rows),
            int((time.monotonic() - started) * 1000),
            "n/a" if state.lock_hold_ms is None else f"{state.lock_hold_ms:.0f}",
            state.teardown_ms,
        )

    # --- convergence ------------------------------------------------------

    def _remaining(self, sql: str, phase: str) -> tuple[int, list[str]]:
        """The count AND up to 20 sampled ids, from ONE scan, in its own txn."""
        with self._own_transaction():
            self._arm_scan(phase)
            return remaining_scan(self.conn, sql)

    # --- backfill ---------------------------------------------------------

    def _select_page(self, state: _BatchState, last_id: str | None) -> list:
        first = last_id is None
        if self.env.locked_selection:
            sql = (
                self.bundle.select_batch_first_locked if first else self.bundle.select_batch_locked
            )
        else:
            sql = self.bundle.select_batch_first if first else self.bundle.select_batch
        self._arm_batch_stmt(state, "backfill")
        stmt = text(sql).bindparams(batch_size=self.batch_size)
        if not first:
            stmt = stmt.bindparams(last_id=last_id)
        # THIS is where the lock hold starts in per-batch mode, and where the
        # observed-lock-hold clock starts with it. Taken before the statement, so
        # it is conservative by the grant latency.
        if self.env.locked_selection:
            state.lock_started = time.monotonic()
        page = self.conn.execute(stmt).all()
        if not page:
            state.lock_started = None  # nothing was selected, so nothing was locked
        return page

    def _load_moves(self, state: _BatchState, ids: list) -> dict:
        self._arm_batch_stmt(state, "backfill")
        return _load_moves(self.conn, self.bundle, ids)

    def _compute(self, state: _BatchState, page: list, grouped: dict, watchdog) -> list:
        """Score each session, checking the clock and arming the watchdog PER SESSION."""
        results = []
        for row in page:
            self.clock.check("backfill", state.deadline)
            watchdog.arm(self.clock.remaining_ms(state.deadline))
            try:
                acc = _accuracy_for(grouped.get(str(row.id), []), row.player_color, row.pgn)
            finally:
                watchdog.disarm()
            results.append((row.id, acc))
        return results

    def _apply(self, state: _BatchState, results: list) -> int:
        self.clock.check("backfill", state.deadline)
        # In atomic mode the guarded UPDATE is the FIRST lock-bearing statement of
        # the run, so the residual stall deadline is armed immediately before it —
        # and BEFORE _arm, so that this statement is itself armed against the
        # residual budget. Arming it first would hand the very statement that opens
        # the hold an allowance bounded only by the revision deadline, i.e. an
        # unbounded lock wait at the one moment the hold is guaranteed to be open.
        if self.stall is not None:
            self.stall.arm()
        # Each branch arms its OWN statements, because the two branches issue a
        # different NUMBER of them: one set-returning UPDATE on PostgreSQL, and up
        # to `batch_size` single-row UPDATEs on SQLite.
        if self.bundle.dialect == "postgresql":
            self._arm_batch_stmt(state, "backfill")
            admitted = (
                self.conn.execute(
                    text(self.bundle.update_sql).bindparams(
                        ids=[str(sid) for sid, _ in results],
                        accuracies=[acc for _, acc in results],
                    )
                )
                .scalars()
                .all()
            )
            stamped = len(admitted)
        else:
            # SQLite has no statement_timeout, so "best effort BETWEEN statements"
            # is the whole of its enforcement — and this branch issues up to
            # `batch_size` statements, not one. Arming once before the loop would
            # check the clock before the first update and then write the remaining
            # 999 unchecked, i.e. keep writing past the revision deadline for as
            # long as the loop takes. Re-arming per statement is what makes the
            # contract true here; on SQLite `_arm` is pure Python (the two
            # set_config calls are PostgreSQL-only), so the cost is a subtraction.
            stamped = 0
            for sid, acc in results:
                self._arm_batch_stmt(state, "backfill")
                res = self.conn.execute(
                    text(self.bundle.update_sql).bindparams(sid=str(sid), accuracy=acc)
                )
                stamped += 1 if res.rowcount else 0
        skipped = len(results) - stamped
        if skipped:
            # Expected, not an error: these are rows a live Release-A hook
            # stamped version 1 first. The guarded predicate's post-lock recheck
            # dropped them, so the migration cannot overwrite the fresher value.
            logger.info("20260719_01: backfill yielded %d row(s) to a live hook", skipped)
        return stamped

    def _backfill_pass(self, watchdog) -> tuple[int, int]:
        """One full keyset sweep of the stale population. Returns (selected, stamped)."""
        last_id: str | None = None
        selected = stamped = 0
        while True:
            self.clock.check("backfill")
            with self._unit("backfill") as state:
                page = self._select_page(state, last_id)
                if not page:
                    return selected, stamped
                state.rows = [r.id for r in page]
                selected += len(page)
                grouped = self._load_moves(state, state.rows)
                stamped += self._apply(state, self._compute(state, page, grouped, watchdog))
            # Advance to the maximum selected ID — the last row, since the
            # statement is ORDER BY id. Updated rows leave the stale predicate,
            # which is exactly why OFFSET would skip remaining rows.
            last_id = str(page[-1].id)

    # --- repair -----------------------------------------------------------

    def _materialize(self) -> None:
        """The one set-wide detector scan per repair pass, in its own transaction.

        NOT a batch: it runs before the loop, holds no row lock of its own, and is
        armed with SCAN_STMT_TIMEOUT_MS — never MAX_BATCH_MS. Embedding the
        detector in each batch's selection instead would pay a full session_moves
        scan PER BATCH, inside the batch's armed budget.
        """
        with self._own_transaction():
            self._arm_scan("repair")
            self.conn.execute(text(self.bundle.repair_candidates_ddl))
            self._arm_scan("repair")
            self.conn.execute(text(self.bundle.repair_clear))
            self._arm_scan("repair")
            self.conn.execute(text(self.bundle.repair_populate))

    def _repair_candidates(self, state: _BatchState, last_id: str | None) -> list:
        first = last_id is None
        if self.env.locked_selection:
            sql = (
                self.bundle.repair_select_first_locked
                if first
                else self.bundle.repair_select_locked
            )
        else:
            sql = self.bundle.repair_select_first if first else self.bundle.repair_select
        self._arm_batch_stmt(state, "repair")
        stmt = text(sql).bindparams(repair_batch_size=REPAIR_BATCH_SIZE)
        if not first:
            stmt = stmt.bindparams(last_id=last_id)
        if self.env.locked_selection:
            state.lock_started = time.monotonic()
        page = self.conn.execute(stmt).scalars().all()
        if not page:
            state.lock_started = None
        return page

    def _repair_one(self, state: _BatchState, sid) -> int:
        # 1. Lock. Not optional in atomic mode, where selection is unlocked — and
        #    in atomic mode this is the first lock-bearing statement of a run with
        #    nothing to back-fill, so the residual stall deadline is armed here.
        if self.bundle.repair_lock is not None:
            # Same ordering as the backfill's guarded UPDATE, and for the same
            # reason: on a run with nothing to back-fill THIS is the first
            # lock-bearing statement, so the residual deadline must exist before it
            # is armed or its lock wait is bounded by nothing but the revision clock.
            if self.stall is not None:
                self.stall.arm()
            self._arm_batch_stmt(state, "repair")
            if state.lock_started is None:
                state.lock_started = time.monotonic()
            self.conn.execute(text(self.bundle.repair_lock).bindparams(sid=str(sid)))
        # 2. Re-read in a FRESH statement with the SESSION-SCOPED detector. Under
        #    READ COMMITTED every statement takes its own snapshot, including
        #    inside atomic mode's single long transaction, so this sees the grid as
        #    of AFTER the lock was granted — including a /moves upload that just
        #    repaired it. Safety comes from HERE, never from the selection.
        self._arm_batch_stmt(state, "repair")
        broken = _scalar(self.conn, self.bundle.ply_detector_one, sid=str(sid))
        if not broken:
            return 0
        # 3. Act, only now.
        self._arm_batch_stmt(state, "repair")
        return self.conn.execute(
            text(self.bundle.repair_update).bindparams(sid=str(sid))
        ).rowcount

    def _repair_pass(self) -> tuple[int, int]:
        """Materialize once, then page the materialized set. Returns (selected, nulled).

        The materialized set is snapshot-stale, and that is FINE: it produces
        CANDIDATES only. A candidate the hook has since fixed is detected at the
        re-read and skipped; a row that BECAME broken after materialization is
        caught by this pass's fresh convergence count and ultimately by the
        soundness assertion.
        """
        self._materialize()
        last_id: str | None = None
        selected = nulled = 0
        while True:
            self.clock.check("repair")
            with self._unit("repair") as state:
                candidates = self._repair_candidates(state, last_id)
                if not candidates:
                    return selected, nulled
                state.rows = list(candidates)
                selected += len(candidates)
                for sid in candidates:
                    self.clock.check("repair", state.deadline)
                    nulled += self._repair_one(state, sid)
            last_id = str(candidates[-1])

    # --- the pass loop, shared by both phases -----------------------------

    def run_phase(self, phase: str, watchdog) -> None:
        """Passes, convergence by DIRECT REMAINING COUNT, backoff, exhaustion.

        SKIP LOCKED needs repeated passes, and the keyset cursor is reset after
        each one. A skipped row is not complete merely because the cursor passed
        it, so success requires the fresh remaining count to be ZERO — never a
        zero-row pass, because every remaining row might be transiently locked.
        """
        remaining_sql = (
            self.bundle.backfill_remaining if phase == "backfill" else self.bundle.repair_remaining
        )
        max_passes = self.env.max_passes
        remaining, sample = -1, []
        for k in range(1, max_passes + 1):
            self.clock.check(phase)
            pass_started = time.monotonic()
            with _as_exhaustion(self.clock, phase):
                if phase == "backfill":
                    selected, changed = self._backfill_pass(watchdog)
                else:
                    selected, changed = self._repair_pass()
                remaining, sample = self._remaining(remaining_sql, phase)
            logger.info(
                "20260719_01: phase=%s pass=%d selected=%d updated=%d remaining=%d elapsed_ms=%d",
                phase,
                k,
                selected,
                changed,
                remaining,
                int((time.monotonic() - pass_started) * 1000),
            )
            if remaining == 0:
                return
            if k == max_passes:
                break
            self._backoff(phase, k, remaining, sample, max_passes)
        raise _exhausted(
            self.clock,
            phase,
            remaining=remaining,
            passes=max_passes,
            max_passes=max_passes,
            first_remaining=sample,
        )

    def _backoff(
        self, phase: str, k: int, remaining: int, sample: list[str], max_passes: int
    ) -> None:
        """0.5, 1, 2, 4, 5, 5, … seconds. No jitter; there is one runner.

        CLAMPED to the remaining revision budget, and a backoff that would sleep
        PAST the deadline raises instead of sleeping into it — a migration that
        naps through its own deadline reports the wrong cause.

        No transaction is open here: the pass's last one was committed or rolled
        back before we got here, so the runner holds no snapshot and no lock while
        it waits.
        """
        sleep_s = min(0.5 * 2 ** (k - 1), 5.0)
        remaining_s = self.clock.remaining_ms() / 1000.0
        if sleep_s >= remaining_s:
            raise _exhausted(
                self.clock,
                phase,
                remaining=remaining,
                passes=k,
                max_passes=max_passes,
                first_remaining=sample,
            )
        time.sleep(sleep_s)


def _timed_teardown(state: _BatchState, finish) -> None:
    """``teardown_ms``: from the moment PYTHON ISSUES commit/rollback to its return.

    A DIAGNOSTIC, and named for exactly what it measures. On the breach path this
    is the rollback ONLY: it does not include the cancellation's delivery,
    PostgreSQL's interrupt latency, or the statement's unwind, because all three
    have already happened by the time the driver raises and Python reaches here.
    That is why ``TEARDOWN_ALLOWANCE_MS`` is frozen from ``cancel_to_unlock_ms``
    measured from a SECOND SESSION instead — the writer-felt tail is wider than
    this by precisely the part this starts too late to see.
    """
    started = time.monotonic()
    try:
        finish()
    finally:
        # Recorded on the way out either way: the breach path's teardown is the
        # number the ERROR log line reports, and a raise here must not lose it.
        state.teardown_ms = (time.monotonic() - started) * 1000.0


def _assert_fail_closed(conn, bundle: StatementBundle, clock: _RunClock, env: _ModeEnv) -> None:
    """Both fail-closed assertions, on the MIGRATION connection, inside the clock.

    They run AFTER the runner has returned, which is one of the three holes a
    runner-scoped clock leaves — so they are armed with
    ``min(SCAN_STMT_TIMEOUT_MS, every deadline in force)`` and a budget that
    expires here raises with ``phase=assert`` rather than scanning past the
    deadline. In per-batch mode they hold no row lock; in atomic mode they hold
    every row lock the run took, which is what the projection's scan terms price.
    """
    with _as_exhaustion(clock, "assert"):
        clock.check("assert")
        _arm(
            conn,
            clock,
            stmt_cap_ms=SCAN_STMT_TIMEOUT_MS,
            lock_wait_ms=env.lock_wait_ms,
            phase="assert",
        )
        uncovered = int(conn.execute(text(bundle.coverage_assert)).scalar() or 0)
        if uncovered:
            raise MigrationError(
                f"20260719_01 phase=assert coverage: {uncovered} ended-visible session(s) are "
                f"not stamped version {ALGO_VERSION}; cache-only reads must not serve"
            )
        clock.check("assert")
        _arm(
            conn,
            clock,
            stmt_cap_ms=SCAN_STMT_TIMEOUT_MS,
            lock_wait_ms=env.lock_wait_ms,
            phase="assert",
        )
        unsound = int(conn.execute(text(bundle.soundness_assert)).scalar() or 0)
    if unsound:
        raise MigrationError(
            f"20260719_01 phase=assert soundness: {unsound} ended-visible session(s) carry a "
            "version-1 accuracy computed over a broken ply-coordinate grid; cache-only reads "
            "must not serve"
        )


def _count_population(conn, sql: str, clock: _RunClock, *, lock_wait_ms: int) -> int:
    """A pre-flight population count, armed against the revision deadline only.

    Both counts happen during mode binding, BEFORE any row is touched, so they are
    charged to the health window and not to the writer stall. The repair count is
    one scan-bearing ``session_moves`` statement and it is priced as such.
    """
    with _as_exhaustion(clock, "validate"):
        _arm(
            conn,
            clock,
            stmt_cap_ms=SCAN_STMT_TIMEOUT_MS,
            lock_wait_ms=lock_wait_ms,
            phase="validate",
        )
        count, _ = remaining_scan(conn, sql)
    return count


def _run_phases(
    conn,
    bundle: StatementBundle,
    *,
    clock: _RunClock,
    env: _ModeEnv,
    batch_size: int,
    stall: _AtomicStall | None,
) -> None:
    """Both phases under the mode's envelope, on the right connection.

    In per-batch mode the runner opens its OWN connection so its commits are not
    swallowed by Alembic's outer transaction. (The env.py guard lock still spans
    it: that lock is session-scoped on its own connection and is indifferent to
    the runner's commits.) The connection's transaction lifecycle is explicit,
    because labelling and the PID lookup EXECUTE SQL and therefore autobegin a
    transaction — so the setup transaction is COMMITTED before the batch loop
    opens its first one, and ``application_name`` is a SESSION GUC that survives
    that commit and every later batch boundary.
    """
    if not env.per_batch:
        with _ComputeWatchdog() as watchdog:
            runner = _Runner(
                conn, bundle, clock=clock, env=env, batch_size=batch_size, stall=stall
            )
            runner.run_phase("backfill", watchdog)
            runner.run_phase("repair", watchdog)
        return

    with conn.engine.connect() as runner_conn:
        # 1. Setup transaction: label the SESSION and log the backend PID, then
        #    COMMIT — an explicit close of the transaction those two statements
        #    autobegan. Without it, either conn.begin() would raise ("a transaction
        #    is already in progress") or the first batch would share a transaction
        #    with the setup, so a first-batch rollback would silently drop the
        #    application_name the operator is watching for.
        _label_connection(runner_conn, RUNNER_APP_NAME)
        _log_backend_pid(runner_conn, RUNNER_APP_NAME)
        runner_conn.commit()
        try:
            with _ComputeWatchdog() as watchdog:
                runner = _Runner(
                    runner_conn,
                    bundle,
                    clock=clock,
                    env=env,
                    batch_size=batch_size,
                    stall=None,  # per-batch mode has no single hold to bound
                )
                runner.run_phase("backfill", watchdog)
                runner.run_phase("repair", watchdog)
        finally:
            # Covers a raise mid-batch before its `with conn.begin()` context
            # unwound; the `with engine.connect()` handles the close.
            with contextlib.suppress(Exception):
                runner_conn.rollback()


def upgrade() -> None:
    # ONE clock, taken HERE — before VALIDATE and before the population counts.
    clock = _RunClock()
    conn = op.get_bind()
    bundle = bundle_for(conn.dialect.name)
    batch_size = resolve_batch_size()

    _validate_check(conn, clock)

    # --- mode binding ------------------------------------------------------
    n_stale = _count_population(
        conn, bundle.backfill_population_count, clock, lock_wait_ms=ATOMIC_LOCK_WAIT_MS
    )
    n_repair = _count_population(
        conn, bundle.repair_population_count, clock, lock_wait_ms=ATOMIC_LOCK_WAIT_MS
    )
    g_moves, g_sessions, dims = probe_growth(conn, bundle, clock)
    logger.info(
        "20260719_01: n_stale=%d n_repair=%d g_moves=%.3f g_sessions=%.3f live=%s "
        "sized=%s dialect=%s",
        n_stale,
        n_repair,
        g_moves,
        g_sessions,
        dims,
        {
            "sessions_bytes": SIZED_SESSIONS_BYTES,
            "moves_bytes": SIZED_MOVES_BYTES,
            "total_rows": SIZED_TOTAL_ROWS,
            "m_total": SIZED_M_TOTAL,
        },
        bundle.dialect,
    )
    # The runtime scan-budget recheck, in BOTH modes, before the first row lock.
    assert_runtime_scan_budget(g_moves=g_moves, g_sessions=g_sessions)
    env = bind_mode(
        bundle.dialect,
        n_stale=n_stale,
        n_repair=n_repair,
        g_moves=g_moves,
        g_sessions=g_sessions,
    )
    logger.info(
        "20260719_01: mode=%s batch=%d dialect=%s", env.name, batch_size, bundle.dialect
    )

    if n_stale == 0 and n_repair == 0:
        # Nothing to do, so the runner does not run — but VALIDATE already ran and
        # BOTH fail-closed assertions still do, including the soundness
        # assertion's set-wide scan. They cost what they cost, but they take no
        # row lock, so they stall no writer and land in the health window only.
        # This path executes TWO scan-bearing session_moves statements, not four:
        # the pre-flight repair count above and the soundness assertion. The
        # materialization and the convergence counts belong to the runner, and the
        # runner did not run. No row lock is ever taken, so nothing is under lock
        # and the atomic stall is not merely small but structurally absent.
        logger.info("20260719_01: both populations empty; skipping the runner")
        _assert_fail_closed(conn, bundle, clock, env)
        logger.info(
            "20260719_01: complete mode=%s runner=skipped elapsed_s=%.1f",
            env.name,
            clock.elapsed_s(),
        )
        return

    stall = stall_for(
        bundle,
        clock,
        env=env,
        n_stale=n_stale,
        n_repair=n_repair,
        g_moves=g_moves,
        g_sessions=g_sessions,
    )
    _run_phases(conn, bundle, clock=clock, env=env, batch_size=batch_size, stall=stall)
    _assert_fail_closed(conn, bundle, clock, env)
    logger.info(
        "20260719_01: complete mode=%s n_stale=%d n_repair=%d elapsed_s=%.1f",
        env.name,
        n_stale,
        n_repair,
        clock.elapsed_s(),
    )


def downgrade() -> None:
    """Explicit no-op. Production rollback is a forward revert, not data reversal."""
