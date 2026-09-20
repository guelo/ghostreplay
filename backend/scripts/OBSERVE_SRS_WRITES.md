# SRS write observations and retention census

Authority: `g-srs-observe-window` and its parent `g-compact-srs-events`.
Implementation and the baseline-ready census were completed under the closed
`g-srs-write-telemetry`. This tooling does not select M, freeze evidence, prune
opportunity rows, or approve a release. A code review/commit completes the
implementation stage only. Deployment verification, a current census,
approximately 30 representative days, a longer historical repair audit, and the
product decision remain separate stages.

> **Status 2026-09-20 — collection is intentionally OFF, and the observation
> track is closed.** The instrumentation is deployed and live, but
> `GHOSTREPLAY_SRS_TELEMETRY_DIR` is unset, so nothing is collected. That is a
> decision, not a misconfiguration.
>
> The census measured one active user over the trailing 30 days. On an
> unreleased, effectively single-user app, a 30-day window would have measured
> the operator's own play, so it could never be the *representative* observation
> Gate B requires. Rather than run a window that licenses nothing, the owner
> chose a conservative retention horizon directly from the census and dropped
> the measurement track as over-engineering for this stage.
>
> **Decision: M = 60 days, G = 1 hour**, accepting the unmeasured late-write
> tail explicitly. At the 2026-09-20 census that folds 77.3% of rows (418,615 of
> 541,878) against Gate A's 50% floor, and costs about 18 MiB more than M = 30
> would. Target pins held back nothing at any horizon tested (30/45/60/90 days:
> zero old-session pins, zero extra residence), so the horizon trades storage
> against late-write safety and nothing else. Authority for the number is
> `g-compact-srs-events`.
>
> **Do not enable collection or start an observation clock to "finish" this.**
> The tooling stays in the tree, tested and ready, for if this app gets real
> users and an M shorter than 60 days ever becomes worth evidencing. Two things
> would need solving first: the service has no volume, and a Railway volume
> binds to one service, so the independent hourly expiry job below cannot simply
> be a second service sharing the spool.

## Private collection

Set `GHOSTREPLAY_SRS_TELEMETRY_DIR` to an existing absolute directory with mode
0700 on a private persistent volume. Collection is disabled when unset. The
directory must be outside every Git worktree and cloud-synced directory; do not
use a developer checkout, Documents, Desktop, or an ephemeral deployment disk.
The collector creates `observations.sqlite3` with mode 0600. Do not back up or
export this spool, send it to analytics, or attach it to a tracker. Use one spool
per host/replica, including hosts running manual repairs. The writer validates
registered worktrees when running in a Git checkout; packaged deployments do not
need Git. Mount/configure the directory before enabling the environment variable.
The application lifespan validates the collector before starting workers or accepting
requests. Expect the static `srs_write_telemetry collector_ready` message, or a
single `srs_write_telemetry collector_unavailable` error for invalid/unavailable
configuration. The latter is cached for that configured path, so events do not
retry validation or spawn Git subprocesses. Fix configuration and restart before
beginning the observation period. Gameplay remains available while collection is
unavailable; do not start or continue a qualified observation clock in that state.

The spool uses SQLite WAL with `synchronous=NORMAL` on a **local** private volume;
WAL requires all users of a spool to be on the same host. Do not use a network
filesystem. Include the `-wal` and `-shm` sidecars in the volume's privacy/expiry
boundary. An OS/power failure can lose recent NORMAL-mode writes; reconcile such
incidents as observation gaps, not proof of no writes.

Rows expire **45 days after initial observation**, including pending operations.
Completion/coalescing does not renew that deadline. Reports prune on read;
writes and the evidence worker run expiry at most once per hour, including worker
startup and idle periods. Expiry uses SQLite secure deletion and a truncating WAL
checkpoint; a busy checkpoint logs a collection gap and must be retried. Physical
removal has up to one hour of scheduling lag. Provision an **independent hourly expiry job**
for every volume, including repair hosts and stopped/disabled application hosts:

```bash
cd backend && source .venv/bin/activate
python scripts/report_srs_writes.py --private-dir /private-volume/srs --expire-only
```

Verify that job's execution and alert on missed expiry or insufficient disk. Delete
the private volume when this observation project finishes. Volume snapshots,
backups, clock jumps, and offline hosts need explicit operator treatment: the
application cannot expire an inaccessible disk or a provider backup. Disable
provider backups for this volume. Keep aggregate reports before the raw TTL
expires; do not extend TTL to preserve a stalled observation project.

The collector writes only a session identifier, immutable session start, bounded
source labels, a random transaction/job identifier, mutation/outcome flags, clock
sample, and expiry metadata. It contains no moves, FEN, score, user identifier,
SQL, exception message, or API payload. Sink/clock failures log only
`srs_write_telemetry observation_missing count=N`; **N is process-local**, so logs,
process restarts and collector health must be reconciled independently. A missing
spool cannot prove there were no writes. Collection failures never deliberately
fail, commit, or roll back the gameplay/repair transaction.

## What the observations mean

The worker registers a pending observation before acquiring the graph advisory
lock or setting transaction timeouts; `_compute_blunder_opportunity_events` reuses
it, or registers one for a direct/session-repair caller. Blunder-grain repairs
register only at actual upsert or stale/pre-creation deletion sites; scanning an
unaffected session adds no row. Upserts and deletions mark actual evidence mutation.
SQLAlchemy root transaction events distinguish confirmed commit, rollback,
abandonment/close, and an ambiguous commit failure. A savepoint-local writer is
reported as unsupported instead of claiming a successful root write. Current
production writers use root transactions. Newly introduced write paths or nested
transactions require coverage review before extending the observation period.

After the root transaction ends and returns its connection, a separate connection
samples PostgreSQL `clock_timestamp()`. For confirmed commits this is a
**conservative completion upper bound**: it follows the durable commit, including
lock waits and any commit delay. Worker completion clock/spool work follows the
`evidence_commit` timer, after the advisory lock has been released. Queue spool
writes also run outside the scheduler lock, and stored job states advance
monotonically so a late enqueue cannot erase completion. The query neither starts an ORM transaction nor
changes its isolation/timeouts. SQLite's clock is a test seam only. A successful
commit with a missing clock remains an explicit observation gap. A failed commit
acknowledgement is conservatively unknown, even if a later rollback succeeds.
Do not substitute event `created_at`, `ended_at`, receipt time or queue time.

The per-operation age distribution is at **session/transaction grain**: a bulk
transaction touching multiple sessions has one completion bound for each session.
The distinct-session distribution takes MAX across its successful mutations.
Committed no-op recomputes remain visible but do not advance last evidence write.
Coalesced jobs retain all upload source labels, so source cohorts overlap. The
queue record is diagnostic, never a successful write. A queued/started record with
no completion exposes process death; a finished opportunity job without a
transaction observation exposes a missing instrumented path. Ordinary incremental
jobs that intentionally do not request opportunity are reported as `not_requested`.
This also applies to their enqueue failures, worker failures, timeouts and drops.
A cache/recompute failure after a successful evidence commit leaves the SRS job
finished; its transaction/clock observation still determines coverage.

| Path | Source/observation | Verification |
| --- | --- | --- |
| Ordinary uploads | `upload_ordinary`; opportunity request bit preserved | Deferred worker + transaction commit |
| Final uploads | `upload_final`, independently of recompute bit | Verify actual worker completion after receipt |
| Legacy clients | `upload_legacy` when recompute field omitted | No final-upload-only denominator |
| Reverts/replacement lines | `upload_revised_line` when a nonzero revision requests recompute | Includes later uploads of the revised line; protocol cannot uniquely label reverts |
| Coalesced/deferred worker | Queue ID joins all transaction attempts | Delay, restart/kill gap, non-draining shutdown, start failure |
| Lock timeouts | `retry`, rolled-back attempts, `dropped` on exhaustion | Both retry success and exhaustion |
| Four repair modes | `repair_session`, `repair_all_sessions`, `repair_blunder`, `repair_all_blunders` | Existing per-session/per-batch commit boundaries, deletion-only repairs |
| Other failures | Rolled-back/abandoned/unknown outcome plus worker failure | Sink and database-clock failures must preserve application behavior |
| Frozen attempts | `frozen_attempt(db, session)` integration seam | **Not yet an active runtime path.** Retention implementation must call it before every explicit/bulk frozen skip and verify it before activation |

Raw SQL maintenance outside these writers is not automatically observed. Inventory
such tools and audit their use; no “all repair coverage” claim is valid while an
external writer remains uninstrumented. Move-only evaluation repair, empty uploads,
and evidence-ineligible uploads do not run an SRS opportunity writer. Keep those
exclusions in the deployed path inventory. A new retention skip must remain visible
even though it writes no evidence, or the shorter-horizon tail becomes censored.

## Aggregate report

```bash
cd backend && source .venv/bin/activate
python scripts/report_srs_writes.py --private-dir /private-volume/srs
```

Repeat `--private-dir` to include all replica/repair volumes available privately on
the reporting host. Do not copy private rows into the repository to combine them.
The report includes session maxima, operation distributions, overlapping source
cohorts, absolute late counts, frozen attempts, and unresolved observations. It
also reports uncensored session coverage using successful writes **plus frozen
would-have-written attempts**. Success-only coverage must not be used once a
cutoff is active. `--candidate-days N` evaluates an explicitly supplied candidate
alongside the 30-day baseline; it never chooses or activates a candidate.

Reports always leave `gate_b_eligible=false`: the command cannot verify deployment,
representativeness, external missing observations, historical repair coverage or
product approval. A numeric 99.9% result alone is insufficient. Review absolute
late session/operation counts and categories; a busy session's repeated uploads
must not dilute affected-session rates. Record workload gaps, deployment gaps,
maintenance/incident periods and clock/failover anomalies even if numeric coverage
looks good. The aggregate report itself contains no session or job identifiers.
Age distributions (including source cells and maximum) are suppressed for fewer
than five distinct sessions; repeated operations from one session do not defeat
suppression. Counts and candidate coverage remain visible for the absolute-tail
review. This is small-cell protection, not a general anonymization guarantee;
review aggregate combinations before putting production results in a tracker.

Both reporting and `--expire-only` require an existing, valid spool; an absent
file/table produces an error and no report. Reporting streams rows once, retaining
numeric age samples and session/job aggregates rather than all row dictionaries.
Large real mutation cohorts still require memory proportional to their samples.

## Read-only census

Run before the independent decision-retention work prunes original envelopes.
Use reviewed database access with credentials supplied through the environment,
never a URL/password/user ID in a recorded command. From the activated venv:

```bash
python scripts/census_srs_retention.py
```

This uses one read-only REPEATABLE READ transaction, one database
`clock_timestamp()` as-of, and a 60-second per-statement timeout. It queries original
event/session/blunder/decision tables and emits aggregate JSON only. It does not
use telemetry to guess last writes from historical event rows. Optional
`--candidate-days N` values must come from the reviewed observation report; 30 days
is always presented as a possible baseline. G is the proposed one hour, not a
verified request-lifetime guarantee.

Diagnostic age cohorts overlap. Only `foldable_union` is the deduplicated eligible
set for a scenario: session start + M + G has passed, no individually eligible
current target pins the pair, and the event passed the ordinary timestamp audit.
The target window is inclusive, with creation eligibility applied before grouping;
duplicate decisions for a pair do not multiply pins. Broad-ineligible reached rows
still retain their targeted pin. NULL/mismatched/future timestamps, including
converted-session times, require a separate explicit audit and are conservatively
excluded from foldability. This is not permission to normalize their timestamps.

The report separates normal/drill/converted and active sessions, observed broad
fanout, all targeted pairs, targeted pairs with event rows, targets without uploads,
target ages, extra residence and original replay storage. Physical total/index/TOAST
sizes are separately measured; they can change concurrently with a logical
snapshot. Cohort allocation estimates prorate physical bytes by row count;
payload bytes are measured directly. Do not present prorating as an exact physical
attribution to a session. Two censuses are required for actual net decision growth;
`served_30d` is retained arrival volume, not net growth.

Projections use observed recent sessions/day, F and T_e for each cohort:

`sessions/day × [F × (M + G) + T_e × 30 + F × 1 day cleanup lag]`

Current/10x/100x raw rows and allocated bytes are scenarios, using the current
all-in event bytes/row. Recently started sessions have incomplete lifetime fanout.
Summary/state/recovery-export storage is not implemented here and is explicitly
excluded; measure it in the equivalent compaction fixture before claiming Gate A's
net 50% storage reduction. Decision storage and its arrival projections are
reported independently. The census cannot prove the full subsystem is bounded.

## Release and observation milestones

Before starting the observation clock, review/deploy the instrumentation, verify
the actual Railway commit, enable private collection and independent expiry on
every writer host, then exercise and reconcile every applicable path above in the
deployed version. Verify silent/idle expiry and collector-failure alert delivery.
Measure telemetry latency/connection-pool overhead under the release workload;
local correctness tests do not establish production capacity. Confirm database
clock/failover assumptions. Record a database-sourced UTC observation start and an
expected report date about 30 days later. Frozen coverage is required before any
retention activation; the currently absent frozen path is not “verified deployed.”

The baseline-ready milestone requires a **current** census, target pins, two-point
decision growth, projection limitations, verified instrumentation, clock start,
gaps and expected report date. It does not claim that 30-day evidence exists.
P0-B may independently accept the 30-day baseline's unknown late-write tail, subject
to the parent's other gates. No shorter M can be approved from this milestone.

During the period, reconcile every process/repair spool and aggregate health log.
Save aggregate snapshots before expiry. Separately audit all available longer-term
repair/maintenance/incident history, recording its actual covered dates, operation
and distinct-session counts by repair category/old-age band, missing intervals and
limits of timestamp confidence. Legacy history does **not** establish last actual
write time retrospectively; mark that gap instead of using preserved creation time
or final receipts. Do not mix a sparse historical audit into the busy-upload
denominator. Keep its source rows private and subject to finite expiry too.

At about 30 days, report the representative coverage, uncensored tail, historical
audit and unresolved gaps to the parent. Product review must explicitly acknowledge
the excluded tail. Keep this bead in progress through that report even if a
separately approved baseline ships first. A later shortening is a separate
conditional decision; no command here activates it.

## Validation

```bash
cd backend && source .venv/bin/activate
pytest test_srs_write_telemetry.py test_session_evidence_scheduler.py \
  test_recompute_srs_opportunities.py test_session_graph_lock.py -q -W error
pytest test_srs_write_telemetry_pg.py \
  test_pg_gate_plugin.py::test_manifest_matches_real_pg_gate_collection -q -W error
```

The PostgreSQL tests require the repository's disposable test database environment
(`GHOSTREPLAY_TEST_PG_URL`); never point that variable at production. They are
registered in the required PostgreSQL gate. They verify post-commit wall time,
rollback, duplicate/expired/current targets, broad-ineligible reach, exact target
window equality, conservative legacy audit, census immutability and private output.
