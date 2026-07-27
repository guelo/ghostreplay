# Analysis benchmark results

Committed JSONL from the two-search grading benchmark
(bead `g-two-search-grade` §10; the browser half is `g-grade-device-runner`).

Both harnesses write the SAME schema — `src/bench/benchRecord.ts`,
`BENCH_SCHEMA_VERSION` — so a device run and a Node corpus run are directly
comparable:

- **browser device runner** (`src/bench/device/`, this bead) — the only source of
  iPhone/Safari and Android/Chrome timing. Loads the actual bundled
  `analysisWorker` and measures the shipping orchestration.
- **Node corpus harness** (`g-grade-corpus-harness`, not yet built) — correctness
  vectors, the 200-position corpus, and the depth-26/27 references.

Never quote Node timing as mobile evidence (§10.1).

## Files

| File | What it is |
|---|---|
| `device-baseline-desktop-chromium-*.jsonl` | Desktop control, current protocol, 40-ply thermal sequence, 3 repeats, 60s between blocks |
| `device-baseline-cold-desktop-chromium-*.jsonl` | Desktop control, smoke set, `--mode cold` (fresh worker per measurement) |
| `device-baseline-warm-desktop-chromium-*.jsonl` | Desktop control, smoke set, `--mode sequence --warmup` — the warm pair for the file above |

Mobile baselines are added here as they are captured; each file names its device
in the `run` record's `device.label`.

## Is a file evidence? Read three fields first

Every run states its own validity, so this never has to be reconstructed from
memory:

- **`summary.completion`** — `complete` or `stopped`. `stopped` means the SCHEDULE
  was abandoned, not merely that Stop was pressed: pressing it during the final
  measurement still finishes the plan and still reads `complete`. A stopped run's
  medians look entirely ordinary over whatever fraction of the schedule it
  reached, so only `complete` is quotable.
- **`summary.methodWarnings`** (also in the run header, minus the outcome ones) —
  every departure from §10.4's method, in words: too few repeats, too short a
  thermal sequence, an uncounterbalanced arm order, no cooldown between blocks (or
  one too short to be one), a dev build, a depth override, an unidentified or dirty
  bundle. An empty array means the run is method-valid.
- **`run.source`** — `gitRevision`, `gitDirty`, and the worker chunk's filename and
  sha256 when a scripted run recorded them. The revision is injected at build
  time, so hand-run phone captures carry it too. A file that cannot name the
  orchestration bytes it measured cannot be compared against a later one.

A file with **no `summary` row at all** is a run that crashed rather than
finished — the rows it does contain are diagnostics, not a measurement.

Warnings are for a run that *departed* from the method. A configuration that
could not be applied AS ASKED is REFUSED before the first measurement
(`src/bench/device/config.ts`), because such a value applies as silence rather
than as an error:

- a non-numeric cooldown — `NaN > 0` is false, so it would neither sleep nor
  warn, and would serialize into the plan as `null`;
- an unknown mode or position set — each would run as the other branch of a
  two-way test while the header recorded the string it was given;
- a repeated arm — doubles that arm's blocks while `armOrderBalanced` still calls
  the rotation balanced;
- more thermal plies than the stored game has (60) — the set builder caps rather
  than refuses, so `--plies 500` would measure 60 and say nothing;
- `--warmup` with `--mode cold` — every cold measurement already gets a fresh
  worker, so the schedule drops the warm-up while `plan.warmup: true` would claim
  a priming row the file does not contain;
- a missing device label — the header could not name the hardware it measured.

The same guard runs on all three paths into a run — the CLI driver's flags, the
browser page's form, and `runBench` itself. For the form that takes two things,
because a substituted value never reaches a guard at all. The page reads its
number fields without a fallback (`Number(field.value) || 40` answers a typo with
a plausible number, which the header then records as the requested one), and its
numeric controls are `type="text" inputmode="numeric"` rather than
`type="number"` — a number input discards an unreadable entry before any script
sees it, leaving `.value === ''`, which is exactly what an untouched field looks
like. The browser's own `min`/`max` never fire either, since the page starts the
run from a `type="button"`. `form.test.ts` types garbage into the real
`bench/device/index.html` and asserts the run is refused.

The desktop control files here were captured from a working tree that still had
the runner uncommitted, so they carry the dirty-tree warning. The mobile
baselines the kill gate runs on should be captured from a clean checkout.

## Cold versus warm — read this before comparing them

A block owns one worker, so in `--mode sequence` exactly one measurement per
block is cold, and it is always the FIRST position of the set. Its cohort is
therefore confounded with the position: in the thermal file the cold rows are all
ply 1 (`1.e4`), which is far cheaper than the middlegame — cold looks four times
*faster* than warm there, and that number means nothing.

A cold-start comparison must hold the position fixed: run the same set twice,
once with `--mode cold` (every measurement gets a fresh worker) and once with
`--mode sequence --warmup`, and compare per `positionId`. That is what the
`device-baseline-cold-*` / `device-baseline-warm-*` pair above is for. §11's "no
cold-start regression" gate is stated on that paired comparison, not on the
thermal file's three cold rows.

`--warmup` matters for the warm half. Without it the block's cold row is the
set's first position, so THAT position never gets a warm measurement and the
comparison quietly covers one position fewer than the set. `--warmup` prepends a
priming measurement that spends the cold slot, leaving every position in the set
warm. Warm-up rows are written to the file with `"warmup": true` and are excluded
from every summary statistic — they are visible, not hidden, and not counted.

`workerBootMs` is recorded separately on each block's first row (and on any row
that follows a mid-run worker rebuild), so worker construction is never buried
inside a move's latency.

## Cool down between blocks — every run, not just a comparison

A block is one (repeat × arm) pair in sequence mode, or one measurement in cold
mode. Without a cooldown the blocks run back-to-back, so **only the first one
starts on a cooled device** — and the summary pools all of them. Three repeats of
the current protocol are three blocks: repeat 3's numbers carry two sequences'
worth of accumulated heat, and the by-move-index curve averages them with repeat
1's. That is a thermal ramp reported as a device curve.

The driver therefore cools for 60s between blocks by default; `--cooldown 0` opts
out and the file then says so. On the page, keep the field at 60000. A gap under
`MIN_BLOCK_COOLDOWN_MS` (30s, `src/bench/method.ts`) is reported as too short to be
a cooldown, so the control cannot be satisfied by a token value. `runElapsedMs` on
every row is how much wall clock the device had been working when that measurement
started — the axis for checking whether heat actually leaked across blocks.

## The game-weighted numbers §11's gate is read off

§11 states the performance gate on a **game-weighted end-to-end median** (≥25%
improvement) and a **p95** (≥20%). Both live in `summary.gameWeighted`, one entry
per arm — read them there rather than reconstructing them from the split cells.

The construction, once, so no reader has to choose one: the warm `P===B` rows
carry total weight `m` and the warm `P!==B` rows total weight `1 - m`, and
`medianMs` / `p90Ms` / `p95Ms` / `worstMs` are quantiles of that single mixture.
Warm rows only — a game's moves after the first are warm, and the cold row is
already reported in its own cohort.

Two consequences worth knowing:

- `m` is observed on those same warm rows, so the mixture IS the pooled warm
  sample and `gameWeighted` equals the `warm`/`all` cell. That equality is the
  check that the weighting was applied once, not the sign of a redundant field.
- All four are `null` when either split is empty — an all-cold capture, or a set
  the engine never disagreed on. A single split reported as if it were a game is
  the overstatement the weighting exists to prevent.

An earlier schema (v1) reported only `gameWeightedMedianMs`, computed as
`m * median(P===B) + (1 - m) * median(P!==B)`. That is a defensible expected cost
but it is not a quantile, so it had no honest p95 counterpart — and the two
obvious ways to build one differed by about 8% on the same desktop file. v2
replaces it; `parseJsonl` refuses v1 rather than misreading it.

## Comparing two protocols in one run

Select both arms — on the page, tick `current` and the candidate; from the driver,
`--arms current,variantA`. They alternate in a counterbalanced order, one block
each per repeat.

Two more things are then part of the measurement:

- **Repeats must be a multiple of the arm count.** Rotation balances the order only
  then: 3 repeats over 2 arms gives AB, BA, AB, handing the first arm the opening
  slot twice. Use 4 repeats for a two-arm run — §10.4's 3-repeat minimum and a
  balanced two-arm order are not otherwise satisfiable at once. An unbalanced run
  says so in `methodWarnings`.
- **The cooldown matters more here**, because heat is then confounded with the thing
  being compared rather than merely added to it: counterbalancing the order
  averages that bias across arms, it does not remove it.

The by-move-index chart draws one curve per arm; it never pools arms into a single
line.

## Running the desktop control

```bash
npm run bench:baseline -- \
  --label "MacBook Pro M1, macOS 15, Chromium" \
  --set thermal-40 --repeats 3 --cooldown 60000
```

It builds the bench entry with `BENCH=1`, serves `dist` with `vite preview`,
drives the same page an operator uses, records the worker chunk's digest, prints
any method warnings last, and writes the JSONL under `docs/analysis/`.

The paired cold/warm capture:

```bash
npm run bench:baseline -- --label "…" --set smoke-6 --mode cold --repeats 3 \
  --cooldown 60000 --out docs/analysis/device-baseline-cold-<device>-<date>.jsonl
npm run bench:baseline -- --label "…" --set smoke-6 --mode sequence --warmup --repeats 3 \
  --cooldown 60000 --out docs/analysis/device-baseline-warm-<device>-<date>.jsonl
```

Both halves get the same cooldown treatment on purpose: cooling only one of them
would put a thermal difference between the two things the pair exists to compare.
In cold mode every measurement is its own block, so the cold half spends most of
its wall clock idling — which is also what a cold start actually is on a phone
someone has not been using.

`--skip-build` reuses whatever is in `dist`; the recorded digest and git revision
describe that bundle, so it is safe rather than a trap — but `dist` must contain a
`BENCH=1` build or the page will not be there at all.

## Running on a phone

No headless driver substitutes for real mobile silicon and thermals, so the
iPhone/Safari and Android/Chrome numbers are captured by hand.

1. Build and serve the bundled page on the LAN:

   ```bash
   npm run bench:device:build     # BENCH=1 vite build
   npm run bench:device:preview   # vite preview --host
   ```

   `vite preview` prints a `Network:` URL. The phone must be on the same wifi.

2. Open `http://<host>:4173/bench/device/index.html` on the phone.

3. Prepare the device — §10.4's method is part of the measurement, not ceremony:
   - start **cooled** (leave it idle off-charge for ~10 minutes; a warm phone
     throttles and the thermal curve is then the previous run's, not this one's);
   - plug it in and disable low-power mode;
   - fixed brightness, screen lock off, tab in the foreground (a backgrounded tab
     is throttled and the run is void);
   - close other tabs and apps.

4. Fill in the device label (hardware, OS, browser — it is the run's only record
   of what produced it) and notes, pick the position set, tick the arms to measure,
   keep **repeats at 3 or more** (a multiple of the arm count for a comparison),
   leave the **cooldown at 60000** so each repeat starts from the same thermal
   state, and start. The page idles between blocks by itself; keep the tab in the
   foreground through the cooldowns too.

5. Watch the warning list under the status line. It appears before the first
   measurement, so a misconfigured run can be stopped in the first few seconds
   rather than after forty moves.

6. When it finishes, use **Copy JSONL** (or Download) and commit the file here as
   `device-baseline-<device>-<date>.jsonl`.

Runs from `npm run bench:device:dev` are marked `"build": "dev"` in the run
record. They are convenience checks only — §10.1 requires the bundled worker, so
never report a `dev` run as a device baseline.

## What a run contains

- `run` — schema version, harness, build mode, engine identity, depth policy,
  device/environment, the build provenance (`source`), the plan (mode, arms,
  repeats, position set, blocks, warm-up, arm-order balance, cooldown), and the
  plan's `methodWarnings`.
- `move` — one per measurement: end-to-end latency, `runElapsedMs` (the thermal
  axis), per-phase nodes/time/depth (root / post-played / post-best), the reset
  time, cold-vs-warm cohort, `warmup`, `workerRestarted`, `engineRebuilt`, the
  worker's own analysis result, `pEqualsB`, §4 snapshot rejections, §4.3 selector
  divergence, and heartbeat/streaming ping counts.
- `summary` — `completion`, planned-vs-measured counts, the full
  `methodWarnings`, median/p90/p95/worst per arm × cohort × P===B split, the
  observed P===B share `m` (null when there are no warm rows), the §4 counters,
  and `gameWeighted` — the median/p90/p95/worst §11's gate is stated on.

The §4 counters are **observer-recomputed**: the runner replays the worker's own
logged UCI transcript through the same `pvSnapshots` module the worker runs. The
inputs are identical; the worker does not export its counter instance.

That replay happens AFTER the move's clock stops. The transcript is only buffered
while a measurement is in flight, because the worker's `analysis` response queues
behind its log messages — so parsing them as they arrived would have put the
observer's own cost inside `e2eMs`, in proportion to how many info lines the arm
emitted.

Warm-up rows are excluded from the §4 counters as well as from the latencies: a
discarded priming duplicate is no more part of the adoption diagnosis than it is
part of the median. Errored rows are NOT excluded from them — a phase that
terminated before the failure recorded a real acceptance.

Three cases where a counter or a cohort is deliberately not what the schedule said:

- A phase with `"terminated": false` never answered `bestmove` (a worker error or
  a harness timeout). Its `snapshot` is `null` and it contributes to no rejection
  or divergence count — judging a truncated search against the full requested
  depth would manufacture rejections the worker never made, and those counters
  decide §12 step 9.
- A row with `"workerRestarted": true` was measured on an engine built after the
  failure on the row before it. Its engine is genuinely cold, so its `cohort` is
  `cold` whatever the schedule said, and it carries that rebuild's
  `workerBootMs`.
- A row with `"engineRebuilt": true` is the row that CAUSED the above: the worker
  destroyed and rebuilt its own Stockfish during it (its deadline-grace or
  reset-timeout path). The worker reports that as a request-scoped error —
  indistinguishable from a bad FEN — so the runner reads it off the transcript
  instead, waits for the replacement engine, and hands the next row the cold label
  and the rebuild's cost. Without that, a brand-new engine would sit in the warm
  cohort as a slow outlier.

## Reading a file

```bash
node -e "
const rows = require('fs').readFileSync(process.argv[1],'utf8').trim().split('\n').map(JSON.parse)
console.log(rows.find(r => r.kind === 'summary').cells)
" docs/analysis/<file>.jsonl
```

`parseJsonl` in `src/bench/benchRecord.ts` validates every row — every declared
field present, every number finite, nested objects included — and refuses an
unknown schema version rather than silently misreading an older run. Every string
that names a CATEGORY a reader splits rows by (`arm`, `cohort`, `split`, phase
name, `classification`, `stopReason`, the §4.3 divergence reason, the §4.2
rejection reason) is checked against its known set, because an unrecognized value
there is not a new category — it is a row nobody counts.

Row validity is not enough on its own: rows can be MISSING, and a summary can be
edited, while every remaining line still parses. `benchFileProblems`
(`src/bench/benchFile.ts`) therefore rebuilds the summary from the file's own move
rows and compares it, alongside checking record order, run ids, contiguous
sequence numbers, the plan's counts, and that a `complete` run really measured its
whole plan — dropping the LAST row and regenerating otherwise survives every other
check, and demotes the shortfall to a method warning on a file that still reads as
quotable. `committedResults.test.ts` runs both over every file in this directory,
so a baseline that no longer matches its own measurements cannot be committed.
