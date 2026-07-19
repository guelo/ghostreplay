"""Backfill + repair cached session accuracy, fail closed (Release B, g-b-backfill-core).

This revision is the **correctness state machine** of Release B. It runs three
phases in order — ``validate``, ``backfill``, ``repair`` — and then two
fail-closed assertions that must both pass before any cache-only read is allowed
to serve.

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

Scope boundary
--------------
This revision owns CORRECTNESS and IDEMPOTENCE. The production runtime envelope —
exact mode binding, per-batch transactions on an independent connection, the
shared revision deadline, residual stall budgets, ``statement_timeout`` /
``lock_timeout`` arming, the compute watchdog, and the retry contract — is
layered by **g-b-runtime-envelope** on top of the statement bundle and phase
functions defined here. Until then both modes execute on the migration connection
inside Alembic's single transaction (atomic semantics), which is why
``REPAIR_MAX_PASSES`` is 1: see "Retry and convergence" below.

Deviations from the plan, stated rather than hidden
---------------------------------------------------
- The ``*_locked`` selection statements are bundle FIELDS that are ``None`` on
  SQLite, alongside ``repair_lock``. The plan named only ``repair_lock`` as
  nullable, but the "runner never reads a statement constant directly" rule
  applies to the per-batch runner too, so its statements must be reachable
  through the bundle.

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

import logging
import os
from typing import NamedTuple

from alembic import op
from sqlalchemy import bindparam, text

# FROZEN imports. Never `app.accuracy` — see the module docstring.
from app.accuracy_rows_v1 import ply_color, ply_coordinates_intact
from app.accuracy_v1 import (
    AccuracyMove,
    compute_game_accuracy,
    expected_total_moves_from_pgn,
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
VALIDATE_LOCK_TIMEOUT = "10s"

DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 5000
REPAIR_BATCH_SIZE = 200

# A pass drains everything its selection can see; a nonzero remaining count means
# a concurrent writer produced new work. Bounded so a pathological writer cannot
# spin the migration forever.
BACKFILL_MAX_PASSES = 3
# Atomic selection takes no SKIP LOCKED, so no repair candidate is ever
# transiently skipped. A nonzero remaining count after pass 1 therefore does not
# mean "retry" — it means a writer is producing broken grids WITH a non-NULL
# accuracy while the guard is supposedly live, which is the live-bug case. Raise
# rather than back off into passes no stall projection ever admitted.
REPAIR_MAX_PASSES = 1

CANDIDATES_TABLE = "_accuracy_repair_candidates"


class MigrationError(RuntimeError):
    """Raised when a phase cannot converge or a fail-closed assertion fails."""


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


# ---------------------------------------------------------------------------
# Statements. Every statement's FIRST TOKEN is a distinct marker comment.
#
# A SQL comment changes no plan and no semantics, and it makes the statement
# identifiable FROM ANOTHER SESSION: pg_stat_activity.query reports the text a
# backend is currently executing, so a marker turns "which statement is this
# backend running" from a guess about whitespace into an exact match. The
# runtime envelope's cancellation probe and listener tests depend on that.
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
# which is what the runtime envelope's armed batch deadline needs — a driver
# executemany may expand into several server statements and a single armed
# timeout would then bound none of them.
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

BACKFILL_REMAINING_SQL = f"""/* ghostreplay:backfill_remaining */
    SELECT count(*) FROM game_sessions
    WHERE {POPULATION_PREDICATE_SQL}
"""

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
REPAIR_REMAINING_SQL = f"""/* ghostreplay:repair_remaining */
    SELECT count(*) FROM game_sessions
    WHERE {REPAIR_PREDICATE_SQL}
      AND id IN ({PLY_DETECTOR_SQL})
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
})

#: Bundle fields whose text is identical on both dialects BECAUSE they bind no
#: UUID — asserted to be the SAME OBJECT in both bundles, so "identical" is
#: identity rather than two copies that can drift.
DIALECT_NEUTRAL_FIELDS = frozenset({
    "select_batch_first",
    "repair_select_first",
    "backfill_remaining",
    "repair_clear",
    "repair_populate",
    "repair_remaining",
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


def resolve_mode() -> str:
    """Parse the execution mode: ``.strip()`` then an EXACT, case-sensitive match.

    Case is not folded on purpose — a deployment variable should be exactly what
    the runbook says, and silently normalizing ``ATOMIC`` hides a service-config
    value nobody actually reviewed. ``ATOMIC``, ``Batch``, a typo, and a
    blank-but-set string all raise, on every dialect, before any row is touched.

    g-b-runtime-envelope binds this to distinct transaction/deadline envelopes.
    In this revision both modes execute on the migration connection inside
    Alembic's transaction.
    """
    raw = os.environ.get(ENV_MODE)
    if raw is None:
        return "atomic"
    mode = raw.strip()
    if mode not in VALID_MODES:
        raise MigrationError(
            f"{ENV_MODE}={raw!r} is not one of {VALID_MODES} (exact, case-sensitive)"
        )
    return mode


def resolve_batch_size() -> int:
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
# Phases.
# ---------------------------------------------------------------------------


def _validate_check(conn) -> None:
    """Step 0: validate the Release-A ``NOT VALID`` CHECK.

    Validation takes SHARE UPDATE EXCLUSIVE on ``game_sessions`` and scans the
    whole table. That lock does not conflict with ROW EXCLUSIVE or ROW SHARE, so
    it does NOT block the /moves hook's writes or its FOR NO KEY UPDATE row
    locks; it blocks other DDL and autovacuum. SQLite skips this step because its
    CHECK was created validated.
    """
    if conn.dialect.name != "postgresql":
        return
    conn.execute(
        text("SELECT set_config('lock_timeout', :v, true)").bindparams(v=VALIDATE_LOCK_TIMEOUT)
    )
    conn.execute(text(f"ALTER TABLE game_sessions VALIDATE CONSTRAINT {CHECK_NAME}"))
    # MANDATORY, and not decoration. set_config(..., true) is SET LOCAL: it lasts
    # until the end of the TRANSACTION, not the statement, and alembic/env.py:50
    # opens exactly one transaction around run_migrations(). Without this reset,
    # every subsequent blocking statement on this connection — the backfill's
    # guarded UPDATEs and the repair's FOR NO KEY UPDATE — would silently inherit
    # VALIDATE_LOCK_TIMEOUT, a value chosen for a DDL lock wait and never
    # reviewed as a row-lock wait. Resetting to 0 (wait forever) was the other
    # option and is the wrong one: an unbounded row-lock wait inside the atomic
    # transaction is precisely the writer-stall hazard, and it is unbounded while
    # we ourselves hold every row lock taken so far. g-b-runtime-envelope
    # replaces this literal with its named ATOMIC_LOCK_WAIT_MS constant.
    conn.execute(text("SELECT set_config('lock_timeout', :v, true)").bindparams(v="1000ms"))


def _scalar(conn, sql: str, **binds) -> int:
    stmt = text(sql)
    if binds:
        stmt = stmt.bindparams(**binds)
    return int(conn.execute(stmt).scalar() or 0)


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


def _load_moves(conn, bundle: StatementBundle, ids: list) -> dict:
    """All of the batch's plies in ONE ordered query, grouped by session."""
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


def _backfill_pass(conn, bundle: StatementBundle, batch_size: int) -> int:
    """One full keyset sweep of the stale population. Returns rows stamped."""
    last_id: str | None = None
    stamped = 0
    while True:
        # No cursor yet -> the first-page statement, which has no `id >` clause
        # at all. A sentinel "minimum ID" would silently skip a schema-valid
        # session whose ID happens to be the nil UUID.
        if last_id is None:
            stmt = text(bundle.select_batch_first).bindparams(batch_size=batch_size)
        else:
            stmt = text(bundle.select_batch).bindparams(
                last_id=last_id, batch_size=batch_size
            )
        batch = conn.execute(stmt).all()
        if not batch:
            return stamped
        ids = [r.id for r in batch]
        grouped = _load_moves(conn, bundle, ids)
        results = [
            (r.id, _accuracy_for(grouped.get(str(r.id), []), r.player_color, r.pgn))
            for r in batch
        ]

        if bundle.dialect == "postgresql":
            admitted = conn.execute(
                text(bundle.update_sql).bindparams(
                    ids=[str(sid) for sid, _ in results],
                    accuracies=[acc for _, acc in results],
                )
            ).scalars().all()
            stamped += len(admitted)
            skipped = len(results) - len(admitted)
        else:
            skipped = 0
            for sid, acc in results:
                res = conn.execute(
                    text(bundle.update_sql).bindparams(sid=str(sid), accuracy=acc)
                )
                if res.rowcount:
                    stamped += 1
                else:
                    skipped += 1
        if skipped:
            # Expected, not an error: these are rows a live Release-A hook
            # stamped version 1 first. The guarded predicate's post-lock recheck
            # dropped them, so the migration cannot overwrite the fresher value.
            logger.info("20260719_01: backfill yielded %d row(s) to a live hook", skipped)

        # Advance to the maximum selected ID — the last row, since the statement
        # is ORDER BY id. Updated rows leave the stale predicate, which is
        # exactly why OFFSET would skip remaining rows.
        last_id = str(batch[-1].id)


def _run_backfill(conn, bundle: StatementBundle, batch_size: int) -> None:
    for attempt in range(1, BACKFILL_MAX_PASSES + 1):
        stamped = _backfill_pass(conn, bundle, batch_size)
        remaining = _scalar(conn, bundle.backfill_remaining)
        logger.info(
            "20260719_01: backfill pass %d stamped=%d remaining=%d", attempt, stamped, remaining
        )
        # Success requires ZERO remaining. A zero-row pass is never on its own
        # treated as success — the fresh count is the only convergence signal.
        if remaining == 0:
            return
    raise MigrationError(
        f"20260719_01 phase=backfill failed to converge after {BACKFILL_MAX_PASSES} pass(es); "
        f"{remaining} ended-visible session(s) remain unstamped"
    )


def _repair_pass(conn, bundle: StatementBundle) -> int:
    """Materialize once, then page the materialized set. Returns rows nulled.

    The materialized set is snapshot-stale, and that is FINE: it produces
    CANDIDATES only. Safety comes from the per-session re-read, never from the
    selection. A candidate the hook has since fixed is detected at the re-read
    and skipped; a row that BECAME broken after materialization is caught by this
    pass's fresh convergence count and ultimately by the soundness assertion.
    """
    conn.execute(text(bundle.repair_candidates_ddl))
    conn.execute(text(bundle.repair_clear))
    conn.execute(text(bundle.repair_populate))

    last_id: str | None = None
    nulled = 0
    while True:
        if last_id is None:
            stmt = text(bundle.repair_select_first).bindparams(
                repair_batch_size=REPAIR_BATCH_SIZE
            )
        else:
            stmt = text(bundle.repair_select).bindparams(
                last_id=last_id, repair_batch_size=REPAIR_BATCH_SIZE
            )
        candidates = conn.execute(stmt).scalars().all()
        if not candidates:
            return nulled
        for sid in candidates:
            # 1. Lock. Not optional in atomic mode, where selection is unlocked.
            if bundle.repair_lock is not None:
                conn.execute(text(bundle.repair_lock).bindparams(sid=str(sid)))
            # 2. Re-read in a FRESH statement with the SESSION-SCOPED detector.
            #    Under READ COMMITTED every statement takes its own snapshot,
            #    including inside atomic mode's single long transaction, so this
            #    sees the grid as of AFTER the lock was granted — including a
            #    /moves upload that just repaired it.
            if _scalar(conn, bundle.ply_detector_one, sid=str(sid)) == 0:
                continue
            # 3. Act, only now.
            nulled += conn.execute(
                text(bundle.repair_update).bindparams(sid=str(sid))
            ).rowcount
        last_id = str(candidates[-1])


def _run_repair(conn, bundle: StatementBundle) -> None:
    """Convergence is guaranteed under a live guarded hook.

    A hook that writes a broken grid now stamps NULL, so ``player_accuracy IS
    NULL`` and the row is not a repair candidate: a candidate can only LEAVE the
    population, never enter it. If the remaining count fails to converge, a
    writer is producing non-mainline grids WITH a non-NULL accuracy, which means
    the guard is not actually live — a live bug, and the correct outcome is to
    raise rather than to serve.
    """
    for attempt in range(1, REPAIR_MAX_PASSES + 1):
        nulled = _repair_pass(conn, bundle)
        remaining = _scalar(conn, bundle.repair_remaining)
        logger.info(
            "20260719_01: repair pass %d nulled=%d remaining=%d", attempt, nulled, remaining
        )
        if remaining == 0:
            return
    raise MigrationError(
        f"20260719_01 phase=repair failed to converge after {REPAIR_MAX_PASSES} pass(es); "
        f"{remaining} ended-visible session(s) still serve a value computed over a "
        "broken ply-coordinate grid — the live guard is not effective"
    )


def _assert_fail_closed(conn, bundle: StatementBundle) -> None:
    uncovered = _scalar(conn, bundle.coverage_assert)
    if uncovered:
        raise MigrationError(
            f"20260719_01 phase=assert coverage: {uncovered} ended-visible session(s) are not "
            f"stamped version {ALGO_VERSION}; cache-only reads must not serve"
        )
    unsound = _scalar(conn, bundle.soundness_assert)
    if unsound:
        raise MigrationError(
            f"20260719_01 phase=assert soundness: {unsound} ended-visible session(s) carry a "
            "version-1 accuracy computed over a broken ply-coordinate grid; cache-only reads "
            "must not serve"
        )


def upgrade() -> None:
    conn = op.get_bind()
    bundle = bundle_for(conn.dialect.name)
    mode = resolve_mode()
    batch_size = resolve_batch_size()
    logger.info("20260719_01: mode=%s batch=%d dialect=%s", mode, batch_size, bundle.dialect)

    _validate_check(conn)
    _run_backfill(conn, bundle, batch_size)
    _run_repair(conn, bundle)
    _assert_fail_closed(conn, bundle)


def downgrade() -> None:
    """Explicit no-op. Production rollback is a forward revert, not data reversal."""
