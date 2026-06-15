# Opening-score read freshness — Approach B (durable dirty cursor), REJECTED

**Status:** Rejected in favor of **Approach A** (non-blocking reader). The live
plan is in bead **g-v2tk**; this document archives the rejected design and why.

**Bead:** g-v2tk · **Epic:** g-opening-score-v2 · Related: g-6zhp (cheap SQL gate,
shipped), g-opening-score-v2.

## Background

`/opening` was slow on every read — plain reloads *and* every navigation down the
opening tree — because all three read endpoints funnel through `_load_cached_rows`,
which calls a **blocking** `refresh_now`. g-6zhp removed the ~2.6s python-chess
overlay build on cache hits, but the read still paid (a) the blocking scheduler
round-trip and (b) `raw_evidence_inputs_digest` (O(all `session_moves`)) on every
load.

Two ways to fix it:

- **Approach A — non-blocking reader (stale-while-revalidate).** Serve the cached
  batch immediately; schedule a coalesced *background* recompute. The existing
  g-6zhp gate (`recompute_opening_scores_if_needed`) does the real content-based
  freshness check off the read path. Trade-off: post-write reads are eventually
  consistent.
- **Approach B — block only when a recompute is genuinely owed.** Preserve
  synchronous read-your-writes, but make the "is a recompute owed?" decision cheap
  and durable. This is the approach archived below.

## Why Approach B was rejected

Approach B went through four review rounds and accumulated **ten findings**
(F1–F10). The decisive realization: every one of those findings is an artifact of
maintaining a **separate, cheap, durable, exhaustively-covered freshness signal**
that runs parallel to the real evidence, purely so the read path can answer "is a
recompute owed?" without doing the real check. That is inherently hard, and:

1. **Silent-staleness failure mode.** B's correctness depends on *exhaustive*
   evidence-write-surface coverage (every write must bump the durable signal). Miss
   one surface — including a future evidence source someone forgets to wire — and B
   serves wrong scores with no signal. The review already found two missed surfaces
   (F7 blunder creation, F9 out-of-process scripts); the next one is a latent bug.
2. **A sidesteps the entire class.** A's background recompute reads *real* evidence
   via the content digest (ghost targets, `analysis_cache`, etc.), so any evidence
   change is caught automatically. No write-surface audit to keep exhaustive, no
   durable cursor, no migration, no scheduler freshness machinery. F4/F5/F6/F10 (the
   blocking-debt correctness work), F7/F9 (write-surface coverage), and F8 (restart
   durability) all become non-issues. A is **robust to future evidence sources**
   where B is **fragile** to them — a lasting maintainability difference.
3. **B's read-your-writes is a multi-second block anyway.** B's only advantage was
   synchronous freshness, but a post-write read still pays the *full* recompute
   (~2.6s, bounded by the 5s `refresh_now` timeout). So B's "fresh" read is a
   multi-second spinner after a drill. A shows the prior scores **instantly** and
   converges on the next navigation. For a derived analytics metric (not a
   transactional value), instant-stale-then-converge is the better UX.
4. **The motivating feature doesn't need it.** The planned "after a drill, show how
   it affected your opening score" screen is read-your-writes, but it should get a
   **dedicated scoped/incremental endpoint** (compute and return old→new for just
   the drilled opening) — which it wants anyway for snappiness. That never required
   the generic reader to be synchronous.

Net: the complexity (a migration + three web write paths + three scripts + the
scheduler + the reader + the recompute) buys a worse-UX version of a guarantee we
don't actually need generically. Approach A solves the real complaint (slow reads /
navigation) with a ~10-line reader change and far less surface area.

## Findings log (B), for posterity

- **F1** self-heal must not block the next navigation.
- **F2** evidence-derivation version (`OPENING_EVIDENCE_INPUTS_VERSION`) is outside
  `opening_score_inputs_fingerprint`, so a cheap registry_fingerprint read check
  misses semantic drift.
- **F3** a read racing an in-flight recompute enqueues a redundant second recompute.
- **F4** read-before-gate ordering returns a pre-write snapshot.
- **F5** a failed freshness recompute loses the debt.
- **F6** using `_last_result` (latest outcome) as the satisfaction source lets a
  later *background* failure clobber a successful *write* — re-opening satisfied
  debt. (Needs a monotonic success-only watermark.)
- **F7** blunder creation (`_record_target`) is an uncovered evidence-write surface.
- **F8** in-memory freshness debt is not durable across process restart.
- **F9** out-of-process script writers (`scripts/precompute_openings.py:725`,
  `scripts/ingest_scores.py:159`, `scripts/repair_analysis_cache.py`) mutate
  `analysis_cache` and cannot enqueue in the web scheduler.
- **F10** `refresh_now` must wait against the reader's *observed* dirty_seq, not the
  entry-time seq.

The durable-cursor design (v5) below resolves all ten — F4/F5/F6 dissolve into a
monotonic DB comparison — and is archived in full for reference.

---

## Archived plan (Approach B / v5 — full text)

```text
========================================================================
PLAN v5 — Approach B: DURABLE dirty cursor is the freshness authority
(rewritten after 4 review rounds; addresses Findings F1..F10)
========================================================================

ARCHITECTURE
------------
The freshness signal is a DURABLE per-(user,color) dirty cursor in the DB, not
in-memory scheduler state. Every evidence-changing write bumps a monotonic
dirty_seq; every successful recompute records the dirty_seq it observed at its
start into cleaned_seq. A read is "owed a recompute" iff dirty_seq > cleaned_seq.
This is durable across process restart (F8), visible to out-of-process writers
(F9), race-free against write-during-recompute, and it DISSOLVES the entire
in-memory freshness-debt model (the v2-v4 _freshness_seq / _last_success_seq /
has_freshness_required_work machinery and Findings F4/F5/F6 disappear — they were
all symptoms of using non-durable in-memory state as the authority).

PROBLEM RECAP
-------------
After g-6zhp the cache-hit overlay build is skipped, but every /opening read still
(a) blocks on refresh_now() and (b) runs raw_evidence_inputs_digest() (O(all
session_moves)). Paid on plain reloads AND on every tree navigation, since all 3
read endpoints funnel through _load_cached_rows() -> refresh_now(), though the
cached batch is identical across navigations of a (user,color).

FINDINGS ADDRESSED (cumulative)
-------------------------------
F1  self-heal must not block the next navigation -> only EVIDENCE writes bump
    dirty_seq; self-heal (registry/decay/legacy) schedules a background recompute
    and never dirties, so the cursor stays clean and reads don't block.
F2  evidence-version drift caught cheaply on read -> fold OPENING_EVIDENCE_INPUTS_
    VERSION into opening_score_inputs_fingerprint (registry_fingerprint).
F3  redundant recompute on read-while-recompute -> bounded by scheduler coalescing
    + the cursor going clean (accepted; see Scheduler note).
F4  read-before-gate ordering -> read the cursor BEFORE the batch; the durable
    dirty flag travels with the data, so no stale-snapshot window.
F5  failed recompute must not lose the debt -> cleaned_seq only advances on a
    SUCCESSFUL recompute; a failure leaves dirty_seq>cleaned_seq -> retried.
F6  a later failed run must not re-open satisfied debt -> cleaned_seq is monotonic
    and only set forward by successes; nothing a failure does can lower it.
F7  blunder creation is an evidence write -> mark dirty in _record_target.
F8  (NEW) durability across restart -> dirty cursor is in the DB.
F9  (NEW) out-of-process script writers (precompute_openings.py:725,
    ingest_scores.py:159, repair_analysis_cache.py) -> scripts mark affected
    cursors dirty (or run backfill).
F10 (was F3-medium) refresh_now waits against the reader's OBSERVED dirty_seq, so a
    write landing mid-wait cannot let it return early against a stale target.

EVIDENCE WRITE-SURFACE AUDIT (every surface the digest/overlay reads; each must
bump dirty_seq for the affected (user,color)). Verified by grep across app/ AND
scripts/:
  # Surface (collector)                 Writer(s)                                    dirty bump
  1 session_moves                        session.py move recording (commit @617/662)  ADD (Change 1)
  2 analysis_cache (fallback)            write_analysis_cache_rows <- _upsert_analysis_cache @session.py:630,676
                                         AND scripts/{precompute_openings.py:725, ingest_scores.py:159};
                                         repair_analysis_cache.py deletes rows         ADD: web path Change 1; scripts Change 2 (F9)
  3 blunders/ghost targets               _upsert_blunder_target (sole Blunder ctor, blunder.py:246)
                                         <- _record_target <- record_blunder + record_manual_blunder   ADD (Change 1, F7)
  4 blunder_reviews                      srs.py review submission                     ADD (Change 1)

========================================================================
CHANGE 0 — migration: durable dirty cursor columns
========================================================================
Add to opening_score_cursors (model OpeningScoreCursor, models.py:373):
    dirty_seq:   Mapped[int] BIGINT NOT NULL server_default "0"
    cleaned_seq: Mapped[int] BIGINT NOT NULL server_default "0"
Alembic migration adding both columns (confirm migrations dir; add upgrade/
downgrade). Existing rows default 0/0 -> not dirty -> served from cache (correct:
existing batches are as-fresh-as-last-recompute; the next write dirties them). No
recompute storm on rollout.
Rollout caveat: an evidence write whose pre-deploy in-memory recompute was still
pending at deploy time did NOT bump dirty_seq (old code) and is lost on restart ->
that one (user,color) may be briefly stale post-deploy, backstopped by decay/next
write. One-time, acceptable.

========================================================================
CHANGE 1 — web write paths bump dirty_seq (atomic-ish with evidence)
files: backend/app/api/session.py, srs.py, blunder.py; helper in opening_cache.py
========================================================================
New helper (opening_cache.py):
    def mark_opening_evidence_dirty(db, user_id, player_color) -> None:
        # Upsert the cursor and bump dirty_seq by 1 (postgresql/sqlite dialect
        # inserts, mirroring write_analysis_cache_rows' on_conflict pattern):
        #   INSERT INTO opening_score_cursors(user_id,player_color,latest_generation,
        #       dirty_seq,cleaned_seq) VALUES (?,?,0,1,0)
        #   ON CONFLICT(user_id,player_color) DO UPDATE SET dirty_seq = dirty_seq + 1
        # Best-effort/swallowing like prune; logs on failure.
Call site contract: bump dirty AFTER the evidence is durably committed (so a
recompute that misses the bump still SEES the committed evidence -> at worst a
redundant recompute, never staleness). Prefer same-transaction as the evidence
commit where the path has a single commit:
  - blunder.py _record_target: right before db.commit() @326 (same tx); replaces
    the v4 request_recompute-only fix (F7). Guard on is_new.
  - srs.py review submit: in the same tx as the review commit.
  - session.py: in the same tx as the session_moves commit (@617 and @662), i.e.
    add the cursor bump before those commits, for BOTH the insert and upsert
    branches. (analysis_cache is written just after; its change is covered because
    the bump-after-evidence rule => a recompute either sees the new AC or is
    re-triggered; never stale.)
Keep the existing request_recompute(user_id, player_color) calls: they remain as a
PROMPTNESS optimization (recompute in the background so the post-write read need
not block). Correctness no longer depends on them — the durable cursor is the
backstop if the in-memory enqueue is lost.

========================================================================
CHANGE 2 (F9) — out-of-process scripts mark affected cursors dirty
files: backend/scripts/{precompute_openings.py, ingest_scores.py,
       repair_analysis_cache.py}
========================================================================
After committing analysis_cache mutations, each script must durably mark the
affected (user_id, player_color) cursors dirty so the web app recomputes. Options
in order of preference:
  (a) precise: changed fen_before set -> session_moves(user_id,color) referencing
      them with a missing primary eval -> bump those cursors' dirty_seq.
  (b) broad + logged: bump dirty_seq for all cursors (one UPDATE), acceptable for a
      bulk maintenance run; log the count.
  (c) run backfill_opening_scores after the mutation.
Document the chosen approach in each script; (b) is fine for the bulk ingest/
precompute scripts, (a)/(c) for repair. These run out-of-process, so the durable
cursor (not the in-memory scheduler) is the only viable channel.

========================================================================
CHANGE 3 — recompute reconciles the cursor (capture pending; set cleaned_seq)
file: backend/app/opening_cache.py (recompute_opening_scores_if_needed +
      recompute_opening_scores)
========================================================================
In recompute_opening_scores_if_needed, FIRST thing (before reading any evidence /
digest / overlay), capture:
    pending = SELECT dirty_seq FROM opening_score_cursors WHERE user_id,player_color
              (0 if the row is absent)
Run the existing gate logic (registry drift / raw-digest change / decay / stale
branch keys) unchanged — it may rebuild or serve cached. On ANY successful
completion (rebuild OR confirmed-cached), reconcile in the SAME committed tx as the
batch write (or as a standalone committed UPDATE on the serve-cached branch):
    UPDATE opening_score_cursors SET cleaned_seq = pending
    WHERE user_id,player_color AND cleaned_seq < pending
Why race-free: pending is captured BEFORE the evidence read; any write during the
recompute bumps dirty_seq beyond pending, so afterwards dirty_seq > cleaned_seq ->
still dirty -> re-recompute. A write whose dirty bump is visible (dirty_seq>=pending
at capture) committed its evidence first (bump-after-evidence rule), so the
recompute's evidence read sees it. Reconciling on the serve-cached branch too
prevents an infinite-dirty loop when a dirty bump did not actually change scores.
The recompute reports the reconciled `pending` to the scheduler (for refresh_now's
wait signal, below).

========================================================================
CHANGE 4 (F1 + F4 + F10) — reader: cursor-first, block only when dirty
file: backend/app/api/openings.py
========================================================================
_load_cached_rows(db, user_id, player_color):
      # (F4) Read the durable cursor BEFORE the batch. The dirty flag travels with
      # the data, so a recompute completing mid-read cannot yield a stale snapshot.
      cursor = get_opening_score_cursor(db, user_id, player_color)   # PK lookup
      dirty = cursor is not None and cursor.dirty_seq > cursor.cleaned_seq
      observed = cursor.dirty_seq if cursor is not None else 0

      batch, rows = list_cached_opening_scores(db, user_id, player_color)

      if batch is None or dirty:
          # Block until a recompute reconciles AT LEAST `observed` (F10), then reload.
          refresh_now(user_id, player_color, observed_dirty=observed)
          batch, rows = list_cached_opening_scores(db, user_id, player_color)
          return batch, _snapshot_cached_rows(rows)

      # SELF-HEAL (registry/evidence-version drift, legacy rows, decay): these are
      # NOT evidence writes, so they never set dirty; schedule a NON-blocking
      # background recompute and serve the current batch (F1: next nav won't block).
      registry_fp = opening_score_inputs_fingerprint(get_opening_graph(), get_opening_roots())
      computed_at = batch.computed_at
      if computed_at is not None and computed_at.tzinfo is None:
          computed_at = computed_at.replace(tzinfo=timezone.utc)
      decay_stale = (computed_at is None
                     or computed_at < datetime.now(timezone.utc) - OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL)
      if (batch.registry_fingerprint != registry_fp
              or _batch_has_stale_branch_keys(rows)
              or decay_stale):
          request_recompute(user_id, player_color)   # background, non-blocking
      return batch, _snapshot_cached_rows(rows)
  Imports: datetime/timezone; get_opening_score_cursor + mark/fingerprint helpers
  from opening_cache; refresh_now, request_recompute from the scheduler;
  _batch_has_stale_branch_keys from opening_aggregate; get_opening_graph/roots.
  Cursor-first ordering note: if cursor reads clean, cleaned_seq>=dirty_seq means a
  successful recompute already reflected every committed evidence write, so the
  batch read next IS fresh; a write landing after the clean cursor read is
  concurrent (not a read-your-writes violation).

========================================================================
CHANGE 5 — scheduler simplification + observed-dirty wait
file: backend/app/opening_score_scheduler.py
========================================================================
REMOVE (no longer needed — the cursor is the authority):
  _freshness_seq, _last_success_seq, has_freshness_required_work,
  request_background_recompute, and the _Entry.freshness_required / _inflight_*
  bookkeeping introduced in v2-v4.
KEEP: the debounce/coalesce executor, single-worker serialization (_inflight),
  request_recompute (used by writes for promptness AND by the reader's self-heal).
ADD a wait signal:
  _last_cleaned_seq: dict[Key,int]   # highest dirty_seq a SUCCESSFUL recompute
                                     # reconciled (reported by Change 3). Monotonic.
  _run_one: after a successful recompute, set
      _last_cleaned_seq[key] = max(_last_cleaned_seq.get(key,0), reconciled_pending)
  and notify. (A failure does not advance it -> F5/F6.)
refresh_now(user_id, player_color, observed_dirty, timeout=5.0) -> bool:
  - enqueue an immediate recompute for the key (coalesces with any pending);
  - wait on the condition until _last_cleaned_seq[key] >= observed_dirty (a covering
    successful run reconciled the reader's observation) or timeout/shutdown;
  - the immediate run is enqueued AFTER the reader observed `observed`, so its
    captured pending >= observed -> it reconciles >= observed -> wakes the waiter.
  Returns True on covering success, False on timeout/shutdown (reader serves the
  reloaded batch either way; the durable cursor drives a retry on the next read).
F3 note (accepted): concurrent navigations-while-dirty coalesce into one pending
  entry; once the recompute cleans the cursor, later navigations read clean and do
  not call refresh_now. A single extra recompute is possible if a navigation
  enqueues just after the worker popped the prior run; bounded and self-terminating.

========================================================================
CHANGE 6 (F2) — persist evidence-derivation version in the logic fingerprint
file: backend/app/opening_cache.py   (unchanged from v2-v4)
========================================================================
Append ":{OPENING_EVIDENCE_INPUTS_VERSION}" to opening_score_inputs_fingerprint so
batch.registry_fingerprint folds it -> the read-path self-heal drift check AND the
worker registry_drift gate both catch an evidence-semantics bump. Simplify the now-
redundant explicit fold in opening_score_raw_inputs_fingerprint; update docstrings.
One-time self-heal recompute on rollout; no extra migration.

CORRECTNESS SUMMARY
-------------------
- Read-your-writes (F4/F8/F9): writes (incl. blunders and out-of-process scripts)
  durably bump dirty_seq after committing evidence. The reader reads the cursor
  first; dirty_seq>cleaned_seq -> block + recompute + reload. Durable across restart
  and visible to scripts.
- No false clean / no false dirty (F5/F6): cleaned_seq advances ONLY on a
  successful recompute and is monotonic; dirty_seq advances only on evidence writes.
  Failures and background self-heal failures cannot mis-set either.
- Mid-recompute write (race): pending captured before evidence read; a concurrent
  write leaves dirty_seq>cleaned_seq -> re-recompute. Never stale.
- Self-heal non-blocking (F1): registry/decay/legacy never dirty -> reads don't
  block; background recompute converges.
- Observed-dirty wait (F10): refresh_now waits for cleaned reconciliation >= the
  dirty_seq the reader actually saw.

BEHAVIOUR CHANGE TO REVIEW (intentional)
----------------------------------------
Deploy/evidence-version/decay/legacy reads serve a slightly stale batch and
converge on a later load instead of blocking. Evidence-write freshness (moves,
reviews, blunders, scripts) is durable and exact. Flag in the PR for sign-off.

WHY B OVER A (+ future feature)  [unchanged]
-------------------------------
"After a drill, show how it affected your opening score" is read-your-writes; A
serves the pre-drill batch first, B serves it fresh (the drill dirties the cursor;
the reader blocks on the genuine recompute). Follow-on (separate bead): snappiness
of that screen = scoped/incremental post-write recompute, not switching to A.

PERFORMANCE EXPECTATION
-----------------------
- Plain reload / navigation, no writes: cursor clean -> NO refresh_now, NO digest
  -> two indexed lookups (cursor PK + batch) + in-memory aggregation. ~instant.
- Post-write read: blocks on the genuine recompute (unchanged), returns durably
  fresh; redundant recomputes bounded by coalescing.

TEST PLAN (pytest, backend/tests; named files in isolation — pre-push suite flaky
under parallel load)
-----------------------------------------------------------------------------------
Cursor / durability (core):
  - F8 restart sim: write bumps dirty_seq, NO recompute runs (simulate lost
    in-memory enqueue), then read -> dirty -> recompute -> fresh; cleaned_seq==
    dirty_seq after.
  - F5: recompute stub fails -> cleaned_seq NOT advanced -> still dirty -> next read
    retries to success.
  - F6: a later self-heal recompute success at higher dirty cannot lower cleaned_seq;
    a failure never lowers it.
  - Mid-recompute write race: bump dirty_seq during a recompute (pending captured
    earlier) -> after reconcile, dirty_seq>cleaned_seq -> dirty -> re-recompute.
  - Serve-cached branch still reconciles cleaned_seq (no infinite-dirty loop when a
    dirty bump did not change scores).
Reader (_load_cached_rows / endpoints):
  - Steady state (cursor clean): all 3 read endpoints call neither refresh_now nor
    any overlay/digest build (spies assert zero); cached batch returned.
  - Navigation: N sequential /children reads, cursor clean -> zero recompute.
  - F4 ordering: cursor read precedes batch read (call-order spy); a recompute
    completing mid-read never yields a stale snapshot.
  - F1: force registry/decay/legacy drift (cursor CLEAN) -> serve current + schedule
    background recompute (NOT block); a second read does not block.
  - Cold cache (no batch) computes synchronously.
Scheduler:
  - refresh_now blocks until _last_cleaned_seq>=observed_dirty; returns False on
    timeout; coalesces concurrent waiters onto one run (F3/F10).
Write surfaces (F7/F9 + audit): record_blunder + record_manual_blunder (is_new),
  session move recording, srs review submit each bump dirty_seq; dedupe/first-move-
  skip does not. Script(s) mark affected cursors dirty after analysis_cache mutate.
Fingerprint (F2): opening_score_inputs_fingerprint changes on OPENING_EVIDENCE_
  INPUTS_VERSION bump; raw fp still changes on that bump.

FILES TOUCHED
-------------
- migration (Alembic): opening_score_cursors +dirty_seq +cleaned_seq
- backend/app/models.py                    (cursor columns)
- backend/app/opening_cache.py             (mark_opening_evidence_dirty,
                                            get_opening_score_cursor, pending capture +
                                            cleaned_seq reconcile, F2 fingerprint)
- backend/app/opening_score_scheduler.py   (simplify; _last_cleaned_seq; observed-
                                            dirty refresh_now)
- backend/app/api/openings.py              (cursor-first reader, self-heal background)
- backend/app/api/session.py, srs.py, blunder.py  (mark dirty on evidence commit)
- backend/scripts/{precompute_openings.py, ingest_scores.py, repair_analysis_cache.py}
                                            (mark affected cursors dirty)
- backend/tests/...                        (cursor/durability, reader, scheduler,
                                            write-surface, fingerprint tests)

MIGRATION: YES (Change 0) — two BIGINT columns on opening_score_cursors, default 0,
self-healing rollout (no recompute storm).
```
