# Calibrate Opening Scores (v2)

`calibrate_opening_scores_v2.py` produces reproducible calibration evidence for
the opening-score **v2** model. v2 is the only live scoring model — there is no
v1 baseline — so this script *calibrates v2 directly* rather than comparing two
models. It scores every candidate `(user_id, player_color)` pair **in memory**
and reports the distributions, source mix, phase-horizon behaviour, and
recursion accounting needed to decide grade thresholds and the `tau` parameters.

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
| `--json` | off | Emit the report as JSON |
| `--write-bench` | off | Time one isolated recompute + cache read (needs `--allow-writes` + guarded URL) |
| `--allow-writes` | off | Required acknowledgement alongside `--write-bench` |
