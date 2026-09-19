# Opponent decision retention: expansion and targeting handoff

`g-decision-facts` supplies the additive schema, atomic dual writer, backfill,
verification and selectable counter source. It does **not** initialize deadlines,
enforce expiry, prune rows, deploy, or activate production readers. Those steps
belong to `g-decision-expiry`, `g-decision-cleanup` and `g-decision-rollout` under
the shared design in `g-retain-decisions`. Keep cleanup disabled throughout this
handoff. The parent requires one integrated release and a seven-day no-deletion
interval before its separate cleanup activation.

## Storage and reader contract

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

## Coverage before switching

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

## Sibling SRS pin handoff

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

## Validation

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
