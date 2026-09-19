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
there is no deployment activation flag yet. Legacy readers explicitly reject
current-format markers until reader integration lands. Maintenance calls require
a fresh session transaction. Legacy bulk insert paging and timing events are
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

`g-score-store-readers` supplies compatible APIs and freshness/baseline consumers;
`g-score-store-qualify` owns integrated correctness and serial sustained storage/
delta-lane release measurements over the actual deployment network. The reviewed
fixture budgets and at least 500 comparable read samples remain prerequisites,
with separately reviewed production-shape ceilings required before cutover and
observation. No benchmark, activation or observation milestone is completed by
these writer tests.
