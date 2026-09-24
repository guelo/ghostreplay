# Disposable opening-score storage selection

`g-score-store-spike` owns this experiment. Its parent, `g-cut-score-churn`,
defines the selection budgets and stop rules. The scripts under this runbook
change no application models, migrations, readers, locks, or rollout flags.
The user approved B50, the revised fixture budgets and spike code on 2026-09-19,
completing the spike review gate and unblocking the writer implementation bead.
The original sealed
harness/report are preserved for audit; their A-derived `proposed_release_budgets`
were rejected and must not be reused as release ceilings.

## Isolation and reproduction

Use a **new synthetic-only PostgreSQL 18 cluster**, not a restored production
dump, retained template, application database, or the shared PostgreSQL test
gate. The script accepts only an explicit loopback URL with a
`gr_score_spike_*` database name, no URL query overrides, the cluster label below,
no unrelated clients, and no tables outside its private `ss_*` schemas.
It never defaults to the application's database URL and never drops a schema.
Each invocation creates fresh schemas so failed measurements can be inspected.

Example setup on this development machine (choose unused paths and port):

```bash
/opt/homebrew/opt/postgresql@18/bin/initdb \
  -D /private/tmp/ghostreplay-score-spike-pg -A trust --no-locale -E UTF8
/opt/homebrew/opt/postgresql@18/bin/pg_ctl \
  -D /private/tmp/ghostreplay-score-spike-pg \
  -l /private/tmp/ghostreplay-score-spike-pg.log \
  -o '-p 55439 -k /private/tmp -c listen_addresses=127.0.0.1 -c cluster_name=ghostreplay-score-storage-spike -c autovacuum=off -c checkpoint_timeout=1h -c max_wal_size=4GB' start
/opt/homebrew/opt/postgresql@18/bin/createdb \
  -h 127.0.0.1 -p 55439 gr_score_spike_synthetic
psql -h 127.0.0.1 -p 55439 -d gr_score_spike_synthetic \
  -c 'CREATE EXTENSION pg_walinspect' -c 'CREATE EXTENSION pgstattuple'

cd backend
source .venv/bin/activate
export GHOSTREPLAY_STORAGE_BENCH_DATABASE_URL=\
'postgresql+psycopg://127.0.0.1:55439/gr_score_spike_synthetic'
export POSTHOG_DISABLED=true
python -m scripts.bench_opening_score_storage \
  --output /private/tmp/score-storage-representative.json
python -m scripts.probe_opening_score_storage_sql \
  --benchmark /private/tmp/score-storage-representative.json \
  --output /private/tmp/score-storage-sql-probe.json
TMPDIR=/private/tmp pytest -q -W error \
  test_opening_score_storage_spike_release.py -m release_seal
```

Run the benchmark and PostgreSQL tests **serially**, with no other cluster
writers. The test module is entirely `release_seal` and excluded from pre-push.
Run it when changing this experiment or reviewing a storage selection. The
production-shape delta-lane release benchmark belongs to the later integrated
qualification bead and uses its own runbook/database; it must also run serially.

The default is 64 historical synthetic sessions, 20 advancing-time steady
rebuilds, approximately 20,000 initial persisted positions, and exactly 100
fixed-working-set qualification cycles. A smaller `--target-positions` is a
smoke test and cannot qualify a production selection. The output is written
after every cell and preserved on failure. Output contains synthetic aggregate
measurements, not database URLs, statement parameters, or production telemetry.

After retaining the report and completing the tests, stop only this cluster:

```bash
/opt/homebrew/opt/postgresql@18/bin/pg_ctl \
  -D /private/tmp/ghostreplay-score-spike-pg stop
```

## Workload and parity

`opening_score_storage_workload.py` uses deterministic legal chess lines through
the installed opening graph, followed by legal moves beyond the reference.
It writes synthetic normal and converted-drill sessions, then invokes the actual
`recompute_opening_scores_if_needed`, evidence collector, scorer, and existing
writer at explicitly controlled times. The isolated fixture uses current model
DDL and explicit evidence-counter changes; it is not a test of application
terminal-route hooks or migration triggers.

The cadence is four active requests 20 minutes apart followed by a two-day idle
gap, repeated through the observation window. Active requests include normal
terminals, drill terminals, a quality correction, and an evidence deletion.
Each is followed by a warm request and unrelated epoch activity. The report
counts actual `REBUILT` outcomes using all five existing reasons, separately
from `CACHED`, `NO_EVIDENCE`, and cached epoch re-arms. Startup and forced
registry/legacy-branch controls are excluded from steady-state percentages.
Synthetic proportions are **not measured production frequencies**.

Every completed candidate is captured once. Captured semantic rows are checked
against the scorer and persisted writer output, then the exact same detached
rows, timestamps, and freshness bundle are replayed for each layout. Exact
Python equality includes nullable metrics, confidence, names/branches, counters,
timestamps, keys, edge payload, and shared scope. There is no epsilon comparison.
The field lists derive from current model columns; publication metadata is
explicitly excluded from semantic equality. Every measured publication has a
full database parity oracle outside the timing and WAL interval.

Actual real-scorer row counts are reported. If smaller than the target, the
persistence fixture repeats entire payloads with explicitly marked replica keys.
These keys are **not legal FENs**, and the enlarged fixture is **not additional
real evidence**. Replication preserves the original changed-field distribution,
NULL values, and graph-shaped edge rows; the report distinguishes its sizes from
the actual scorer's sizes. It cannot prove production workload representativeness.

An experimental hourly scorer-time bucket is computed independently against
each same overlay, retaining the real publication/freshness timestamps. Bucket
crossings, exact field-change counts, and confidence deviations are reported.
One winning-adapter replay estimates storage savings. This is a nonshipping
control; it does not authorize a scoring-model/fingerprint or UX change.

## Layouts and measurement boundaries

- **A:** the actual current publisher, schema defaults, two-generation retention,
  Core position/edge inserts, ORM root/scope inserts, and committed prune.
- **B/100 and B/50:** one wide current set, exact full-read Python diff;
  fillfactor applies to position/root tables.
- **D/100 and D/50:** compact stable bases with separate nullable confidence
  rows; fillfactor applies only to confidence tables. The base/confidence
  relation, indexes, FK work, and join cost are counted.

All B/D machine-key columns use PostgreSQL `C` collation. New inserts/updates
use bounded executemany with 500 rows per chunk. New ID maps use bounded keyed
lookups; retained rows retain numeric identities. There is no COPY, staging
table, persisted hash/read shortcut, chunked vector, or production publication-lock protocol in
these adapters. The future SQLite implementation must use `BINARY` collation.

Each comparison has two untimed publications, an initial ordinary vacuum,
checkpoint before publications 1 and 11, and ordinary `VACUUM (ANALYZE)` every
ten publications. The same schedule applies to A and candidates. Automatic
vacuum and scheduled checkpoints are disabled for experimental control, not as
a production recommendation.

Recorded measurements include:

- WAL insert-LSN deltas for publication (including A's prune), with normal
  vacuum/ANALYZE WAL added to the combined budget. Warm and post-checkpoint
  publication WAL remain separate. `pg_walinspect` attributes whole record bytes
  to a table when all block references resolve to it; mixed, metadata, and
  unattributed records plus alignment/page-header bytes remain explicit.
- Heap/index/total relation sizes and exact live/dead tuple counts via
  `pgstattuple`, before and after equal vacuum boundaries. Total relation sizes
  include TOAST, associated indexes, FSM, and VM, without double-counting.
  PostgreSQL update/HOT/delete counters are reported rather than assumed.
- Publication, payload-read, Python-diff, and cache-accounting time. Scoring and
  fixture capture are outside persistence timing and reported separately.
  Bounded reads include up to 32 direct positions, 16 roots, edges for 16 parent
  keys, and the candidate's marker fence. Five warmed reads follow each write.
- Exact libpq result DataRow byte counts including per-field lengths. These
  exclude protocol RowDescription/control messages, TLS, and request bytes;
  they are not a packet capture. Instrumentation is enabled equally on all timed
  cells. Their `statements` arrays are SQLAlchemy **after-event templates**, which
  can precede insertmanyvalues expansion. The separate untimed SQL probe uses
  `before_cursor_execute` to measure actual expanded statements for A and the
  selected adapter; use that output for SQL-text conclusions. It replays the
  last two captured synthetic A candidates into fresh schemas. Neither probe
  retains SQL text or parameter values.
- Process RSS high-water (including the pre-generated replay library), bounded
  cache retained-size estimates, and a separate untimed allocation-peak control.
  A fresh spawned process receives just two candidates and measures persistence
  worker RSS separately; that excludes scorer/evidence allocations. Scorer-process
  high-water is recorded before persistence replication. Tracing overhead is
  kept separate from the proposed untraced persistence-worker RSS limit.

The representative comparison can grow as sessions are added. Qualification
uses the union of captured keys as a fixed membership envelope and cycles the
captured exact values 100 times. Missing keys in an earlier capture retain their
union values. This deliberately tests reuse of a fixed storage working set;
it is not 100 new observed requests or a reconstructed historical evidence trace.
An identical A reference uses that same envelope and schedule. Equal-vacuum
footprint and live counts must plateau, and one held repeatable-read snapshot
must release its dead tuples after rollback and ordinary vacuum.

## Adaptive selection and review

Comparison targets are at least 50% less combined WAL than A, no greater final
vacuumed total footprint, and no more than 10% regression in warm publication
or bounded-read p95. Exact parity is mandatory. A passing B may win for
simplicity; a passing D stops any B tuning. Only the selected setting enters
100-cycle qualification. No passing candidate leaves the gate unresolved.

When evidence changes exceed half of steady rebuilds, a paired bounded payload
cache is measured at one promising existing setting before any hashing. It
contains immutable payloads and final ID maps, validates the database marker,
and is capped at eight pairs and 64 MiB estimated retained memory. Cold load,
restart/eviction, and external-marker replacement are labeled misses. Cache
retention/accounting runs inside measured publication time. A passing cache
variant is retained only when it improves publication p95 by at least 5%, or
rescues a configuration that otherwise misses a gate. The declared timed
workload has one active pair; an eight-pair LRU pressure control is separately
labeled and cannot establish production residency.

Hash eligibility is estimated from exact stable-output equality, restricted to
residual cache misses when a cache is selected. If even zero hash/narrow-read
cost cannot save 5% of publication p95, hashing is screened out. Otherwise a
conditional, unpersisted canonical-encoding CPU probe charges encoding and
SHA-256 on **every** representative publication, including output changes and
cache hits. If the optimistic savings after this cost still exceed the threshold,
a paired hash adapter experiment is required before selection is final. The
harness does not mark that unresolved branch ready for approval. The cost probe
does not define a selected durable hash format or add hash metadata/read paths.

C is excluded. E is only eligible after a measured D WAL miss. Additional
fillfactor, transport, vacuum, and scale trials need a measured concern.
Fallback-frequency testing is only justified when measured bounded reads reach
the scheduler's 1,500 ms quiet window; mandatory reader concurrency correctness
tests belong to the reader implementation bead regardless of this screen.

The original output's budgets are **rejected**, not release criteria. The approved
replacement contract below accounts for the synthetic reason mix, persistence-only
scaling, local DB latency, controlled maintenance schedule and memory/residency
limits. Production-shape qualification and ceiling review remain separate gates.

## Budget revision after selection review

`remeasure_opening_score_budgets.py` uses the sealed adapters and workload,
verifies the original seven source hashes, and writes a separate artifact. It
does not repeat selection or tune losing layouts. On a new isolated cluster
configured above (the recorded rerun used database `gr_score_spike_budgets`):

```bash
cd backend
source .venv/bin/activate
export GHOSTREPLAY_STORAGE_BENCH_DATABASE_URL=\
'postgresql+psycopg://127.0.0.1:55439/gr_score_spike_budgets'
export POSTHOG_DISABLED=true
python -m scripts.remeasure_opening_score_budgets \
  --original ../docs/analysis/opening-score-storage-spike-2026-09-19.json \
  --output /private/tmp/score-storage-budget-revision.json
python -m scripts.summarize_opening_score_budgets \
  --input /private/tmp/score-storage-budget-revision.json \
  --original ../docs/analysis/opening-score-storage-spike-2026-09-19.json \
  --output /private/tmp/score-storage-budget-reviewed-metrics.json
TMPDIR=/private/tmp pytest -q -W error \
  test_opening_score_storage_budgets_release.py
```

The script reconstructs the same deterministic timeline, checks sizes and actual
reason accounting against the original, then alternates A/B50 in ten-publication
blocks (AB, BA, AB, BA). Both run 100 fixed-membership publications with matched
checkpoint/vacuum schedules. This exposes drift instead of measuring the entire
A reference in one later time window. All raw timings remain, including outliers;
publication wall time, client CPU time and host load are recorded. Client CPU
includes A fixture-object preparation, while the adapter's publication timer
continues to exclude it. Load/CPU observations help assess stability but do not
prove the host is free of interference.

Keep all five individual warmed read samples after every publication. Report
pooled p95 across 500 reads and each 50-read block; the original p95 of 20
maxima-of-five is not the revised latency statistic. Review block stability and
paired uncertainty before declaring a 10% read difference meaningful. An
inconclusive comparison requires additional paired sampling, not automatic
rejection of a layout or deletion of outliers.

The 8 ms local read ceiling applies only to the **pooled p95 of at least 500
individual reads** per comparable workload-size/revision/settings window.
Retain all samples. Require at least 500 for each layout in the matched relative
comparison too. Fifty-read block p95s are diagnostics, not separate ceiling
checks; the recorded B50 blocks include 8.28 and 6.23 ms while the full pool is
3.79 ms. A smaller sample is insufficient evidence, not pass/fail; collect more
representative reads without combining unlike workloads. The 100-read
checkpoint-heavy control does not independently qualify the read ceiling.
The future integrated qualification/observation evaluators must enforce this
sample requirement before returning an acceptance verdict.

The summary command attaches a deterministic 4,000-resample paired-block
percentile interval for each p95 ratio. A relative latency gate passes only
when the upper 95% interval endpoint is at most 1.1; it fails when the lower
endpoint exceeds 1.1 and is otherwise inconclusive. This describes the local
sample, not production uncertainty. It also verifies the original artifact,
all measured sources and all deterministic timeline fields (excluding elapsed
scorer time and process RSS), and evaluates the proposed absolute ceilings.

A second pair of fresh schemas replays the 20 representative publications with
a checkpoint before **every publication**, and the same ten-publication ordinary
vacuum schedule. This directly measures the checkpoint-heavy endpoint. Report
warm and post-checkpoint publication WAL separately, with vacuum WAL separate;
retain A-relative comparison under the identical schedule. No production
checkpoint frequency is assumed or measured by this local experiment.

Five fresh spawned persistence workers measure RSS and transient allocation
peaks. Proposed memory ceilings use 1.5× the maximum across those repetitions,
rounded upward; they remain scoped to two candidate payloads and persistence,
not an integrated scorer/evidence/application worker. The other B50 ceilings
use 1.5× selected WAL/space and fixed-set publication measurements, and 2× pooled
individual-read p95. The retained 1,500 ms publication ceiling is only 1.47×
the checkpoint-heavy p95 of 1,022.33 ms, rather than at least 1.5× every schedule.
The post-checkpoint WAL anchor has 10 samples and the growing-set warm anchor
has 18; nearest-rank p95 equals the sample maximum in both cases. Their 1.5×
headroom remains a small-sample choice, not a population-tail estimate.
Rounding units and exact operands are retained in code and JSON. Warm WAL
and footprint anchors also cover the growing representative fixture. Retain
both these absolute ceilings and the A-relative minimum-improvement gates.
The summary adds a tighter fixed-working-set WAL profile: use its own warm
publication and vacuum anchors for fixed membership, rather than granting the
larger growing-membership allowance. The applicable profile governs both
per-state checks and the count-weighted combined-WAL envelope.
Changes in workload size, checkpoint frequency or maintenance cadence require
matched measurements rather than direct comparison with a per-100 constant.

The cache is deferred for baseline simplicity/performance and its approximately
30 MiB entries. Its measured recursive deep-size overhead is method-dependent;
it does not establish that caching cannot improve performance. No cache or hash
is selected by this revision.

The full-read diff returns about 11.37 MB of DataRows per representative
publication versus 0.589 MB for A. Loopback timings cannot establish the cost
over the deployed network. `g-score-store-qualify` must measure the actual
application-to-database path, RTT/throughput, complete worker memory, actual
checkpoint settings and per-state WAL before final latency ceilings or
activation. Local latency limits remain provisional; revised/final limits need
review before cutover.

All synthetic absolute ceilings are limited to this fixture: about 23k positions
and 70k logical payload rows after 17× persistence replication. The unreplicated
synthetic user has 1,149–1,363 positions. Logical rows are positions + roots +
edges + scope, excluding the marker, indexes and dead versions.
`g-score-store-qualify` must derive and obtain review of production-shape WAL,
footprint, memory and latency limits before closing. Measure representative
per-owner/color sizes, row mixes, changed-row fractions, fixed/growing membership
and deployed checkpoint/vacuum/network settings; record size ranges, logical-row
denominators, fixed overhead and sample windows. Use measured size-specific
limits or reviewed per-row formulas, retaining A-relative improvement gates.
Directly copying the synthetic constants or scaling linearly by 17 is not
qualification. The cutover handoff must include this reviewed profile.
`g-score-store-observe` must use that profile for its seven-day decision; absent
profiles, out-of-range workloads and insufficient read samples remain unresolved
and require qualification/review. The sealed local artifacts are unchanged and
do not authorize a production acceptance verdict.

## Recorded experiment

The [2026-09-19 selection report](../../docs/analysis/opening-score-storage-spike-2026-09-19.md)
links the complete synthetic measurements and measured source hashes. Its
selection is approved; its original budgets are rejected and replaced by the approved
[separate budget revision](../../docs/analysis/opening-score-storage-budgets-2026-09-19.md)
with its read-sample and production-shape applicability conditions. The sealed JSON
retains its historical proposal status and field names; the reports record approval.
The recorded environment
used PostgreSQL 18.4, SQLAlchemy 2.0.51, psycopg 3.3.4, chess 1.11.2, and
pytest 9.1.1. Reproduction should record any library/server version differences.

## Application writer milestone (inactive)

The reviewed B50 implementation now lives in `app/opening_score_storage.py`, with
schema migration `20260919_03`. This is separate from the sealed disposable spike
adapters above: their historical measurements do not qualify the application
implementation. Production calls still default to legacy. The test-only/internal
`storage_format=StorageFormat.CURRENT` argument exercises the selected writer;
there is no deployment activation flag yet. Readers now serve both formats (see
"Reader contract" below). Maintenance calls require a fresh session transaction. Legacy bulk insert paging and timing events are
preserved; qualification must include the already-active atomic retirement cost.

Writer correctness checks (activate `backend/.venv` first):

```bash
TMPDIR=/private/tmp pytest -q -W error test_opening_score_storage.py \
  test_opening_cache.py test_opening_score_scheduler.py \
  test_opening_recompute_analytics.py test_model_schema.py
# With explicit disposable PostgreSQL test and maintenance URLs configured:
TMPDIR=/private/tmp pytest -q -W error test_opening_score_storage_pg.py
TMPDIR=/private/tmp pytest -q \
  test_pg_gate_plugin.py::test_manifest_matches_real_pg_gate_collection
```

The required PostgreSQL gate includes schema/model parity and fillfactor,
collations, atomic failure boundaries, conversion/downgrade, commit recovery,
serialized competing publishers, evidence-order independence, advisory namespace
isolation and repeatable-snapshot visibility. Current transport is bounded Core
executemany of 500 with full-read exact diffs; legacy root/scope transport remains
available. No cache/hash/COPY deliverables were selected.

For rollback **within the compatibility release**, retain both format readers and
all publisher guards. Stop/drain publishers and direct scripts before any binary
or schema rollback. Disable selected-format writes, then rebuild legacy lazily or
invoke `convert_pair(db, owner, color, StorageFormat.LEGACY)` for each current
pair. Conversion preserves evidence/scoring stamps and retires the old marker in
the same transaction. Inspect for any non-legacy markers and any current payload
before downgrade; the migration refuses either condition. Do not start an old
binary until conversion is complete. This is an implementation contract, not a
claim that a production rollout or rollback has run.

## Reader contract (inactive writes, active readers)

`g-score-store-readers` made every opening-score consumer format-agnostic while
production writes stay legacy. Two shapes, and the difference is operational:

- **Latest-marker single statement** — every non-tree reader (`/openings`,
  `/stats`, the session lineage, the delta lane's cached reads). The statement
  resolves the newest marker inside itself and outer-joins the payload to it, so
  there is no retirement to observe, no retry, no fence and no fallback. Zero
  rows means the pair has no marker at all; one row with a NULL natural key is a
  live marker with no payload.
- **Exact-handle statement** — the tree builder's bounded waves, the cheap
  evidence-freshness scope read, baseline proof 2, and the push-fill. These CAN
  observe retirement and raise `RetiredScoreHandle`; each caller maps it to an
  existing stale/skip outcome. Retirement is always RETRYABLE, never terminal: a
  baseline job that hits it reports `skipped_stale` and captures on its next run,
  and an independent post-publication push-fill discards its work and returns 0.

**Tree read, operationally.** `/tree` is the one multi-statement reader. It
resolves a fresh marker, builds, then executes a final SQL marker fence (never an
ORM identity-map hit). An invalidated attempt discards the whole builder —
partial rows, canonical line, cached edges and its timings — and retries ONCE
without a second scheduler enqueue. If both optimistic attempts are invalidated
it completes inside one short read-only REPEATABLE READ snapshot opened after
bootstrap/graph/routing loading, with no scorer, no `refresh_now` and no
whole-edge-graph fetch inside it.

The timing log carries `score_read_attempts` and `score_read_mode`
(`optimistic` / `snapshot`), so the fallback is observable in production even
though a healthy system should never reach it. A sustained non-zero
`score_read_mode=snapshot` rate means publications are landing inside tree reads
far more often than the scheduler's quiet window should allow — investigate the
publication cadence, not the reader. `cache_state` is labelled against the marker
actually served rather than the bootstrap hint, so a request that blocked on a
bootstrap and was then overtaken by a fresh publication reports `bootstrapped`
instead of making the client discard a good tree.

**Bounded reads and collation.** Current-format keys are `C`-collated and the
global `shared_evidence_*` tables are not. PostgreSQL resolves a comparison
between them to `C`, and it will only use an index whose own collation matches
the clause's — so a join between the two silently loses the shared table's
primary key and degrades to a sequential scan of every shared FEN on the
instance. Session-start baseline proof 2 is the one place the two meet; it names
the shared side's collation to keep the probe bounded by the batch's own scope
(`_shared_probe` in `app/opening_score_delta.py`). Equality under deterministic
collations is byte equality, so this is a plan concern only. Anything new that
joins a current-format key to a default-collated table needs the same treatment
and an `EXPLAIN` gate; the existing ones are in `test_opening_score_reader_pg.py`.

Reader correctness checks:

```bash
TMPDIR=/private/tmp pytest -q -W error test_opening_score_storage.py \
  test_opening_score_format_matrix.py test_opening_cache.py test_tree_api.py \
  test_openings_api.py test_stats_api.py test_session_openings.py \
  test_opening_freshness_signal.py test_opening_baseline_scheduler.py \
  test_opening_score_delta.py
# With explicit disposable PostgreSQL test and maintenance URLs configured:
TMPDIR=/private/tmp pytest -q test_opening_score_reader_pg.py
```

`g-score-store-qualify` owns integrated correctness and serial sustained storage/
delta-lane release measurements over the actual deployment network. The reviewed
fixture budgets and at least 500 comparable read samples remain prerequisites,
with separately reviewed production-shape ceilings required before cutover and
observation. No benchmark, activation or observation milestone is completed by
these writer tests.

## Integrated qualification (`g-score-store-qualify`)

The qualification ran on 2026-09-23 and was evaluated on 2026-09-24. Its verdict
is **pass**, which authorises READINESS for the cutover workflow and is not a
claim that production is deployed or observed. The decision record is
[opening-score-storage-qualification-2026-09-24.md](../../docs/analysis/opening-score-storage-qualification-2026-09-24.md)
beside its sealed
[JSON](../../docs/analysis/opening-score-storage-qualification-2026-09-24.json).

**Chosen design.** B50 as approved on 2026-09-19 — one wide current row set per
pair, exact full-read Python diff, Core executemany groups of at most 500, no
payload cache, no content hash, no COPY, no chunked confidence. The marker value
is `current-b50-v1`. The qualification drives the SHIPPED code
(`opening_cache.recompute_opening_scores`, the `opening_score_storage` readers,
the `/tree` builder's own loop body), never the spike's raw-DDL adapters: the
spike selected a design, this bead qualified the code that ships.

**Two revisions, not one.** The twenty-six cell reports were measured at
`0c690ac`. The evaluator that read them is `8222b9b`, run from a clean tree at
`43db250`. They are different artefacts with different lifetimes — changing how
a record is READ re-measures nothing — and the homogeneity check in §5 would
refuse a cell re-run at the evaluator's commit into this run's inputs. The
record prints both rather than printing one and implying it covers both.

### Environment and identity

PostgreSQL 18.4 by absolute path throughout; the prefix is recorded in every
report, so a run made with a different build is visible rather than assumed:

```
/Applications/Postgres.app/Contents/Versions/18/bin
```

Host `macOS-26.2-arm64-arm-64bit`, Python 3.12.7, settings digest
`a39de1bec346d79e`. Four stated differences from production are recorded in
`profile_identity.stated_differences` and carried into every artifact:
`server_version` 18.4 (Postgres.app) local against 18.6 (Debian) production;
`datcollate`/`datctype` `en_US.UTF-8` against `en_US.utf8`; `max_wal_size` where
the one permitted deviation applies; and the host itself. **`active_users_30d`
is 1**, so "production shape" here means representative SIZE and ROW MIX and
never representative traffic. Every report carries
`traffic_representative: false`, and `g-score-store-observe` inherits that
limitation.

`wal_keep_size=2GB` is a deliberate difference from the census, not an
oversight: an S3 layout-A publication writes about 160 MB against a 128 MB
`max_wal_size`, so a checkpoint could recycle the segments holding `start_lsn`
before `pg_walinspect` reads them. It changes what is retained, not what is
written.

### Both clusters

Two disposable clusters, built by explicit recipes and never reused from
anything. **QC-PROD** carries production-derived data and therefore lives inside
the mode-700 private store with `scram-sha-256` and a generated password that
never reaches a command line:

```bash
PREFIX=/Applications/Postgres.app/Contents/Versions/18/bin
STORE="$HOME/.ghostreplay-private/score-store-qualify"
umask 077; mkdir -p "$STORE/sock"; chmod 700 "$STORE" "$STORE/sock"

/usr/bin/python3 -c 'import secrets; print(secrets.token_urlsafe(32))' \
  > "$STORE/.initdb-pw"
chmod 600 "$STORE/.initdb-pw"
"$PREFIX/initdb" -D "$STORE/qc-prod" -U postgres -A scram-sha-256 \
  --pwfile="$STORE/.initdb-pw" --encoding=UTF8 --locale=en_US.UTF-8 \
  --locale-provider=libc -k
printf '127.0.0.1:55440:*:postgres:%s\n' "$(cat "$STORE/.initdb-pw")" \
  > "$STORE/qc-prod.pgpass"
chmod 600 "$STORE/qc-prod.pgpass"; rm -f "$STORE/.initdb-pw"

# Census-matched postmaster settings, ALL given at start: no ALTER SYSTEM
# apparatus, so what `ps` shows is what the cluster runs.
"$PREFIX/pg_ctl" -D "$STORE/qc-prod" -l "$STORE/qc-prod.log" -w -t 60 -o "\
-p 55440 -c cluster_name=ghostreplay-score-storage-qual \
-c listen_addresses=127.0.0.1 -c unix_socket_directories=$STORE/sock \
-c shared_buffers=128MB -c max_connections=500 \
-c checkpoint_timeout=300s -c checkpoint_completion_target=0.9 \
-c max_wal_size=128MB -c min_wal_size=32MB -c wal_keep_size=2GB \
-c full_page_writes=on -c wal_compression=off -c wal_level=replica \
-c synchronous_commit=on -c fsync=on \
-c autovacuum=on -c autovacuum_naptime=60s \
-c autovacuum_vacuum_threshold=50 -c autovacuum_vacuum_scale_factor=0.2 \
-c autovacuum_analyze_scale_factor=0.1 -c autovacuum_vacuum_cost_delay=2ms \
-c work_mem=4MB -c maintenance_work_mem=64MB \
-c default_toast_compression=pglz -c effective_cache_size=4GB \
-c random_page_cost=4 -c track_counts=on -c track_io_timing=on" start
```

Giving every GUC at start has a consequence worth knowing before a cell is
re-run: a command-line setting outranks `postgresql.auto.conf`, so on this
cluster `ALTER SYSTEM SET` plus `pg_reload_conf()` changes NOTHING. It was
measured rather than assumed — after `ALTER SYSTEM SET max_wal_size = '8GB'`
and a reload returning true, `pg_settings` still read `128 MB` with
`source = command line`. Changing any of these settings means restarting the
postmaster with different `-o` options; derive the new option list from the
RUNNING command line by substitution rather than retyping it, and diff the two
before starting so exactly one option differs.

**QC-SPIKE** is deliberately the documented spike cluster, unchanged, so the SF
tie-back is settings-matched to the approved fixture ceilings. It holds
synthetic fixture data only, which is why `/private/tmp` and `-A trust` are
correct there and would not be on QC-PROD:

```bash
PREFIX=/Applications/Postgres.app/Contents/Versions/18/bin
DATA=/private/tmp/ghostreplay-score-storage-spike
"$PREFIX/initdb" -D "$DATA" -U postgres -A trust --no-locale --encoding=UTF8
"$PREFIX/pg_ctl" -D "$DATA" -l "$DATA.log" -w -t 60 -o "\
-p 55439 -c cluster_name=ghostreplay-score-storage-spike \
-c listen_addresses=127.0.0.1 -c unix_socket_directories=$DATA \
-c autovacuum=off -c checkpoint_timeout=1h -c max_wal_size=4GB" start
```

Teardown, after the reports are retained — stop only these two clusters, and
remove QC-PROD's data directory rather than leaving production-derived pages on
disk:

```bash
"$PREFIX/pg_ctl" -D "$HOME/.ghostreplay-private/score-store-qualify/qc-prod" stop
"$PREFIX/pg_ctl" -D /private/tmp/ghostreplay-score-storage-spike stop
rm -rf "$HOME/.ghostreplay-private/score-store-qualify/qc-prod" \
       /private/tmp/ghostreplay-score-storage-spike
```

Everything derived from production stays in the private store: no user IDs,
FENs, scores or grades reach any artifact, document or bead. Only aggregates
reach `docs/analysis`.

### Snapshot, restore and capture

The census (§1.1) is read-only and is run by the user, not by the harness. The
snapshot is a fresh `pg_dump` taken 2026-09-22T06:02:34Z, and the snapshot date
is part of the profile identity. **The URL never appears on a command line**:
`pg_dump -Fc "$URL"` would expand the password into `ps` output. Instead the
dump runs in its own subshell that sets `umask 077`, reads `DATABASE_URL` from
its own environment, splits it into `PGHOST`/`PGPORT`/`PGUSER`/`PGDATABASE` and
a `PGPASSFILE`, traps deletion of both files on exit including on failure, and
invokes `pg_dump -Fc -Z6 --no-sync` with no connection argument at all.
`PGSSLMODE=require` is not optional: splitting a URL into `PG*` variables drops
any `sslmode` it carried, and libpq's default `prefer` would then allow a silent
plaintext fallback over the public proxy. A recent scheduled dump from valtron
(`docs/backups.md`) is an equally valid source and is preferred when one is
fresh enough, since it touches production not at all.

Restore into the template, then verify — never migrate the template:

```bash
export PGPASSFILE="$STORE/qc-prod.pgpass"
"$PREFIX/createdb" -h 127.0.0.1 -p 55440 -U postgres gr_snap_base
"$PREFIX/pg_restore" -h 127.0.0.1 -p 55440 -U postgres -d gr_snap_base \
  --no-owner --no-acl "$STORE/prod-20260922T060234Z.dump"
"$PREFIX/psql" -h 127.0.0.1 -p 55440 -U postgres -d gr_snap_base \
  -c 'VACUUM (FREEZE, ANALYZE)' -c 'CHECKPOINT'
```

Verify about 1 GB free first. Run `VACUUM (FREEZE, ANALYZE)` and `CHECKPOINT`
immediately after the restore and again after every template clone, so
autovacuum has no backlog to work through inside a timed cell. `gr_snap_base`
legitimately trails Alembic head — it restored at `20260920_01` against a repo
head of `20260923_01` — and each clone is migrated on its own; making the gate
pass is never a reason to migrate the template.

Payload capture (§1.3) is the only step that runs the real writer and mutates
evidence on production-derived data, so it carries its own guard, its own
provenance sentinel and a closure check against an independently recomputed
control database:

```bash
cd backend && source .venv/bin/activate
env -u DATABASE_URL -u DATABASE_PRIVATE_URL PGPASSFILE="$STORE/qc-prod.pgpass" \
  TMPDIR=/private/tmp POSTHOG_DISABLED=true \
  GHOSTREPLAY_STORAGE_QUAL_ADMIN_URL=\
'postgresql+psycopg://postgres@127.0.0.1:55440/postgres' \
  GHOSTREPLAY_STORAGE_QUAL_CLUSTER=ghostreplay-score-storage-qual \
  GHOSTREPLAY_STORAGE_QUAL_PG_PREFIX="$PREFIX" \
  python -m scripts.qualify_opening_score_capture capture \
    --run-id r1 --pair-index 0 --census "$STORE/census-20260921.json" \
    --cutoffs 100 --output "$STORE/capture-s1-r1.pickle"
```

The admin URL carries no password — `PGPASSFILE` supplies it — so no connection
string reaches argv or a process listing. The S1 capture closed at 24,848
logical rows with `equal: true` and `pinned_clock_matches: true`; S0 closed at
245.

### Cells

One cell per process, serially, with no other writer on either cluster. Each
invocation creates a fresh `gr_score_qual_<run-id>_<cell>` database, runs the
cell and drops it:

```bash
env -u DATABASE_URL -u DATABASE_PRIVATE_URL PGPASSFILE="$STORE/qc-prod.pgpass" \
  TMPDIR=/private/tmp POSTHOG_DISABLED=true \
  GHOSTREPLAY_STORAGE_QUAL_ADMIN_URL=\
'postgresql+psycopg://postgres@127.0.0.1:55440/postgres' \
  GHOSTREPLAY_STORAGE_QUAL_CLUSTER=ghostreplay-score-storage-qual \
  GHOSTREPLAY_STORAGE_QUAL_PG_PREFIX="$PREFIX" \
  python -m scripts.qualify_opening_score_storage run \
    --cell C1 --capture "$STORE/capture-s1-r1.pickle" \
    --profile S1 --copies 1 --run-id r1 --output "$STORE/cells-r1/C1-S1.json"
```

`--copies` is the size multiplier: S1 is 1×, S2 2×, S3 4× of the captured
24,848 logical rows, giving fit points at 245 / 24,848 / 49,696 / 99,392.
SF cells use the fixture capture and point at QC-SPIKE (port 55439, cluster
`ghostreplay-score-storage-spike`, no passfile). Twenty-six reports were
produced: C1 at S0–S3 and SF, C2 at S1–S3, C3 at S0–S3 and SF, C5 at S1 and SF,
C6 at S0–S3 and SF, C4 at S1 and SF for both `A_old` and `A_new`, and C7 in both
storage formats. Every report carries the same identity block, so none is exempt
from the homogeneity check; only `A_old`, and only on REVISION, is exempt,
because it IS the predecessor commit by construction.

### Evaluation

Every required result is an evaluator INPUT, so a result that was never run is a
recorded gap rather than silence:

```bash
python -m scripts.summarize_opening_score_qualification \
  --run-id r2 --output ../docs/analysis/opening-score-storage-qualification-2026-09-24.json \
  --cell "$STORE/cells-r1/C1-S0.json"  … --cell "$STORE/cells-r1/C2-S3.json" \
  --memory "$STORE/cells-r1/C3-S0.json" … --network "$STORE/cells-r1/C6-S3.json" \
  --plateau "$STORE/cells-r1/C5-S1.json" \
  --control "$STORE/cells-r1/C4-S1-A_old.json" \
  --control "$STORE/cells-r1/C4-S1-A_new.json" \
  --fixture-control "$STORE/cells-r1/C4-SF-A_old.json" \
  --fixture-control "$STORE/cells-r1/C4-SF-A_new.json" \
  --fixture-cell "$STORE/cells-r1/C1-SF.json" … \
  --delta-lane "$STORE/cells-r1/C7-S1-legacy.json" \
  --delta-lane "$STORE/cells-r1/C7-S1-current.json"
```

It writes the JSON and the adjacent `.md` decision record together. Run it from
a CLEAN tree: `evaluator_revision` records `<sha>-dirty` otherwise, and a dirty
hash names neither artefact.

### Recorded results

50 gates: 45 pass, 5 deferred, no failures, no insufficient gates and no
coverage gaps. The A-relative gates are the acceptance gates, because ratios
transfer across hosts and absolute local numbers do not:

| gate | limit | measured range |
|---|---|---|
| combined WAL | ≤ 0.5 × A | 0.052 – 0.191 |
| vacuumed footprint | ≤ 1.0 × A | 0.056 – 0.074 |
| publication p95 | ≤ 1.1 × A | 0.391 – 0.832 |
| composite D read p95 | ≤ 1.1 × A | 0.475 – 1.011 |
| composite T format-stage p95 | ≤ 1.1 × A | 0.132 – 1.010 |

The C4 legacy-retirement control passed at both sizes — `A_new` publication p95
at 0.573× `A_old` at S1 and 0.855× at SF — so the already-active atomic
retirement did not regress the production-default legacy writer. C7's delta lane
passed in BOTH storage formats against §4.9's 3000 ms warm
whole-graph-contention bound: legacy 1851.7 ms normal / 1851.5 ms drill, current
1873.8 ms normal / 1792.9 ms drill.

Fitted `production_shape` ceilings, each as fixed overhead plus a per-logical-row
slope with its applicable size range, 1.5× headroom applied:

| ceiling | value | applicable range | note |
|---|---|---|---|
| post-checkpoint publication WAL | 28,573,696 B at 95,224 rows | 23,806 – 95,224 | production-applicable |
| warm publication WAL | 4,980,736 B at 99,392 rows | 245 – 99,392 | LOWER BOUND |
| vacuum WAL per ten publications | 1,048,576 B at 99,392 rows | 245 – 99,392 | |
| vacuumed footprint | 71,303,168 B at 99,392 rows | 245 – 99,392 | |

C2's post-checkpoint figure is the production-applicable one and C1's warm
figure is a lower bound: `active_users_30d` is 1 and the gaps between
publications are expected to exceed `checkpoint_timeout`, so nearly every
production publication is post-checkpoint.

Five `local_host_only` ceilings are **deferred to `g-score-store-cutover`** and
are that gate's EXPECTATION and a local regression baseline, never production
limits: publication p95 2507 ms, composite D read p95 138 ms, composite T
format-stage p95 28 ms, integrated worker RSS 565,731,541 B, publication
allocation peak 239,735,198 B — all at 99,392 logical rows. An aggregate `pass`
with these deferred is correct, because the A-relative counterpart of each is
measured and enforced here.

**One recorded settings deviation**, permitted by §9.5 and named in the decision
record: `max_wal_size` raised from the census 128 MB to 8 GB for C1 only, after
observed discards left fewer than two complete paired blocks. C1's warm ceiling
is already a lower bound, so the deviation cannot loosen the
production-applicable ceiling, which is C2's and keeps the census value. Pool
identity includes the settings digest, so nothing is pooled across the deviation
in either direction.

The upstream seal has drifted: six of the twelve source digests recorded in the
approved budget report no longer match, so `summarize_opening_score_budgets` and
`remeasure_opening_score_budgets` would raise `measured source changed` against
it. Comparability for the SF tie-back therefore rests on the regenerated
timeline's deterministic fields. The tie-back was never an acceptance gate.

### Plateau, and what layout A actually does

Fixed live counts, proved reclamation and clean orphan checks are gated for BOTH
layouts at both sizes — those are the leak proofs, and all of them passed. The
last-five growth GATE is the SELECTED DESIGN's only: the approved budget places
the 5% rule under `selected_design_ceilings`, and layout A enters that budget
solely through the A-relative ratios, none of which is a plateau. B50 measured
0.00000 against the 0.05 limit.

Layout A's growth is a characterisation. A ten-window C5 at `43db250` with
QC-PROD at the census `max_wal_size` — deliberately EXCLUDED from the verdict,
because its `tested_revision` differs and §5 compares revisions for equality —
answers what the six-window cell could not:

| profile | layout | last-five growth | vacuumed window series (MiB) |
|---|---|---|---|
| S1 | A | 0.00005 | 84.59 132.63 140.91 141.83 142.09 142.10 142.11 142.11 142.11 142.11 |
| S1 | B50 | 0.00000 | 10.45 10.46 × 9 |
| SF | A | 0.06782 | 12.09 18.57 18.45 19.75 18.55 19.80 18.55 19.80 18.55 19.80 |
| SF | B50 | 0.00000 | 1.84 × 10 |

**A does plateau at production shape.** The six-window cell read windows 1–5,
which are the steepest part of the settling curve, and reported 0.07139; the
ten-window series reproduces those six windows to the byte and then flattens, so
its last five are windows 5–9 and read 0.00005. The constant was the defect, not
the layout. At the fixture size A instead settles into a period-2 limit cycle,
alternating between exactly 19.80 and 18.55 MiB indefinitely — retention, not a
leak, confirmed by `fixed_live_counts` and proved reclamation — and no number of
additional windows would cure a relative threshold on a footprint that small.

The statistic itself changed with the same review: `(max - min) / min` over the
last five windows, which equals the superseded `(last - first) / first` on a
monotone series and is strictly larger on one that oscillates. The SF cycle is
exactly the case the old formula would have read as zero growth.

Final vacuumed footprints in that run: at S1, A 99.84 MiB against B50 14.24 MiB
(7.01×); at SF, A 13.50 MiB against B50 2.21 MiB (6.11×). The earlier six-window
cell's final footprints agree to within 8 KB, which is the reproducibility check
on the whole cell.

### Verified release revision, forward conversion and the cutover sequence

The verified release revision is `43db250` on `master` — cells at `0c690ac`,
evaluator logic in `8222b9b`, harness change in `43db250`. The full pre-push
gate passed on it with nothing bypassed.

**There is still no production activation switch**, and this section does not
create one. `opening_cache.default_storage_format()` returns
`StorageFormat.LEGACY` and is the single patch point, resolved in the writer
BODY rather than as a def-time default. Introducing a real switch, and the
authority to flip it, belongs to `g-score-store-cutover`.

Forward conversion is per pair and explicit. `convert_pair(db, user_id, color,
StorageFormat.CURRENT)` reserves a generation, reads the source under the
publication lock and republishes it in the target format, preserving
`computed_at` and every evidence stamp; it performs no scoring and reinterprets
no evidence. It requires a fresh transaction — including when there is nothing
to convert — and raises `ValueError("conversion requires a fresh transaction")`
otherwise. Absent and already-converted pairs are skipped without reserving a
generation. There is no startup fleet sweep and none should be added.

A conversion participates in ordinary publication ordering in both directions:
an intervening higher reservation supersedes it normally, and it can itself
supersede an earlier-reserved rebuild that held fresher evidence. Generation
order is publication order and not a freshness claim, so the next ordinary
evidence freshness check is what schedules the rebuild; nothing needs to force
one.

The cutover runs with a **single publisher**, which is what this deployment has
anyway. The sequence:

1. Satisfy the `g-score-store-cutover` pre-activation gate first. A matched
   A/B50 run over the real application-to-database path must produce the
   absolute publication p95, composite D/T read p95 and worker RSS ceilings this
   bead could not, and confirm the A-relative ratios hold there within ≤ 1.1.
   The deferred numbers above are its expectation, not its limits.
2. Deploy the compatibility binary. Readers already serve either format, so this
   step changes nothing observable and can precede the decision to convert.
3. Quiesce publication. One worker, and no second publisher on the same pair —
   the two-int4 transaction advisory lock (class `0x47525343`) makes a race
   safe, not free, and a converted pair flipping back mid-window invalidates any
   measurement taken across it.
4. Convert each pair with `convert_pair(..., StorageFormat.CURRENT)`, one fresh
   transaction per pair, tolerating `PublicationSuperseded` as a normal outcome
   and re-reading the pair rather than retrying blindly.
5. Only then switch new writes to `StorageFormat.CURRENT`. Real rebuilds also
   convert lazily when their target format differs, so a pair missed in step 4
   converges on its next rebuild rather than breaking.
6. Hand the observation window to `g-score-store-observe`, whose acceptance
   thresholds come from its own window and never from the local figures here.

### Rollback within the compatibility binary

Rollback is a REVERSE CONVERSION, not a flag flip, and the order matters.

`convert_pair(..., StorageFormat.LEGACY)` writes a complete legacy snapshot and
clears the pair's current rows in the same transaction that retires the previous
marker — atomic marker retirement on format switching is what makes the
intermediate state unobservable. Publication of a legacy batch whose predecessor
was not legacy also deletes every current-format row for that pair, so the
reverse path leaves nothing behind for an old reader to trip over.

To return to a pre-compatibility binary:

1. **Disable new current writes.** Necessary and, on its own, **insufficient** —
   it stops the format spreading, and converts nothing already written.
2. **Keep both readers and the publication guard in place** for the whole
   rollback. They are what allows a mixed fleet to serve correct results while
   pairs are converting; removing either one mid-rollback is what turns a slow
   rollback into an outage.
3. **Drain publishers**, then reverse-convert **every** pair before starting an
   old binary. A single unconverted pair is an unreadable pair for a binary that
   has never heard of `opening_current_*`.
4. **Then, and only then, run the downgrade.** Migration `20260919_03` refuses
   while anything current remains, with two distinct refusals:
   `reverse-convert current opening scores before downgrade` when any
   `opening_current_roots` / `_positions` / `_edges` / `_scope` row survives, and
   `reverse-convert current opening markers before downgrade` when any
   `opening_score_batches.storage_format <> 'legacy'` marker survives. The second
   check exists because an EMPTY current publication is a marker with no payload
   rows — still incompatible with an old reader, and invisible to a payload-only
   check. Orphan payloads are deliberately included rather than silently dropped:
   dropping them would hide an incomplete conversion.

`test_opening_score_storage_pg.py::test_pg_conversion_retirement_and_downgrade_guard`
exercises exactly this order — convert, refuse, publish an empty current batch,
refuse again on markers, reverse-convert, downgrade — under a non-C database
default, so the machine-key collation is exercised too.

### What this does not authorise

Passing authorises readiness for the cutover workflow. It does not deploy,
activate or observe anything. The deferred gate on `g-score-store-cutover`
blocks B50 activation for any production pair, and its option (b) — an
explicitly authorised maintenance window on production with one pair converted
and reverse-converted — needs its own authorisation, which nothing in this
qualification grants. Material divergence from the spike (§5.5) returns to a
reviewed design/budget decision before any further migration; layout A's
plateau is a characterisation and stops nothing.
