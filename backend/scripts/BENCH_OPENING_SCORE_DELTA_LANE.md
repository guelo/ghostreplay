# Terminal opening-score delta lane release gate

`test_opening_score_delta_lane_release.py` is the production-shape acceptance
gate for `g-delta-priority-lane`. It is marked `release_seal`, excluded from
pre-push, and must be run when validating this phase/epic and before a release.

The gate uses real PostgreSQL 18 data and writes recompute batches and synthetic
freshness-counter advances, so it must target a disposable database cloned from
the restored production-dump template. It refuses the template, `postgres`, and
`railway` database names.

## Setup and command

A local PostgreSQL 18 cluster with a template named `gr_snap_base`, restored
from a production dump and deliberately NOT migrated. The long-standing
development arrangement is port 5433; the 2026-09-23 qualification used the
disposable QC-PROD cluster on port 55440 instead, and both are described
below.

```bash
/opt/homebrew/opt/postgresql@18/bin/createdb -p 5433 \
  -T gr_snap_base gr_delta_lane_bench

cd backend
source .venv/bin/activate
export DATABASE_URL=\
'postgresql+psycopg://localhost:5433/gr_delta_lane_bench'
alembic upgrade head
export GHOSTREPLAY_DELTA_BENCH_DATABASE_URL=\
'postgresql+psycopg://localhost:5433/gr_delta_lane_bench'
TMPDIR=/private/tmp pytest -c pytest.ini -s -q \
  test_opening_score_delta_lane_release.py -m release_seal

/opt/homebrew/opt/postgresql@18/bin/dropdb -p 5433 gr_delta_lane_bench
```

Always migrate the disposable clone even when `gr_snap_base` was previously
described as migrated: the restored template is retained across development and
can legitimately trail current Alembic head. Never migrate the template merely to
make this gate pass.

`GHOSTREPLAY_DELTA_BENCH_REPS` defaults to 10 and may be overridden for a
verification run; values below 5 are rejected. Ten repetitions make the
nearest-rank p95 the observed maximum, a conservative small-sample gate.

### Where the template comes from

This gate needs a restored production snapshot and had no dump procedure of its
own to inherit. The one `g-score-store-qualify` used, with PostgreSQL 18.4 by
absolute path:

```bash
PREFIX=/Applications/Postgres.app/Contents/Versions/18/bin
STORE="$HOME/.ghostreplay-private/score-store-qualify"
```

**The URL never appears on a command line.** `pg_dump -Fc "$URL"` expands the
password into `ps` output. The dump runs in its own subshell that sets
`umask 077`, reads `DATABASE_URL` from its own environment, splits it into
`PGHOST` / `PGPORT` / `PGUSER` / `PGDATABASE` and a mode-600 `PGPASSFILE`, traps
deletion of both files on exit including on failure, and calls
`pg_dump -Fc -Z6 --no-sync` with **no connection argument at all**. Export
`PGSSLMODE=require` explicitly: splitting a URL into `PG*` variables drops any
`sslmode` the URL carried, and libpq's default `prefer` then permits a silent
plaintext fallback over the public proxy. A recent scheduled dump from valtron
(`docs/backups.md`) is an equally good source and is preferred when one is fresh
enough, because it touches production not at all. Record which one was used with
its timestamp: the snapshot date is part of the result's identity.

Restore and settle, then leave the template alone:

```bash
export PGPASSFILE="$STORE/qc-prod.pgpass"   # omit on a trust-auth dev cluster
"$PREFIX/createdb" -h 127.0.0.1 -p 55440 -U postgres gr_snap_base
"$PREFIX/pg_restore" -h 127.0.0.1 -p 55440 -U postgres -d gr_snap_base \
  --no-owner --no-acl "$STORE/prod-20260922T060234Z.dump"
"$PREFIX/psql" -h 127.0.0.1 -p 55440 -U postgres -d gr_snap_base \
  -c 'VACUUM (FREEZE, ANALYZE)' -c 'CHECKPOINT'
```

Check for about 1 GB free first, and run `VACUUM (FREEZE, ANALYZE)` and
`CHECKPOINT` again after each clone, so autovacuum has no backlog to work
through inside a timed repetition. The template restores at whatever Alembic
version production held — `20260920_01` for the 2026-09-22 snapshot, against a
repo head of `20260923_01` — and that is correct. Migrate the clone, never the
template.

Restored data is production-derived: keep it on a disposable cluster, keep the
dump in a mode-700 private store outside every worktree and iCloud-synced path,
and let only aggregate timings reach a report, `docs/analysis` or a bead.

### Storage format and report knobs

Four environment variables drive the gate:

- `GHOSTREPLAY_DELTA_BENCH_DATABASE_URL` — the disposable clone. Required.
- `GHOSTREPLAY_DELTA_BENCH_STORAGE_FORMAT` — `legacy` or `current`. **One format
  per invocation**, and the lane must be measured in BOTH: the readers are
  format-agnostic, which is a claim worth measuring rather than assuming.
- `GHOSTREPLAY_DELTA_BENCH_REPS` — defaults to 10, values below 5 rejected.
- `GHOSTREPLAY_DELTA_BENCH_REPORT` — writes the run's summary to this path. The
  integrated qualification evaluator reads a file; it cannot read a printed
  line, so a run intended for `summarize_opening_score_qualification` must set
  this.

The `p95_limit_ms` the report carries is read back by that evaluator only to
REFUSE a report measured against a limit other than 3000 ms. The limit is the
evaluator's, not the report's — otherwise a report could declare a thirty-second
limit and pass itself.

Running one format against the qualification cluster, with the URL read inside
the subshell from a mode-600 file so it never reaches argv or shell history:

```bash
(
  set +o history
  export GHOSTREPLAY_DELTA_BENCH_DATABASE_URL="$(cat "$STORE/gr_delta_lane_qual.url")"
  export GHOSTREPLAY_DELTA_BENCH_STORAGE_FORMAT=current
  export GHOSTREPLAY_DELTA_BENCH_REPS=10
  export GHOSTREPLAY_DELTA_BENCH_REPORT="$STORE/cells-r1/C7-S1-current.json"
  export PGPASSFILE="$STORE/qc-prod.pgpass"
  export TMPDIR=/private/tmp POSTHOG_DISABLED=true
  unset DATABASE_URL DATABASE_PRIVATE_URL
  cd backend && .venv/bin/python -m pytest -q -c pytest.ini -m release_seal \
    test_opening_score_delta_lane_release.py
)
```

The harness also records `_git_revision()`, the platform and `SHOW cluster_name`
in the report, so a lane file from another machine or another commit cannot
satisfy the integrated evaluator's homogeneity check.

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

## Integrated qualification run (2026-09-23)

`g-score-store-qualify` ran this gate as its cell C7 on the disposable QC-PROD
cluster (`ghostreplay-score-storage-qual`, port 55440), against
`gr_delta_lane_qual` — a clone of the `gr_snap_base` restored from the
2026-09-22T06:02:34Z production dump, migrated on its own to `20260923_01` while
the template stayed at `20260920_01`. Ten warm repetitions per mode, PostgreSQL
18.4, revision `0c690ac`.

Both storage formats passed both `<3000 ms` warm whole-graph-contention gates:

| format | normal | drill |
|---|---|---|
| `legacy` | 1851.687 ms | 1851.490 ms |
| `current-b50-v1` | 1873.815 ms | 1792.858 ms |

The two formats land within about 1% of each other at the p95, which is the
result the format-agnostic reader work predicted and the reason the delta lane
is not a cutover risk. Process-cold visibility was recorded and, as designed,
never mixed into the warm p95.
