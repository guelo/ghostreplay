# Terminal opening-score delta lane release gate

`test_opening_score_delta_lane_release.py` is the production-shape acceptance
gate for `g-delta-priority-lane`. It is marked `release_seal`, excluded from
pre-push, and must be run when validating this phase/epic and before a release.

The gate uses real PostgreSQL 18 data and writes recompute batches and synthetic
freshness-counter advances, so it must target a disposable database cloned from
the restored production-dump template. It refuses the template, `postgres`, and
`railway` database names.

## Setup and command

The local PostgreSQL 18 cluster is expected on port 5433 with the restored,
migrated template named `gr_snap_base`.

```bash
/opt/homebrew/opt/postgresql@18/bin/createdb -p 5433 \
  -T gr_snap_base gr_delta_lane_bench

cd backend
source .venv/bin/activate
export GHOSTREPLAY_DELTA_BENCH_DATABASE_URL=\
'postgresql+psycopg://localhost:5433/gr_delta_lane_bench'
TMPDIR=/private/tmp pytest -c pytest.ini -s -q \
  test_opening_score_delta_lane_release.py -m release_seal

/opt/homebrew/opt/postgresql@18/bin/dropdb -p 5433 gr_delta_lane_bench
```

`GHOSTREPLAY_DELTA_BENCH_REPS` defaults to 10 and may be overridden for a
verification run; values below 5 are rejected. Ten repetitions make the
nearest-rank p95 the observed maximum, a conservative small-sample gate.

## Cells and boundary

The harness selects the heaviest `(user_id, player_color)` in the restored copy
that has both a supported normal terminal and a supported drill terminal with a
played registered opening. It warms the Phase-1 evidence replay cache before
warm measurements.

It reports:

- idle lane publication for normal and drill sessions;
- normal and drill lane publication while a real whole-graph recompute for the
  same key is already inside `_build_cached_scores`;
- overlap with the real asynchronous baseline job on its epoch-drift scoped
  digest branch;
- process-cold normal and drill visibility after clearing the evidence replay
  cache.

The primary boundary starts immediately after the synthetic terminal
freshness-counter transaction is durable and ends on the first poll read that
returns `is_fresh=true` after the lane publication completes. The structured
`DELTA_LANE_BENCH_RESULT` line reports median and p95 separately for
queue-to-dispatch, poll read, end to end, and every Phase-2 stage:
`session_load`, `counter`, `overlay`, `digest`, `score`, and `publish`.

The load-bearing gate is the warm whole-graph-contention end-to-end p95:

- normal terminal session: `< 3000 ms`;
- drill terminal session: `< 3000 ms`.

Process-cold results are recorded for correctness and visibility only; they are
not mixed into the warm p95.

## Current qualification

The 2026-07-31 qualification against a disposable PostgreSQL 18.4 copy used 10
warm whole-graph-contention repetitions per mode. End-to-end p95 was 1845.580 ms
normal and 1829.906 ms drill. A final five-repetition verification after wiring
the real epoch-drift baseline-digest branch measured:

- normal: `1741.842 ms`;
- drill: `1792.669 ms`.

Both runs passed both `<3000 ms` gates. The final verification's idle p95 was
`595.030 ms` normal and `502.488 ms` drill. Its baseline epoch-drift overlap p95
was `574.358 ms`, so the measured result does not justify adding a baseline
defer/retry state machine. Process-cold visibility was approximately 12.9
seconds for each mode and remains outside the warm gate, as designed.
