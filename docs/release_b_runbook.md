# Release B runbook — sizing derivation and admission constants

Release B (epic `g-aeq8`, under `g-madh`) validates the Release-A `NOT VALID`
CHECK, backfills and repairs `game_sessions.player_accuracy`, and then switches
the stats/history reads onto the cache. This runbook is the **provenance record
for the admission constants** frozen into revision `20260719_01`: what was
measured, on what, with what method, and which bound won.

It is the companion to [`release_a_runbook.md`](./release_a_runbook.md) and to
the general [`migration-deploy-runbook.md`](./migration-deploy-runbook.md).

Owning beads:

- `g-b-size-derive` (**this document**) — the measurement harness, the Phase 1
  bootstrap measurement, the Phase 2 constant freeze, the timing model, and the
  execution-mode decision.
- `g-b-runtime-envelope` — consumes these constants to arm the SQL timeouts, the
  run-time growth factors, and the admission projection.
- `g-b-sizing-harness` — **Phase 3**: reruns the frozen shipped revision with its
  guards armed on fresh restores, verifies the constants against production, and
  records the health-window verdict. **Nothing merges to production on the
  strength of the Phase 1/2 run recorded here.**

---

## 0. Status of the numbers in this document

> **These constants are DERIVED BUT NOT PRODUCTION-QUALIFIED.**
>
> The Phase 1 measurement recorded below was taken against a **locally
> synthesized snapshot-shaped database**, not a production restore: no production
> snapshot, fork, or dump was available to the derivation. Every constant is a
> real measurement of the real shipped statements against a real PostgreSQL, and
> every derivation rule in the design was applied — but the **absolute values
> describe a 6,000-session / 357,000-move database on an Apple-silicon laptop
> with a local SSD**, and production is neither of those things.
>
> Concretely, the numbers most likely to move and the direction they will move:
>
> | Constant | Why it will change | Direction |
> |---|---|---|
> | `SIZED_*` (all four) | production's relations are larger | **up** |
> | `MARGINED_MS_PER_SCAN_STMT` | scales with the whole `session_moves` relation | **up** |
> | `MARGINED_MS_COVERAGE_ASSERT` | scales with the whole `game_sessions` relation | **up** |
> | `TEARDOWN_ALLOWANCE_MS` | a local SSD with no fsync contention is the best case for commit and for cancel-to-unlock | **up** |
> | `MARGINED_MS_ATOMIC_TEARDOWN_FIXED` / `_PER_ROW` | same | **up** |
> | `MARGINED_MS_PER_ROW` | CPU-bound PGN parse; laptop cores are fast | **up** |
> | `B_TESTED` / `R_TESTED` | bounded by what was exercised here | up **or** down |
>
> `g-b-sizing-harness` **must** re-derive against a production restore and
> replace this section. An adjusted number there is a refinement, not a
> reopening; a structural runtime change reopens `g-b-runtime-envelope`.

---

## 1. The harness

`backend/scripts/size_accuracy_backfill.py`. It is a standalone script, not a
mode of the revision: the shipped revision contains **no bypass**, because an
environment variable that disarms the atomic projection guard and the batch
deadline is production-reachable by definition — matching `current_database()`
only prevents *accidental* reuse against a differently named database, and the
production database name is knowable.

The harness imports the revision's SQL constants through
`ScriptDirectory.get_revision("20260719_01").module` and calls the revision's own
`_accuracy_for`, so the population it measures and the statements it times are
the ones that ship. `backend/test_release_b_sizing.py` asserts that import
identity object-by-object, so a statement that changes in the revision cannot
keep a stale twin alive in the harness.

```
cd backend && source .venv/bin/activate

# Phase 1a — the atomic shape at the FULL teardown point, on snapshot copy 1
python -m scripts.size_accuracy_backfill \
  --url "postgresql+psycopg://.../snapshot_copy_1" \
  --mode atomic --batch-size 1000 --repair-batch-size 3000 --scan-trials 3 \
  [--synthesize-stale] [--synthesize-repair K]   # K >= 1000, or the whole set \
  --confirm-mutates --out m_atomic.json

# Phase 1a' — the EMPTY teardown point, on its OWN FRESH copy. --synthesize-stamped
# empties both populations while leaving the CHECK unvalidated, so this run still
# executes VALIDATE and the scans and mutates nothing. It is NOT a second pass of
# the run above: that one has already validated the constraint, so its commit
# flushes no catalog change and the difference would charge VALIDATE's commit to
# the per-row slope.
python -m scripts.size_accuracy_backfill \
  --url "postgresql+psycopg://.../snapshot_copy_1b" \
  --mode atomic --batch-size 1000 --repair-batch-size 3000 --scan-trials 3 \
  --synthesize-stamped --confirm-mutates --out m_empty.json

# Phase 1b — the batch shape, ONE FRESH COPY PER CANDIDATE SIZE
python -m scripts.size_accuracy_backfill \
  --url "postgresql+psycopg://.../snapshot_copy_N" \
  --mode batch --batch-size N --repair-batch-size R --scan-trials 3 \
  [--synthesize-stale] [--synthesize-repair K] \
  --confirm-mutates --out m_batch_N.json

# Phase 1c — the breach path, at BOTH scopes
python -m scripts.size_accuracy_backfill --url ... --cancel-probe \
  --probe-scope batch  --batch-size <max(MAX_BATCH_SIZE, REPAIR_BATCH_SIZE)> \
  --trials 20 --confirm-mutates
python -m scripts.size_accuracy_backfill --url ... --cancel-probe \
  --probe-scope atomic --batch-size <whole population> --trials 20 --confirm-mutates

# Phase 2 — freeze. Pure arithmetic, no database.
python -m scripts.size_accuracy_backfill --derive \
  --measurement m_atomic.json --measurement m_empty.json \
  --measurement m_batch_*.json \
  --measurement m_cancel_batch.json --measurement m_cancel_atomic.json \
  [--production-dimensions prod.json] --out derived.json
```

Each batch candidate size needs its **own fresh copy**: the first run consumes
the population.

### What `--derive` refuses to do

Phase 2 fails closed rather than substituting a default, because every one of
these substitutions produces a plausible small integer that looks like a
measurement. It **raises** when:

- either atomic teardown point is missing, or the empty-point run did not itself
  execute `VALIDATE`;
- either cancel-probe scope is missing, or a probe landed fewer than 20 trials;
- the **batch** probe locked fewer rows than the largest admitted batch —
  `max(MAX_BATCH_SIZE, REPAIR_BATCH_SIZE)`, **not** `MAX_BATCH_SIZE` alone.
  `TEARDOWN_ALLOWANCE_MS` bounds one per-batch-mode batch of *either* phase, and
  `REPAIR_BATCH_SIZE` divides by a cheaper per-row cost so it can be the larger
  of the two (here 2,500 against 1,000);
- the **atomic** probe locked fewer rows than the atomic transaction mutated;
- the empty-point run mutated anything, or its restore had no
  `ck_game_sessions_player_accuracy` at all (a missing CHECK is a hard error, not
  a zero-duration `VALIDATE` — otherwise "this run validated" would be true of a
  run that validated nothing);
- the **snapshot** stale or repair population is empty — that is an
  unsynthesized measurement, not a zero branch, and the per-row constant would be
  fabricated. (A **production** population of zero is legitimate: the term drops
  out of Decision 1 while the constant stays declared, because the runtime guard
  multiplies it by the live count.)
- no batch candidate both executed and satisfied `3 × observed <= MAX_BATCH_MS`.

Multiple probes of one scope are combined by **maximum**, never first-wins.

### Measurement traps this harness hit, and how it closes them

These were found by running it, and each one silently produced *optimistic*
numbers. They are recorded because a later operator will meet them again.

1. **Stale planner statistics after synthesis.** Synthesizing a stale population
   rewrites the version column across the whole ended-visible set. Left with
   pre-synthesis statistics, PostgreSQL estimated the repair population at ~1 row
   and chose a nested loop that re-executed the whole set-wide detector once per
   candidate: `REPAIR_POPULATE_SQL` ran for **over 7 minutes** instead of 155 ms.
   That is the cost of a plan production will never choose. The harness now runs
   `ANALYZE` on both relations after any synthesis (`analyze_after_synthesis`).
2. **Candidate clustering in the id space.** Selecting synthetic repair
   candidates with `ORDER BY id LIMIT k` yields the *k lowest* session ids. The
   scan-bearing statements are `<outer> ... WHERE id IN (<detector>)`, which
   PostgreSQL can serve with a **merge join** — and a merge join stops as soon as
   the outer side is exhausted. With clustered candidates the join terminated
   ~5 % into `session_moves` and every scan measured **~9 ms instead of ~155 ms**,
   a 16x under-measurement that looks exactly like good news. Candidates are now
   drawn with `ORDER BY md5(id::text)`, which spreads them the way production's
   defects are spread.
3. **A pooled canceller cancelling itself.** The cancel probe drew a fresh
   connection per trial for `pg_cancel_backend`; SQLAlchemy's pool handed it the
   very backend it was about to cancel, so the probe cancelled its own statement.
   The canceller and prober are now long-lived, the canceller runs `AUTOCOMMIT`,
   and a trial whose target pid equals the canceller's own pid is **discarded**
   rather than recorded — a silent self-cancel would look like a fast unlock and
   bias `TEARDOWN_ALLOWANCE_MS` **down**.

Three more were found in code review of the harness itself, all of which biased
constants **downward** — the direction that admits atomic mode:

4. **The cancel landed before the batch was fully dirtied.** The controller slept
   for half of `--park-seconds` after a signal raised *before* the `UPDATE` was
   issued. On a production-sized batch the cancel can land mid-write; the trial
   still looks valid, because the preceding `SELECT … FOR NO KEY UPDATE` already
   locked every row and the prober still measures a real lock release — but the
   rollback covers a partially dirtied batch. The controller now **waits for
   proof of the park**: the `AFTER … FOR EACH STATEMENT` trigger fires only once
   every row is written, so it polls `pg_stat_activity` for
   `wait_event = 'PgSleep'` on the holder's backend and discards a trial that
   never parks. Measured effect: the atomic-scope maximum rose from 2.697 ms to
   **4.098 ms**, and the atomic/batch scopes now separate the way transaction
   size predicts.
5. **Repair-phase commits were absent from `TEARDOWN_ALLOWANCE_MS`.** Only
   `batch_commit` was read, so a slow repair commit hid behind a fast backfill
   commit. Both phases' commits are now in the maximum.
6. **`B_TESTED` / `R_TESTED` recorded the requested `LIMIT`, not the executed
   page.** The first Phase 1 run froze `R_TESTED = 301` against a population of
   300 — a size nothing ever ran. The harness now records the actual page
   cardinality per batch, derivation uses that, and a phase with no passing
   candidate is a hard failure rather than a fall-through to the formula.

A guard was added for a seventh: `scan_plan_inversion` is reported on every run
and is `true` when the bare `PLY_DETECTOR_SQL` diagnostic out-costs all four
complete statements. The design treats the bare detector as a *lower bound that
is never the priced number*; when the planner picks an early-terminating join
that claim is false, and the derivation takes the diagnostic instead rather than
freezing the smaller number. On the recorded run `scan_plan_inversion` is
**false** (complete statements 168.2–173.5 ms, bare detector 172.1 ms), which is
the design's stated relationship holding.

---

## 2. Phase 1 — measured

**Timed SHA:** `8e6a964` · **Date:** 2026-07-24 · **Server:** PostgreSQL 15.18
(Homebrew) on aarch64-apple-darwin25.4.0 · **Snapshot:** locally synthesized (see
§0), Alembic revision `20260718_01`, seeded with real parseable PGNs of 60 plies
each and a contiguous mainline ply-coordinate grid.

### Relation dimensions and populations

| Dimension | Snapshot value | Frozen as |
|---|---|---|
| `count(*) game_sessions` | 6,000 | `SIZED_TOTAL_ROWS` |
| `pg_total_relation_size('game_sessions')` | 10,010,624 | `SIZED_SESSIONS_BYTES` |
| `count(*) session_moves` (`M_total`) | 357,000 | `SIZED_M_TOTAL` |
| `pg_total_relation_size('session_moves')` | 93,241,344 | `SIZED_MOVES_BYTES` |

| Population | Value | Note |
|---|---|---|
| `N_stale` | 3,000 | **synthesized** — version nulled across the entire ended-visible set |
| `M_moves` (moves of the stale set) | 180,000 | 60.0 mean plies per stale session |
| `N_broken_audit` | 3,000 | gate condition 6, the writer-defect signal (wider) |
| `N_repair` | 3,000 | **synthesized** — `K = 3000`; the harness now *enforces* `K >= 1000` (or the whole eligible set when smaller), one ply deleted per session, then stamped version 1 with a non-`NULL` accuracy, candidates spread by `md5(id)` |
| `N_mut_snap` | 6,000 | `N_stale + N_repair` as actually mutated by the atomic run |

Both populations were synthesized because the seeded database had no stale rows
and no broken grids. `N_stale` synthesis nulls the version across the **entire**
ended-visible set, which reproduces the real PGN and move distribution — the same
rows, the same plies, the same parse cost. Synthesis runs **stale first, repair
second**: repair synthesis stamps its `K` rows version 1, taking them back out of
the stale population, and the two populations are disjoint by construction.

### Timings (snapshot)

| Measurement | Value |
|---|---|
| `VALIDATE CONSTRAINT` (full-point run / empty-point run) | 1.711 ms / 1.459 ms |
| Backfill total (select + load + compute + guarded update), 3,000 sessions | 4,051.4 ms |
| `per_row_snap` — backfill total ÷ `N_stale_snap` | 1.350 ms/session |
| `max_single_session_compute_ms` (n = 3,000) | 26.28 ms (median 1.084 ms) |
| `T_repair_per_candidate` — lock + session-scoped re-read + conditional update, **scans excluded** (n = 3,000) | median 0.358 ms (max 1.199 ms) |
| `T_atomic_teardown_empty` — `COMMIT` of an atomic run that mutated **nothing**, on its **own fresh restore**, `VALIDATE` executed | 0.646 ms |
| `T_atomic_teardown_full` — `COMMIT` at `N_mut_snap = 6,000` | 0.748 ms |
| `max_batch_commit_ms` — **both phases**, across all batch candidates | 1.408 ms |
| `max_batch_cancel_to_unlock_ms` — **batch scope**, 20 trials, 0 discarded, **2,500 rows locked** (the largest admitted batch, `REPAIR_BATCH_SIZE`) | **2.230 ms** |
| `rollback_only_teardown_ms` beside it (batch scope) | 0.182 ms |
| `max_atomic_cancel_to_unlock_ms` — **atomic scope**, 20 trials, 0 discarded, 6,000 rows locked | **4.098 ms** |
| `rollback_only_teardown_ms` beside it (atomic scope) | 0.449 ms |

`cancel_to_unlock_ms >= rollback_only_teardown_ms` at both scopes, by a factor of
~8-9. That is the point of measuring it from outside: the process-side clock
starts *after* PostgreSQL's interrupt latency and the statement unwind have
already elapsed, so `rollback_only_teardown_ms` is structurally incapable of
containing the tail a live writer actually waits through. It is recorded as the
narrower rollback-only metric and is **never** the frozen input.

### Scan-bearing statements, timed whole and standalone

Three trials each, at `N_repair = 3,000`. The cold (first) trial is reported
alongside the maximum and the median.

| Statement | Cold (ms) | Max (ms) | Median (ms) |
|---|---|---|---|
| `REPAIR_POPULATE_SQL` | 170.37 | **173.49** | 170.67 |
| `REPAIR_REMAINING_SQL` | 169.50 | 169.50 | 168.56 |
| `SOUNDNESS_ASSERT_SQL` | 165.46 | 168.23 | 166.35 |
| repair population count (pre-flight) | 167.86 | 168.78 | 167.86 |
| — bare `PLY_DETECTOR_SQL` *(diagnostic only, never priced)* | 172.12 | 172.12 | 169.45 |
| `COVERAGE_ASSERT_SQL` | 1.74 | **1.74** | 1.41 |

`T_scan_stmt_snap = 173.49 ms` — the **maximum across the four complete
statements**, not the bare detector. `T_coverage_assert_snap = 1.74 ms`, priced
by its own constant because it scans a different relation and scales by a
different ratio.

### Batch candidates

`B_tested` / `R_tested` are the largest sizes actually exercised whose observed
maximum single-batch duration satisfied `3 * observed <= MAX_BATCH_MS (5000)`.

The **demonstrated** size is the observed page cardinality, not the requested
`LIMIT`. Every candidate below reached the size it asked for.

| Backfill batch: requested | demonstrated | Observed max single batch | `3x` | Passes |
|---|---|---|---|---|
| 100 | 100 | 148.4 ms | 445 ms | ✅ |
| 250 | 250 | 365.8 ms | 1,098 ms | ✅ |
| 500 | 500 | 699.5 ms | 2,098 ms | ✅ |
| **1,000** | **1,000** | **1,445.2 ms** | **4,336 ms** | ✅ ← `B_tested` |
| 1,500 | 1,500 | 2,162.5 ms | 6,487 ms | ❌ |

| Repair batch: requested | demonstrated | Observed max single batch | `3x` | Passes |
|---|---|---|---|---|
| 200 | 200 | 88.4 ms | 265 ms | ✅ |
| 500 | 500 | 218.3 ms | 655 ms | ✅ |
| 1,000 | 1,000 | 486.9 ms | 1,461 ms | ✅ |
| 2,000 | 2,000 | 838.5 ms | 2,516 ms | ✅ |
| **3,000** | **3,000** | **1,313.9 ms** | **3,942 ms** | ✅ ← `R_tested` |

`R_tested = 3,000` still passes the 3x rule, so the repair batch is bounded by
the **formula** (`R_formula = 2,500`), not by the fixture. That is the state the
`min(formula, tested)` rule wants: the admitted maximum is the smaller of a
budget and a demonstration, and here the budget binds.

---

## 3. Phase 2 — frozen

Full restore assumed (`r_sessions = r_moves = 1.0`); no production dimensions
file was supplied, so `SIZED_*` are the snapshot's own dimensions. Margin is
**3x** throughout.

### Policy bounds (chosen, not measured)

| Constant | Value |
|---|---|
| `MAX_WRITER_STALL_MS` | 30,000 |
| `MAX_BATCH_MS` | 5,000 |
| `BATCH_LOCK_WAIT_MS` | 1,000 |
| `ATOMIC_LOCK_WAIT_MS` | 1,000 (`== BATCH_LOCK_WAIT_MS`) |
| `VALIDATE_LOCK_TIMEOUT` | `'10s'` |
| `REVISION_DEADLINE_S` | 900 |
| `MAX_PASSES` | 20 |
| `ATOMIC_SCANS_UNDER_LOCK` | 3 |

### Measured constants

| Constant | Value | Derivation |
|---|---|---|
| `SIZED_TOTAL_ROWS` | 6,000 | recorded dimension |
| `SIZED_SESSIONS_BYTES` | 10,010,624 | recorded dimension |
| `SIZED_M_TOTAL` | 357,000 | recorded dimension |
| `SIZED_MOVES_BYTES` | 93,241,344 | recorded dimension |
| `MARGINED_MS_PER_ROW` | 5 | `ceil(3 * 1.350 per-session * 1.0 plies-growth)` |
| `MARGINED_MS_PER_REPAIR_ROW` | 2 | `ceil(3 * 0.358)` |
| `MARGINED_MS_PER_SCAN_STMT` | 521 | `ceil(3 * 173.49)` |
| `MARGINED_MS_COVERAGE_ASSERT` | 6 | `ceil(3 * 1.74)` |
| `MARGINED_MS_BACKFILL_REMAINING` | 6 | **PROVISIONAL** — `ceil(3 * 1.74)`, from the recorded `COVERAGE_ASSERT_SQL` measurement (see below) |
| `MARGINED_MS_BACKFILL_SELECT_SWEEP` | 37 | **PROVISIONAL** — `ceil(3 * 7 pages * 1.74)`, same recorded measurement (see below) |
| `BACKFILL_SELECT_SWEEPS_UNDER_LOCK` | 1 | structural per-pass count (atomic backfill converges in one unlocked-selection pass) |
| `BACKFILL_REMAINING_UNDER_LOCK` | 1 | structural per-pass count (same argument) |
| `SCAN_STMT_TIMEOUT_MS` | 521 | `max(521, 6, 6)` — the maximum over **every** statement it is armed on: the four complete `session_moves` scans (which already include `REPAIR_REMAINING_SQL`), the coverage assertion, **and `BACKFILL_REMAINING_SQL`** via `MARGINED_MS_BACKFILL_REMAINING`. The two convergence scans are priced by *different* terms — the repair one by `MARGINED_MS_PER_SCAN_STMT`/`G_moves`, the backfill one by `MARGINED_MS_BACKFILL_REMAINING`/`G_sessions` — and only the latter needed adding. `MARGINED_MS_BACKFILL_SELECT_SWEEP` is deliberately absent: each page of the sweep is armed by the mode's batch cap, so the sweep constant prices a multi-statement unit no single armed value has to cover. |
| `MAX_SINGLE_SESSION_COMPUTE_MS` | 79 | `ceil(3 * 26.28)` |
| `TEARDOWN_ALLOWANCE_MS` | 7 | `ceil(3 * max(1.408 commit, 2.230 cancel-to-unlock))` — **cancel-to-unlock won** |
| `MARGINED_MS_ATOMIC_TEARDOWN_FIXED` | 2 | `ceil(3 * 0.646)` (empty-population point, own restore) |
| `MARGINED_US_ATOMIC_TEARDOWN_PER_ROW` | 2 | `ceil(3 * 1000 * (max(0.748, 4.098) - 0.646) / 6000)` — **microseconds** |
| `B_TESTED` | 1,000 | largest **demonstrated** backfill page |
| `R_TESTED` | 3,000 | largest **demonstrated** repair page |
| `MAX_BATCH_SIZE` | 1,000 | `min(B_formula = 1000, B_tested = 1000)` — **bound by formula** (tie) |
| `DEFAULT_BATCH_SIZE` | 1,000 | `== MAX_BATCH_SIZE` |
| `REPAIR_BATCH_SIZE` | 2,500 | `min(R_formula = 2500, R_tested = 3000)` — **bound by formula** |
| `EST_MAX_LOCK_HOLD_MS` | 5,007 | `MAX_BATCH_MS + TEARDOWN_ALLOWANCE_MS = 5000 + 7`, derived at import. **No `MAX_SINGLE_SESSION_COMPUTE_MS` addend**: the per-session compute watchdog is armed to `min(MAX_SINGLE_SESSION_COMPUTE_MS, batch remaining, revision remaining, atomic remaining)`, so no session's compute can pass the batch deadline and `MAX_BATCH_MS` is already batch-wide over SQL *and* Python. Adding the ceiling on top would double-count it; it survives only as that watchdog ceiling. |

`MARGINED_MS_PER_ROW` is derived **per row first, then multiplied by the
production population** — never by projecting a total and dividing by the
production count. The division form is undefined at `N_stale_prod = 0`, which is
a legitimate production state, and any fallback it falls through to fabricates
the constant. Per-row survives a production zero intact, which is exactly what
the design requires: the backfill *term* drops out of Decision 1 while the
*constant* stays declared, because the runtime guard multiplies it by the **live**
count.

### The two PROVISIONAL backfill terms

`MARGINED_MS_BACKFILL_SELECT_SWEEP` and `MARGINED_MS_BACKFILL_REMAINING` price the
backfill's **own** `game_sessions` work: its keyset selection sweep (all
`SELECT_BATCH_*` pages of one pass) and `BACKFILL_REMAINING_SQL`. Both filter
`game_sessions` on `player_accuracy_algo_version IS NULL OR < 1`, a predicate **no
index covers** (`app/models.py:188`, `app/models.py:224`), so each costs
`O(G_sessions)` and **not** `O(N_stale)` — every version-1 row Release A stamps
between sizing and deploy *grows* the scanned relation while *shrinking* the
population. Omitting them priced a growing relation at zero, which is why they
are now charged in both the scan budget and the atomic stall projection.

They are **PROVISIONAL** because the Phase 1 run above predates the discovery of
that leak and therefore never timed either statement directly. Their values are
derived from that same run's recorded `game_sessions` scan measurement
(`COVERAGE_ASSERT_SQL`, 1.74 ms max at the sized dimensions), which is the same
shape against the same relation:

- `MARGINED_MS_BACKFILL_REMAINING = ceil(3 * 1.74) = 6` — a count over
  `game_sessions` on an unindexed predicate, i.e. the coverage assertion's shape.
- `MARGINED_MS_BACKFILL_SELECT_SWEEP = ceil(3 * 7 * 1.74) = 37` — a sweep at the
  sized dimensions is `ceil(6000 / 1000) + 1 = 7` pages (the `+1` is the empty page
  that terminates it), and **each page is priced at a whole `game_sessions`
  scan**, the worst case for an unindexed filter. Deliberately conservative: a
  direct measurement can only lower it.

`scripts/size_accuracy_backfill.py` now measures both directly
(`backfill_remaining` and `backfill_select_sweep` in `time_scan_statements`, and
`--derive` emits both constants), so the next sizing run produces measured values.
`g-b-size-derive-backfill-terms` owns that re-measurement, and sizing
qualification may adjust the numbers and rerun this bead's gates — an adjusted
*number* is expected refinement, not a structural change.

`MARGINED_US_ATOMIC_TEARDOWN_PER_ROW` is denominated in **microseconds** on
purpose. The measured slope is 0.000575 ms/row; rounded up to an integer
millisecond it would be `1`, adding a phantom second of projected stall per
thousand rows and making atomic mode inadmissible on populations it comfortably
handles. `g-b-runtime-envelope`'s projection divides it by 1000, and a constant
test pins that division so a future "tidy-up" into milliseconds fails loudly.

The atomic teardown floor is measured at the **empty** point and the slope from
the **full** point; one measurement point cannot yield both, and each point is
**its own run against its own fresh restore**. That separation is load-bearing: a
second pass in the same process has already validated the constraint, so its
`COMMIT` flushes no catalog change, and subtracting it from a full point that
*did* validate pushes `VALIDATE`'s own commit cost into the per-row slope. Both
runs here recorded `validated_in_run: true`, and `--derive` refuses an
empty-point run that did not.

The empty point has no cancel-to-unlock counterpart and needs none — an atomic
run that mutated nothing holds no row lock, so there is no lock for a competing
writer to wait on and the commit is the whole of its teardown. The **full**
point's teardown takes the larger of its commit (0.748 ms) and the atomic-scope
cancel-to-unlock (4.098 ms), because an atomic run that breaches rolls back the
whole population.

### Invariants (checked at import, in the revision)

| Invariant | Value | Limit | Holds |
|---|---|---|---|
| Zero-batch, backfill: `MARGINED_MS_PER_ROW <= MAX_BATCH_MS` | 5 | 5,000 | ✅ |
| Zero-batch, repair: `MARGINED_MS_PER_REPAIR_ROW <= MAX_BATCH_MS` | 2 | 5,000 | ✅ |
| Scan budget: `(2*20 + 2) * 521 + 6 + 20 * (37 + 6)` | 22,748 ms | 900,000 ms | ✅ |
| `EST_MAX_LOCK_HOLD_MS <= MAX_WRITER_STALL_MS` | 5,007 | 30,000 | ✅ |
| `BATCH_LOCK_WAIT_MS < MAX_BATCH_MS` | 1,000 | 5,000 | ✅ |
| `SCAN_STMT_TIMEOUT_MS >= max(per_scan_stmt, coverage, backfill_remaining)` | 521 | 521 | ✅ |

`EST_MAX_LOCK_HOLD_MS <= MAX_WRITER_STALL_MS` is arithmetic over frozen
literals. It proves that the *estimate* fits the budget and **nothing more** — a
comparison of literals cannot enforce a production observation, and a green test
suite is not evidence that a row lock is never held for 30 seconds. What backs
the estimate at run time is `g-b-runtime-envelope`'s compute watchdog, its armed
SQL timeouts, and its observed-lock-hold tripwire.

---

## 4. Decision 1 — execution mode

The stall is **everything between the first row lock and the return of the
commit**. `VALIDATE` and the pre-flight population counts are outside it:
`VALIDATE`'s SHARE UPDATE EXCLUSIVE does not conflict with the row writes or the
`FOR NO KEY UPDATE` locks the `/moves` hook takes, and both complete before the
first row lock.

```
T_stall_prod = T_backfill_prod                                  4051.4 ms
             + T_repair_prod                                    1074.2 ms
             + ATOMIC_SCANS_UNDER_LOCK * T_scan_stmt_prod        520.5 ms   (3 x 173.49)
             + T_coverage_assert_prod                              1.7 ms
             + T_atomic_teardown_floor_prod                        0.6 ms
             + T_atomic_teardown_per_row_prod * (N_stale + N_repair)
                                                                   3.5 ms   (0.000575 x 6000)
             = 5651.9 ms

3 * T_stall_prod = 16,956 ms  <=  MAX_WRITER_STALL_MS = 30,000 ms
```

**Verdict on the recorded snapshot: `atomic`.**

```
GHOSTREPLAY_ACCURACY_BACKFILL_MODE=atomic
```

**This verdict is NOT executable against production yet.** It is the verdict for
a 6,000-session database on a laptop, and it clears the bound by 43 %. The
production verdict is `g-b-sizing-harness`'s to record, from a re-derivation
against a real restore and the real audit counts, and it must be set on the
Railway service before the deploy with the exact value written back here.

The three non-row terms are not rounding. On a clean audit with a small stale
set they are the **whole** stall — three full `session_moves` scans plus a
`game_sessions` scan, held across every row lock the backfill took — and a
population-scaled model scores exactly that run at nearly zero and admits atomic
mode for it. The teardown terms are what make the formula agree with its own
first sentence: the stall ends when `COMMIT` returns, so a model that stops at
the last assertion is measuring the stall minus its final, largest,
whole-population flush.

The revision's own admission guard (`g-b-runtime-envelope`) rechecks this bound
at run time against **both live populations and both live relation dimensions**,
so a verdict sized against a smaller population, a repair count that grew since
the audit, or relations that grew since sizing **fails rather than stalls**.
Migration placement and the health timeout do not relax the stall limit.

---

## 5. Outstanding for `g-b-sizing-harness` (Phase 3)

- [ ] Re-run Phase 1 against a **production snapshot / fork / restored dump**,
      full restore where possible, and re-derive every constant in §3.
- [ ] Record production's `N_stale`, `N_repair` and `N_broken_audit` **from the
      audit** — an audit that was never run blocks the deploy.
- [ ] Record production's four relation dimensions as `SIZED_*`; if the restore
      was partial, record the scaling applied.
- [ ] Re-run the cancel probe at **both scopes** on production-like storage;
      `TEARDOWN_ALLOWANCE_MS` and the two atomic-teardown constants are the ones
      a laptop SSD most flatters.
- [ ] Run the frozen shipped revision with its guards **armed** on fresh
      restores — twice: once production-shaped, once seeded to force the
      full-size batches the production population may be too small to produce.
- [ ] Record the health-window verdict and the final
      `GHOSTREPLAY_ACCURACY_BACKFILL_MODE`.
