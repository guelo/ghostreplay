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
to false and no fold prefix exists, so a fresh deployment behaves exactly as the
previous one: with no freeze there is no age arm to evaluate, whatever M says.

The compactor that actually performs the transfer — one atomic batch, a verified
export written before any lock, and a seven-day window in which every deleted row
can be put back exactly — arrived with `g-srs-fold-recovery` and is documented in
**[`RECOVER_SRS_FOLD.md`](RECOVER_SRS_FOLD.md)**. It deletes nothing until
`cleanup_enabled` is set.

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
  bounded fold transfer (which arms `ghostreplay.srs_fold_mode` for the one
  DELETE statement and clears it again), and the cascade from deleting the parent
  blunder (the parent already being gone is the discriminator).
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

A game left open longer than M used to be the sharp edge here: the ghost-move
route passes the in-progress session as the counter exclusion, an exclusion of a
frozen session is refused (correctly — its share may already be folded), and the
refusal reached the player as a 500 on their next move. `g-srs-target-publish`
closed that: the route now catches every retention refusal, drops the TARGET and
still serves a recorded legal move. See **Target publication and folding** below
for what the player gets and what it costs.

Reviews are accepted at **every** session age, take no user advisory lock and
inherit no graph-lock timeout. A review resets the folded since-review counters
(every folded event predates it) and retains the lifetime total.

## Target publication and folding

A target pin and a fold are two decisions about the same evidence. A target
published against a session the compactor has already folded pins history that no
longer exists, and `targeted_30d` — a *denominator* — then silently shrinks and
inflates `p_reach`. An unlocked freeze check cannot prevent that, because the fold
can commit between the check and the INSERT.

So both sides serialize on ONE row, `user_opportunity_retention_state`
(`app/srs_target_admission.py`):

| side | lock | on contention |
| --- | --- | --- |
| publication (`admit_target_publication`) | `FOR SHARE`, waiting up to **750 ms** | suppress the target, serve a move |
| the compactor (`lock_state_for_fold`) | `FOR UPDATE NOWAIT`, ceiling **500 ms** | skip this user, fold on a later sweep |

SHARE rather than exclusive, so a user's own concurrent moves never queue behind
each other — only folding conflicts with publication. Both sides create the row
if it is absent (the migration backfilled only the users that existed then), which
is what makes the interlock total: whichever transaction inserts first holds it,
and the other blocks on the primary-key conflict instead of running unserialized.
**A compactor that locks without creating first is not interlocked at all.**

A user with no row yet serializes on that insert exactly once. The second
publication blocks only while the first one's insert is uncommitted, which is
normally a few milliseconds, and is then admitted like any other; it degrades
only if the creator holds the row past the 750 ms budget. Either way nothing
unserialized gets through and no move is failed.

After the lock, and never before it, publication re-reads policy, fold prefix,
session and `clock_timestamp()` in ONE fresh statement, and holds the share lock
through the decision INSERT and COMMIT. Fresh is load-bearing twice: the
transaction may have started long before the ghost search finished, and it may
have just woken from a wait during which the fold committed.

The compactor **must not** read `false` from `lock_state_for_fold` as "nothing to
fold". It means "a publication holds this user"; a later sweep sees the committed
pin and folds around it.

`false` also leaves the caller's transaction **usable**, with its own timeouts
restored. A lock timeout aborts a PostgreSQL transaction, so the acquisition runs
inside a SAVEPOINT: a sweep over many users can skip one and go straight on to the
next in the same transaction, which is the only thing that makes a *per-user* skip
worth having. Pinned by
`test_pg_a_skipped_fold_leaves_the_sweep_transaction_usable`.

### When a target is suppressed

Every reason below ends the same way: no target, no invented counter, no
targeting fact, no retention HTTP error — and a legal move, recorded in
`opponent_decisions`, replayable by a retry.

| internal reason | condition | log level |
| --- | --- | --- |
| `targeting_after_fold` | `started_at <= folded_through_started_at` | **ERROR — alarm** |
| `mutation_window_expired` | `started_at <= clock_timestamp() - M`, under `freeze_enabled` | INFO |
| `state_lock_timeout` | a fold held the row past 750 ms | WARNING |
| `publication_timeout` | the bounded INSERT/fact window expired | WARNING |
| `missing_retention_policy` | policy row 1 is gone; there is no M to hold a target against | ERROR |
| `missing_retention_state` | the state row could not even be created | ERROR |
| `session_unavailable` | the session vanished mid-request | WARNING |
| `retention_counters_unavailable` | `load_opportunity_counters` raised (missing summary, stale review basis, discarded targeted history, frozen exclusion) | ERROR |

`targeting_after_fold` is the one that is not an expected degradation. The prefix
only advances over evidence already deleted, so a live session steering behind it
means the eligibility side let through history that is gone — alarm on it, do not
merely count it.

`missing_retention_policy` on **every** request, rather than once, is not a
retention event at all: it means the database was built by
`Base.metadata.create_all` instead of by the migrations, so row 1 was never
seeded and no target can ever be published. That is what
`app.opportunity_retention.ensure_retention_policy_row` is for — the e2e seed
database and the PostgreSQL gate's post-TRUNCATE restore both call it, and a
migrated deployment already has the row. Check the alembic revision before
reaching for anything in this table.

The reasons are **internal telemetry only** (a `retention_suppression` property on
`opponent_move_served`, plus the log line). No response field, schema value or
enum carries them: to the client and to root confirmation this is an ordinary
non-targeted move.

The served move is a structural drill move where the drill has one, and otherwise
the ordinary engine move — `choose_move`, exactly as any untargeted request would
get. A suppressed request loses its *target*, not its opponent: several of these
reasons do not clear on their own (`retention_counters_unavailable` raises on
every move of a frozen session, and on every game of a user whose summary needs
repair), so anything weaker than the engine here would degrade that player's
opponent for the rest of the game and write those moves into their position graph.

Only when the engine **also** fails does a deterministic local legal move answer
(`opponent_move_controller.fallback_move`, seeded per user/position/session over
the sorted legal UCIs). That is the floor that makes the guarantee unconditional,
and it covers both ways `choose_move` can fail: the remote Maia3 API is allowed to
be down and answers 503 when it is, and the move it returns is derived from
`moves` rather than from the request FEN, so it can come back illegal in the
position actually being played and be rejected as a 400. Neither is the client's
fault and a legal move demonstrably exists in both, so serving one beats erroring.
An *ordinary* request still gets that 503 or 400 — nothing about a suppressed
target makes an engine failure acceptable for everyone else. The one error that
survives here is a position with no legal move at all (malformed or terminal FEN):
`fallback_move` reads the FEN and nothing else, so it raises too and the request
gets its ordinary 400.

The targeted candidate itself is dropped rather than re-served untargeted:
recording the steer with no target would drop a real attempt out of the `p_reach`
denominator. Re-deciding may legitimately land on the same move — a post-root
drill constrains the ghost search to the structural set — and that is fine; what
must not happen is a choice made *because of* a blunder being kept while the
target that explains it is not.

### Publication lifetime and cancellation bounds

The share lock is held for exactly one window, and G is a drain gap for in-flight
writers, so that window has to be finite. It is bounded by transaction-local
settings, which reset at COMMIT/ROLLBACK — precisely the critical section:

| window | `lock_timeout` | `statement_timeout` |
| --- | --- | --- |
| acquisition (`FOR SHARE`) | 750 ms | 5 s |
| publication: freeze check → INSERT → targeting fact → COMMIT | 2 s | 10 s |
| the compactor's acquisition | 500 ms | its own |

Those two bound **statements**, not the transaction, so a third is armed for the
whole publication: `idle_in_transaction_session_timeout` = **5 s**. Without it a
worker that stalled *between* statements — a blocked thread, a paused process, a
live connection nobody is driving — would hold the share lock for as long as it
stayed alive, and TCP keepalives only ever notice a peer that is already gone.
Every gap in this window is microseconds of Python, so a bound in seconds can
only fire on a genuine stall; when it does, PostgreSQL terminates that backend,
which is the only way to get the lock back from one. That last step is the whole
value of the setting and is tested as such, not merely configured:
`test_pg_an_idle_publication_is_terminated_and_frees_the_row` drives the ceiling
down to 500 ms, stalls a real publication holding `FOR SHARE`, and waits for the
next fold to acquire the row.

The real lifetime is therefore the **sum** over the handful of statements in the
window, not any single number in the table.

The statement ceiling exceeds the lock budget on purpose: with the two equal, the
acquisition would be *cancelled* (57014) before `lock_timeout` (55P03) could fire,
and every contended publication would be indistinguishable from a cancellation.

The 2 s lock bound covers the one wait inside the window that is not ours: a
concurrent identical request that has speculatively inserted the same
`(session_id, request_fingerprint)`. Exceeding it degrades to a non-targeted move
rather than failing one.

Nothing remote runs inside the window. The ghost search and the route BFS complete
before acquisition; the engine call on a suppressed request happens *after* the
rollback, with no retention lock held. So an in-flight publication drains in
**seconds**, three orders of magnitude inside G = 1 hour.

An HTTP or proxy timeout is **not** a substitute for any of this: cancelling the
request does not roll back a backend still blocked inside the database. The
request as a whole — ghost search plus a remote Maia call — is not bounded here,
and does not need to be: it holds no retention lock while it runs.

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

It reaches **folded** evidence too. Those raw rows are already deleted, so what
is left is the `opportunity_fold_batches` manifest that describes them and the
export it points at — and nothing cascades, because this purge keeps the account
the manifest hangs off. No transaction can unlink a file, so the manifest is
**expired in place** rather than deleted: `expires_at` and `restored_at` are set
to now and the per-blunder ledger is emptied, which leaves a row that still names
the file for the next `fold_srs_opportunities.py expire` and holds nothing open —
not the recovery anchor, not the schema downgrade
(`scripts/RECOVER_SRS_FOLD.md`). Run `expire` after a purge if you need that
file gone on a deadline rather than on the sweep's schedule; until it runs, the
tombstone shows up in `status` as a restored batch that is due to expire.

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
python -m pytest test_opportunity_retention.py test_opportunity_compaction_migration.py \
  test_srs_target_publication.py test_opponent_move_controller.py
GHOSTREPLAY_TEST_PG_URL=... GHOSTREPLAY_TEST_PG_MAINT_URL=... \
  python -m pytest test_opportunity_lifecycle_pg.py test_opportunity_compaction_migration.py \
    test_srs_target_publication_pg.py
```

The PostgreSQL file is not optional coverage. Triggers, transaction-local custom
settings, the deferred constraint trigger and `clock_timestamp()` inside PL/pgSQL
have no SQLite equivalent, and the shared-policy suite does not stand in for them.
