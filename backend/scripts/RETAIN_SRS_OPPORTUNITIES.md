# SRS opportunity retention: storage, freeze boundary and purge

Focused reference for `g-srs-retention-state`. It describes what the storage and
guards DO once deployed. It is not an activation procedure: deployed readiness,
writer drain and activation belong to `g-srs-retain-rollout`.

P0-B is **DECIDED** (`g-compact-srs-events`, 2026-09-20): **M = 60 days, G = 1
hour**, accepting the unmeasured late-write tail explicitly. Migration
`20260919_04` seeds the policy row at exactly those values and the insert
supplies only `id`, so an activation that flips the switches without setting M
gets the decided horizon rather than a placeholder. Changing M remains a policy
change that bumps `version`.

Everything below ships **inert**. `freeze_enabled` and `cleanup_enabled` default
to false, no fold prefix exists, and no code in this release folds or deletes a
raw row. A fresh deployment therefore behaves exactly as the previous one: with
no freeze there is no age arm to evaluate, whatever M says.

## The three tables

`opportunity_retention_policy` — exactly one row, `id = 1`.

| column | meaning |
| --- | --- |
| `mutation_window_days` (M) | how long a session's broad opportunity evidence stays writable; seeded at the decided **60** |
| `grace_seconds` (G) | the compactor's drain gap AFTER M; freeze at `started_at + M`, fold no earlier than `+ M + G`; seeded at the decided **3600** |
| `version` | bumped on every policy change, so one fold snapshots one version |
| `readiness` | every blunder has a summary AND a current review basis — verified, not assumed (see below) |
| `freeze_enabled` | old sessions stop accepting evidence writes |
| `cleanup_enabled` | raw rows may be physically deleted |

M and G are global. A per-user copy would let two users disagree about which raw
rows are foldable while sharing one blunder's summary arithmetic.

`user_opportunity_retention_state` — one row per user.

| column | meaning |
| --- | --- |
| `folded_through_started_at` | permanent inclusive fold prefix |
| `targeted_discarded_max_served_at` | newest discarded targeted `served_at` |
| `sweep_progress_started_at` | operational progress only |

`blunder_opportunity_summaries` — one row per blunder, created eagerly, holding
`folded_eligible_count`, `folded_opportunities_since_review`,
`folded_reached_since_review` and the review basis those counters were folded
against.

## The rollout ladder

`readiness` → `freeze_enabled` → `cleanup_enabled`, enforced by check
constraints, not by callers. Folding evidence that writers can still rewrite
would lose writes; freezing before every summary exists would freeze
undetectable holes. Each step is a separate approval.

### Before `readiness`

`readiness` is a claim the reader enforces: after it, a blunder with no summary
or with a review basis that lags its live latest review raises
`RetentionInvariantError` — on the ghost-move path, which must never fail a
move. So the switch is gated on a query, not on "the migration ran":

```bash
cd backend && source .venv/bin/activate
python scripts/reconcile_srs_review_basis.py --check   # exit 0 == ready
```

Migration `20260919_04` stamps every existing blunder's summary WITH its current
review basis, but that is a snapshot taken mid-deploy. Old application instances
keep inserting blunders and reviews, with neither summary nor basis, until the
last one is replaced. Run the reconciler after the deploy has fully rolled, as
many times as needed — it is idempotent:

```bash
python scripts/reconcile_srs_review_basis.py --dry-run  # reports, rolls back
python scripts/reconcile_srs_review_basis.py
```

Only set `readiness = true` once `--check` exits 0 with both counts at zero.

Turning the switches back off stops freezing NEW history. It does **not** unfreeze
what was already folded: those raw rows are gone, and a writer allowed to rewrite
that session would recreate rows a summary has already absorbed. The fold-prefix
arm of every guard is therefore unconditional.

## What each guard does

* **Evidence writes** — `_compute_blunder_opportunity_events` is the one choke
  point for uploads, the deferred worker and both session-grain repair modes. It
  refuses a frozen session before any delete or upsert and returns a skip, which
  the repair CLI prints and counts. Both blunder-grain repair modes take the same
  decision through `frozen_session_ids`.
* **Individual session delete** — a PostgreSQL `BEFORE DELETE` trigger on
  `game_sessions`, which also stops the cascade to the event rows.
* **Individual event delete** — a `BEFORE DELETE` trigger on
  `blunder_opportunity_events`, allowing exactly two legitimate deletions: a
  bounded fold transfer, and the cascade from deleting the parent blunder (the
  parent already being gone is the discriminator).
* **Reads** — `load_opportunity_counters` adds the folded totals to the live rows
  in ONE statement. After readiness, a missing summary or a lagging review basis
  raises instead of quietly serving a smaller number.

Every freeze decision samples `clock_timestamp()` **after** the per-user graph
write lock — the upload path and the worker already hold it, and every repair
mode now takes it itself. That matters because the event guards cover DELETE
only: an unserialized repair could pass the freeze check and then have a fold
commit underneath it.
A transaction queued before the deadline that wakes up after it is rejected.
`run_opportunity` is not finality, and being enqueued early is not a rescue:
observe and repair failed evidence BEFORE it freezes.

Do **not** set `freeze_enabled` before `g-srs-target-publish` ships, even with
readiness satisfied. The ghost-move route passes the in-progress session as the
counter exclusion and checks only that the session is *active*, with no age
limit. An exclusion of a frozen session is refused — correctly, since its share
may already be folded — so a game left open longer than M would take a 500 on
its next move. `g-srs-target-publish` owns the fallback that turns that into
suppressed targeting and a legal move. The bead dependencies already sequence
this; the only way to reach it is to flip the switch by hand first.

Reviews are accepted at **every** session age, take no user advisory lock and
inherit no graph-lock timeout. A review resets the folded since-review counters
(every folded event predates it) and retains the lifetime total.

## Whole-user purge

`app.opportunity_purge.purge_user_training_history` is the only code that sets
the transaction-local markers `ghostreplay.srs_purge_mode = 'user_training'` and
`ghostreplay.srs_purge_user_id = <owner>`. Both must match exactly; half a marker
set is no marker at all. It is an accidental-misuse guard, **not** authorization:
an administrator with arbitrary SQL can set them, and what prevents that is
database access control.

It bypasses only the new freeze guards. `session_replication_role` is never
touched and every pre-existing foreign key still applies. The caller owns the
COMMIT, because the deferred completeness assertion only runs at COMMIT — a
partial purge rolls back rather than leaving orphaned summaries behind a deleted
prefix.

Lock order is user → retention state → parent rows. No trigger takes a user lock,
so none can invert it.

It deletes more than evidence. Four tables reference `blunders` or `game_sessions`
with no cascade, and all four go first: `session_moves.target_blunder_id` and
`opponent_decisions.target_blunder_id` are nulled, `opponent_target_facts` and
`blunder_reviews` are deleted, and `rating_history` goes with the sessions it was
earned in. **The user's Elo and games-played chain resets.** That is what deleting
a whole training history means; say so before running it, do not discover it from
a foreign key error.

A single-blunder deletion does **not** go through this helper. It cascades through
its own foreign keys.

## Verification

```bash
cd backend && source .venv/bin/activate
python -m pytest test_opportunity_retention.py test_opportunity_compaction_migration.py
GHOSTREPLAY_TEST_PG_URL=... GHOSTREPLAY_TEST_PG_MAINT_URL=... \
  python -m pytest test_opportunity_lifecycle_pg.py test_opportunity_compaction_migration.py
```

The PostgreSQL file is not optional coverage. Triggers, transaction-local custom
settings, the deferred constraint trigger and `clock_timestamp()` inside PL/pgSQL
have no SQLite equivalent, and the shared-policy suite does not stand in for them.
