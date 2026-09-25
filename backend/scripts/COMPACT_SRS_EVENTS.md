# SRS retention correctness qualification

`g-srs-retention-gates` qualifies the integrated implementation with synthetic
facts and disposable PostgreSQL databases. The decided policy is **M = 60 days,
G = 1 hour**. This report does not activate retention or authorize production
deletion. Deployment, writer drain, durable exports, canary and recurring cleanup
remain owned by `g-srs-retain-rollout`.

## Reproduction

Activate `backend/.venv`. Supply explicit URLs for an **empty test database** and
its maintenance database; the fixtures truncate test tables and create/drop
randomly named `ghostreplay_mig_test_*` databases. Never use production URLs.
The maintenance role must be able to create databases. Each test invocation takes
the existing PostgreSQL schema lease. Run versions sequentially.

```bash
cd backend && source .venv/bin/activate
export GHOSTREPLAY_TEST_PG_URL=postgresql+psycopg://127.0.0.1:55445/ghostreplay_test
export GHOSTREPLAY_TEST_PG_MAINT_URL=postgresql+psycopg://127.0.0.1:55445/postgres
: "${GHOSTREPLAY_TEST_PG_URL:?explicit test URL required}"
: "${GHOSTREPLAY_TEST_PG_MAINT_URL:?explicit maintenance URL required}"
python -m pytest \
  test_srs_retention_gates.py test_srs_retention_integration_pg.py \
  test_srs_opportunity.py test_opportunity_retention.py \
  test_opportunity_lifecycle_pg.py \
  test_srs_target_publication.py test_srs_target_publication_pg.py \
  test_opportunity_compaction.py test_opportunity_compaction_pg.py \
  test_opportunity_compaction_migration.py \
  test_opportunity_cleanup.py test_opportunity_cleanup_pg.py \
  test_recompute_srs_opportunities.py \
  test_opponent_decision_retention.py test_opponent_session_expiry.py \
  test_pg_gate_plugin.py::test_manifest_matches_real_pg_gate_collection \
  -q -rP -W error --durations=12
```

The `-rP` output includes `SRS_STORAGE_REPORT`, a synthetic-only JSON record of
both turnovers, relation sizes and file bytes. Check that **nothing skipped**.
For the required-mode **whole** PostgreSQL gate, use the same explicit URLs and
`GHOSTREPLAY_REQUIRE_PG_TESTS=1 python -m pytest -m pg_gate --strict-markers -rs`.
That mode intentionally requires the complete manifest, so it cannot be applied
to the focused selection above. New qualification cases and every parameter
variant are registered in `backend/pg_gate_plugin.py`.

Local rehearsal uses isolated Homebrew clusters created with `initdb -A trust
--no-locale -E UTF8`, listening only on loopback, under
`/private/tmp/g-srs-gates-pg15` (port 55445) and
`/private/tmp/g-srs-gates-pg18` (port 55446). PostgreSQL 15 matches the CI major;
18 matches the documented release major. A local 18.4 run is not a claim about
the installed patch level or settings of a live Railway database.

Final rehearsal on 2026-09-24: **394 passed, zero skipped** on PostgreSQL
**15.18** and **18.4**, each in 51.44 seconds with warnings treated as errors.
The storage test took 13–14 seconds; each randomized seed took about two seconds.
The two archived raw-reader functions were also compared byte-for-byte with
`15854a1`. Local logs: `/private/tmp/g-srs-gates-pg15-final.out` and
`/private/tmp/g-srs-gates-pg18-final.out`. The report records durable results;
those temporary logs are not an operational dependency.

## Evidence and coverage

| Obligation | Executable evidence |
| --- | --- |
| Five counters and score parity after committed operation sequences | `test_srs_retention_gates.py::test_pg_randomized_commits_preserve_the_raw_oracle`, seeds 7 and 29 |
| Final served stamp, uncommitted target pin, commit/rollback/disconnect beyond M and G | `test_srs_retention_integration_pg.py::test_pg_final_stamp_interlocks_with_real_fold_past_grace` |
| Computation resumes after a real fold; M increase preserves the prefix; legal fallback/replay with Maia down | `test_pg_computation_outliving_grace_falls_back_after_real_fold` in that same file |
| Real fold holds state; publication waits, then sees changed policy/clock, or times out and rolls back before fallback | `test_pg_endpoint_waits_for_actual_fold_then_rechecks_or_degrades` |
| Missing policy, unavailable targeted history and failed state creation give persisted legal fallback without Maia | `test_pg_missing_authority_serves_legal_replay_without_maia`, `test_pg_state_creation_failure_rolls_back_before_legal_fallback` |
| New-owner state creation preserves a target | `test_pg_missing_state_is_created_before_target_publication` |
| Real evidence worker overlaps folding across grace without lost evidence | `test_pg_evidence_worker_commit_outliving_grace_is_not_lost` |
| Review lock ordering/NOWAIT, statement cancellation, stalled client, failed rollback, recovery-anchor races and released locks | `test_opportunity_compaction_pg.py`, `test_srs_target_publication_pg.py` |
| Direct frozen-session/event deletes, whole-blunder cascade, deferred purge completeness, purge marker rollback and ownership | `test_opportunity_lifecycle_pg.py` |
| Activity during export, forced cleanup, bounded attempts and backlog truth | `test_opportunity_cleanup.py`, `test_opportunity_cleanup_pg.py` |
| Current targeting source, inclusive pin/window edges, immutable prefix, frozen repair/exclusion and review reset | `test_srs_opportunity.py`, `test_opportunity_retention.py`, `test_opportunity_compaction.py`, `test_recompute_srs_opportunities.py` |
| Independent replay R expiry and deletion races | `test_opponent_decision_retention.py`, `test_opponent_session_expiry.py` |
| Restore original facts alongside later writes/reviews/purge; refuse expired rollback | `test_opportunity_compaction_migration.py` |

The randomized test preserves the two raw SQL reader functions **verbatim from
commit `15854a1`** in `backend/srs_raw_oracle.py`. The oracle has its own SQLite
database and retains original event facts when PostgreSQL folds them. It never
reads summaries, exports, contributions or fold watermarks. Supported mutable
repair replacements are mirrored explicitly; frozen repairs must report skips.
Each seed shuffles five repetitions of fold, review, upload/recompute, repair,
policy change, clock advance and replay, comparing all five counters using both
original decisions and backfilled compact facts, plus practice-priority inputs
and supported current-session exclusions. SQLite is only the independent raw
arithmetic oracle; the implementation and all contention run on PostgreSQL.
Existing deterministic tests own NULL timestamps, broad-ineligible reaches,
review ties, exact lower bounds and other component edge cases.

New race tests use independent connections and `Event` barriers; the waiting
publication is observed in PostgreSQL's lock manager before release. SQL clock
expressions advance synthetic time; no test changes the host/server clock or
uses a sleep to stand in for a commit. The existing component suites exercise
actual `clock_timestamp()` and server-side lifecycle triggers. Temporary SQL
fault injection rejects state creation with a real integrity error and is
removed in `finally`.

The end-to-end restoration rehearsals now migrate to **head** before running
current models/writers, then downgrade only after restoration. Previously they
stopped at the manifest migration and failed inserting `GameSession` because
the current model includes the subsequent `last_activity_at` column. Historical
schema-only migration tests keep their pinned revisions.

## Net storage measurement

The fixture adds 1,200 sessions with four pairs each per turnover: 4,000 old
pairs eligible to fold and 800 young pairs retained. Half the blunders carry
targeting and half never do; half the pairs are reached, and none has reviews.
Young targeting is in-window, old targeting has expired. The second turnover
advances by eight days for recovery expiry and then another 60 days plus one
hour, so the previous young cohort becomes foldable. There are 9,600 original
pairs in total, with at most 5,600 raw pairs present at once. This is an ordinary
seconds-long test, not the retired 500,000-pair capacity benchmark.

Accounting includes `pg_total_relation_size` for:

- `blunder_opportunity_events`, including every index and TOAST relation;
- `blunder_opportunity_summaries`, including the retained reach BIGINT;
- `user_opportunity_retention_state` and `opportunity_retention_policy`;
- `opportunity_fold_batches`, including JSON contribution ledgers and indexes;
- every recovery export file, reporting both payload length and allocated blocks.

The original baseline is **only the raw event relation**, not the sum of new
empty tables. Turnover two conservatively compares against only the raw rows
present before that sweep, excluding the earlier rows already folded. Existing
sessions, decisions and target facts are unchanged by the fold and are outside
both sides of this incremental opportunity-storage comparison.

`VACUUM FULL ANALYZE` is used **only in the disposable test database** to measure
an equivalent packed footprint, including indexes/TOAST. The report separately
captures allocated sizes immediately after DELETE. Ordinary deletion does not
shrink the volume; neither a production rewrite nor an immediate disk reduction
is part of this qualification.

Measured relation sizes (bytes) on the local PostgreSQL 15 and 18 rehearsals:

| Quantity | Turnover 1 | Turnover 2 |
| --- | ---: | ---: |
| Original raw relation before fold | 802,816 | 925,696 |
| All opportunity relations allocated immediately after DELETE | 983,040 | 1,114,112 |
| All opportunity relations packed, before export expiry | 335,872 | 344,064 |
| Recovery export payload bytes | 1,371,093 | 1,646,640 |
| Recovery export allocated bytes | 1,474,560 | 1,769,472 |
| Packed relations plus export payloads, before expiry | 1,706,965 | 1,990,704 |
| All opportunity relations packed, after expiry | 286,720 | 286,720 |
| Net packed reduction after expiry | **64.3%** | **69.0%** |

After expiry the total comprises 180,224 bytes of remaining raw events, 24,576
each for summaries, user state and policy, and 32,768 for the empty manifest
relation. Files are gone. Export byte counts can vary slightly with timestamps
and identifiers; the emitted JSON is authoritative for each invocation.

The parent's retained threshold is **at least 50% net reduction after catch-up
and recovery-artifact expiry**. Both turnovers meet it. Before expiry, storage
**increases** in this catch-up fixture: recovery exports duplicate the deleted
facts until their finite window closes. That temporary space must be available
for rollout. These are not production-size forecasts or a promise that every
small cohort saves 50%: an exploratory 2,400-pair fixture with the same old/young
fraction reduced 425,984 bytes to 229,376 bytes (46.2%), because minimum allocated
relation/index pages dominate at that size. Doubling to 4,800 pairs amortizes
those fixed costs while keeping the test small. Real cardinalities and backlog
must still be checked during rollout.

## Grace, lifetime and remaining rollout obligations

The shipped contract does **not** impose an end-to-end HTTP request lifetime
below G. Search/remote work can finish later; admission checks fresh policy,
clock and prefix after taking state protection. A publisher holds state SHARE
through its served stamp, fact insertion and commit. A writer holds the user
advisory lock through evidence commit. Folding tries those locks and skips while
either is held, even if the request spans G. The new races demonstrate both
cases, including a request resuming only after a fold has committed.

Publication has a 750 ms acquisition wait, then 2 s lock / 10 s statement limits
and a 5 s idle-in-transaction limit. These bound the individual SQL operations
and inter-statement stalls; they are not a universal wall-clock request cap.
Evidence workers have graph lock/statement limits, but no separately enforced
whole-request or whole-transaction lifetime below one hour. On 2026-09-24 the
owner approved qualifying this existing contract: **interlock correctness even
when requests or evidence commits outlive G**, including fresh admission and
safe fallback after folding. This supersedes the earlier request-through-commit
lifetime requirement; no new lifetime cap is required. See
[RETAIN_SRS_OPPORTUNITIES.md](RETAIN_SRS_OPPORTUNITIES.md).

A merely absent new-owner state row is safely initialized. If initialization
fails, the aborted transaction rolls back and the endpoint records the
non-targeted fallback with `missing_retention_state`; missing policy, unavailable
history, prefix violation and timeout have their own exercised diagnostics.

Arithmetic/race fixtures relax the compactor transaction budget to permit test
barriers and assertions. Production timeout/cancellation tests remain unchanged.
No retired latency, throughput, fairness, coverage-observation or capacity gate
is claimed passed. No `release_seal` marker is needed for the new small tests.

Before real deletion, rollout must still verify the deployed Railway revision,
old-writer drain, frozen coverage, export durability and finite rollback limits,
then perform its production canary/catch-up/expiry checks. Git push is not a
production deployment. Recovery mechanics remain in
[RECOVER_SRS_FOLD.md](RECOVER_SRS_FOLD.md); recurring-job operations remain in
[SCHEDULE_SRS_CLEANUP.md](SCHEDULE_SRS_CLEANUP.md).
