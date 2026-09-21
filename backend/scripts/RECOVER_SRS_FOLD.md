# Folding SRS opportunity evidence, and the seven days to undo it

Focused reference for `g-srs-fold-recovery`: what the compactor does when it is
switched on, and exactly how long a fold can be reversed.

Everything here ships **inert**. `cleanup_enabled` is false, so `sweep` reports
`disabled` and deletes nothing. `g-srs-retain-rollout` owns turning it on;
`g-srs-cleanup-schedule` owns running it on a schedule. This document and
`scripts/fold_srs_opportunities.py` are the manual entry point — the rehearsal,
the canary, and answering *"can we still roll back?"* from a prompt.

Read `RETAIN_SRS_OPPORTUNITIES.md` first: M, G, the fold prefix, the publication
interlock and the delete guards are defined there.

## The one-sentence version

A fold moves a blunder's counters from raw rows into its summary **without
changing them**, deletes exactly the rows it exported and verified, and for
**seven days after the first deletion anywhere** those rows can be put back
exactly — after which they cannot, and a raw-history schema downgrade refuses.

## What one batch does

Outside every lock:

1. Sample the database clock, pick at most **100 pairs over at most 16 distinct
   blunders**, oldest sessions first, skipping any pair an eligible current
   target still needs.
2. Serialize those rows' exact original facts — ids, owners, pair keys, flags,
   timestamps including a NULL `occurred_at` — and compute a versioned canonical
   rowset hash.
3. Write the export, `fsync` it, rename it into place, **read it back off disk**
   and verify both digests. No export I/O happens while a lock is held.

Then, in one short transaction on a dedicated connection:

| step | lock | on contention |
| --- | --- | --- |
| the user's graph lock | `pg_try_advisory_xact_lock(user_id)` | skip this user |
| the retention state | `lock_state_for_fold` — `FOR UPDATE NOWAIT` | skip this user (a *cancelled* statement here counts against the deadline, not this row) |
| the affected blunders, ascending id | `FOR NO KEY UPDATE NOWAIT` | defer the batch |
| their summaries, same order | `FOR NO KEY UPDATE NOWAIT` | defer the batch |

Policy, prefix and `clock_timestamp()` are read **after** those locks, in one
statement. That N decides freeze, grace and target pins — not the export's
preview time, however long ago it was taken. The candidate ids, count, canonical
hash and eligibility are then rechecked against the **database**, in a fresh
`READ COMMITTED` statement. The export proves what was written to disk; only this
can prove what is still true.

If it all still holds: add the three contributions to the summaries, delete
exactly the validated rows, insert the manifest, advance
`folded_through_started_at` and the targeted watermark, and commit. An
interruption anywhere rolls back the entire transfer.

### Timing

| bound | value | what it covers |
| --- | --- | --- |
| `lock_timeout` | 25 ms | the only incidental wait; every real acquisition is NOWAIT |
| `statement_timeout` | 100 ms, and never more than the remaining deadline | one statement |
| `idle_in_transaction_session_timeout` | 100 ms | a client that stops driving the transaction |
| whole-transaction deadline | **500 ms**, monotonic, checked between statements | the gap the other three are blind to |
| p99 hold target | **250 ms** | not a failure — the signal that halves the next batch |

Batch size only ever adapts **down**. Raising it is a benchmark decision under the
same ceiling and belongs to `g-srs-retention-gates`. A user that cannot meet the
deadline even at the minimum batch is dropped for that sweep.

A rollback that cannot complete costs the connection: it is invalidated rather
than returned to the pool, because closing the socket is the only way to get this
user's locks back from a wedged backend.

### What is NOT folded

* Anything newer than `started_at + M + G`. G is a drain gap: every writer that
  passed the freeze test at M has that hour to finish and commit.
* Any pair an eligible current target still needs — a decision (or compact
  targeting fact) inside the 30-day targeted window. Its attempt is in the
  `targeted_30d` denominator and the raw row is the only source of the matching
  reach.
* A blunder with no summary, or whose summary's review basis disagrees with its
  live latest review. Both are `RetentionInvariantError`; reconcile first
  (`scripts/reconcile_srs_review_basis.py`).

Pinned pairs leave **holes** behind the prefix. The prefix is `MAX(started_at)`
over what was actually folded, not a claim that everything older is gone, so
later sweeps still find those rows once the pin ages out.

## The seven-day window

`opportunity_retention_policy.first_fold_committed_at` is stamped by the first
fold anywhere and never moves forward. **Seven days later the window closes.** It
lives on the singleton rather than being derived from the manifests because those
are deleted at expiry, and a deadline that recedes as its own evidence is cleaned
up is not a deadline.

It is cleared in exactly one case: a restore that leaves nothing folded (see
*Restoring* below). The anchor protects deleted rows; when there are none, there
is nothing for it to protect.

A fold that is already running when that happens is not stranded. The clear takes
`SHARE ROW EXCLUSIVE` on `opportunity_fold_batches` before it counts, so a fold
that has inserted its manifest is waited for and counted; a fold that has not is
held off, and every fold re-stamps the anchor unconditionally after its insert,
so it opens a fresh window for itself. Without both halves a batch could commit
with no anchor at all — deleted rows with no deadline, and a later window its own
manifest expires inside.

That wait is bounded at 30 seconds, because what else holds that table is a
whole-user purge or an account deletion, for as long as the rest of its
transaction takes. Past the bound the restore says `recovery anchor still set`
and stops there: every batch is already back by then, the anchor only means the
clock is still running, and the next `restore` clears it.

```bash
cd backend && source .venv/bin/activate
python scripts/fold_srs_opportunities.py status
```

Inside the window, no manifest has expired yet — a batch committed on day three
expires on day ten — so the surviving manifests are the **complete** record of
what was deleted. That is what lets a restore move the fold prefix back to exactly
the right place, and it is why restoring outside the window refuses instead of
computing a prefix from a partial record.

### Restoring

1. **Stop the compactor.** Set `cleanup_enabled = false` and let in-flight sweeps
   drain. `restore` refuses while it is true: a fold deleting rows behind a
   restore produces a state neither of them describes.
2. Pause writers if you are rehearsing a full rollback. Leave **freeze ON** —
   the ladder allows cleanup off with freeze on. The prefix arm protects an
   already-folded session unconditionally, switches or no switches, but turning
   freeze off reopens everything the prefix does *not* cover, including the holes
   that target pins left behind it.
3. Run it. One transaction per batch, re-runnable, oldest first:

```bash
python scripts/fold_srs_opportunities.py restore            # everything
python scripts/fold_srs_opportunities.py restore --user 1234
```

Rows come back with their **original ids and timestamps**. The id generator is
*checked*, never moved: those ids came from it, so it is already past them, and a
`setval` here could only drag it backwards over ids a later batch of the same
rollback is about to re-insert.

It can only be behind out of band. `pg_dump` is **not** that case — it carries
the sequence's position across a reload. A logical-replication cutover is: rows
arrive without their sequence, and the `setval(max(id))` run by hand afterwards
reads a maximum these folded rows are missing from. The restore then refuses and
prints the `setval` to run. One run reports **every** batch it refused, so take
the largest id across all of them rather than fixing them one at a time.

A batch that cannot be read back (a missing or altered artifact) is reported and
**skipped**, not raised: it stays unrestored, the batches behind it — including
other users' — still go back, and the run exits 1. Fix the artifact and re-run.

The restore happens alongside whatever happened since, and the report says so:

| what happened after the fold | what the restore does |
| --- | --- |
| a **review** landed | it already zeroed the since-review counters and moved the basis, so only the lifetime total is subtracted (`since_review_left_alone`) |
| new evidence was **written** | untouched; only absent ids are re-inserted (`rows_skipped_present`) |
| an account, session or blunder was **purged** | stays purged (`rows_skipped_missing_parent`, `summaries_missing`) |

4. Verify: counters unchanged, `folded_through_started_at` back to NULL (or to
   whatever is still folded), every batch showing `restored_at`.

When a restore leaves **no unrestored batch anywhere**, the anchor is cleared and
the run says `recovery anchor cleared`. Nothing is deleted, so there is nothing
for the deadline to protect, and the next fold stamps a fresh seven days. That is
what makes a canary usable: without it, a fold-and-roll-back rehearsal on day zero
would burn the window the real rollout needs, and day fourteen would open with
`restore` refusing artifacts it had just written and verified.

`recovery anchor still set` instead means the rows came back but the clock did
not stop — see above. Re-run `restore`; it is idempotent, and with nothing left to
put back it does only the clearing step.
5. Only then may the schema go back. `alembic downgrade 20260919_05` refuses
   while any batch is unrestored.

### After the window

```
the SRS fold recovery window closed 7 days after <t>; the exports that could
refill the deleted raw rows are gone, so a raw-history schema downgrade is
impossible. Roll back to a compact-aware release instead.
```

Both `restore` and the `20260920_01` downgrade raise this. It is **permanent**, it
is announced here in advance so it is never first discovered during an incident,
and it is the point: a recovery path that never expired would be the unbounded
raw history this epic exists to remove, kept under another name.

### Expiry

```bash
python scripts/fold_srs_opportunities.py expire
```

Deletes manifests past `expires_at` and their artifacts (rows committed first,
files unlinked after — the other order can leave a manifest pointing at nothing),
then removes **orphans**: files older than seven days that no manifest claims,
which is what a batch whose export was written and whose transaction rolled back
leaves behind. Orphans go on age alone, because nothing will ever claim them —
with one exception. An artifact whose owner's **SRS evidence was deleted on
request** goes immediately: a restore would skip its rows as orphaned anyway, so
waiting out a window that cannot bring them back would only keep deleted history
on disk.

The two deletions reach that outcome by different routes. Closing an **account**
cascades its manifests away and leaves the exports as orphans, which is what the
exception above is for; the test is the owner's `user_opportunity_retention_state`
row, which the cascade removes and which a fold always commits before it writes an
export. `purge_user_training_history` keeps the account, so it cannot rely on a
missing row — a purged user who keeps playing is handed one again at their next
served target. It **expires its manifests in place** instead, so they come through
the first half here, by name, on the first `expire` after the purge.

That rule is **off unless `GHOSTREPLAY_SRS_FOLD_EXPORT_DIR` is set**, and this is
the second reason to set it. It reads an owner id out of a file name and asks
this database whether that user still has evidence, which only answers "deleted"
when one database owns the directory. The default sits beside the backend package
and is shared by every local database pointed at the checkout, so there an
unknown id may simply belong to another one; those files keep the age rule they
always had.

Folding keeps writing exports after the window closes. They are not dead weight:
the export is the integrity proof the delete is validated against, and each one
still expires seven days after its own batch.

Artifacts live in `GHOSTREPLAY_SRS_FOLD_EXPORT_DIR` (default
`backend/.srs_fold_exports`), owner-only (0700/0600) because they hold one user's
rows verbatim. Point it at durable storage on its own volume: an artifact a
redeploy can delete is not a seven-day guarantee, and the default directory is
inside the deploy image. `g-srs-retain-rollout` owns making that mandatory before
folding is switched on.

## Verification

```bash
cd backend && source .venv/bin/activate
python -m pytest test_opportunity_compaction.py test_srs_opportunity.py \
  test_opportunity_retention.py test_recompute_srs_opportunities.py
GHOSTREPLAY_TEST_PG_URL=... GHOSTREPLAY_TEST_PG_MAINT_URL=... \
  python -m pytest test_opportunity_compaction_pg.py \
    test_opportunity_compaction_migration.py
```

The PostgreSQL files are not optional. `pg_try_advisory_xact_lock`, `NOWAIT`,
`statement_timeout` cancelling a running query,
`idle_in_transaction_session_timeout` terminating a stalled backend, and the
question those tests exist to answer — *does the user get their locks back?* —
are properties of the lock manager, and SQLite has none.
