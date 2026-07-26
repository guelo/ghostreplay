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
  guards armed on fresh restores and records the health-window verdict.
  Re-scoped 2026-07-25 to the **from-scratch** run — see §0 and §5. It was
  written as the merge gate ("nothing merges to production on the strength of the
  Phase 1/2 run recorded here"), and that gate did not hold: the revision reached
  production before the constants existed.
- `g-b-size-pg18-major` — production runs PostgreSQL **18.4**; every measurement
  in §2 was taken on **15.18**.

---

## 0. Status of the numbers in this document

> **The migration these constants size HAS ALREADY RUN IN PRODUCTION.**
>
> Established 2026-07-25 from a full restore of the valtron dump
> `ghostreplay-20260724T101501Z.dump` (source server PostgreSQL 18.4, `pg_dump`
> 18.3, database `railway`). Four independent facts, any one of which is
> suggestive and which together are conclusive:
>
> - `alembic_version` is **`20260720_01`**, whose `down_revision` is
>   `20260719_01`.
> - `ck_game_sessions_player_accuracy` is **`convalidated = true`**.
>   `20260709_01` creates it `NOT VALID`; `20260719_01` is the only thing that
>   validates it.
> - Both populations are **empty**: `BACKFILL_REMAINING` and `REPAIR_REMAINING`
>   both return 0, and `COVERAGE_ASSERT_SQL` returns 0.
> - **1,603 of the 1,646** ended-visible sessions ended *before the serving write
>   hook existed* — back to 2026-02-08 — and carry
>   `player_accuracy_algo_version = 1`. The hook was committed 2026-07-11 23:57
>   PDT (`95be57a`), so no deploy of it can predate that instant; the cutoff is
>   the commit, not a deploy date, which makes the count a lower bound. A hook
>   that stamps a session when it *ends* cannot have stamped a session that ended
>   in February. Something backfilled them, and 95 rows carry the backfill's own
>   fail-closed fingerprint: version 1 with a `NULL` accuracy.
>
>   Do **not** date this from the `20260709_01` filename. Revision filenames are
>   authored dates, not deploy dates — that revision was itself committed
>   2026-07-11 (`8b49bed`), two days after the date it carries.
>
> What ran was the version that landed 2026-07-19 (`e2967d2`, "Release B
> backfill/repair/fail-closed core"). The sizing harness (`5e47bfa`, 2026-07-24)
> and the runtime envelope (`40b3379`, 2026-07-25) were committed **afterwards**,
> and the revision has since grown 2,030 insertions / 218 deletions. So **none of
> the admission constants, the armed SQL timeouts, the admission projection, the
> compute watchdog, or the observed-lock-hold tripwire executed in production** —
> and Alembic will not re-run `20260719_01` against that database. The run
> completed without incident and left the clean terminal state verified in §6,
> but the guard rails in this document are not what made that true.
>
> **What the constants still govern** is any environment that runs the migration
> **from scratch**: a fresh development or staging database, a rebuilt
> production, or a restore brought up to head. That is now their entire purpose,
> and it is the frame for the remaining sizing work in §5.

### Production's row counts, and what the dump does *not* tell us

| Dimension | Production (2026-07-24) | Phase 1 snapshot | Ratio |
|---|---|---|---|
| `count(*) game_sessions` | 4,184 | 6,000 | 0.70 |
| `count(*) session_moves` | 131,676 | 357,000 | 0.37 |

**Row counts only.** A logical dump does not carry the source database's physical
footprint, so `pg_total_relation_size` on a restore describes the *locally
rebuilt* relations — no bloat, no dead tuples, PostgreSQL 18's page layout, and
freshly built indexes — and not production's on-disk size. Nothing here licenses
a `SIZED_SESSIONS_BYTES` or `SIZED_MOVES_BYTES` claim about production, and the
byte column that first appeared in this table has been removed rather than
qualified.

Three readings of the same restore make the point better than the argument does:
4,079,616 bytes on first query, 4,096,000 minutes later once autovacuum
materialised the FSM/VM forks, and 6,144,000 on a sizing copy after synthesis
rewrote every ended-visible row. The number measures what has been done to the
copy, not what production holds.

**Fewer rows does not mean smaller constants.** The direction table this section
previously carried claimed the frozen constants were "conservative relative to
production". That does not follow and has been withdrawn. Every `MARGINED_*` term
is an *elapsed time*, and elapsed time is a joint property of relation size,
chosen plan, storage, CPU and server major version. Production runs PostgreSQL
**18.4** against measurements taken on **15.18**; a planner two majors newer may
pick a different plan, and the table below already concedes that plan direction
moves either way. Row counts transfer from a dump. Timings do not.

What remains unqualified, and which way each one leans:

| Constant | Status |
|---|---|
| `SIZED_TOTAL_ROWS`, `SIZED_M_TOTAL` | snapshot is 1.4-2.7x production's **row** counts |
| `SIZED_SESSIONS_BYTES`, `SIZED_MOVES_BYTES` | **not obtainable from a logical dump.** A production-restore run measures the restore's own footprint. Only the live database can supply these. |
| `MARGINED_MS_PER_SCAN_STMT`, `MARGINED_MS_COVERAGE_ASSERT`, `MARGINED_MS_BACKFILL_*` | measured on **PostgreSQL 15.18**; production runs **18.4**. These price a *plan*, not an intrinsic relation cost, and a planner two majors newer is free to choose differently — in either direction. See `g-b-size-pg18-major`, and trap #2 below for what a plan change is worth. §7 re-measures them on 18.4. |
| `TEARDOWN_ALLOWANCE_MS`, `MARGINED_MS_ATOMIC_TEARDOWN_FIXED` / `_PER_ROW` | an Apple-silicon laptop with a local SSD and no fsync contention is the best case for both commit and cancel-to-unlock — **up** against a real host |
| `MARGINED_MS_PER_ROW` | CPU-bound PGN parse; laptop cores are fast — **up** |
| `B_TESTED` / `R_TESTED` | bounded by what was exercised — up **or** down |

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

**Every `--out` file is evidence, and evidence gets committed.** A derivation
whose inputs were not retained cannot be re-executed, so an error inside
`derive()` is detectable only by re-reading the arithmetic in prose — which is
the state §2 and §7 were left in, and what §8 exists to end. Retain every one,
commit them under `docs/sizing/`, and name each one `<kind>_…_<date>.json` for
the `kind` it declares: that directory is globbed by prefix (`sweep_*.json`
selects the sweep domain), so a filename there is a selector rather than a label.
The committed set and the command that consumes it are in
[§8](#8-the-measurements-are-on-disk-g-b-size-measurement-json-2026-07-26).

Every Phase 1 run — atomic, batch, cancel probe, sweep domain — reads the four
dimensions **twice**: once before its synthesis block and once at the start of
the measured pass. Both readings are written to its artifact under
`dimension_bases`, alongside a `timing_basis` naming the one its timings are
paired with, and Phase 2 refuses an artifact that carries neither. The pass
reading is the basis; the earlier one is provenance, divided by nowhere. Why the
two differ and what each is admissible for:
[§7, the two dimension readings](#the-two-dimension-readings-g-b-size-harness-defects).

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
- an artifact of **any** kind carries no machine-readable measurement basis, or
  carries one whose `post_synthesis` reading disagrees with the
  `dimensions_before` its statements were timed against, or selects a
  `timing_basis` other than `post_synthesis` — see
  [§7's provenance subsection](#the-two-dimension-readings-g-b-size-harness-defects);
- `SIZED_*` would be frozen from an atomic snapshot whose **pre**-synthesis
  reading was never recorded. Only on the full-restore path: with
  `--production-dimensions` the basis comes from production facts that were never
  synthesized.
- `--production-dimensions` was passed but does not **completely** declare a
  production relation — all four dimensions, each a positive integer, plus
  `n_stale` / `n_repair` / `m_moves` as explicit non-negative integers. The flag
  is what waives the rule above, so a file that declares nothing would waive it
  and then freeze the synthesized snapshot's basis anyway; a partial one would
  fill the gaps from the snapshot's synthesized numbers and label the result
  production. Zero populations are written as zeros — absent is not zero.

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

> **Which of these numbers has an artifact behind it.** Two do. Every other
> measured constant in this section is a **transcription**: the §2 run's eight
> measurement JSONs were never committed and its snapshot no longer exists, so the
> arithmetic below can be re-read but not re-executed, and an error in it is
> visible only by re-reading prose (`g-b-size-measurement-json`).
>
> | | Backed by | Re-derived by |
> |---|---|---|
> | `MARGINED_MS_BACKFILL_SWEEP_SCAN`, `MARGINED_US_BACKFILL_SWEEP_PER_PAGE` | `docs/sizing/sweep_*.json` | `test_frozen_sweep_model_matches_the_published_envelope` |
> | everything else in this section | §2's tables only | nothing — no artifact to re-derive from |
>
> This is not a statement about whether the numbers are *right*. It is a statement
> about what could catch them being wrong, and for the sweep pair that turned out
> to matter twice: once when it retired the withdrawn OLS line, and once when it
> was the only row of §7's derived table anything could check — which is how §8
> found the two that were not.
>
> §8 commits a full measurement set for the **18.4 production restore** and makes
> that derivation reproducible end to end. It does not make *these* literals
> reproducible and nothing can: their fixture is gone. Re-freezing them from
> committed evidence is `g-b-sizing-harness` (Phase 3).

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
| `MARGINED_MS_BACKFILL_SWEEP_SCAN` | 72 | `ceil(3 * 23.867343)` — the relation-scan coefficient of the covering LP over the sweep domain, **at the frozen basis**. Milliseconds. §7. |
| `MARGINED_US_BACKFILL_SWEEP_PER_PAGE` | 518 | `ceil(3 * 0.172440 * 1000)` — the per-page slope of the same LP. **Microseconds**, divided by 1000 in `backfill_sweep_ms`. §7. |
| `BACKFILL_SELECT_SWEEPS_UNDER_LOCK` | 1 | structural per-pass count (atomic backfill converges in one unlocked-selection pass) |
| `BACKFILL_REMAINING_UNDER_LOCK` | 1 | structural per-pass count (same argument) |
| `SCAN_STMT_TIMEOUT_MS` | 521 | `max(521, 6, 6)` — the maximum over **every** statement it is armed on: the four complete `session_moves` scans (which already include `REPAIR_REMAINING_SQL`), the coverage assertion, **and `BACKFILL_REMAINING_SQL`** via `MARGINED_MS_BACKFILL_REMAINING`. The two convergence scans are priced by *different* terms — the repair one by `MARGINED_MS_PER_SCAN_STMT`/`G_moves`, the backfill one by `MARGINED_MS_BACKFILL_REMAINING`/`G_sessions` — and only the latter needed adding. **Neither** sweep component is in that maximum: each page of the sweep is armed by the mode's batch cap, so the two sweep constants price a multi-statement unit no single armed value has to cover — the scan component is a relation walk spread across every page rather than the cost of any one of them, and the per-page component is a per-statement slope in microseconds. |
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

### The backfill's own `game_sessions` terms (one PROVISIONAL, one replaced)

The backfill's selection sweep and `MARGINED_MS_BACKFILL_REMAINING` price the
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
- the sweep scalar, `ceil(3 * 7 * 1.74) = 37` — a sweep at the
  sized dimensions is `ceil(6000 / 1000) + 1 = 7` pages (the `+1` is the empty page
  that terminates it), and **each page is priced at a whole `game_sessions`
  scan**, the worst case for an unindexed filter. Deliberately conservative: a
  direct measurement can only lower it.

  > **Withdrawn 2026-07-25 by §7.** "A direct measurement can only lower it" was
  > not a safe claim, and its stated reason is wrong: the sweep is keyset
  > pagination, which PostgreSQL serves from the primary key with a filter, so it
  > reads the relation roughly **once in total** rather than once per page. Nine
  > times the pages costs 1.4x the time, not 9x. The `pages x scan` model
  > overestimates by the page count, so the direction happened to hold here — but
  > it held for a reason the derivation did not state and had never measured.

`scripts/size_accuracy_backfill.py` measures the convergence count directly
(`backfill_remaining` in `time_scan_statements`). The sweep is not a scan-statement
timing at all and is no longer measured there: it is a **domain**, produced by
`--mode sweep-domain` and fitted in `--derive`. `g-b-size-derive-backfill-terms`
owns the convergence count's re-measurement, and sizing qualification may adjust
the number and rerun this bead's gates — an adjusted *number* is expected
refinement, not a structural change.

**Measured 2026-07-25 against the production restore; see §7.** An earlier
version of this paragraph concluded the opposite — that the restore "cannot
supply these two numbers", because production's backfill population is zero and a
sweep over an empty population is a single empty page. **That was wrong, and it
is retracted rather than annotated**, because it was the reasoning that nearly
skipped the measurement entirely.

Two errors in it. The harness *synthesizes* both populations from a
production-shaped restore — `synthesize_stale` and `synthesize_repair`, which
§5's own Phase 1 procedure already prescribes — so an empty production population
is not a constraint on what can be measured. And `BACKFILL_REMAINING_SQL` filters
`game_sessions` on the unindexed version predicate, so it is a full relation scan
whether it matches zero rows or a million; `--derive` files it under "scan work:
no population, no zero branch" for exactly that reason. The one true observation
in the paragraph — that 4,184 sessions is smaller than the 6,000-row basis — is a
reason to *normalize* the result, which §7 does, not a reason not to measure.

Both terms were measured directly. `MARGINED_MS_BACKFILL_REMAINING = 6` is now
**qualified** — the worst of seven measurements, each normalized on the basis of
the copy it ran on, is exactly 6.

The sweep scalar did not survive that measurement. The label "PROVISIONAL" came to
mean something specific and worse than "not yet measured": a **scalar cannot price
this statement at all**, because its cost depends on the operator-chosen batch
size and the runtime admits batch sizes down to `MIN_ADMITTED_BATCH = 1`.
`g-b-sweep-batch-cost` (P1) **deleted** `MARGINED_MS_BACKFILL_SELECT_SWEEP` and
replaced it with the two-component model of §7:
`MARGINED_MS_BACKFILL_SWEEP_SCAN = 72` ms on the relation and
`MARGINED_US_BACKFILL_SWEEP_PER_PAGE = 518` µs per page, evaluated by
`backfill_sweep_ms` over the page count `backfill_sweep_pages` derives from the
live population and the resolved batch size.

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
| Scan budget: `(2*20 + 2) * 521 + 6 + 20 * (sweep(6001 pages) + 6)` | 85,618 ms | 900,000 ms | ✅ |
| `EST_MAX_LOCK_HOLD_MS <= MAX_WRITER_STALL_MS` | 5,007 | 30,000 | ✅ |
| `BATCH_LOCK_WAIT_MS < MAX_BATCH_MS` | 1,000 | 5,000 | ✅ |
| `SCAN_STMT_TIMEOUT_MS >= max(per_scan_stmt, coverage, backfill_remaining)` | 521 | 521 | ✅ |

The scan budget charges the sweep at the **declared worst case** —
`IMPORT_WORST_CASE_SWEEP_PAGES = ceil(SIZED_TOTAL_ROWS / MIN_ADMITTED_BATCH) + 1
= 6,001` pages — because module load has no database, no population and no
resolved batch size. `assert_runtime_scan_budget` re-derives it from the live
`N_stale` and the resolved `GHOSTREPLAY_ACCURACY_BACKFILL_BATCH` before the first
row lock, which is the only check that sees a live population past the sized basis
combined with a small override.

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

## 5. Outstanding for `g-b-sizing-harness` (Phase 3), re-scoped

Phase 3 was written as the merge gate: "nothing merges to production on the
strength of the Phase 1/2 run recorded here." That gate did not hold — §0 — so
Phase 3 is no longer gating a production deploy that has not happened. It is
re-scoped to qualifying the **from-scratch** run, which is the only run these
constants can still govern.

The re-scope changes what a production restore is *for*. It is no longer the
population to be measured — production's populations are both zero, so a restore
brought to head does nothing and times nothing. It is a **shape**: real PGN
lengths, real eval density, real ply distributions, at a relation size a
from-scratch environment will approach as it fills.

- [ ] Re-run Phase 1 on **PostgreSQL 18.x**, matching production's major version.
      Everything below inherits this; a scan constant measured on 15 prices a
      plan 18 may not choose (`g-b-size-pg18-major`).
- [ ] Drive the populations by **synthesis**, and say so. A production restore
      has `N_stale = 0` and `N_repair = 0`; the harness's `--synthesize-stale` /
      `--synthesize-repair K` paths are now the *only* way to obtain either, and
      §1's `--derive` refusals already reject an unsynthesized snapshot rather
      than fabricating a per-row constant from it.
- [ ] Size against the **largest** relation a from-scratch environment is
      expected to reach, not against today's 4,184 sessions. The two backfill
      terms are `O(G_sessions)` on an unindexed predicate and the sweep's page
      count is `ceil(G_sessions / MAX_BATCH_SIZE) + 1`, so both are functions of
      the relation the migration will meet, not of the one it met in July 2026.
- [ ] Re-run the cancel probe at **both scopes** on production-like storage;
      `TEARDOWN_ALLOWANCE_MS` and the two atomic-teardown constants are the ones
      a laptop SSD most flatters, and §0 leaves them the constants still leaning
      **up**.
- [ ] Run the frozen shipped revision with its guards **armed** on fresh
      restores — twice: once production-shaped, once seeded to force the
      full-size batches a small population cannot produce. This is the only
      remaining check that exercises the runtime envelope at all, since
      production never did.
- [ ] Record the health-window verdict and the final
      `GHOSTREPLAY_ACCURACY_BACKFILL_MODE` for a from-scratch deploy.
- [ ] **Commit every measurement `--derive` consumes**, and re-freeze the shipped
      constants from that committed set rather than from a table. The artifacts
      go in `docs/sizing/` alongside 2026-07-26's, as their own set and their own
      `derived_…` rather than appended to that one, since a derivation's atomic
      and batch timings have to come from a single fixture state.
      §8 does this for the 18.4 production restore and it is the pattern to
      follow, but it cannot reach §3's literals — their 15.18 snapshot is gone,
      so only a re-freeze makes them reproducible. Until then §3's constants have
      no artifact behind them and §3 says so (`g-b-size-measurement-json`).

---

## 6. Production verification of the deployed run (2026-07-25)

Read-only, against the restore described in §0. Nothing was written; the
statements are the revision's own, imported through Alembic rather than restated,
so this checks the shipped assertions and not a paraphrase of them.

### The revision's own assertions

| Assertion | Result | |
|---|---|---|
| `COVERAGE_ASSERT_SQL` — ended-visible rows not at version 1 | 0 | ✅ |
| `SOUNDNESS_ASSERT_SQL` — a *served value* computed over a broken grid | 0 | ✅ |
| `BACKFILL_REMAINING_SQL` | 0 | ✅ converged |
| `REPAIR_REMAINING_SQL` | 0 | ✅ converged |

### The 95 rows left at version 1 with a `NULL` accuracy

Version 1 with a `NULL` value is the backfill's fail-closed outcome: the row is
marked *considered* so it leaves the population, while no accuracy is served. All
95 were re-read through `LOAD_MOVES_PG` and re-run through the revision's own
`_accuracy_for`:

| Refusal | Rows |
|---|---|
| Incomplete — stored plies < the PGN's mainline ply count | 46 |
| Eval gap on a player transition (`eval_cp`/`eval_mate` missing) | 30 |
| Broken ply grid — `ply_coordinates_intact` refused | 10 |
| PGN unparseable, or no mainline plies | 5 |
| No `session_moves` rows at all | 4 |

**Not one of them recomputes to a value today.** Every `NULL` is a refusal the
frozen algorithm still makes on the same rows, so none of them is lost work that
a rerun would recover.

The 10 broken grids are also why `REPAIR_REMAINING` is legitimately 0 rather than
suspiciously 0: the repair phase targets rows carrying a *served value* over a
broken grid, and the backfill refused all ten with a `NULL` instead of writing
one. `SOUNDNESS_ASSERT_SQL` returning 0 is the same fact from the other side.

### What this does not certify

95 of 1,646 ended-visible sessions — **5.8%** — serve no accuracy. That is the
backfill behaving correctly over defective inputs, not a migration defect, and it
is out of scope here. The upstream capture defects it exposes (46 truncated move
records, 30 partial eval coverage, 10 broken ply grids, 5 unparseable PGNs, 4
sessions with no moves at all) are tracked separately in `g-acc-null-cohort`.

---

## 7. Phase 1/2 re-derivation on a production restore, PostgreSQL 18.4 (2026-07-25)

**Owning bead:** `g-b-size-derive-backfill-terms`. This is the run that finally
times `BACKFILL_REMAINING_SQL` and the selection sweep directly, instead of
pricing them from the coverage assertion as §3 did.

> **Which of this section's numbers has an artifact behind it.** The sweep
> domains do — `docs/sizing/sweep_batch_domain_20260725.json` and
> `…_endpoint_20260725.json`, which is why the fit, its vertex, its active
> constraints and its coverage of every retained trial are all re-derived by
> tests rather than read here. Everything else in §7 is a **transcription** of a
> run whose copies were dropped and whose measurement JSONs were never retained:
> the two directly-timed terms below, the atomic/batch/probe inputs, and the full
> derived table. §8 re-measures the same fixture with every artifact committed,
> and the first thing that comparison found was two wrong rows in that table.

**Fixture.** The 2026-07-24 production dump, restored into a local Homebrew
`postgresql@18` cluster — **18.4, production's exact major and minor** — with one
disposable copy per run, cloned `CREATE DATABASE … TEMPLATE`. Production has the
CHECK already `convalidated` (§0), so each copy drops and re-adds it `NOT VALID`,
reconstructing the pre-`20260719_01` state; the condition is `20260709_01`'s
verbatim. Populations come from the harness's own `--synthesize-stale` /
`--synthesize-repair`, which is the documented procedure — production's own
populations are both zero, and §1's `--derive` refusals reject an unsynthesized
snapshot rather than fabricating a per-row constant from it.

Production's 1,646 ended-visible sessions cannot supply a large backfill batch
**and** a contract-floor repair population at once: synthesis runs stale-then-
repair and the two are disjoint, so `K = 1000` leaves `N_stale = 646`. That is a
fixture ceiling, not a cost measurement, and it is why `B_TESTED` below is not
frozen.

### The two terms, measured

| Statement | Trials | Max (ms) | Median (ms) | `ceil(3 x max)` | Frozen in §3 |
|---|---|---|---|---|---|
| `BACKFILL_REMAINING_SQL` (`N_stale` = 646) | 3 | 1.15 | 1.04 | 4 | 6 |
| `BACKFILL_REMAINING_SQL` (`N_stale` = 1,646) | 5 | 1.07 | 0.96 | 4 | 6 |
| selection sweep, worst case (below) | 5 | 4.86 | 4.17 | 15 | 37 |

**Both values were conservative here by measurement rather than by assumption**
— but by less than the raw columns suggest, because a timing has to be carried
onto the frozen basis before it can be compared to a frozen constant. Normalized
on the basis of the copy each ran on (see "Normalizing to the frozen basis"
below): `MARGINED_MS_BACKFILL_REMAINING` by **3.58x**
(`6 / (1.147 x 1.46172)`), the sweep scalar by **4.12x**
(`37 / (4.86 x 1.84871)`) — not the 3.6x and ~5x an unnormalized reading gives.
For the sweep, read "here" strictly: the row above is ONE page size, and the
scalar is breached at others, including at page size 100 once enough trials are
run. That is what retired it; see the domain measurement two sections down.

### The sweep does not re-scan the relation once per page

The sweep had to be re-measured outside the harness, because the harness measures
it with whatever stale population the run happens to carry, paged at the
**currently frozen** `MAX_BATCH_SIZE` — while the same run re-derives
`MAX_BATCH_SIZE` afterwards. The Phase 1a run swept `N_stale = 646` at page size
1,000 and reported **2 pages**; the worst case for this shape is the whole
ended-visible set at the shipped page size. Measured on a copy with
`N_stale = 1,646`, `G_sessions = 4,184`:

| Page size | Pages | Max (ms) | Median (ms) | `ceil(3 x max)` |
|---|---|---|---|---|
| 1,646 | 2 | 4.55 | 3.27 | 14 |
| 1,000 | 3 | 4.45 | 4.42 | 14 |
| 646 | 4 | 4.86 | 4.17 | **15** |
| 500 | 5 | 5.02 | 4.32 | 16 |
| 250 | 8 | 4.99 | 4.66 | 15 |
| 100 | 18 | 6.46 | 5.89 | 20 |

Nine times the pages costs 1.4x the time. **So §3's derivation model is wrong
about the plan, not merely pessimistic about the number.** It priced each page at
"a whole `game_sessions` scan, the worst case for an unindexed filter" and
multiplied by the page count. The population predicate is indeed unindexed, but
the sweep is keyset pagination — `WHERE … AND id > :last ORDER BY id LIMIT n` —
which PostgreSQL serves from the primary key with a filter, so the sweep reads
the relation roughly **once in total** and each additional page adds startup, not
a scan. The `pages x scan` product overestimates by the page count.

That also retires §3's claim that "a direct measurement can only lower it". The
claim happened to hold here, but its stated reason — that per-page full scans are
the worst case — describes a plan PostgreSQL does not choose. A different planner
may not choose this one either; the honest statement is that the model's shape
was never measured until now.

### …but it is not page-count *independent* either

The table above stops at page size 100 because that is where the *sizing*
procedure stops. The **runtime** does not: `resolve_batch_size`
(`20260719_01:1198`) admits `GHOSTREPLAY_ACCURACY_BACKFILL_BATCH` anywhere in
`1..MAX_BATCH_SIZE`. Extending the same measurement across the full admissible
domain, on the same copy — **all twelve points, 3 trials each, maxima**:

| Page size | Pages | Max (ms) | Median (ms) | `ceil(3 x max)` vs frozen 37 |
|---|---|---|---|---|
| 1 | 1,647 | 257.05 | 236.39 | **772** — 21x under-charged |
| 2 | 824 | 134.66 | 134.28 | 404 |
| 5 | 331 | 54.85 | 54.70 | 165 |
| 10 | 166 | 25.05 | 25.02 | 76 |
| 25 | 67 | 12.02 | 11.83 | **37** — break-even |
| 50 | 34 | 8.08 | 7.70 | 25 |
| 100 | 18 | 5.96 | 5.47 | 18 |
| 250 | 8 | 5.58 | 5.03 | 17 |
| 500 | 5 | 4.49 | 4.34 | 14 |
| 646 | 4 | 13.60 | 4.60 | 41 |
| 1,000 | 3 | 3.79 | 3.57 | 12 |
| 1,646 | 2 | 3.60 | 3.25 | 11 |

The correct reading is that the sweep is *not proportional to* page count, not
that it is independent of it. Past roughly 50 pages the per-page term dominates,
and the frozen scalar is break-even at page size 25 on today's population — a
break-even point that moves with `N_stale`.

The `646` row is a host outlier — 13.60 ms max against a 4.60 ms median at four
pages — and it is kept in the table rather than dropped. A maximum over trials is
precisely the statistic outliers land in, and every number fitted to this set
changes depending on whether it is present.

A single frozen scalar therefore cannot price this statement across the range the
runtime admits. Tracked as `g-b-sweep-batch-cost` (P1), which **blocks** the
de-provisionalization of this term and `g-b-runtime-envelope`, since the
projection and the import-time scan budget are its arithmetic.

`MARGINED_MS_BACKFILL_REMAINING` is untouched by this: `BACKFILL_REMAINING_SQL`
runs once per pass, not once per page, so it carries no page-count term.

### A least-squares fit is not a cost model

An earlier draft of this section reported `sweep_ms ≈ 4.101 + 0.15424 x pages`
and read the two coefficients as a ready-made replacement model — a 4.1 ms
relation-scan component plus 154 µs per page. **That line is withdrawn as a
model.** The two-component *shape* survives; the fitted line does not, for two
reasons.

**It is a central tendency, not a bound.** An ordinary least-squares line through
a set of maxima passes *through* them, so it necessarily sits below some of the
very measurements it was fitted to. At 824 pages it predicts 131.20 ms against a
measured 134.66 ms; tripling both coefficients gives 393.59 ms, still short of
the `ceil(3 x 134.66) = 404` ms the same margin applied to the measurement
demands. The 3x margin is being spent covering fit error instead of covering
variance. A model that is frozen and then divided into a deadline has to be an
**upper envelope** over its measurements, not a regression through them.

**It was not reproducible from what was published.** The fit was computed over
all twelve maxima; the table printed beside it listed eight, and the copy in
`g-b-sweep-batch-cost` listed eleven. Those eleven yield
`3.029 + 0.15517 x pages` — a materially different intercept — and the
twelve-point fit's own worst residual is at the `646` outlier the eleven-point
table had silently removed. The raw per-trial timings had not been kept, so
neither line could be checked against its inputs.

Re-measured with **7 trials per point and every trial preserved to disk** —
[`docs/sizing/sweep_batch_domain_20260725.json`](sizing/sweep_batch_domain_20260725.json),
which carries both runs, the copy's dimensions, and the withdrawn fit — same
copy, `N_stale = 1,646`, `G_sessions = 4,184`:

| Page size | Pages | Max (ms) | Median (ms) | Min (ms) |
|---|---|---|---|---|
| 1 | 1,647 | 282.25 | 273.53 | 245.30 |
| 2 | 824 | 155.00 | 127.83 | 117.56 |
| 5 | 331 | 64.07 | 51.01 | 47.08 |
| 10 | 166 | 32.95 | 30.46 | 28.35 |
| 25 | 67 | 16.16 | 14.79 | 14.00 |
| 50 | 34 | 10.37 | 9.49 | 8.75 |
| 100 | 18 | 6.94 | 6.37 | 6.04 |
| 250 | 8 | 6.57 | 5.67 | 5.39 |
| 500 | 5 | 5.61 | 5.11 | 4.70 |
| 646 | 4 | 4.94 | 4.50 | 4.09 |
| 1,000 | 3 | **12.75** | 3.57 | 3.46 |
| 1,646 | 2 | 3.66 | 3.31 | 3.10 |

Two things this run establishes that the first could not. **The outlier moved** —
three pages here, four pages before — so it is host noise rather than a property
of a page count, and *some* point will always carry one. The model has to
tolerate an outlier, not have it removed. And **eleven of the twelve maxima
rose** — which is the *tendency* of a larger sample, not a guarantee. Be precise
about why: a maximum over more independent trials has a higher expected value,
because more draws means more chances at the tail. It is monotonic only for a
*cumulative* maximum over appended trials; these are two independent runs, so
nothing forces any individual point up. The twelfth point shows it — at 4 pages
the maximum *fell*, 13.60 → 4.94, because that is precisely where run B's outlier
had landed. Both directions are the same phenomenon: a maximum over few trials is
whatever the host happened to do, so a 3-trial maximum is an estimate of a bound
and not a bound.

### The shipped fit: a covering LP, in frozen-basis coordinates

OLS over these maxima gives `5.9466 + 0.170443 x pages`, which under-predicts its
own worst measurement by 8.61 ms. An earlier draft repaired that by shifting the
intercept up by exactly that residual — `14.5556 + 0.170444 x pages` — which does
cover every point, but is the crudest construction that does: it inherits OLS's
slope wholesale and over-charges by 103.31 ms across run C. **That shifted
envelope is superseded.** What ships is the *least conservative* line covering
every measured maximum: a two-variable LP, solved exactly.

```
minimize   sum over the OBJECTIVE points of (A / N_copy(i) + b x pages(i) - max_ms(i))
subject to A / N_copy(i) + b x pages(i) >= max_ms(i)   for every point in the COVERAGE set
           A >= 0, b >= 0
```

`A` is the relation-scan coefficient **at the frozen basis** and `b` the per-page
slope. The fit is **not** raw-then-scaled: each point carries its own
`N_copy(i)` *inside* the fit, so measurements taken on different copies enter on
their own bases. Fitting raw `(a, b)` across several copies and multiplying `a`
by one shared factor afterwards prices every point through whichever copy's
dimensions happened to be chosen — the basis error the section below exists to
prevent.

**Coverage set** — every measured maximum on record, every run, every
sweep-domain artifact, *including* run B's 3-trial points. A published maximum is
a measurement and a bound has to cover it. "A 3-trial maximum is an estimate of a
bound, not a bound" is a reason not to let it *steer* a fit; it is never a licence
to sit below a number the host actually produced. Run B is not a formality here:
`(4 pages, 13.60 ms)` is an **active constraint** of the shipped solution and the
only evidence at that page count.

**Objective set** — only points whose per-trial timings are retained
(`raw_trials_retained: true`), i.e. run C's twelve. Run B's maxima cannot be
audited or re-derived, so they constrain the fit without steering it. The
objective is summed in each point's own measurement coordinates, so a point is not
weighted by how far its copy sits from the frozen basis.

**Solver** — the objective's coefficients are strictly positive, so the optimum is
at a vertex, and the vertices come in three families: every pair of coverage
constraints made tight (an exact 2x2, `det = 1/N₁ x p₂ - 1/N₂ x p₁`, degenerate
pairs skipped), the `b = 0` boundary, and the `A = 0` boundary. Both axes are
load-bearing rather than defensive padding — with points at `(10, 1.0)` and
`(1000, 1000.0)` the single pair-intersection has `A < 0`, and a solver without
the `A = 0` boundary returns the flat line at an over-charge of 999 ms instead of
the true optimum `(A = 0, b = 1)` at 9 ms. `fractions.Fraction` throughout,
including `N_copy(i)` (a ratio of *integer* dimensions), so the solution is exact
and reproducible bit for bit from the retained JSON.

Over runs B + C on `gr_p1_sweep`, `N_copy = 1222/661`:

```
A = 23.867343231615 ms      (exact 16170721723/677525000)   scan, at the frozen basis
b =  0.172439878049 ms/page (exact 1414007/8200000)
A / N_copy = 12.910240 ms — what the scan term costs on gr_p1_sweep itself
published, rounded up:  A <= 23.8674 ms,  b <= 0.172440 ms/page
active constraints:     (4 pages, 13.60 ms, run B)  and  (824 pages, 155.0007 ms, run C)
sum over-charge over run C: 89.77 ms   (the withdrawn shifted-OLS envelope: 103.31 ms)
```

**Both coefficients round UP**, always, and that is not cosmetic. The
construction makes the line *touch* its worst point, so rounding to display
precision in the normal, nearest direction pushes it below the very measurement
it was built to cover. The shifted envelope printed as `0.170443` yielded
155.000632 against a measured 155.0007: a bound broken by 68 nanoseconds, entirely
by transcription. The same trap applies when the frozen literals are written into
the revision, and the per-component `ceil` below is what closes it.

`--derive` re-runs this LP over whatever sweep-domain artifacts it is given and
emits `sweep_scan_coeff_frozen_basis_ms`, `sweep_envelope_per_page_ms`,
`sweep_envelope_active_constraints`, `sweep_envelope_sum_overcharge_ms`,
`sweep_domain_points`, `sweep_domain_max_pages` and `sweep_copy_growth_factors` —
one entry per artifact, with that copy's dimensions beside it, so no reader has to
guess which basis a point was normalized on. Measurement JSON is read through one
intake with `parse_float=Decimal`: a plain `json.loads` turns `155.0007` into the
nearest binary float *before* the fit can see it, and `Fraction` of that float is
the exact value of a different number — at precisely the digits where the LP picks
between vertices.

### Freezing the two components: units, margin, ceiling

| | Decision | Why |
|---|---|---|
| Units | `MARGINED_MS_BACKFILL_SWEEP_SCAN` in **ms**; `MARGINED_US_BACKFILL_SWEEP_PER_PAGE` in **µs**, divided by 1000 at one call site | The per-page term is ~0.52 ms. Rounding a sub-millisecond slope up to an integer millisecond nearly doubles it and manufactures ~2.9 s of phantom stall at the 6,001-page worst case. Same convention and same reasoning as `MARGINED_US_ATOMIC_TEARDOWN_PER_ROW`, and pinned by a constant test for the same reason. |
| Margin and ceiling | **Per component, at freeze time, margin before ceiling** | A single ceiling on the margined *total* can only be applied once a page count exists, i.e. at run time — which would leave the frozen literals as floats, breaking the "every frozen constant is a positive int" contract and hiding which component the rounding landed on. Per-component ceilings keep both literals integer and auditable against their own measurement, and each rounds up. The cost is bounded and stated: under 1 ms on the scan component, under 1 µs/page. |
| Growth factor at run time | `g_sessions` multiplies the **scan component only** | The scan component is a relation read. The per-page component is statement startup — parse, plan, execute, round-trip — and a larger relation does not make starting a statement more expensive. Applying `g_sessions` there over-charges; applying nothing to the scan component under-charges. |

```
MARGINED_MS_BACKFILL_SWEEP_SCAN     = ceil(3 x 23.867343231615)        = ceil(71.602030)  = 72   # ms
MARGINED_US_BACKFILL_SWEEP_PER_PAGE = ceil(3 x 0.172439878049 x 1000)  = ceil(517.319634) = 518  # µs
```

There is **no run-time ceiling**: `backfill_sweep_ms` returns a float, like every
other term in `project_atomic_stall_ms` and `_scan_budget_ms`.

### The invariant, carried as a test rather than as a table

For every measured point *i*, de-normalized back onto the copy it actually ran on:

```
MARGINED_MS_BACKFILL_SWEEP_SCAN / N_copy(i)
    + MARGINED_US_BACKFILL_SWEEP_PER_PAGE x p_i / 1000  >=  3 x max_i
```

It holds by construction — the LP covers every point in frozen-basis coordinates
and both literals are `ceil`-ed *up* from `3 x` the solution — which is exactly
why it is worth asserting: **a frozen model whose margined value drops below `3x`
a measurement it was fitted to has silently stopped being conservative, and
nothing else in the revision would notice.** At the shipped constants the two
tightest slacks are **0.776 ms** at 824 pages (run C) and **0.218 ms** at 4 pages
(run B's outlier) — the same two points the LP made active, which is the sign that
the margin is being spent on variance rather than on fit error.

Carried by
`test_release_b_sizing.py::test_frozen_sweep_model_covers_every_retained_measurement`,
which ranges over every point of every sweep-domain artifact on disk, each with
its own `N_copy(i)`. A second measuring copy does not get a second invariant.
`test_frozen_sweep_model_matches_the_published_envelope` re-runs the LP from the
retained trials and asserts the frozen literals — the derivation is reproducible
from the evidence on disk, which is precisely what the withdrawn OLS line was not.

**The `3x` belongs to those two and to nowhere else.** It holds by construction
for the *fitted* points, so asserting it is a tripwire on the freezing arithmetic.
Asserting it of a **live** sweep is a different claim entirely: dividing the
margined model by 3 leaves the raw fit — coefficients measured on another machine,
carrying no margin — and demands it cover the running host's worst reading. That is
the zero-margin cross-host comparison the endpoint gate's own history rules out, so
`test_pg_frozen_sweep_model_covers_a_live_sweep` asserts what a live sweep can
carry: `margined_model >= observed`, the margin left for host variance. It was
written with the `3x` and caught doing it — passing alone and failing under
full-gate load, budgeting ~24 ms for a two-page sweep on a relation the test drains
in 0.4 s unloaded.

### Normalizing to the frozen basis

The shipped constants are frozen against `SIZED_TOTAL_ROWS = 6,000` and
`SIZED_SESSIONS_BYTES = 10,010,624`. Bringing a measurement onto that basis uses
the growth factor the runtime itself uses, `max(rows ratio, bytes ratio)` — with
the ratios taken against **the copy the measurement actually ran on**.

**`N_copy` is not `G_sessions`, and neither substitutes for the other.** They are
two different quantities pointing in two different directions, and confusing them
is the single easiest way to under-charge a scan term:

| | `N_copy` (freeze time) | `G_sessions` (run time) |
|---|---|---|
| Definition | `max(1, SIZED_rows / copy_rows, SIZED_SESSIONS_BYTES / copy_bytes)` | `_growth_factor(live relation vs the frozen basis)` |
| Direction | measurement copy → frozen basis | frozen basis → live relation |
| Value for `gr_p1_sweep` | **1222/661** = 1.848714… | — |
| Value for `gr_p2_sweep6000` | **1** — the `max(1, …)` clamp, since that copy is *larger* than the basis on both axes | — |
| Value *at* the frozen basis | — | **1.0** |
| Where it lives | inside the LP, as each point's own divisor | applied at every runtime call site |
| Applies to | the scan component only | the scan component only |

`N_copy(i)` is a ratio of **integer** dimensions, so it is exactly rational and
the fit stays exact end to end. It is a **per-point** quantity: points measured on
different copies carry different `N_copy(i)`, which is the whole reason the LP is
solved in frozen-basis coordinates rather than scaled after the fact.

`gr_p1_sweep` is *smaller* than the frozen basis, so its timings must be scaled
**up** by 1.848714… to state what they would have cost at the basis the constants
are frozen against. At that basis the live `G_sessions` is exactly 1 — so
`G_sessions` can never supply that normalization; it prices growth *beyond* the
basis and nothing else. The per-page component takes **neither** factor beyond the
3x margin.

That last clause is not pedantry. All seven sizing copies hold the same 4,184
sessions, and their `game_sessions` relations differ by 26%:

| Copy | `game_sessions` bytes | Rows ratio | Bytes ratio | Growth factor |
|---|---|---|---|---|
| `gr_p1_sweep` | 5,414,912 | 1.43403 | **1.84871** | 1.84871 |
| `gr_p1_empty` | 5,431,296 | 1.43403 | **1.84314** | 1.84314 |
| `gr_p1_b1000` | 6,144,000 | 1.43403 | **1.62933** | 1.62933 |
| `gr_p1_b100` | 6,758,400 | 1.43403 | **1.48121** | 1.48121 |
| `gr_p1_b250` | 6,815,744 | 1.43403 | **1.46875** | 1.46875 |
| `gr_p1_atomic`, `gr_p1_b500` | 6,848,512 | 1.43403 | **1.46172** | 1.46172 |

`gr_p2_sweep6000` is the eighth copy and the only one that is not 4,184 rows:
`--synthesize-sessions 6000` grew it to 8,538 rows / 14,008,320 bytes, so both
ratios fall below 1 (0.70274 and 0.71462) and the clamp takes its growth factor
to **1**. It is the same restore of the same dump; what differs is that its
synthesis added rows rather than only rewriting them.

**Same source population and same `game_sessions` row count — not identical
data.** Every copy is the same restore of `ghostreplay-20260724T101501Z.dump`,
but each was then mutated by the synthesis its own measurement needed, into three
distinct states:

| Copies | Synthesis | `session_moves` | `N_stale` |
|---|---|---|---|
| `gr_p1_atomic`, `gr_p1_b100/250/500/1000` | stale + repair | 130,676 | 646 |
| `gr_p1_sweep` | stale only | 131,676 | 1,646 |
| `gr_p1_empty` | stamped | 131,676 | 0 |

`synthesize_repair` *deletes* one ply from each of K sessions, which is where the
missing 1,000 moves went. So the byte spread is partly dead tuples from rewrite
and vacuum state and partly a genuinely different post-synthesis relation, and
**neither component is recoverable from the frozen dimension alone** — which is
the provenance defect in `g-b-size-harness-defects` #1, seen from the outside,
[since fixed](#the-two-dimension-readings-g-b-size-harness-defects). It
matters because the normalization factor is
`SIZED_bytes / measured_bytes`, so the **least** bloated copy demands the
**largest** factor. Normalizing a timing with some other copy's byte reading
charges it wrongly in whichever direction that reading errs; an earlier version
of this section normalized everything with 6,144,000 — `gr_p1_b1000`'s figure
alone, the **most** bloated of the seven — which under-charged every leaner copy.

**`MARGINED_MS_BACKFILL_REMAINING`**, every Phase 1 measurement paired with its
own copy:

| Source | Copy | Max (ms) | Growth | `ceil(3 x max x growth)` |
|---|---|---|---|---|
| `m_atomic` | `gr_p1_atomic` | 1.147 | 1.46172 | **6** |
| `m_batch_100` | `gr_p1_b100` | 0.989 | 1.48121 | 5 |
| `m_batch_1000` | `gr_p1_b1000` | 1.056 | 1.62933 | **6** |
| `m_batch_250` | `gr_p1_b250` | 1.018 | 1.46875 | 5 |
| `m_batch_500` | `gr_p1_b500` | 0.916 | 1.46172 | 5 |
| `m_empty` | `gr_p1_empty` | 0.920 | 1.84314 | **6** |
| 5-trial rerun, `N_stale` = 1,646 | `gr_p1_sweep` | 1.070 | 1.84871 | **6** |

`MARGINED_MS_BACKFILL_REMAINING = 6` is **directly qualified and stays**: the
worst of seven independent measurements, each normalized on its own basis, is
exactly 6. It carries no page-count term, and nothing in `g-b-sweep-batch-cost`
touches it.

**The scalar that was there** — `MARGINED_MS_BACKFILL_SELECT_SWEEP = 37`, now
deleted. Every sweep run was on `gr_p1_sweep`, growth **1.84871**:

| Source | Pages | Max (ms) | `ceil(3 x max x 1.84871)` | vs the scalar 37 |
|---|---|---|---|---|
| run A, 5 trials, page size 100 | 18 | 6.46 | 36 | ✅ within, by 1 ms |
| run B, 3 trials, page size 100 | 18 | 5.96 | 34 | ✅ within |
| **run C, 7 trials, page size 100** | 18 | **6.94** | **39** | ❌ **breached** |
| run B, 3 trials, page size 25 | 67 | 12.02 | 67 | ❌ breached |
| run C, 7 trials, page size 25 | 67 | 16.16 | 90 | ❌ breached |
| run B, 3 trials, page size 1 | 1,647 | 257.05 | 1,426 | ❌ breached |
| **run C, 7 trials, page size 1** | 1,647 | **282.25** | **1,566** | ❌ breached |

**This retired the "qualified for page sizes ≥ 100" carve-out.** Two errors were
propping it up. The basis error above: with `gr_p1_sweep`'s own 5,414,912 bytes
the factor is 1.84871, not 1.62933, which alone moves run A from 32 to 36 — one
millisecond of headroom rather than five. And three trials: run C's seven trials
put the same page size at 39. So the scalar was not qualified at *any* page size
measured with enough trials to estimate a maximum. It was not "qualified over part
of its domain"; it was unqualified, and the domain finding was about *how badly*,
not *whether*.

**It was deleted rather than re-picked**, because a scalar cannot be repaired by
choosing a different scalar: there is no single value honest at page size 1 and
not absurd at page size 646, and the measured range spans 42x. Deleted rather than
kept at some value, too — a scalar left in the module is a scalar something will
go on to price a sweep with. `g-b-sweep-batch-cost` replaced it with
`MARGINED_MS_BACKFILL_SWEEP_SCAN` + `MARGINED_US_BACKFILL_SWEEP_PER_PAGE`, and
this table is that bead's acceptance evidence.

### How bad was it, and what the model changes

Every figure below is **final** — computed from the shipped
`MARGINED_MS_BACKFILL_SWEEP_SCAN = 72` and
`MARGINED_US_BACKFILL_SWEEP_PER_PAGE = 518`, with the units, margin and ceiling
placement decided above. An earlier version of this section carried a caveat block
saying they were provisional on exactly those decisions; the decisions are made
and the caveat is retired.

Every row beyond 1,647 pages **was** extrapolated when this section was first
written, and carried a reservation saying so. That reservation is now retired
too: `g-b-sweep-endpoint-measure` measured the domain out to 6,001 pages on a
production-shaped copy and the fit did not move. Two subsections down.

At the worst reachable configuration — `MIN_ADMITTED_BATCH = 1`,
`SIZED_TOTAL_ROWS = 6,000`, so `ceil(6000/1) + 1 = 6,001` pages, growth factors
1 — the model prices the sweep at `72 x 1.0 + 518 x 6001 / 1000` = **3,180.518
ms**. Note what is and is not scaled there: only the **scan component** carries
`g_sessions`. The per-page component is statement startup and is indifferent to
relation size. The deleted scalar's line multiplied the *whole* term by
`g_sessions`, and the two-component model deliberately does not inherit that.

**Import-time scan budget** — comfortable:

| | Charged | Bound |
|---|---|---|
| the deleted scalar | `42x521 + 6 + 20x(37+6)` = 22.748 s | |
| the model, at 6,001 pages | `42x521 + 6 + 20x(3180.518+6)` = **85.618 s** | `REVISION_DEADLINE_S` = 900 s |

**814.4 s of headroom**; it takes `SIZED_TOTAL_ROWS` at or above **84,609** to
breach. An earlier version of this section asserted the opposite — that
`MAX_PASSES` sweeps at batch size 1 could not fit the revision deadline — which
was written without doing the multiplication. **Nothing here forces raising the
minimum admitted batch size**, and `resolve_batch_size`'s
`MIN_ADMITTED_BATCH..MAX_BATCH_SIZE` range is left alone.

**Atomic stall projection** — this is where the term actually bites, and an
earlier version of this section got it wrong by comparing the sweep *contribution*
against the whole 30 s bound. `project_atomic_stall_ms` is seven terms, not one:
per-stale-row and per-repair-row mutation, three `session_moves` scans, the
coverage assertion, the sweep, the convergence count, and the teardown reserve. At
`N_stale = 6,000`, `N_repair = 0`, growth factors 1:

| Sweep term | Full projection | vs `MAX_WRITER_STALL_MS` = 30 s |
|---|---|---|
| the deleted scalar, 37 ms | 31.626 s | rejects |
| model at `DEFAULT_BATCH_SIZE` (7 pages) | 31.665 s | rejects |
| model at `MIN_ADMITTED_BATCH` (6,001 pages) | **34.770 s** | rejects, by more |

All three reject at 6,000, so the interesting quantity is not a single point but
where each *starts* rejecting. Sweeping `N_stale` under the same favourable
assumptions:

| | First `N_stale` rejected |
|---|---|
| model at `MIN_ADMITTED_BATCH` = 1 | **5,136** |
| model at `DEFAULT_BATCH_SIZE` = 1,000 | **5,668** |
| the deleted scalar 37 | **5,675** |

**The false-admission band is `5,136 … 5,674` at `batch_size = 1`** — populations
atomic mode admitted before this bead and refuses now — **narrowing to
`5,668 … 5,674` at the default batch.** That it is a *function of batch size* is
the whole point: the band exists because a scalar cannot see the variable that
moves it, and it sits just below the frozen `SIZED_TOTAL_ROWS = 6,000`. The
~58,511 figure an earlier version of this table carried is only where the sweep
term *alone* reaches 30 s; it is not an atomic-projection threshold and should not
be read as one.

So the severity was never an impending failure. It was that **an admission
projection whose dominant variable is missing from it cannot refuse an
inadmissible configuration** — its verdicts were a property of the current
relation size rather than of the check. Refusing the populations in the band is
the fix working, not a regression to mitigate.

Two arithmetic notes, kept because both errors are easy to repeat.
`ceil(3 x 257.05 x 1.629333…) = ceil(1256.4604) = 1257`; the **1,256** an earlier
version of this section read was a slip, and is superseded by the 1,426 above in
any case. And carry ratios unrounded into the `ceil`:
`10010624 / 6144000 = 1.629333…`, and writing it as `1.6292` rounds the growth
factor *down*, against the one direction it exists to protect. A factor rounded
down and then `ceil`-ed can only under-charge; the ceiling has to be last.

### The sweep domain, measured to the endpoint (`g-b-sweep-endpoint-measure`)

**The domain now reaches 6,001 pages — the exact page count the import-time
budget evaluates, and past the atomic rejection boundary near 5,137.** Nothing in
the section above rests on extrapolation any more, and the fit did not move.

`g-b-sweep-batch-cost` froze the pair from a domain stopping at 1,647 pages and
declared everything past it a linear extrapolation, because its primary remedy —
a second sweep-domain artifact on a copy grown to the endpoint — was believed
unexecutable: *"the 2026-07-24 dump is no longer on disk, and the 18.4 cluster
the Phase 1 copies lived in is gone."* **Both statements were wrong.** The dump
is at `tmp/ghostreplay-20260724T101501Z.dump` (21,645,384 bytes, TOC header
`Dumped from database version: 18.4`, `dbname: railway`, archived 2026-07-24
03:15:01 PDT) and the cluster is at `/opt/homebrew/var/postgresql@18` — stopped,
and holding nothing but its template databases, which is why the Phase 1 copies
were not found. Only the **copies** had been dropped. So the fallback was never
needed and the primary path ran.

#### `gr_p2_sweep6000`

The same dump, restored fresh into that 18.4 cluster (`--no-owner
--no-privileges`, port 5433). It came up as §0's fixture exactly:
`alembic_version` `20260720_01`, 4,184 `game_sessions`, 131,676 `session_moves`,
`ck_game_sessions_player_accuracy` `convalidated`. The CHECK is left as
production left it — `run_sweep_domain` never calls `time_validate`, and a
constraint's validation state cannot move a `SELECT`'s plan, so §7's
drop-and-re-add-`NOT VALID` step belongs to the atomic and batch runs, not to
this one.

```
--mode sweep-domain --synthesize-sessions 6000 --scan-trials 7
```

cloned 4,354 ended-visible rows and stamped the whole set stale: `N_stale` =
**6,000** exactly, 8,538 rows, 14,008,320 bytes. Ten batch sizes, **7 trials
each, every trial retained**, page counts agreeing across all seven trials and
with `backfill_sweep_pages` at every point (`agreed_sweep_pages`). 14.7 s wall
clock. [`docs/sizing/sweep_batch_domain_endpoint_20260725.json`](sizing/sweep_batch_domain_endpoint_20260725.json).

| Batch size | Pages | Max (ms) | Median (ms) | Min (ms) |
|---|---|---|---|---|
| 1 | **6,001** | **1,004.13** | 944.62 | 926.80 |
| 2 | 3,001 | 482.03 | 473.27 | 406.99 |
| 5 | 1,201 | 212.37 | 200.99 | 194.81 |
| 10 | 601 | 111.90 | 105.17 | 103.39 |
| 25 | 241 | 58.90 | 52.68 | 49.28 |
| 50 | 121 | 35.35 | 33.15 | 30.57 |
| 100 | 61 | 24.57 | 21.61 | 19.73 |
| 250 | 25 | 16.91 | 14.62 | 14.26 |
| 500 | 13 | 24.71 | 14.51 | 12.78 |
| 1,000 | 7 | 24.74 | 15.68 | 15.19 |

**Its `N_copy` is 1, and that is what makes it admissible.**
`max(1, 6000/8538, 10010624/14008320) = max(1, 0.7027, 0.7146)` — the copy is
*larger* than the frozen basis on both axes, so the clamp binds and its timings
enter the LP undiscounted, charging a bigger relation's cost against a smaller
basis. Contrast the fixture-scale probe below, whose relation sits far *below*
the basis: normalizing that one would multiply the scan coefficient by a factor
with no measurement behind it, which is why it does not enter the fit.

**The fit does not move.** Re-solved over both artifacts — 34 coverage points, 22
objective, `gr_p1_sweep` on `N_copy = 1222/661` and `gr_p2_sweep6000` on 1 — the
LP returns the same vertex, `a = 16170721723/677525000`, `b = 1414007/8200000`,
and the same two active constraints (run B at 4 pages, run C at 824). The only
number that changes is the objective's total over-charge, 89.77 ms → 276.58 ms,
which is 22 points being summed instead of 12. Not one point of the new basis
binds: at 6,001 pages the frozen pair models `72 x 1.0 + 518 x 6001 / 1000` =
**3,180.518 ms** against the `3 x 1,004.131` = 3,012.394 ms coverage requires, a
ratio of **1.056**.

So `MARGINED_MS_BACKFILL_SWEEP_SCAN = 72` and
`MARGINED_US_BACKFILL_SWEEP_PER_PAGE = 518` are unchanged. What changed is their
standing: the extrapolation turned out to be correct, and is no longer
load-bearing. `test_measured_sweep_domain_reaches_the_page_count_the_budget_charges`
now asserts the evidence reaches `IMPORT_WORST_CASE_SWEEP_PAGES` rather than
asserting that the shortfall is declared, and
`test_the_endpoint_basis_enters_the_fit_without_moving_it` pins the second basis's
`N_copy`, its retention, and the unchanged vertex.

**What this copy is valid for.** The sweep domain, and nothing else. Its 4,354
clones carry no `session_moves` rows, so every `session_moves`-scaled term and the
whole repair population on it are meaningless — the measurement records
`sessions_synthesized: true` and `--derive` hard-fails if it is offered as any
other kind. The sweep statement reads `game_sessions` alone, which is why it is
measured exactly here. The clones are of *production's own* ended-visible
sessions, so page width and predicate selectivity stay production's.

#### The same claim, re-checked on whatever host runs the gate

The above is one pair of copies on one machine. The *shape* the model assumes — a
per-page term that stays a per-page term as the page count grows — is a property
of the host, so it is also checked at fixture scale on every gate run. 6,000
cloned ended-visible rows, all stale, swept five times at each of eight batch
sizes, maxima:

| Batch size | Pages | Max (ms) | Marginal µs/page vs the next row |
|---|---|---|---|
| 1 | 6,001 | 1,026.05 | 175.7 |
| 2 | 3,001 | 498.99 | 161.9 |
| 5 | 1,201 | 207.52 | 41.4 |
| 10 | 601 | 182.67 | 259.5 |
| 25 | 241 | 89.25 | 377.5 |
| 100 | 61 | 21.30 | 165.0 |
| 500 | 13 | 13.38 | — (host outlier at 7 pages) |
| 1,000 | 7 | 39.27 | — |

End to end, 6,001 pages against 7: **164.6 µs/page**, against a fitted `b` of
172.4 µs/page and a frozen margined slope of 518 µs/page. The per-segment column
is noisy — a maximum over five trials on a laptop is — but it does not trend
upward with page count, which is the claim. **The slope does not degrade at the
endpoint on this host.**

That noise is worth naming rather than excusing: 41.4 next to 259.5 next to 377.5
is what subtracting *independently sampled maxima* produces, and it is precisely
why the test below does **not** compute its slopes this way. This table is
recorded as the manual probe it was.

This is a linearity check and nothing more, and it deliberately does **not** enter
the LP. The copy's rows are clones of small seeded fixtures rather than production
sessions, so its `game_sessions` relation is neither production-width nor
production-sized; normalizing its timings by `SIZED_SESSIONS_BYTES / <its bytes>`
would inflate the scan coefficient by a factor with no measurement behind it. A
timing may only be normalized by the basis of the copy it ran on, and this copy's
basis is not one the scan component can be stated against.

It is carried as a **test** rather than as this table:
`test_release_b_pg_runtime.py::test_pg_frozen_sweep_model_covers_the_import_worst_case_page_count`
grows the fixture the same way, sweeps the same eight batch sizes, and compares
**this host against itself** — the slope of each segment *beyond* 1,647 pages
against a reference slope measured *inside* it, with `MARGIN` as the tolerance.
Nothing in that comparison is a frozen constant, which is the point: three earlier
forms failed, and the first two failed by comparing this host to one.

| Form | Why it fails as a linearity test |
|---|---|
| `model(6001) >= 3 x observed` | The model IS 3x a fit, so this reduces to `fitted_slope >= this host's slope` — two machines, zero margin. Written first; flipped between passing and failing on repeated runs of the same fixture. |
| `marginal_slope <= 518 µs/page` | The same defect one step removed. A perfectly linear host at 600 µs per statement fails it; a genuinely nonlinear host under 518 passes. |
| one marginal slope over 1,201 → 6,001 | Averages a late spike away over 4,800 pages — and a late spike is the shape a failure of linearity would actually take. |
| segmented, but subtracting per-point **maxima** | Segmenting the interval does not make the arithmetic a slope. Each maximum came from an unrelated trial, so one slow reading at a segment's low end *suppresses* that segment, and one in the reference range *inflates* the budget — both errors point the same way, toward passing a real late nonlinearity. |
| paired and median-reduced, but always swept in the **same order** | Position-in-round is then perfectly confounded with page count. Pairing and medians cannot remove it: it is the same bias in every round, not noise, so repeating the run — or the whole gate — does not average it away. |

So the sweeps are **interleaved** — all eight batch sizes, one warm-up round
discarded, six rounds retained — and every slope subtracts two readings **from the
same round**, then takes the **median** over rounds. Pairing makes each slope
describe one machine state; the median makes it immune to a single outlier on
either side. Segments narrower than 500 pages are excluded outright: at 6 pages
apart, per-trial noise *is* the numerator.

The order **reverses every round** (boustrophedon), deterministically rather than
by shuffling, so the run stays reproducible and pairing is untouched — every size
still runs exactly once per round. With an *even* number of retained rounds each
size occupies each end equally often, so all of them share one mean position and a
monotone within-round drift cancels in the median instead of accumulating. The
helper asserts that balance (`rounds` even, summed positions identical across
sizes) rather than claiming it in prose.

That correction was not cosmetic: fixing the order had visibly moved the readings.
Under a fixed ascending order the in-domain reference measured 126–135 µs/page and
the 3,001 → 6,001 segment 120–138; balanced, they read 117–122 and 107–116. The
spread between segments narrowed from ~9% to ~4% of the reference.

The beyond-domain side is then checked **segment by segment** (1,201 → 3,001 and
3,001 → 6,001). The in-domain reference pools every in-domain pair at least 500
pages wide, across all rounds, and takes the median of the pool — under the linear
model each such pair estimates the same per-page cost, since the fixed per-sweep
overhead cancels in a difference, so pooling is more data for one quantity rather
than an average of several. Median rather than maximum, because a maximum over
pairs is the inflated reference the row above describes.

Absolute coverage is a **separate** assertion, against the frozen pair rather than
against this host, and it uses the **worst** round at 6,001 pages: coverage is a
claim about the tail, a slope is not.

Observed across four consecutive runs on this host:

| | run 1 | run 2 | run 3 | run 4 |
|---|---|---|---|---|
| in-domain reference (µs/page) | 121.9 | 116.9 | 120.9 | 121.9 |
| 1,201 → 3,001 (µs/page) | 115.5 | 120.9 | 119.5 | 118.9 |
| 3,001 → 6,001 (µs/page) | 116.2 | 113.1 | 107.3 | 112.0 |
| endpoint worst round (ms) | 941.69 | 751.28 | 707.32 | 718.02 |

Every beyond-domain median lands within ~4% of the in-domain reference against a
tolerance of 3x, and the endpoint's worst round is covered by a modelled 3,180.52
ms. Worth recording how much tighter this is than the earlier max-subtracting form
managed on the same machine — 163.7 / 177.1 µs/page against a 403 µs/page budget.
Pairing, the median and the balanced order removed noise and bias; they did not
add slack. The headroom above is a property of the host, not of the sampling.

**What this does not establish.** Linearity *on this host*, on a fixture of clones,
and nothing more. Its timings are not evidence for the frozen pair and never enter
the LP — the constants' docstrings, `SPEC.md` and the gate manifest all say so.
`gr_p2_sweep6000` is where the endpoint became a measured claim about the shipped
constants; this is where that claim keeps being re-checked on hosts nobody sized.

The test is pinned in `pg_gate_plugin.REQUIRED_PG_GATE_TESTS`, because a gate
whose whole job is to run on unsized hosts is worth nothing if it can silently
stop being collected, and so is
`test_pg_synthesize_sessions_establishes_the_stale_population_it_promises` — the
endpoint is only the endpoint if the population really is `SIZED_TOTAL_ROWS`
stale rows, and `--synthesize-sessions` targets that population directly
(cloning against the ended-visible predicate, not `count(*)`, and stamping
originals as well as clones) rather than leaving it to a separate flag.

### Full derived table, and why it is *not* frozen

`--derive` over the eight measurements emits every constant. Recorded for the
qualification bead, **not** applied to the revision:

> **This table is a TRANSCRIPTION and cannot be regenerated.** None of the eight
> measurements it was derived from was committed; the copies were dropped and the
> run cannot be re-executed. §8 re-measures the same fixture, commits every
> artifact, and finds two rows of this table wrong — the sweep pair below is at
> the *shipped* basis while every other row is at this run's, and the scan
> coefficient at this run's basis is 71 or 44, not 72. See
> [what the artifacts caught](#what-the-artifacts-caught-two-mixed-basis-rows-in-7s-table).
> Nothing frozen moved: the shipped pair is the LP at the shipped basis, and
> always was.

| Constant | §3 frozen (15.18) | This run (18.4) |
|---|---|---|
| `SIZED_TOTAL_ROWS` | 6,000 | 4,184 |
| `SIZED_SESSIONS_BYTES` | 10,010,624 | 6,144,000 |
| `SIZED_M_TOTAL` | 357,000 | 130,676 |
| `SIZED_MOVES_BYTES` | 93,241,344 | 45,817,856 |
| `MARGINED_MS_PER_ROW` | 5 | 5 |
| `MARGINED_MS_PER_REPAIR_ROW` | 2 | 2 |
| `MARGINED_MS_PER_SCAN_STMT` | 521 | 164 |
| `MARGINED_MS_COVERAGE_ASSERT` | 6 | 4 |
| `MARGINED_MS_BACKFILL_REMAINING` | 6 | 4 |
| `MARGINED_MS_BACKFILL_SWEEP_SCAN` | 72 | ~~72~~ — **wrong: the shipped value restated.** At this run's own basis the same evidence gives 71, or 44 from the baseline artifact alone (§8) |
| `MARGINED_US_BACKFILL_SWEEP_PER_PAGE` | 518 | 518 — right, but by coincidence: `b` is basis-independent for the baseline-alone fit, so this *is* what the run emitted |
| `SCAN_STMT_TIMEOUT_MS` | 521 | 164 |
| `MAX_SINGLE_SESSION_COMPUTE_MS` | 79 | 68 |
| `TEARDOWN_ALLOWANCE_MS` | 7 | 7 |
| `MARGINED_MS_ATOMIC_TEARDOWN_FIXED` | 2 | 4 |
| `MARGINED_US_ATOMIC_TEARDOWN_PER_ROW` | 2 | 2 |
| `MAX_BATCH_SIZE` / `DEFAULT_BATCH_SIZE` | 1,000 | 646 |
| `REPAIR_BATCH_SIZE` | 2,500 | 1,000 |
| `EST_MAX_LOCK_HOLD_MS` | 5,007 | 5,007 |

Decision 1 re-runs to the same verdict: `T_stall_prod = 1,578.7 ms`, margined
4,736.0 ms against `MAX_WRITER_STALL_MS = 30,000` — **atomic**.

Three reasons this table is recorded rather than applied, all of which the
qualification bead has to clear first:

1. **The `SIZED_*` this run recorded are post-synthesis.** `_run_atomic` reads
   `dimensions_before` at the start of the measured pass, and `main()` runs
   synthesis *before* that — so `SIZED_SESSIONS_BYTES` is 6,144,000 against
   4,096,000 pre-synthesis (the stale UPDATE's dead tuples), and `SIZED_M_TOTAL`
   is 130,676 against production's 131,676 (repair synthesis deletes one ply from
   each of `K` sessions).

   This is a **provenance** problem, not an unsafe number. Those *are* the
   dimensions the timed statements actually saw, so each timing is correctly
   paired with them; what is missing is the pre-synthesis reading beside it, and
   the record of which one each timing belongs to. Swapping in pre-synthesis
   dimensions **without rebasing the timings** would be strictly worse — a
   measurement taken against 6,144,000 bytes, divided by a 4,096,000-byte basis,
   is mismatched provenance dressed as a correction. The fix is to record both
   and keep every timing with its own basis — done 2026-07-26 under
   `g-b-size-harness-defects`, see
   [the two dimension readings](#the-two-dimension-readings-g-b-size-harness-defects).
2. **The byte dimensions are not production's at all**, synthesized or not — §0.
   A logical dump does not carry a physical footprint.
3. **`MAX_BATCH_SIZE` and `REPAIR_BATCH_SIZE` are fixture-bounded**, per the
   ceiling above. `min(formula, tested)` is behaving exactly as designed; it is
   the fixture that cannot demonstrate 1,000, not the cost.

Applying two rows of this table while the `SIZED_*` rows stay at §3's values
would still be wrong, though not for the reason first given here. `_growth_factor`
(`20260719_01:1506`) is `max(1.0, byte ratio, row ratio)` — **clamped at 1.0**, so
a shrunk relation earns no discount and applying `4`/`15` would *not* charge 0.70
against today's relation; it would charge them in full. The real gap is the blind
spot the mismatch opens above today's size: with the terms measured at 4,184 rows
but the basis still declaring 6,000, the relation can grow all the way from 4,184
to 6,000 — a 1.43x increase in exactly the quantity these terms scale with —
while the growth factor sits pinned at 1.0 and charges nothing extra. A frozen
term and the basis it was measured against have to move together.

### The two dimension readings (`g-b-size-harness-defects`)

**Fixed 2026-07-26.** The harness synthesizes its populations *before* the
measured pass, so the relation the timed statements run against is not the one
the copy arrived as. `synthesize_stale` UPDATEs every ended-visible row and
leaves the dead tuples, `synthesize_repair` DELETEs one ply from each of K
sessions, `synthesize_sessions` clones rows outright. On the production restore:

| | post-synthesis | pre-synthesis |
|---|---|---|
| `SIZED_SESSIONS_BYTES` | 6,144,000 | 4,096,000 |
| `SIZED_M_TOTAL` | 130,676 | 131,676 |

**The post-synthesis reading was never the wrong one.** It is the relation the
timed statements ran against, so every timing is correctly paired with it, and
`SIZED_*` stays frozen from it — a term and its declared basis have to move
together. The defect was **provenance**: it was the only reading recorded, and
nothing marked which one it was.

What each run now emits, for every kind — atomic, batch, cancel probe, sweep
domain:

```json
"timing_basis": "post_synthesis",
"dimension_bases": {
  "pre_synthesis":  {"status": "measured", "total_rows": 4184, "sessions_bytes": 4096000, ...},
  "post_synthesis": {"status": "measured", "total_rows": 4184, "sessions_bytes": 6144000, ...}
}
```

`dimensions_before` keeps exactly the meaning it always had — the reading taken
at the start of the measured pass. What changed is that it is now labelled and
**checkable**: `--derive` refuses an artifact whose `post_synthesis` reading
disagrees with it. That check is the one worth having. Substituting the
pre-synthesis reading while leaving the timings alone reads as a correction and
is not one — it divides a statement timed against 6,144,000 bytes by a
4,096,000-byte basis, and nothing downstream can tell.

It is **not** refused for being optimistic. The error runs in *both* directions,
depending on which side of a ratio the substituted reading lands on:

| reading substituted downward | factor | effect |
|---|---|---|
| a **sweep copy's** own | `N_copy = frozen / copy` rises | that point **over**-charged |
| the **frozen basis** | every `N_copy` falls | the whole fit **under**-charged |
| the **frozen basis**, at run time | `g_sessions = live / SIZED` rises | scan terms **over**-charged |

So the guard holds because a timing and its basis have to move together, not
because either direction is safe. The pre-synthesis reading is emitted under
`scaling.measurement_bases` and divided by **nowhere**; carrying a timing onto it
is a separate, explicit step, and this is the evidence such a step would start
from.

Two further rules, both fail-closed:

- **`SIZED_*` may not be frozen from a copy whose displacement is unrecorded.**
  Only on the full-restore path, where the basis falls back to the atomic
  snapshot's own dimensions. This is the blind spot described above: a basis
  inflated by synthesis and frozen without that being visible leaves
  `_growth_factor` pinned at 1.0 across the whole gap.
- **A basis is never inferred** — not from prose, not from a filename, not from a
  sibling copy, and not by inverting the growth factor.

**The two committed sweep artifacts were migrated, not re-measured.** Their
`dimensions_before` *is* an accurate post-synthesis reading, which is all the
sweep fit needs, so both keep their points and their `N_copy` and both remain
active constraints. Their pre-synthesis reading was never taken and is recorded
as `status: "not_recorded_legacy"` rather than reconstructed — for `gr_p1_sweep`
it is genuinely unrecoverable, since readings of the same restore moved
(4,079,616, then 4,096,000 once autovacuum materialised the FSM/VM forks) before
any synthesis ran. `--derive` reports them as incomplete in
`sweep_copy_growth_factors[].pre_synthesis_recorded`. Neither supplies `SIZED_*`.

Regression tests in `test_release_b_sizing.py` cover the intake, the substitution
guard on both an atomic and a sweep artifact, the `SIZED_*` gate, the completeness
of its `--production-dimensions` escape (empty, populations-only, partial, and
zero-dimension declarations each refused), and the migration of both shipped
artifacts; plus
`test_release_b_pg_matrix.py::test_pg_the_harness_records_the_reading_its_synthesis_moved`,
PostgreSQL-gated and pinned in the manifest, which asserts the delta against
`synthesize_repair`'s exact displacement — one ply per corrupted session, rather
than a byte figure that would test the host's vacuum state.

**Defect #2 of that bead — the sweep measured at one arbitrary point — was
closed earlier**, by `g-b-sweep-batch-cost` (the domain sweep, the
`MIN_SWEEP_TRIALS = 7` floor enforced at generation, retained raw trials, and the
removal of the sweep from `time_scan_statements`) and `g-b-sweep-endpoint-measure`
(the maximum population, via `--synthesize-sessions`). Nothing was added for it
here.

### Harness defect found and fixed under this bead

`synthesize_stamped` stamped **every** ended-visible row with accuracy 100,
including rows whose ply grid is broken — which manufactures repair candidates
(version 1 + non-NULL accuracy + broken grid) instead of clearing them. Its
docstring claimed it stamped "over grids that are NOT broken"; the SQL had no
such filter. Invisible on any fixture with intact grids, which is every fixture
in the repo and the entire Phase 1 snapshot. Against a production restore it
turned the empty teardown point into a 10-row mutation — production's 10 real
broken grids — and `--derive` correctly rejected the whole derivation.

Fixed: a broken grid is now stamped version 1 with a **NULL** accuracy, which is
what the fail-closed backfill itself writes for such a row, and an intact grid
gets 100. Regression test:
`test_release_b_pg_matrix.py::test_pg_synthesize_stamped_empties_both_populations_with_broken_grids_present`,
PostgreSQL-gated and pinned in the gate manifest. It fails against the previous
SQL and passes against the fix.

---

## 8. The measurements are on disk (`g-b-size-measurement-json`, 2026-07-26)

**Every derived constant above this line except the sweep pair was a
transcription.** `--derive` consumes one JSON per Phase 1 run — eight for §7's
derivation, ten for this one — and exactly two of them were ever committed: the
sweep domains. So the sweep model was the only constant with a re-runnable path
from evidence to literal, and §7's derivation as a whole could not be
re-executed. The other rows of §3 and §7 were tables in a runbook with no
artifact behind them, which is the same defect the withdrawn OLS line already
suffered one level up: only the maxima had been kept, so nothing could be checked
against its inputs.

This is a claim about the DERIVED sizing constants, and only those. The
admission gate, the backfill itself and the Phase 0 verification are executable
and tested elsewhere; §§1–6 are not in question here.

The measurement set below closes that for the derivation as a whole. It is a
**new run**, not a recovery: the 2026-07-25 copies were dropped and the numbers
in §7 above stay what they are, a transcription of a run whose inputs are gone.

### What is committed

| File | Kind | What it measures |
|---|---|---|
| [`docs/sizing/atomic_full_20260726.json`](sizing/atomic_full_20260726.json) | `atomic` | Phase 1a — the full teardown point, `N_mut_snap = 1,646` |
| [`docs/sizing/atomic_empty_20260726.json`](sizing/atomic_empty_20260726.json) | `atomic` | Phase 1a' — the empty teardown point, own fresh copy, `VALIDATE` executed |
| [`docs/sizing/batch_b100_r200_20260726.json`](sizing/batch_b100_r200_20260726.json) | `batch` | Phase 1b candidate, backfill 100 / repair 200 |
| [`docs/sizing/batch_b250_r500_20260726.json`](sizing/batch_b250_r500_20260726.json) | `batch` | candidate, 250 / 500 |
| [`docs/sizing/batch_b500_r1000_20260726.json`](sizing/batch_b500_r1000_20260726.json) | `batch` | candidate, 500 / 1,000 |
| [`docs/sizing/batch_b1000_r2000_20260726.json`](sizing/batch_b1000_r2000_20260726.json) | `batch` | candidate, 1,000 / 2,000 — the fixture ceiling |
| [`docs/sizing/cancel_probe_batch_20260726.json`](sizing/cancel_probe_batch_20260726.json) | `cancel_probe` | Phase 1c, **batch** scope, 1,000 rows locked |
| [`docs/sizing/cancel_probe_atomic_20260726.json`](sizing/cancel_probe_atomic_20260726.json) | `cancel_probe` | Phase 1c, **atomic** scope, 1,646 rows locked |
| [`docs/sizing/sweep_batch_domain_20260725.json`](sizing/sweep_batch_domain_20260725.json) | `sweep_domain` | `gr_p1_sweep`, §7 — unchanged |
| [`docs/sizing/sweep_batch_domain_endpoint_20260725.json`](sizing/sweep_batch_domain_endpoint_20260725.json) | `sweep_domain` | `gr_p2_sweep6000`, §7 — unchanged |
| [`docs/sizing/derived_20260726.json`](sizing/derived_20260726.json) | — | `--derive`'s own output over all ten |

**A filename in `docs/sizing/` is a selector, not a label.** The sweep artifacts
are chosen by globbing `sweep_*.json`, so a measurement whose name could match
that glob without being a sweep domain would silently enter the fit. Every file
there is named `<kind>_…_<date>.json` for the `kind` it declares, and
`derived_…` is reserved for a derivation's output, which is not a measurement and
carries no `kind`.
`test_docs_sizing_holds_measurement_artifacts_named_for_their_kind` enforces that
over **every** file in the directory, not just the ones this section lists: it
refuses a name and a `kind` that disagree, refuses a name with no kind prefix at
all, and refuses a `derived_…` file carrying a `kind`.

Membership is the separate question, and the directory is not the answer to it.
Which artifacts a derivation stands on is fixed by the table above, mirrored in
the test as `_COMMITTED_DERIVATION_SET`; all the directory has to satisfy is that
those files are still present. Adding artifacts is therefore expected and
allowed — Phase 3 measures into this same directory — and they belong in a set
and a `derived_…` of their own rather than appended to this one, because the
atomic and batch timings of a derivation have to come from a single fixture
state. The sweep domains are the exception the harness already handles: they are
fitted on their own copies' bases via `N_copy`, which is how the 2026-07-25 pair
sits in a 2026-07-26 derivation without mixing readings.

### The fixture, and re-running it

The same 2026-07-24 dump and the same 18.4 cluster as §7 —
`tmp/ghostreplay-20260724T101501Z.dump` restored with `postgresql@18`'s own
`pg_restore` (`--no-owner --no-privileges`, port 5433). It came up as §0's
fixture exactly: `alembic_version` `20260720_01`, 4,184 `game_sessions`, 131,676
`session_moves`, 1,646 ended-visible, `ck_game_sessions_player_accuracy`
`convalidated`. One disposable copy per run, cloned `CREATE DATABASE … TEMPLATE`;
each atomic and batch copy drops and re-adds the CHECK `NOT VALID`
(`20260709_01`'s condition verbatim), reconstructing the pre-`20260719_01` state.
The two probe copies do not — the probe never calls `time_validate`.

Populations come from the harness's own synthesis, `K = 1000`, which leaves
`N_stale = 646` on production's 1,646 ended-visible sessions: the same fixture
ceiling §7 records, and the same reason `B_TESTED` is not frozen.

Phase 2 is pure arithmetic over the committed files and needs no database, so it
re-runs from a clean checkout:

```
python backend/scripts/size_accuracy_backfill.py --derive \
  --measurement docs/sizing/atomic_full_20260726.json \
  --measurement docs/sizing/atomic_empty_20260726.json \
  --measurement docs/sizing/batch_b100_r200_20260726.json \
  --measurement docs/sizing/batch_b250_r500_20260726.json \
  --measurement docs/sizing/batch_b500_r1000_20260726.json \
  --measurement docs/sizing/batch_b1000_r2000_20260726.json \
  --measurement docs/sizing/cancel_probe_batch_20260726.json \
  --measurement docs/sizing/cancel_probe_atomic_20260726.json \
  --measurement docs/sizing/sweep_batch_domain_20260725.json \
  --measurement docs/sizing/sweep_batch_domain_endpoint_20260725.json \
  --out docs/sizing/derived_20260726.json
```

Run from the repo root, and with those relative paths: `main` labels every
artifact with the path it was handed, so the emitted provenance — and therefore
the committed output — is a function of these strings as well as of the files.
`test_the_committed_measurement_set_re_derives_its_published_output` re-runs
exactly this and compares the serialized payload, so an edited artifact, a changed
formula or a reordered input set fails the gate.

### The two dimension readings, and the measured inputs

On the atomic full point, the copy `SIZED_*` is frozen from:

| | pre-synthesis | post-synthesis (`timing_basis`) |
|---|---|---|
| `count(*) game_sessions` | 4,184 | 4,184 |
| `pg_total_relation_size('game_sessions')` | 4,096,000 | 6,144,000 |
| `count(*) session_moves` | 131,676 | 130,676 |
| `pg_total_relation_size('session_moves')` | 45,817,856 | 45,817,856 |

Both are recorded on every artifact, `g-b-size-harness-defects`'s fix working as
designed on a run that was measured after it: the 2,048,000-byte displacement is
`synthesize_stale`'s dead tuples, the 1,000-row one is `synthesize_repair`
deleting a ply from each of `K` sessions. `SIZED_*` is frozen from the
post-synthesis reading, which is what the timed statements ran against.

| Measurement | Value |
|---|---|
| `N_stale` / `N_repair` / `M_moves` (of the stale set) | 646 / 1,000 / 43,009 (66.58 mean plies) |
| `VALIDATE CONSTRAINT` (full point / empty point) | 0.895 ms / 0.947 ms |
| Backfill total (select + load + compute + guarded update), 646 sessions | 989.6 ms |
| `per_row_snap` | 1.532 ms/session |
| `max_single_session_compute_ms` (n = 646) | 20.32 ms (median 1.106 ms) |
| `T_repair_per_candidate`, scans excluded (n = 1,000) | median 0.360 ms (max 1.491 ms) |
| `T_atomic_teardown_empty` — `COMMIT` mutating **nothing**, own fresh restore | 1.200 ms |
| `T_atomic_teardown_full` — `COMMIT` at `N_mut_snap` = 1,646 | 1.173 ms |
| `max_batch_commit_ms` — both phases, across all four candidates | 0.760 ms |
| `max_batch_cancel_to_unlock_ms` — batch scope, 20 trials, 0 discarded, 1,000 rows locked | **1.962 ms** |
| `rollback_only_teardown_ms` beside it | 0.184 ms |
| `max_atomic_cancel_to_unlock_ms` — atomic scope, 20 trials, 0 discarded, 1,646 rows locked | **2.424 ms** |
| `rollback_only_teardown_ms` beside it | 0.173 ms |

The empty point's `COMMIT` came in **above** the full point's — 1.200 ms against
1.173 ms — which is what a 27 µs difference between two single-sample commits
looks like on a laptop, and it does not corrupt the slope: the full point's
teardown is `max(commit, atomic cancel-to-unlock)` = 2.424 ms, because an atomic
run that breaches rolls back the whole population. The slope is
`(2.424 − 1.200) / 1,646` = 0.744 µs/row. It is worth naming rather than
smoothing, because it is the direct evidence that a *single* commit sample cannot
resolve the floor from the slope at this transaction size, and that the frozen
pair rests on the cancel path instead.

Scan-bearing statements, 5 trials each at `N_repair` = 1,000:

| Statement | Cold (ms) | Max (ms) | Median (ms) |
|---|---|---|---|
| `REPAIR_POPULATE_SQL` | 54.88 | **56.82** | 54.65 |
| `REPAIR_REMAINING_SQL` | 52.66 | 52.83 | 51.92 |
| `SOUNDNESS_ASSERT_SQL` | 53.29 | 53.29 | 52.31 |
| repair population count (pre-flight) | 52.16 | 53.17 | 52.16 |
| — bare `PLY_DETECTOR_SQL` *(diagnostic only, never priced)* | 50.92 | 52.87 | 51.86 |
| `COVERAGE_ASSERT_SQL` | 1.29 | **1.29** | 0.86 |
| `BACKFILL_REMAINING_SQL` | 1.21 | **1.21** | 1.02 |

`scan_plan_inversion` is **false** (complete statements 52.8–56.8 ms, bare
detector 52.9 ms), the design's stated relationship holding.

Four candidates, one fresh copy each, both phases per run:

| Backfill batch: requested | demonstrated | Observed max single batch | `3x` | Passes |
|---|---|---|---|---|
| 100 | 100 | 162.8 ms | 488 ms | ✅ |
| 250 | 250 | 390.1 ms | 1,170 ms | ✅ |
| 500 | 500 | 771.1 ms | 2,313 ms | ✅ |
| 1,000 | **646** | 960.9 ms | 2,883 ms | ✅ ← `B_tested`, fixture-bound |

| Repair batch: requested | demonstrated | Observed max single batch | `3x` | Passes |
|---|---|---|---|---|
| 200 | 200 | 90.9 ms | 273 ms | ✅ |
| 500 | 500 | 206.2 ms | 619 ms | ✅ |
| 1,000 | **1,000** | 465.2 ms | 1,396 ms | ✅ ← `R_tested`, fixture-bound |
| 2,000 | 1,000 | 383.5 ms | 1,150 ms | ✅ |

Both `_tested` values are the **demonstrated page cardinality**, not the requested
`LIMIT`, and both are bounded by the fixture rather than by the deadline: 646 is
the whole stale population and 1,000 the whole repair population.

### The derived table, and why it is still *not* frozen

| Constant | §3 frozen (15.18) | This run (18.4), artifact-backed |
|---|---|---|
| `SIZED_TOTAL_ROWS` | 6,000 | 4,184 |
| `SIZED_SESSIONS_BYTES` | 10,010,624 | 6,144,000 |
| `SIZED_M_TOTAL` | 357,000 | 130,676 |
| `SIZED_MOVES_BYTES` | 93,241,344 | 45,817,856 |
| `MARGINED_MS_PER_ROW` | 5 | 5 |
| `MARGINED_MS_PER_REPAIR_ROW` | 2 | 2 |
| `MARGINED_MS_PER_SCAN_STMT` | 521 | 171 |
| `MARGINED_MS_COVERAGE_ASSERT` | 6 | 4 |
| `MARGINED_MS_BACKFILL_REMAINING` | 6 | 4 |
| `MARGINED_MS_BACKFILL_SWEEP_SCAN` | 72 | **71 — at this run's basis, not a second reading of 72; see below** |
| `MARGINED_US_BACKFILL_SWEEP_PER_PAGE` | 518 | **491 — likewise** |
| `SCAN_STMT_TIMEOUT_MS` | 521 | 171 |
| `MAX_SINGLE_SESSION_COMPUTE_MS` | 79 | 61 |
| `TEARDOWN_ALLOWANCE_MS` | 7 | 6 |
| `MARGINED_MS_ATOMIC_TEARDOWN_FIXED` | 2 | 4 |
| `MARGINED_US_ATOMIC_TEARDOWN_PER_ROW` | 2 | 3 |
| `MAX_BATCH_SIZE` / `DEFAULT_BATCH_SIZE` | 1,000 | 646 |
| `REPAIR_BATCH_SIZE` | 2,500 | 1,000 |
| `EST_MAX_LOCK_HOLD_MS` | 5,007 | 5,006 |

Decision 1 re-runs to the same verdict: `T_stall_prod = 1,548.7 ms`, margined
4,646.0 ms against `MAX_WRITER_STALL_MS = 30,000` — **atomic**. At the minimum
admitted batch size it is 1,654.1 ms margined to 4,962.2 ms, still atomic.

The three reasons §7 gives for recording rather than applying its table all still
hold, minus the one `g-b-size-harness-defects` closed: the byte dimensions are
not production's (§0), and `MAX_BATCH_SIZE` / `REPAIR_BATCH_SIZE` are
fixture-bounded. The pre-synthesis reading is now recorded, so that objection is
gone; what replaces it is the general rule the sweep rows below make concrete —
**a frozen term and the basis it was measured against have to move together**, and
this run's basis is not the shipped one.

### What the artifacts caught: two mixed-basis rows in §7's table

The sweep coefficients are solved in **frozen-basis coordinates**, so both are a
function of the basis the same derivation freezes. Over the same two sweep
artifacts:

| Basis | Sweep evidence | `a` (ms) | `b` (µs/page) | Margined |
|---|---|---|---|---|
| shipped `SIZED_*`, 6,000 / 10,010,624 | both | 23.867343 | 172.440 | **72 / 518** |
| this restore, 4,184 / 6,144,000 | both | 23.592979 | 163.396 | **71 / 491** |
| this restore, 4,184 / 6,144,000 | `gr_p1_sweep` alone | 14.648533 | 172.440 | **44 / 518** |

[§7's full derived table](#full-derived-table-and-why-it-is-not-frozen) lists
**72** and **518** in a column whose every other row is at that run's own basis of
4,184 / 6,144,000. At that basis the sweep evidence gives 71 (both artifacts) or
44 (the baseline alone) — never 72, which is the value at the shipped basis. The
per-page slope reads as consistent for a reason worth stating: `b` is
basis-independent for the baseline-alone fit, so **518** is what that run would
have emitted while **72** is not.

That is a transcription defect, not a derivation defect — no constant moved, and
the shipped pair is and remains the LP at the shipped basis, which
`test_frozen_sweep_model_matches_the_published_envelope` has re-derived from
committed evidence throughout. It is recorded here because it is exactly the
failure the bead predicted: a table that cannot be regenerated cannot be checked,
and the row that was wrong was the one row whose inputs happened to be on disk.

### What this does and does not make reproducible

**Does.** Every term `--derive` computes, from evidence in the repo, on any
checkout, with the gate failing closed if an artifact or a formula changes
underneath the published output.

**Does not.** The §3 literals the revision actually ships. They were measured on
PostgreSQL 15.18 against a locally synthesized 6,000-row snapshot that no longer
exists, and no run on any other fixture can return them — this one included. The
sweep pair remains the sole exception, because its inputs were committed and its
solver is pure. A test cannot fail closed when someone edits
`MARGINED_MS_PER_SCAN_STMT` without re-measuring; what it can now do, and does, is
fail closed when the *artifact-backed* table drifts from the artifacts. Closing
the remaining gap means re-freezing the shipped constants from a committed
measurement set, which is `g-b-sizing-harness` (Phase 3) and its from-scratch
scope in §5.
