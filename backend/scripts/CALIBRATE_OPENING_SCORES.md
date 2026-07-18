# Calibrate Opening Scores (v2)

`calibrate_opening_scores_v2.py` produces reproducible calibration evidence for
the opening-score **v2** model. v2 is the only live scoring model — there is no
v1 baseline — so this script *calibrates v2 directly* rather than comparing two
models. It scores every candidate `(user_id, player_color)` pair **in memory**
and reports the distributions, source mix, phase-horizon behaviour, recursion
accounting, readiness-fold grid, and PASS/FAIL diagnostics needed to decide
score defaults and grade thresholds.

## No-write default

The default run performs **zero database writes**. It reads evidence via
`overlay_evidence` and scores in memory via `compute_all_root_scores`; it never
calls `recompute_opening_scores` (which would reserve a generation and persist a
batch). Run it freely against the production database for read-only calibration:

```bash
cd backend
python -m scripts.calibrate_opening_scores_v2
```

Add `--json` for a machine-readable report, `--min-observations N` to change the
cohort threshold, and `--users` / `--pairs` / `--limit` to target a subset.

## Cohort definition

A pair is **included** in the distribution statistics only when it has at least
`--min-observations` quality observations (default `20`). Pairs below the
threshold are listed under `cohort.excluded_low_evidence_pairs` but contribute no
percentiles, so a handful of one-game accounts can't skew the picture.

Named-root distributions are reported **three ways**, because pooling named-root
rows mixes correlated samples (named roots share ancestor/descendant FENs):

- **Pooled** (`named_score_distribution.pooled`) — all named-root scores
  concatenated. Fast to read, but the rows are *not* independent; treat
  percentiles as indicative.
- **Per-user median summary** (`named_score_distribution.per_user_median_summary`)
  — each pair's own median, then summarized across pairs, so a broad-tree user
  with hundreds of rows doesn't dominate.
- **Per-pair** (`named_score_distribution.per_pair`) — each included pair's full
  distribution (`summarize(named_scores)`, i.e. p5..p95 shape + histogram), so a
  single pair's shape is not lost behind its median.

Only the **included** cohort feeds these distributions. All other telemetry
(source mix, excluded sessions, horizon, recursion, throughput) aggregates over
**all candidate pairs**, so the well-formed early-return telemetry of
low/zero-evidence pairs (e.g. structural raw-middlegame and recursion key counts)
still surfaces even when the included cohort is empty.

The synthetic `__repertoire__` hero row is reported in its **own** section
(`synthetic_hero_distribution`), never mixed into the named-root distribution.

## Metrics emitted

| Section | What it reports |
|---------|-----------------|
| `cohort` | candidate pairs, included count, low-evidence pairs |
| `named_score_distribution` | `pooled` + `per_user_median_summary` + `per_pair` (each: percentiles & 5-bucket histogram) |
| `synthetic_hero_distribution` | the `__repertoire__` row, kept separate |
| `source_mix` | `session_eval` / `analysis_cache` / `eval_delta` as % (zero-denominator guarded) |
| `excluded_sessions_total` | sessions dropped for broken board continuity |
| `horizon` | opening-interval-length distribution; **raw-middlegame root count** and **unscored root count** as two distinct numbers |
| `recursion` | actual-key count and perfect-key count reported **separately** (`_metrics` is keyed `(fen, perfect)`), vs the named-root count |
| `throughput` | total scoring wall-time, **per-pair scoring latency** (median / p95 / max), and emitted row count |
| `gates` | pass/fail vs the documented numeric bars: scoring `< 5s/pair` and cache read `< 50ms` (`n/a` when not measured) |
| `grid` | per-cell distributions over the anchor-first arm grid (each row identified by all six axes), plus per-key deltas vs the current-model reference |
| `diagnostics` | User-14 user-turn true-positive, opponent regression guard + unprepared-branch leak, and thin-but-earned cliff gates |

The recursion section is the bound proof: actual/perfect key counts scale with
the number of unique reachable normalized FENs, not the named-root count, and the
two passes are counted apart rather than conflated into one ≈2× number.

The horizon section keeps **raw-middlegame roots** (roots whose own board
satisfies the middlegame predicate) distinct from **unscored roots** (roots with
no reachable quality observation). A raw-middlegame root can *still* be scored
through observed off-book children, so the two must never be conflated.

## Write-bench mode (cache latency)

Cache-write/read latency benchmarking is **gated** behind `--write-bench`, which
requires `--allow-writes` **and** a `--database-url` that passes the safety rule:

- the URL must be **SQLite under `backend/.tmp/`**, *or* contain an explicit
  `calibrate` database name (e.g. `..._calibrate`), **and**
- it must not be the configured production URL.

Any other URL is refused (`validate_write_bench_database_url`). Under
`--write-bench` the script runs **one** `recompute_opening_scores` on the
isolated database, then times a `list_cached_opening_scores` read:

```bash
python -m scripts.calibrate_opening_scores_v2 \
  --write-bench --allow-writes \
  --database-url sqlite:///.tmp/opening_calibrate.db
```

Populate the isolated database with representative evidence first (e.g. copy a
subset of `session_moves` / `analysis_cache`), or the bench measures an empty
cache.

## CLI reference

| Flag | Default | Description |
|------|---------|-------------|
| `--database-url` | configured app URL | SQLAlchemy DB URL (read-only by default) |
| `--min-observations` | 20 | Quality observations required to include a pair |
| `--users` | all | Comma-separated `user_id`s to restrict to |
| `--pairs` | all | Comma-separated `user_id:color` pairs to restrict to |
| `--limit` | none | Limit candidate pairs |
| `--report-fold-grid` | `0.25,0.5,0.75,1.0` | Comma-separated report-fold `p` values to sweep the arms over; domain `0 < p <= 1` |
| `--include-demo-diagnostics` | off | Add the diagnostics-only demo rows (gate + uniform fold) to a standalone run; never enters cohort scoring |
| `--json` | off | Emit the report as JSON |
| `--write-bench` | off | Time one isolated recompute + cache read (needs `--allow-writes` + guarded URL) |
| `--allow-writes` | off | Required acknowledgement alongside `--write-bench` |

## Release runs: use the launcher

A **release** calibration — one whose winner Phase 3 will apply — must be started
through `release_calibration_launcher.py`, never by invoking the scorer directly:

```bash
# --select-release lands with g-p4ih-release-cli; the launcher forwards any args.
backend/scripts/release_calibration.sh -- --select-release
```

Use the wrapper. It is not sugar: it starts the launcher itself under **`-I -S`**, and the
launcher **refuses to run** otherwise. It defaults to `backend/.venv/bin/python` and fails if
that is missing rather than falling back to `PATH` — an unactivated shell resolves `python3`
to the system interpreter (3.10 here, against the venv's 3.12), and since a non-venv
interpreter is legitimate in a CI image, nothing downstream would have objected to a release
scored against whatever versions the system happened to carry. Set `GHOSTREPLAY_PYTHON` to
override explicitly. The child inherits that interpreter, and its dependency paths are
derived from that interpreter's venv.

The launcher checks out `--rev` (default `HEAD`) into a throwaway `git worktree`
under a private temp dir, marks the files named by `SCORER_SOURCE_FILES` read-only
(`0444`), hashes them from *that* tree, and only then execs the interpreter with the
digest in `GHOSTREPLAY_SCORER_SOURCE_DIGEST` plus an empty, write-disabled bytecode
cache. The rest of the checkout stays writable — the run legitimately writes there
(`app.opening_graph` caches its ~30s build under `backend/.opening_graph_cache`), and
those bytes are not what the digest binds.

Both interpreters run without site initialisation, and both halves are required:

* **The child runs under `-S`**, with its dependency directories passed explicitly on
  `PYTHONPATH`. Otherwise `site.py` executes every `.pth` import line in site-packages and
  imports `sitecustomize` before the scorer's first byte — and such a hook can import a
  manifest module from the correct tree and then rebind a function on it. The source bytes
  are never touched, so the digest still matches and the import-origin check still passes,
  while the code that actually runs is not the code the digest names.
* **The launcher runs under `-I -S`**, or it refuses. The child's `-S` is one interpreter
  too late on its own: whatever starts the launcher runs first, and a `.pth` there executes
  before the launcher imports `hashlib` — early enough to replace `sha256` in the very
  process that computes the digest, making it whatever the hook wants. The launcher cannot
  fix this itself (by the time its code runs, the hook has already run), so it fails closed
  and the entrypoint carries the flags.

`PYTHONNOUSERSITE` does not cover either case: it disables the *user* site directory, while
the live vector is the interpreter's own site-packages. If a future dependency needs a `.pth`
to be importable — an editable or namespace install — the run fails loudly at import rather
than silently degrading, which is the intended behaviour for a release path.

The child's environment is also scrubbed of **every** inherited `PYTHON*` variable, with only
the four the launcher chooses added back. This is an allowlist because the denylist was wrong:
`PYTHONWARNINGS` names its filter category as `module.Class` and the interpreter *imports that
module* to install the filter — before the script body, under `-S`. Measured against the real
venv, `PYTHONWARNINGS=default::sqlalchemy.exc.SAWarning` had SQLAlchemy imported before the
child's first line, through a variable that reads like a logging preference. Non-`PYTHON*`
variables (`DATABASE_URL` and friends) are inherited: they are the run's configuration.

All three halves matter and none can be replaced by in-process code:

* **The hash precedes the interpreter.** CPython compiles the scorer and its
  imports before any of its statements run, so an edit landing in that window
  leaves old code executing while every in-process read agrees on the new bytes.
  Only a hash taken before the process existed catches it.
* **The checkout is isolated from the shared tree.** A private-path worktree with its
  hashed files marked `0444` is what defends against change-and-revert, and what makes
  the digest a claim about a *tree* rather than a *moment*. The working tree — written
  continuously by editors, builds, and other agents — cannot give you this.
* **Nothing auto-executes before the scorer.** The two points above are about bytes on
  disk; `-S` is about code that never touches the tree at all.

Be precise about what this is worth. `0700` excludes other *users*, not other processes
running as you; `0444` stops an accidental write, not a deliberate one, since this uid can
chmod it back; and `git worktree list` publishes the path for the duration of the run. The
interpreter, the standard library, and every installed dependency are **unhashed** — the
digest binds `SCORER_SOURCE_FILES` and nothing else, and no check inside the run can audit
the runtime it is already executing inside. `check_scorer_import_origins()` reads
`__file__`, which reports where a module was *loaded from*, not what its attributes hold
now, so it catches a misconfigured path or the wrong checkout — not a hostile loader.

Taken together: this removes the ambient hazard, which is the realistic one on a machine
that also runs editors, agents, and instrumentation. It is not an airtight boundary, and it
is no defence at all against a hostile operator, who can simply commit the change. A literal
"no other writer" guarantee needs an OS boundary (container, sandbox, separate uid) around
the whole run, which the launcher does not provide — see bead `g-release-os-boundary`.

A run under the launcher stamps `scorer_source_verified_preexec=True` on its
cohort and winner binding. A bare `python backend/scripts/calibrate_opening_scores_v2.py`
run stamps `False` — that is normal and correct for dev and test, and the script
stays fully usable that way. The refusal lives at the release boundary:
`require_preexec_verified_source()` rejects anything carrying `False`, because
`scorer_source_digest` is then fenced over source bytes but not proven to name the
code that actually ran. Because the worktree comes from a commit, uncommitted edits
can never reach a release run.

## g-xnv7 calibration decision

The 2026-07-09 g-xnv7 final run chose `lcb_z=1.0`,
`coverage_fold="gate"`, and `coverage_live_threshold=1`. The chosen grid cell
reported pooled named-root stats mean `14.6`, p5 `0.4`, p25 `5.1`, p50 `9.8`,
p75 `20.8`, p95 `43.9` across 478 rows; all three diagnostics passed. Display
grades were recalibrated once from that combined distribution:
`A>=44`, `B>=29`, `C>=8`, `D>=2`, `F<2`; tones `alert<5`, `watch<29`.
