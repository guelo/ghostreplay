# Opponent decision retention runbook

The authoritative operator procedure for bounding opponent replay history: the
targeting expansion first, then the census against a decided R, activation, the
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

| Setting | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `OPPONENT_DECISION_RETENTION_ENABLED` | `0` | API **and** job | Expiry enforcement. Deletion is refused while off. |
| `OPPONENT_DECISION_RETENTION_SECONDS` | unset | API **and** job | R for newly created sessions. Existing deadlines never move. |
| `OPPONENT_TARGET_SOURCE` | `decisions` | API **and** job | Counter reader. `--apply` is refused until this is `facts`. |
| `OPPONENT_DECISION_CLEANUP_ENABLED` | `0` | job only | The maintenance switch. `--apply` is refused while off. |
| `OPPONENT_DECISION_CLEANUP_NOT_BEFORE` | unset | job only | Offset-carrying ISO instant = recorded activation + 7 days. |

Unreadable values fail rather than falling back to "off". `--apply` is refused —
never silently degraded to a dry run — unless all five are satisfied and the
fresh database clock has reached the not-before instant.

### Which service reads what

Nothing under `backend/app/` imports `app.opponent_cleanup`; only
`scripts/retain_opponent_decisions.py` and the sizing harness do. The two
`CLEANUP_*` switches are therefore read **only by the cleanup job's process**,
and setting them on the API service does nothing at all. Conversely all three
of the others are read by the API (`check_deadline`, `initialize_deadline`,
`current_target_pairs`) **and** by the job, which is why they must be **one
definition** — a Railway project shared variable, or a `${{<api-service>.VAR}}`
reference on the cleanup service — never two independently edited copies. The
reason is the pruning guard in `authorize_deletion`: it refuses to delete while
`OPPONENT_TARGET_SOURCE` is `decisions`, but it can only see the job's own copy.
With two copies, reverting the API to `decisions` while the job's copy still
reads `facts` leaves the guard silent and deletes exactly the attempts the
counters are still reading — the `p_reach` inflation the guard exists to prevent.

`OPPONENT_DECISION_RETENTION_SECONDS` is on that shared list for a
non-obvious reason: `check_configuration()` runs at the top of every sweep and
calls `retention_enabled()`, which raises when enforcement is on and no duration
is set. A cleanup service that resolves `RETENTION_ENABLED=1` without also
resolving `RETENTION_SECONDS` refuses with exit `2` on every hourly run — dry
runs included.

**A variable change is not live until it is deployed.** Railway stages variable
edits as changes you must review and deploy; a dashboard edit left staged
changes nothing about the running process. `railway variable set` triggers a
deploy unless `--skip-deploys` is passed. The rule for every switch change in
this runbook is therefore change → deploy the staged changes on every affected
service → read the value back per service
(`railway variable list --kv --service <name>`) and record it. A read-back
showing a literal `${{` has proved only that the reference exists, not what it
resolves to; get the effective value with
`railway run --service <name> printenv OPPONENT_TARGET_SOURCE`.

A read-back is evidence about the service's **configuration**, not about the
process currently running: `--skip-deploys` commits a value no deployment has
picked up and the read-back still shows it. Only the change → deploy → read-back
order makes it evidence about the running process. Two of the three shared
switches also have a behavioural confirmation — the pause's next execution
exiting `2` (section 7) and activation's 410 (section 4). The reader switch in
section 2 step 4 has none and can have none: while parity holds, `facts` and
`decisions` return the same numbers, so record the variable-change time and the
deploy's creation time and check that the deploy is the later of the two.

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
kept apart deliberately: `1` **with a JSON report on stdout** means a sweep ran
and reported something, `2` means no sweep happened. An unreadable maintenance
variable is a refusal, not a traceback and not an alert — every switch is read
before the first statement, so a typo exits `2` with its reason on stderr and
nothing on stdout. The JSON report is aggregate-only: no session id, target id,
user or payload appears in it.

There is a third shape, and reading it as an alert would be wrong:
`retain_opponent_decisions.main()` catches only `CleanupRefused`, so anything
else — a connection failure, a driver error — leaves a traceback on stderr,
**no JSON on stdout**, and the interpreter's own exit status `1`. Exit `1` with
no JSON is a crash: nothing was measured, and it is handled like a refusal, not
like a backlog alert. "A run happened" is the presence of the report, not the
status.

## 1. Census, against a decided R

**R = 7 days (604800 seconds), decided by the rollout owner on 2026-09-20.**
The census no longer picks R; it records what that R costs and is the last place
the choice can be sent back before anything is deployed.

One read-only aggregate on the primary database. Record its output verbatim
alongside R; the maximum replay age is the number this bead could not supply,
and the original `served_at` does not reveal later replay time.

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
       count(*) FILTER (WHERE started_at < clock_timestamp() - interval '30 days') AS eligible_r30,
       sum(payload_bytes) FILTER (WHERE started_at < clock_timestamp() - interval  '7 days') AS eligible_r7_bytes,
       sum(payload_bytes) FILTER (WHERE started_at < clock_timestamp() - interval '14 days') AS eligible_r14_bytes,
       sum(payload_bytes) FILTER (WHERE started_at < clock_timestamp() - interval '30 days') AS eligible_r30_bytes
FROM d;

SELECT c.relname,
       pg_table_size(c.oid) AS table_bytes,
       pg_indexes_size(c.oid) AS index_bytes,
       COALESCE(pg_total_relation_size(t.oid), 0) AS toast_bytes
FROM pg_class c
LEFT JOIN pg_class t ON t.oid = c.reltoastrelid
WHERE c.relname IN ('opponent_decisions', 'opponent_target_facts', 'game_sessions');
```

The `eligible_r*_bytes` columns are what the R comparison is actually recorded
in. The row counts alone cannot produce it: a long drill history is orders of
magnitude larger per envelope than an opening move
(`app/opponent_cleanup.py:_envelope_batch`), so a share of rows is a poor proxy
for a share of footprint. Record both.

Compare against the `g-retain-decisions` projection (≈3 MB at R=7d, ≈6 MB at
R=14d, ≈12 MB at R=30d, from 10,657,792 bytes over at most 26 days). Those are
linear estimates, not measured steady state. Exceeding every observed replay age
is **not** a mandatory gate — normal and converted play already falls back to the
local engine on 410 — but the maximum and the implausible/future-start counts are
recorded with the choice. `R=30d` was the alternative and is growth prevention,
not a demonstrated reduction from 10.7 MB.

Two census results send R = 7 days back for re-decision rather than merely
getting recorded. The first is `implausible_ages` or `future_starts` above `0`:
every deadline is `started_at + R`, so a bad `started_at` is a bad deadline, and
`eligible_r7` is not trustworthy either. The second is a large count from
section 3's exposure query — the reading that actually matters, and one that can
be taken here, before any deadline exists, by inlining the cut instead of
reading a stored deadline:

```sql
SELECT count(*) AS live_sessions_older_than_r7
FROM game_sessions s
WHERE s.session_mode = 'drill' AND s.drill_state IN ('active', 'root_reached')
  AND s.started_at < clock_timestamp() - interval '7 days'
  AND EXISTS (SELECT 1 FROM opponent_decisions d
              WHERE d.session_id = s.id
                AND d.served_at > clock_timestamp() - interval '1 day');
```

That is how many in-progress drills would take a 410 on their next opponent
request the moment enforcement goes on. `eligible_r7` alone overstates it by
counting sessions nobody is playing. Section 3 runs the same query again against
the real deadlines, because by then the stored deadline — not `started_at + R` —
is what expiry reads. Neither number has a threshold this document can set; both
go in front of the rollout owner before section 2 step 5.

## 2. Deploy, drain, backfill, verify

Order matters; each step's precondition is the previous step's recorded result.

### What "drained" means, and how to see it

A Railway deploy is not a cutover instant. The new container boots, passes its
healthcheck and starts taking traffic; only then is the old one stopped. For
that window two revisions write to one database. "Drained" is the statement that
the old one has stopped writing — a statement about database backends, not about
the dashboard.

It is written "and their in-flight transactions" because the process
disappearing is not sufficient on its own. A request that began before the
cutover can still be inside a transaction, and its INSERT is invisible to your
snapshot until it commits — which can be after you looked. A reading taken while
such a transaction is open certifies nothing: it was true when measured and
false a moment later.

What skipping it costs is specific at each step. At step 2, a writer without the
dual writer inserts an envelope and no fact, so `matches=true` is stale the
moment it prints. At step 5, a container that has not picked up
`RETENTION_SECONDS` stamps NULL deadlines on the sessions it creates; if one is
created after the initialization SQL has run, that deadline stays NULL, and
under enforcement its first opponent move is a 500 rather than the designed 410.

Three signals, and the third is the one most often skipped:

1. Railway reports the previous deployment as `REMOVED`
   (`railway deployment list --service <name> --json`).
2. The database sees exactly one container. Each Railway container has its own
   private IPv6 address, so a second `client_addr` is a second revision
   (`application_name` is empty here, so the address is the only discriminator):

   ```sql
   SELECT client_addr, count(*) AS conns,
          min(backend_start) AS oldest_backend_start,
          max(state_change) AS last_activity,
          min(xact_start) AS oldest_open_xact
   FROM pg_stat_activity
   WHERE datname = current_database() AND client_addr IS NOT NULL
     AND pid <> pg_backend_pid()
   GROUP BY client_addr ORDER BY oldest_backend_start;
   ```

   `pid <> pg_backend_pid()` is not optional. Without it your own psql session
   is a second row with an open transaction — the query is inside that
   transaction while it runs — and the check fails every time it is run. An
   operator arrives over the public TCP proxy, so any other session of yours
   shows a `100.64.x.x` address rather than the containers' `fd12:` private
   IPv6; a non-`fd12:` row is a workstation, not a revision.

3. `oldest_open_xact` is NULL, or later than the cutover. No transaction opened
   by the revision you are replacing may still be open.

One row, whose `oldest_backend_start` postdates the deployment's `createdAt`,
with no open transaction, is a drained deploy; two rows means wait and re-read.
The connection set is evidence, not proof of absence: SQLAlchemy holds pooled
connections open (`pool_size` 10 plus `max_overflow` 10 per process,
`app/db.py`), so a live old container that has served anything will appear, but
one that has served nothing may not. That is why signal 1 belongs with the other
two rather than instead of them.

A drained deploy read on the production primary at 2026-09-21T02:18Z looked like
this: one `client_addr`, one idle connection, `backend_start` 02:17:17Z against a
deployment created 02:15:58Z, `oldest_open_xact` NULL.

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
5. Set `OPPONENT_DECISION_RETENTION_SECONDS=604800` (R = 7 days) with
   `OPPONENT_DECISION_RETENTION_ENABLED` still `0`. Deploy it and wait for
   healthy. **This is the point of no return for fact coverage:**
   `initialize_deadline` stamps every new session as soon as this value is set,
   independently of enforcement, and the backfill refuses `--apply` once **any**
   deadline exists. So it must not be set until step 2 returned `matches=true`,
   and it must be set — and deployed — *before* the SQL below, or sessions
   created in between carry no deadline. Repeat the drain check.
6. Initialize the historical deadlines in one transaction with
   `scripts/initialize_opponent_decision_deadlines.sql`, binding
   `retention_seconds = 604800` from `backend/`:

   ```bash
   psql "$PRIMARY_DATABASE_URL" -X -1 -v ON_ERROR_STOP=1 \
     -v retention_seconds=604800 \
     -f scripts/initialize_opponent_decision_deadlines.sql
   ```

   `-f` is load-bearing. psql interpolates `:retention_seconds` only when
   reading a file or stdin, so the same statement pasted after `-c` dies with
   `syntax error at or near ":"`; dropping `-v` fails the same way rather than
   substituting something, which is the safe direction. Both checked on psql
   15.18, where `make_interval(secs => 604800)` renders `168:00:00`.

   It fills only NULL deadlines and never rewrites a historical `started_at`.
   Record the UPDATE row count. Verify both session
   creation routes (`POST /api/game/start`, `POST /api/drills/start`) stamp
   `opponent_decisions_expires_at = started_at + R` on the deployed revision,
   including the model's server-default `started_at` path.
7. **Two invariant checks, and they are not the same check.** Both must be `0`.

   ```sql
   -- (a) Activation gate: EVERY session must have a deadline.
   SELECT count(*) AS sessions_without_deadline
   FROM game_sessions WHERE opponent_decisions_expires_at IS NULL;

   -- (b) Sweep invariant: only the sessions the cleanup job can see.
   SELECT count(DISTINCT d.session_id) AS envelope_sessions_without_deadline
   FROM opponent_decisions d
   JOIN game_sessions s ON s.id = d.session_id
   WHERE s.opponent_decisions_expires_at IS NULL;
   ```

   (a) is the gate for step 8, because under enforcement a NULL deadline is not
   a 410: `check_deadline` calls `require_deadline`, which raises `RuntimeError`,
   so the user gets a **500** instead of the designed fallback on every route
   that checks (`api/game.py` opponent move and its retry, `api/drills.py` route
   check, opponent move and root confirmation). A session created by an
   undrained container after the SQL ran has no envelope yet, passes (b), and
   fails this way on its first opponent move. (b) is what the sweep reports as
   `missing_deadline_sessions`. A nonzero result means a container without
   `RETENTION_SECONDS` created sessions after the SQL ran: confirm the drain,
   re-run the same idempotent NULL-only SQL, recheck. Re-run both after step 8
   and again before enabling cleanup.
8. Activate expiry (`OPPONENT_DECISION_RETENTION_ENABLED=1`) with every client
   and server component ready, and with check (a) at `0`. Never set it before
   `RETENTION_SECONDS`: the `app/main.py` lifespan rejects
   enabled-without-duration at startup, which fails the healthcheck rather than
   serving traffic — a backstop, not the plan. Section 4 is the go/no-go.

## 3. Size the exposure, record the activation, record the not-before

**Before setting `OPPONENT_DECISION_RETENTION_ENABLED=1`**, size what activation
does: enforcement expires the entire historical backlog at that instant, so any
browser tab still holding a session older than R gets a 410 on its next opponent
request. Deadlines already exist (section 2 step 6), so the count is answerable
while enforcement is still off, and it needs **both** predicates:

```sql
SELECT count(*) FROM game_sessions s
WHERE s.session_mode = 'drill' AND s.drill_state IN ('active', 'root_reached')
  AND s.opponent_decisions_expires_at < clock_timestamp()
  AND EXISTS (SELECT 1 FROM opponent_decisions d
              WHERE d.session_id = s.id
                AND d.served_at > clock_timestamp() - interval '1 day');
```

The deadline predicate is what makes it a count of sessions that will 410; the
activity predicate is what keeps it from counting every drill ever abandoned.
Drop either one and the answer is meaningless. Pick a low-traffic window; a
number large enough to matter is a reason to move the window, not to skip the
step.

Immediately after activation, take the activation instant and the not-before
from **one** clock reading, so the not-before is neither hand arithmetic nor two
`clock_timestamp()` calls microseconds apart:

```sql
SELECT a AS activation_at, a + interval '7 days' AS cleanup_not_before
FROM (SELECT clock_timestamp() AS a) t;
```

Record both verbatim (offset-carrying ISO 8601, e.g.
`2026-10-05T18:22:31.418+00:00`). Copying `psql`'s own rendering works —
`datetime.fromisoformat` accepts its space separator and two-digit offset
(`2026-10-05 18:22:31.418+00`) on Python 3.12. What `cleanup_not_before()`
rejects is a value whose offset was dropped in transcription.

The `OPPONENT_DECISION_CLEANUP_NOT_BEFORE` **variable** is set in section 6, on
the cleanup service, because that service is the only thing that reads it and it
does not exist yet. Recording the instant seven days before it has anywhere to
live is intentional; nothing reads it in the meantime.
`OPPONENT_DECISION_CLEANUP_ENABLED` stays `0` throughout.

Nothing is deleted during the interval, so ordinary code and read-path rollback
stay available for the whole window. **Rollback before the first deletion is one
variable:** `OPPONENT_DECISION_RETENTION_ENABLED=0` (deployed — see *Which
service reads what*). No code rollback and no data change; expiry simply stops
being enforced and every envelope is still there. Re-enabling later is a **new
activation**: new `activation_at`, new not-before, and the seven-day
no-deletion interval restarts from that instant.

The interval also gives already-expired rows seven days without API use before
they are pruned. Cohorts that expire later rely on `R + D` alone.

## 4. The go/no-go client checks, and the daily dry sweep

Activation and client verification are **one sitting**. The client paths cannot
be exercised beforehand: `check_deadline` returns early while the policy is
disabled, so until section 3 there is no 410 to test. The sequence is activate →
verify immediately → keep, or revert with
`OPPONENT_DECISION_RETENTION_ENABLED=0`.

Short synthetic deadlines, not clock changes, and **one disposable session per
path** — a session that has 410'd cannot be reused for the next check:

```sql
-- One disposable session you own, in a non-production or clearly marked session.
UPDATE game_sessions SET opponent_decisions_expires_at = clock_timestamp() - interval '1 minute'
WHERE id = :session_id;
```

Cover all four paths, not just the root arrival:

1. A normal or converted game receives 410
   (`error.details.error_code = OPPONENT_SESSION_EXPIRED`) and keeps playing on
   the local fallback with one fallback move and no new server target.
2. An active or root-reached drill's opponent request stops retrying, keeps its
   board and barrier, and offers restart/abandon — no `drill_state='failed'` and
   no root stamp.
3. Opponent-arrival root confirmation.
4. A pre-root player route check that is **not** at the root, on-route and
   off-route.

Confirm a completed `drill_root_reached_ply` survives. Read counter parity with
`scripts/backfill_opponent_target_facts.py` (read-only), then re-run both
invariant checks from section 2 step 7.

Run the dry sweep as often as you like during the interval — it never deletes.
Export the **production** values of the three shared switches and the recorded
not-before, so `check_configuration()` also proves they parse; a dry run with
the switches unset proves only that the defaults parse:

```bash
cd backend && source .venv/bin/activate
OPPONENT_DECISION_RETENTION_ENABLED=1 OPPONENT_DECISION_RETENTION_SECONDS=604800 \
  OPPONENT_TARGET_SOURCE=facts \
  OPPONENT_DECISION_CLEANUP_NOT_BEFORE='<recorded activation+7d ISO>' \
  python scripts/retain_opponent_decisions.py
```

`sweep()` calls `check_configuration()` before anything else on every run, and
that parses `cleanup_not_before()` whether or not `--apply` was given — so a
transcribed instant that has lost its offset surfaces as exit `2` here, seven
days before it would have blocked the first real run. The dry run still cannot
delete: `authorize_deletion` is reached only under `--apply`.

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
set -o pipefail
PG_DUMP=/opt/homebrew/opt/postgresql@18/bin/pg_dump   # not the one on PATH; see below
"$PG_DUMP" -Fc -t public.opponent_decisions "$PRIMARY_DATABASE_URL" \
  | age -p > ~/private/opponent_decisions-$(date -u +%Y%m%dT%H%M%SZ).dump.age
```

Run it from a real terminal. `age -p` reads the passphrase from `/dev/tty`, so
it cannot be driven from an agent's shell or any other TTY-less context: it exits
with `could not read passphrase: standard input is not a terminal, and /dev/tty
is not available`, and `pg_dump` then dies of SIGPIPE (exit 141). Never leave
age's prompt empty either — it autogenerates a passphrase and **prints it to the
terminal**, which in an agent session writes it straight into the transcript.

Use existing approved credentials and whatever encryption tool is already
approved (`age`, `gpg -c`, …). Two prerequisites are worth checking before the
capture, not after it, and on this workstation the first one already bites.

`pg_dump` refuses a server whose major version is above its own, and the
primary is **PostgreSQL 18.6** while the `pg_dump` on `PATH` is 15.18: it aborts
with `server version mismatch` before writing anything. Measured 2026-09-20, not
predicted. Homebrew's `postgresql@18` is installed but unlinked, and its
`pg_dump` (18.4) reads 18.6 correctly — a minor version below the server is
fine, a major one is not — so the absolute path above is the fix, not an
install. Check it with `"$PG_DUMP" --version` against the server's
`SELECT version()`; the failure is loud, but it arrives on the day the snapshot
is due.

The second is the passphrase, which must have a recorded holder, since an
unopenable dump is not a snapshot. Install the encryption tool during the
no-deletion interval, not on the day, and use that same tool in the verification
below. On this workstation that sequencing mattered: neither `age` nor `gpg` was
present when the prerequisite was measured on 2026-09-20, and `age` 1.3.2 was
installed in time for the 2026-09-23 capture. `gpg` is still absent.

Keep the file in a private directory outside any repository, worktree or
cloud-synced folder. Verify **both** pipeline exit statuses (`pipefail` or
`PIPESTATUS`) — and verify the artifact, because a non-empty file proves neither
that it decrypts nor that it is a valid dump:

```bash
PG_RESTORE=/opt/homebrew/opt/postgresql@18/bin/pg_restore   # not the one on PATH
age -d <file> | "$PG_RESTORE" -f /dev/null   # decrypts and parses; writes no plaintext, restores nothing
```

`pg_restore` is pinned for the same reason `pg_dump` is and fails the same way:
the 15.18 on `PATH` cannot read an archive written by 18.4, exiting with
`unsupported version (1.16) in file header` — after the passphrase has been typed.

**`-f /dev/null`, not `-l`.** `pg_restore -l` reads only the archive's table of
contents at the front of the file, exits 0 and closes the pipe, which SIGPIPEs
`age` on any payload larger than a pipe buffer. The observed result on a provably
intact 3 MB snapshot is `age -d exit=141  pg_restore -l exit=0`: checking both
statuses condemns a good snapshot, and the tempting workaround — dropping the
both-statuses rule — removes the one check that catches a truncated file.
`-f /dev/null` consumes the whole stream, so both statuses stay meaningful, and
it parses every data block rather than just the TOC. Measured 2026-09-23:
`-f /dev/null` from a pipe gives `PIPESTATUS=0 0`, an early-exiting consumer
gives `141 0`, and `age -d` on a deliberately truncated file exits 1 with
`failed to decrypt and authenticate payload chunk` — so a full `age -d` exit 0
is itself proof that every chunk authenticated.

Read the TOC too (`"$PG_RESTORE" -l`, where age's SIGPIPE is expected and
ignored) and confirm it lists `TABLE DATA`, not merely the table definition. An
empty archive parses perfectly.

Record its private location, capture date, deletion date (capture + 7 days), the
accountable owner and who holds the passphrase. Delete it on that date and
record the deletion; never report it as deleted without verifying.

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

Also recorded before the first deletion: both invariant checks from section 2
step 7 still `0`; the resolved switch values read back per service after their
last deploy (*Which service reads what*); and the deployed sibling pin state
rechecked against the revision deployed **now**, not the one recorded a week ago.

### The first deletion is one bounded manual run

Not a canary program — one run, attended, before anything unattended exists.
Run it from a clean git worktree checked out at the **deployed** sha, because
the operator scripts import `app.*` from whatever is on disk and this tree is
edited concurrently:

```bash
cd backend && source .venv/bin/activate
OPPONENT_DECISION_RETENTION_ENABLED=1 OPPONENT_DECISION_RETENTION_SECONDS=604800 \
  OPPONENT_TARGET_SOURCE=facts OPPONENT_DECISION_CLEANUP_ENABLED=1 \
  OPPONENT_DECISION_CLEANUP_NOT_BEFORE='<recorded activation+7d ISO>' \
  python scripts/retain_opponent_decisions.py --apply --run-rows 500
```

Expected, and not a fault: **exit `1`**, because the remaining backlog still
exceeds the 24-hour lag window; and `facts_deleted: 0`, because envelopes and
facts share one run budget and the envelope pass runs first, so a 500-row budget
is spent before the fact pass starts. What the run proves is that deletion is
authorized, bounded and atomic. A missing or mistyped switch is a refusal
(exit `2`), never a silent dry run.

### Scheduling it — the cleanup service

A **new** Railway service in the same project, same repo and branch, same image
and same primary database, running on a `0 * * * *` cron schedule. It is called
the *cleanup service* throughout this runbook.

**Configure it in the service's settings, not in a config file.** Railway's
Config as Code (`railway.toml` / `railway.json`) is deprecated: per Railway's
Infrastructure as Code documentation, "Config as Code is still read from your
service repository during deploy for existing (legacy) services … **New services
cannot opt into Config as Code**", and existing files "stop being read on
**2026-12-01** (hard cutoff)". So a `railway.cron.toml` pointed at this service
would never be read, and the root `railway.toml` does not reach it either. The
project-level replacement is `.railway/railway.ts`, applied by
`railway config plan` / `railway config apply`; adopting it migrates **every**
service in the project at once ("A service cannot be managed by both systems at
the same time"), which is its own change, not part of this rollout.

What must be set on the service, and why each one:

**Custom Start Command** — required, not optional:

```bash
if [ -d backend ]; then cd backend; fi; /opt/venv/bin/python scripts/retain_opponent_decisions.py --apply
```

Without an explicit start command the build's own start runs
(`nixpacks.toml [start]`: `alembic upgrade head && uvicorn …`), which migrates,
never exits, and so makes Railway skip every later execution.

**Cron Schedule** `0 * * * *`. Railway skips a scheduled execution while the
previous one is still running, which is safe here: the remaining rows are the
queue and no state carries between runs.

**Restart Policy** `NEVER`. Exit `1` (alerting) and exit `2` (refused) are
ordinary outcomes for this job. An on-failure policy would re-run the sweep up
to its retry count every hour and turn a refusal into a restart loop.

**Healthcheck** none. A short-lived job has nothing to health-check.

`/opt/venv/bin/python` rather than a bare `python`: the interpreter is the
nixpacks venv, not a `python` on `PATH`. The `cd backend` guard mirrors the API
service's start command and covers both image layouts (nixpacks normally
flattens `backend/` into `/app`); the script puts its own parent directory on
`sys.path`, so it does not depend on the working directory for imports. Confirm
the actual layout and the command that ran from the deployment logs.

Variables **on this service**: `OPPONENT_DECISION_CLEANUP_ENABLED=1` and
`OPPONENT_DECISION_CLEANUP_NOT_BEFORE=<recorded instant>` live here and nowhere
else; all three shared switches — including `RETENTION_SECONDS`, or every run
refuses — are *referenced*, not copied; and the database URL is the same
variable reference the API service uses.

Private networking can need a moment to initialize in a freshly started
container — never an issue for a long-running API, occasionally one for a
short-lived cron process. If the first run fails to connect, point the
cleanup service at the public proxy URL rather than adding retry logic.

The job exits at its finite budget (20,000 rows or 600 seconds by default,
100 rows or 4 MiB per batch) and stores nothing the next run needs. It can be
killed, redeployed or run twice concurrently without losing work: the remaining
rows are the queue and every batch is atomic. It takes no exports.

Record the **first successful hourly execution**: timestamp, exit status and the
full JSON report showing `"applied": true`. A run with no JSON report is not a
successful execution regardless of its exit status. Record the service,
schedule and start command actually used, and the first-deletion totals.

## 7. Monitoring and pausing

Watch the JSON report each hour: `envelopes_deleted`, `envelope_bytes_deleted`,
`facts_deleted`, `eligible_envelopes`, `eligible_envelope_bytes`,
`oldest_overdue_expiry`, `lag_seconds`, `budget_exhausted`, `duration_seconds`
and `alerts`. Exit `1` means either the backlog's oldest overdue deadline is
older than the healthy 24-hour window or a policy-enabled session is missing a
deadline. Healthy steady state is `lag_seconds` well under 24 hours with
`eligible_envelopes` flat or falling across runs; a backlog that grows run over
run means the hourly budget is too small for the load, not that the job is done.

Two report shapes are not what they look like. Exit `1` with **no JSON on
stdout** is a crash, not a backlog alert: nothing was measured, so treat it like
a refusal. And a 500 on an opponent move under an enabled policy is a NULL
deadline, not an expiry — re-run both invariant checks from section 2 step 7.

**Parity stops being an oracle at the first deletion.** `compare_target_facts`
counts a fact with no surviving decision group as an `extra_pair`, and facts
outlive their envelopes by 30 days by design, so once anything is pruned the
read-only comparison returns `matches: false` and exit `1` permanently. That is
the design working. After the first deletion, watch `missing_deadline_sessions`
staying `0`, `facts_deleted` against `eligible_facts`, and the
`opponent_target_facts` row-count trend instead.

Pause by setting `OPPONENT_DECISION_CLEANUP_ENABLED=0` **on the cleanup
service** — the only place that variable is read; setting it on the API service
changes nothing and deletion continues. No **code** change is needed, but a
**deploy** is: Railway stages a variable edit until it is reviewed and deployed,
so a `0` left staged in the dashboard does not stop the next hourly run. Set it,
deploy the staged change on the cleanup service, read it back
(`railway variable list --kv --service <cleanup>`), and confirm the next
execution exits `2` with `{"refused": …}` on stderr. **Until that execution has
been observed, treat deletion as still running.** Pause on any counter-parity or
invariant error before investigating.

After the first real deletion, repeat the section 4 client checks once on
sessions whose envelopes are actually **gone** — section 4's `- interval
'1 minute'` will not do, because `D` is one hour, so that session 410s with its
envelopes intact and the check only re-proves section 4. The order matters or
the assertion passes vacuously:

1. Play a disposable session far enough that it **has** envelopes, and record
   `SELECT count(*) FROM opponent_decisions WHERE session_id = '<id>'` as `> 0`.
   A session stamped before its first opponent move has zero rows whether or not
   anything was deleted, which tests nothing.
2. Stamp `opponent_decisions_expires_at = clock_timestamp() - interval '2 hours'`.
3. Let one hourly run pass, then confirm that same count is now `0`.
4. Only then exercise the path — one disposable session per path, as in
   section 4.

What this shows, and section 4 cannot, is that removing the rows behind an
already-unreadable session changes nothing observable.

Report allocated bytes alongside live rows and live payload bytes. An ordinary
`DELETE` frees reusable space without shrinking the Railway volume; a rewrite or
`VACUUM FULL` is outside this task.

## 8. Rollback limits

During the no-deletion interval everything is still present, so code and
read-path rollback are ordinary, and the named lever is one variable:
`OPPONENT_DECISION_RETENTION_ENABLED=0`, deployed (section 3). No code rollback
and no data change — expiry stops being enforced and every envelope is still
there. Re-enabling afterwards is a **new activation**: new `activation_at`, new
not-before, and the seven-day interval restarts. This is the whole rollback
story before the first deletion; there is no separate post-prune window.

**After the first pruning, rollback is not ordinary.** Any later rollback must
keep writing compact facts and must keep honouring R: a revision that stopped
writing facts would silently lose the denominator for sessions whose envelopes
are already gone, and one that ignored R would serve replays for sessions the
client can no longer rely on. Counters cannot be recomputed from envelopes once
pruning has started, so
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
