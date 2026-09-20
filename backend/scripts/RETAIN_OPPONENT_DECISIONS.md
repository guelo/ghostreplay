# Opponent decision retention runbook

The authoritative operator procedure for bounding opponent replay history: the
targeting expansion first, then census and R selection, activation, the
seven-day no-deletion interval, the hourly cleanup job and its rollback limits.

## Expansion and targeting handoff

`g-decision-facts` supplies the additive schema, atomic dual writer, backfill,
verification and selectable counter source. It does **not** initialize deadlines,
enforce expiry, prune rows, deploy, or activate production readers. Those steps
belong to `g-decision-expiry`, `g-decision-cleanup` and `g-decision-rollout` under
the shared design in `g-retain-decisions`. Keep cleanup disabled throughout this
handoff. The parent requires one integrated release and a seven-day no-deletion
interval before its separate cleanup activation.

### Storage and reader contract

Migration `20260919_01` adds `opponent_target_facts`, keyed by `(session_id,
blunder_id)`, and nullable `game_sessions.opponent_decisions_expires_at`. Existing
deadlines stay NULL. The migration does no data backfill. Session FKs cascade;
target FKs retain the existing restriction. The expansion needs a brief schema
lock, bounded by a five-second PostgreSQL lock timeout.

The independent drill-route migration `20260919_02` landed first, so this
expansion follows it: `20260818_01` → `20260919_02` → `20260919_01`.
Alembic follows `down_revision`, not the numeric ordering of revision names.

`app.api.game._record_decision` publishes a fact only after winning the envelope
insert and uses that insert's returned `served_at`. Envelope and fact commit in
the same transaction. An older timestamp cannot overwrite a newer one. Replays,
fingerprint losers and untargeted decisions do not write facts. A failed fact
write rolls back the envelope. The future expiry work must preserve this contract
when it supplies a fresh database timestamp from its guarded insert.

`OPPONENT_TARGET_SOURCE` accepts exactly `decisions` (default) or `facts`. Readers
select one source; they never union the two. The setting is an operational
checkpoint, not a persisted readiness flag. The API validates it in lifespan,
before accepting traffic or starting workers, so a mistyped value fails startup.

`app.opponent_target_facts.current_target_pairs` is the shared counter/future-pin
query contract. A pair is current iff its served timestamp is at least both the
30-day cutoff and the target's creation time. Both comparisons are inclusive.
MAX preserves these lower bounds, including a session with attempts on both
sides of a boundary. Live callers retain target ownership, requested IDs and
session exclusion. Existing application `now`/scoring interfaces are unchanged;
historical test times do not constitute a historical API.

`app.srs_opportunity.targeted_counters_query` LEFT JOINs current reached evidence
in the same SQL statement/snapshot. Reached requires both `opportunity` and
`reached`, with no broad-event date/creation predicate. Missing uploads count as
failed attempts; later uploads, reversions and repairs can change only the
numerator. Broad-ineligible reached pairs must remain available to this join.

### Coverage before switching

This operator command requires PostgreSQL. Execute on the primary database using the existing approved deployment
credentials. The commands below read the normal backend database environment;
do not place credentials in command arguments. In the repository:

```bash
cd backend
source .venv/bin/activate
python scripts/backfill_opponent_target_facts.py
python scripts/backfill_opponent_target_facts.py --apply
```

The first command is read-only verification. `--apply` commits an idempotent,
grouped-MAX upsert over original targeted decisions, then verifies in a fresh
PostgreSQL REPEATABLE READ, READ ONLY transaction at one sampled database time.
It tolerates concurrent new dual writers. At the current table scale the backfill
is a single statement/transaction, without per-row commits or a watermark.
The command refuses `--apply` if **any** session has a non-NULL
`opponent_decisions_expires_at`, even when that deadline is in the future. Finish
backfill before initializing deadlines. The check is a preflight guard; do not
initialize deadlines concurrently with backfill. Refusal exits with status two
without writing facts; read-only diagnosis remains available.

The aggregate JSON report includes original/fact pair counts, missing pairs,
unequal timestamps, extra pairs, per-target counter mismatches, changed-pair
count, check time and `matches`. It emits no session IDs, target IDs or payloads.
Exit status is zero only when exact MAX coverage and current counters match;
incomplete coverage returns one. A failed comparison never changes the reader
setting. Investigate extra/ahead facts rather than deleting or regressing them.

The rollout owner must perform these steps in order:

1. Verify the actual Railway revision, deploy dual writers everywhere, and drain
   old processes and in-flight old-writer transactions.
2. Rerun `--apply` after the drain, then verify `matches=true`. A check before
   draining old writers cannot certify later coverage. Keep original envelopes
   and all facts during this checkpoint.
3. Record the check time and aggregate result. Select `OPPONENT_TARGET_SOURCE=facts`
   for all readers and any implemented sibling pin consumer before any pruning.
   Repeat ordinary gameplay/library checks with the new reader.
4. Leave deadlines, expiry activation, retention R selection, the seven-day
   no-deletion interval and deletion approval to the remaining child issues.

During the no-deletion period readers can switch back to `decisions`. After any
pruning starts, this full-history backfill/comparison is no longer valid: old
envelopes can resurrect expired facts, and deleted envelopes cannot be a complete
oracle. Do not run it then or revert to envelope-backed counters. No automatic
read fallback, cleanup job, readiness table or second retention watermark is
introduced by this expansion.

### Sibling SRS pin handoff

At implementation of this child, `g-srs-target-publish` and `g-srs-fold-recovery`
are still unimplemented and no sibling compactor/pin consumer is enabled in the
application. This is not a blocker for additive fact storage. The rollout owner
must recheck their actual deployed state before pruning.

A future pin consumer must use the same source selection and exact current-pair
predicate above, initially reading decisions until this coverage checkpoint is
complete. It must retain reached rows even when broad evidence is ineligible.
Its own publication state SHARE lock precedes session/blunder write locks and
is held through envelope + fact commit. This child adds no such state or lock;
it does not implement broad-event folding or change the separately reviewed
legacy-response projection.

### Validation of the expansion

Focused tests (including required PostgreSQL cases when test URLs are configured):

```bash
pytest -q -W error test_opponent_decision_record.py \
  test_opponent_decision_retention_migration.py test_srs_opportunity.py \
  test_blunder_list_api.py test_opponent_target_config.py test_rating_serialize.py \
  test_pg_gate_plugin.py::test_manifest_matches_real_pg_gate_collection
```

The PostgreSQL cases cover populated migration/ordinary FKs, microsecond counter
parity, a live winning dual write racing MAX backfill, real fingerprint contention,
and fact/reached deletion after the reader's statement snapshot starts. The
concurrency tests use database-observed lock barriers and independent connections.
SQLite covers fixture parity, rollback, replay/non-renewal, MAX reruns, default
reader behavior and the mutable reached cases. The complete required PostgreSQL
gate remains `GHOSTREPLAY_REQUIRE_PG_TESTS=1 pytest -m pg_gate --strict-markers`.

Index check, 2026-09-19: PostgreSQL 15.18, isolated temporary tables with 12,000
synthetic facts, 200 targets, ages spread over 60 days, and reached rows for one
third of targets. After ANALYZE, EXPLAIN (ANALYZE, BUFFERS) of the actual counter
query for three targets used the `(blunder_id,last_served_at)` bitmap index,
scanned 60 qualifying facts and performed indexed reached lookups (0.203 ms
execution). An ordered 100-row old-fact selection used the
`(last_served_at,session_id,blunder_id)` index without a Sort (0.081 ms execution).
This supports keeping both indexes for their distinct access patterns; these
local synthetic times are not production latency claims. The cleanup child
still owns its locking/deletion plan and workload qualification.

---

# Bounded cleanup and rollout (g-decision-cleanup)

Everything above is the expansion/targeting handoff. Everything below is the
authoritative rollout and rollback procedure for pruning. Deletion stays disabled
through implementation and through the seven-day no-deletion interval; the steps
are ordered, and each one states what must be true before the next.

## What the job is

`scripts/retain_opponent_decisions.py` pages **remaining envelopes**, not expired
sessions. Candidate sessions come from `DISTINCT session_id` over
`opponent_decisions` on the session-leading replay index, so a session with no
envelopes left disappears from every later run. Each candidate's immutable
deadline is then read by a **correlated scalar subquery**, which is always one
`game_sessions_pkey` probe; expressed as a join the planner hashes the whole of
`game_sessions` instead, putting session history back on the sweep's critical
path. There is no `game_sessions` deadline index, no durable cursor, no `OFFSET`,
no per-row commit, no startup or on-request cleanup, and no retention state
beyond each session's immutable deadline.

Each batch is one short transaction: select eligible envelope rows
`FOR UPDATE OF opponent_decisions ... SKIP LOCKED`, re-evaluate the fresh
`clock_timestamp()` deadline predicate, delete only those ids. Parent sessions
are never locked. Facts are a second, separate pass keyed on
`(last_served_at, session_id, blunder_id)`, with the timestamp re-evaluated under
the lock so an upsert that advanced a candidate keeps its fact. Cleanup never
writes a fact: the atomic dual writer plus the completed backfill already
guarantee one exists, and recreating facts from old envelopes would defeat their
TTL.

Margins are strict — **exact equality retains**. Envelopes survive until
`opponent_decisions_expires_at + D`; facts until `last_served_at + 30 days + D`;
`D = 1 hour`. `D` is insurance against routine delay and the app/database clock
difference under the documented `S + B < D` assumption for the single Railway
PostgreSQL instance, not a request-lifetime guarantee. A row inserted before
expiry but committed very late can stay unreadable until the next sweep; safety
comes from the fresh admission and mutation predicates and the fail-closed
consumers in `g-retain-decisions` section 3, not from every request fitting the
margin.

Under an enabled policy a session with a NULL deadline is an **invariant
violation**, never an expiry: it is skipped, counted in
`missing_deadline_sessions`, and alerts.

| Setting | Default | Meaning |
| --- | --- | --- |
| `OPPONENT_DECISION_RETENTION_ENABLED` | `0` | Expiry enforcement. Deletion is refused while off. |
| `OPPONENT_DECISION_RETENTION_SECONDS` | unset | R for newly created sessions. Existing deadlines never move. |
| `OPPONENT_TARGET_SOURCE` | `decisions` | Counter reader. `--apply` is refused until this is `facts`. |
| `OPPONENT_DECISION_CLEANUP_ENABLED` | `0` | The maintenance switch. `--apply` is refused while off. |
| `OPPONENT_DECISION_CLEANUP_NOT_BEFORE` | unset | Offset-carrying ISO instant = recorded activation + 7 days. |

Unreadable values fail rather than falling back to "off". `--apply` is refused —
never silently degraded to a dry run — unless all five are satisfied and the
fresh database clock has reached the not-before instant.

`OPPONENT_TARGET_SOURCE=facts` is an activation control and not merely a step in
section 2, because skipping it is silent. With R shorter than the 30-day counter
window and the reader still on `decisions`, pruning removes attempts from days
`R+1..30` out of the `targeted_30d` denominator; nothing alerts, and p_reach
simply reads high. While the facts are intact the damage is reversible — putting
the reader back on `facts` restores the counts — but nothing surfaces it, so the
job refuses rather than relying on the operator noticing.

A run without `--apply` still takes the same short `SKIP LOCKED` row locks and
evaluates the same predicates, which is what makes its counts the rows a real run
would have removed rather than an estimate. Those locks block nothing: envelopes
are insert-only, so no live request holds one, and ordinary reads never wait on a
row lock.

Exit status: `0` healthy, `1` alerting (backlog lag or a missing-deadline
invariant), `2` refused. The two non-zero statuses mean different things and are
kept apart deliberately: `1` means a sweep ran and reported something, `2` means
no sweep happened. An unreadable maintenance variable is a refusal, not a
traceback and not an alert — every switch is read before the first statement, so
a typo exits `2` with its reason on stderr and nothing on stdout. The JSON report
is aggregate-only: no session id, target id, user or payload appears in it.

## 1. Census and R selection

One read-only aggregate on the primary database. Record its output verbatim
alongside the selected R; the maximum replay age is the number this bead could
not supply, and the original `served_at` does not reveal later replay time.

```sql
WITH d AS (
    SELECT d.session_id, s.session_mode,
           EXTRACT(EPOCH FROM (d.served_at - s.started_at)) AS age_seconds,
           s.started_at, octet_length(d.response_payload) AS payload_bytes
    FROM opponent_decisions d
    JOIN game_sessions s ON s.id = d.session_id
)
SELECT count(*) AS decisions,
       count(DISTINCT session_id) AS sessions,
       count(*) FILTER (WHERE session_mode = 'drill') AS drill_decisions,
       count(*) FILTER (WHERE session_mode IS DISTINCT FROM 'drill') AS normal_decisions,
       sum(payload_bytes) AS live_payload_bytes,
       max(age_seconds) AS max_age_seconds,
       percentile_disc(0.50) WITHIN GROUP (ORDER BY age_seconds) AS p50_age_seconds,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY age_seconds) AS p95_age_seconds,
       percentile_disc(0.99) WITHIN GROUP (ORDER BY age_seconds) AS p99_age_seconds,
       count(*) FILTER (WHERE age_seconds < 0) AS implausible_ages,
       count(*) FILTER (WHERE started_at > clock_timestamp()) AS future_starts,
       count(*) FILTER (WHERE started_at < clock_timestamp() - interval  '7 days') AS eligible_r7,
       count(*) FILTER (WHERE started_at < clock_timestamp() - interval '14 days') AS eligible_r14,
       count(*) FILTER (WHERE started_at < clock_timestamp() - interval '30 days') AS eligible_r30
FROM d;

SELECT c.relname,
       pg_table_size(c.oid) AS table_bytes,
       pg_indexes_size(c.oid) AS index_bytes,
       COALESCE(pg_total_relation_size(t.oid), 0) AS toast_bytes
FROM pg_class c
LEFT JOIN pg_class t ON t.oid = c.reltoastrelid
WHERE c.relname IN ('opponent_decisions', 'opponent_target_facts', 'game_sessions');
```

Compare against the `g-retain-decisions` projection (≈3 MB at R=7d, ≈6 MB at
R=14d, ≈12 MB at R=30d, from 10,657,792 bytes over at most 26 days). Those are
linear estimates, not measured steady state. Exceeding every observed replay age
is **not** a mandatory gate — normal and converted play already falls back to the
local engine on 410 — but the maximum and the implausible/future-start counts are
recorded with the choice. Proposed R = 7 days. `R=30d` is growth prevention, not
a demonstrated reduction from 10.7 MB.

## 2. Deploy, drain, backfill, verify

Order matters; each step's precondition is the previous step's recorded result.

1. Verify the actual Railway deployed revision. Deploy the dual writers
   everywhere and drain old processes **and** their in-flight transactions.
2. Rerun `python scripts/backfill_opponent_target_facts.py --apply` after the
   drain and confirm `matches=true`. A check taken before the drain certifies
   nothing about coverage afterwards. Both envelopes and facts stay whole here.
3. Record the check time and the aggregate result. The comparison runs at one
   frozen database time inside a REPEATABLE READ, READ ONLY snapshot; that is
   what makes original-vs-fact parity a statement rather than two readings.
4. Set `OPPONENT_TARGET_SOURCE=facts` for every reader, and for any sibling pin
   consumer that is actually deployed. Repeat ordinary gameplay and library
   checks. **As of 2026-09-20 no sibling pin consumer exists** —
   `app.opponent_target_facts.current_target_pairs` has exactly one caller,
   `app.srs_opportunity`, and `g-srs-target-publish` / `g-srs-fold-recovery` are
   still open. The rollout owner rechecks the deployed state rather than
   trusting this sentence.
5. Select R from step 1, then initialize deadlines with
   `scripts/initialize_opponent_decision_deadlines.sql`, binding the chosen
   `retention_seconds`. It fills only NULL deadlines and never rewrites a
   historical `started_at`. Verify both session creation routes stamp a deadline.
   The backfill command refuses `--apply` once **any** deadline exists, so this
   step is deliberately after step 2.
6. Activate expiry (`OPPONENT_DECISION_RETENTION_ENABLED=1`) with every client
   and server component ready.

## 3. Record the activation and set the not-before

Immediately after activation, record the database clock:

```sql
SELECT clock_timestamp() AS activation_at;
```

Write that instant into the handoff, add seven days, and set
`OPPONENT_DECISION_CLEANUP_NOT_BEFORE` to the result (offset-carrying ISO 8601,
e.g. `2026-10-05T18:22:31.418+00:00`). Leave `OPPONENT_DECISION_CLEANUP_ENABLED`
at `0`. Nothing is deleted during the interval, so ordinary code and read-path
rollback stay available for the whole window.

The interval also gives already-expired rows seven days without API use before
they are pruned. Cohorts that expire later rely on `R + D` alone.

## 4. Exercising expiry without waiting R days

Short synthetic deadlines, not clock changes:

```sql
-- One disposable session you own, in a non-production or clearly marked session.
UPDATE game_sessions SET opponent_decisions_expires_at = clock_timestamp() - interval '1 minute'
WHERE id = :session_id;
```

Confirm on that session: a normal or converted game receives 410
(`error.details.error_code = OPPONENT_SESSION_EXPIRED`) and keeps playing on the
local fallback with no server target; an active or root-reached drill stops
retrying, keeps its board, and offers restart/abandon without a drill failure or
a root stamp; a completed root result (`drill_root_reached_ply`) survives. Read
counter parity with `scripts/backfill_opponent_target_facts.py` (read-only).

Run the dry sweep as often as you like during the interval — it never deletes:

```bash
cd backend && source .venv/bin/activate
python scripts/retain_opponent_decisions.py
```

**Expect exit `1` from every dry run during the interval.** Sessions that expired
before the job existed are already weeks past `R + D`, so `lag_seconds` exceeds
the healthy 24-hour window by construction and the run alerts. That is the
backlog being reported, not a fault. The status is only meaningful as a steady
state signal once the job has been applying for long enough to drain it; until
then read `eligible_envelopes` and `oldest_overdue_expiry` directly. An exit `2`
during the interval is different and always wants attention: it means the run was
refused and nothing was measured.

## 5. The one operator snapshot

Once, from the operator's workstation, before the first deletion, piping the dump
directly into local encryption so no plaintext copy is ever written:

```bash
umask 077
pg_dump -Fc -t public.opponent_decisions "$PRIMARY_DATABASE_URL" \
  | age -p > ~/private/opponent_decisions-$(date -u +%Y%m%dT%H%M%SZ).dump.age
```

Use existing approved credentials and whatever encryption tool is already
approved (`age`, `gpg -c`, …). Keep the file in a private directory outside any
repository, worktree or cloud-synced folder. Verify the exit status of **both**
commands and a non-empty output file. Record its location and its deletion date
in the handoff, then delete it seven days after capture and record that deletion.

This is a finite emergency snapshot, not a backup and not a promise to restore
later writes or deletions. A table-only dump excludes `game_sessions`, `blunders`
and the rest; any exceptional restoration needs the matching schema and parents
and must preserve the R and fact contracts. No bucket, service credential,
lifecycle rule, unattended export or restore rehearsal is part of this.

## 6. Enabling the hourly job

Preconditions, all recorded: the deployed revision verified, old writers drained,
`matches=true` from the post-drain backfill, **`OPPONENT_TARGET_SOURCE=facts` set
for every reader and every sibling pin** (section 2 step 4 — the job refuses
without it, and it must not be reverted afterwards, see section 8), D and the
`S + B < D` assumption documented, the seven-day interval elapsed, the encrypted
dump captured, and a dry-run total you are willing to delete.

Then set `OPPONENT_DECISION_CLEANUP_ENABLED=1` and schedule the Railway cron job
hourly on the same backend image, environment and primary database. In the
flattened Railway image the deployed path is normally:

```bash
python /app/scripts/retain_opponent_decisions.py --apply
```

The job exits at its finite budget (20,000 rows or 600 seconds by default,
100 rows or 4 MiB per batch) and stores nothing the next run needs. It can be
killed, redeployed or run twice concurrently without losing work: the remaining
rows are the queue and every batch is atomic. It takes no exports.

## 7. Monitoring and pausing

Watch the JSON report each hour: `envelopes_deleted`, `envelope_bytes_deleted`,
`facts_deleted`, `eligible_envelopes`, `eligible_envelope_bytes`,
`oldest_overdue_expiry`, `lag_seconds`, `budget_exhausted`, `duration_seconds`
and `alerts`. Exit `1` means either the backlog's oldest overdue deadline is
older than the healthy 24-hour window or a policy-enabled session is missing a
deadline. Healthy steady state is `lag_seconds` well under 24 hours with
`eligible_envelopes` flat or falling across runs; a backlog that grows run over
run means the hourly budget is too small for the load, not that the job is done.

Pause by setting `OPPONENT_DECISION_CLEANUP_ENABLED=0` — no deployment needed,
and `--apply` then refuses with exit `2` rather than deleting quietly. Pause on
any counter-parity or invariant error before investigating.

Report allocated bytes alongside live rows and live payload bytes. An ordinary
`DELETE` frees reusable space without shrinking the Railway volume; a rewrite or
`VACUUM FULL` is outside this task.

## 8. Rollback limits

During the no-deletion interval everything is still present, so code and
read-path rollback are ordinary. **After the first pruning they are not.** Any
later rollback must keep writing compact facts and must keep honouring R: a
revision that stopped writing facts would silently lose the denominator for
sessions whose envelopes are already gone, and one that ignored R would serve
replays for sessions the client can no longer rely on. Counters cannot be
recomputed from envelopes once pruning has started, so
`scripts/backfill_opponent_target_facts.py` must not be run and
`OPPONENT_TARGET_SOURCE` must not revert to `decisions`. This limit is documented
rather than enforced by a refused-downgrade mechanism: `--apply` refuses to prune
while the reader is on `decisions`, which keeps the switch ahead of the first
deletion, but nothing stops a revert afterwards and nothing can undo one.

## 9. Sizing and plan evidence

`scripts/size_opponent_decision_retention.py` builds a bounded census-shaped
synthetic population (607 sessions — 152 normal at 28 envelopes, 455 drills at
12 — with incompressible payloads sized to land near the 10,657,792 bytes
`g-prod-db-growth` measured on 2026-08-21) in a **scratch** database, prunes two
thirds of it, and prints sizes, plans, throughput and hot-path latency. It
requires an explicit `--database-url`, never inheriting one from the environment,
and refuses any database holding application data. That refusal runs **before**
`--reset` is allowed to drop anything: checked afterwards it would be inspecting
a database the mistyped URL had already emptied.

```bash
cd backend && source .venv/bin/activate
python scripts/size_opponent_decision_retention.py --reset \
  --database-url postgresql+psycopg://user@localhost:5433/gr_retain_sizing
```

Recorded run, 2026-09-20, PostgreSQL 15.18, local synthetic fixture — not
production latency or production bytes:

| | before | after |
| --- | --- | --- |
| `opponent_decisions` live rows | 9,716 | 3,252 |
| live payload bytes | 10,623,200 | 3,553,200 |
| table bytes (allocated) | 12,853,248 | 12,861,440 |
| index bytes | 1,859,584 | 1,884,160 |
| `opponent_target_facts` live rows | 607 | 203 |

Live payload fell 66.6%; allocated bytes did not move, which is the reusable-space
caveat above, measured rather than asserted. The sweep removed 6,464 envelopes
and 404 facts in 409 batches in 1.25 s (≈5,200 rows/s) — a full first-run backlog
of this size drains in about a second, far inside one hourly budget. Hot-path
medians over 25 samples were unchanged within noise (targeted counters 2.0 → 1.2
ms, replay lookup 0.46 → 0.37 ms).

Plans at that population. These are `EXPLAIN (ANALYZE, BUFFERS)` of the
statements the sweep itself builds — `candidate_sessions_query`,
`envelope_batch_query` and `fact_batch_query` in `app/opponent_cleanup.py` — not
of hand-written lookalikes; `test_pg_the_sweep_plans_never_scan_the_session_history`
asserts on the same builders, so a plan claim here cannot drift away from the
query that runs.

- `candidate_sessions`: `Limit → Result → Unique → Index Only Scan` on
  `uq_opponent_decisions_session_fingerprint`, with an `Index Scan` on
  `game_sessions_pkey` as the deadline subplan. `game_sessions` is reached only
  by primary key, once per candidate: the shape this design refuses — a scan of
  every historical session per page — does not appear, and cannot, because the
  page's `LIMIT` bounds the number of probes. `DISTINCT` still walks many index
  entries; this is not a claim of a skip scan.
- `envelope_batch`: `Limit → LockRows → Sort → Nested Loop` over
  `game_sessions_pkey` and a bitmap scan of the replay index, 3 shared buffers.
  The sort is over one session's ~16 rows, so **no envelope-side index is added**.
- `fact_batch`: sequential scan plus sort at 607 facts, because the table is too
  small for `idx_opponent_target_facts_expiry` to win. Neither other table
  appears: facts age on their own timestamp. The earlier 12,000-fact index check
  above shows that index taking over as the table grows; both are kept for their
  distinct access patterns.

The candidate query was measured in the join form too, and that is why it is not
written that way: at 20,000 emptied historical sessions it plans as
`Hash Join → Seq Scan on game_sessions`, once per page. The cost is small at
today's size — about 2.5 ms per page — but it grows with history that the sweep
is supposed to have stopped paying for, which is the whole point of driving the
queue off the remaining rows.

## 10. Validation

```bash
cd backend && source .venv/bin/activate
pytest -q -W error test_opponent_decision_retention.py \
  test_opponent_session_expiry.py test_opponent_decision_record.py \
  test_opponent_decision_retention_migration.py test_srs_opportunity.py \
  test_drill_root_confirmation.py test_drill_api.py \
  test_pg_gate_plugin.py::test_manifest_matches_real_pg_gate_collection
```

The PostgreSQL cases in `test_opponent_decision_retention.py` cover what only a
real database shows: a locked envelope skipped and taken on the next run, the
parent session row still free while a batch holds its envelopes, a deletion that
becomes eligible mid-transaction and so is taken only under `clock_timestamp()`
and never under `now()`, a fact upsert racing its own deletion in three
interleavings — including one that holds the row lock uncommitted while the batch
skips past it — the counter statement's single snapshot across a committed
deletion, the integrated replay/drill fail-closed behaviour after the real
cleaner pruned, and the access paths above. The four mid-operation
envelope-deletion races stay in `test_opponent_session_expiry.py`. The complete
required gate remains
`GHOSTREPLAY_REQUIRE_PG_TESTS=1 pytest -m pg_gate --strict-markers`.
