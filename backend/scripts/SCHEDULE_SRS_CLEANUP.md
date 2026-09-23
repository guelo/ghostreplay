# Scheduled SRS opportunity cleanup

`app.opportunity_cleanup.scheduled_sweep` schedules the existing bounded fold
transaction. `app.opportunity_cleanup_job` runs it hourly inside the API service,
followed by recovery-artifact expiry. This keeps both folding and expiry on the
service that owns `GHOSTREPLAY_SRS_FOLD_EXPORT_DIR`.

The job defaults **off**. This change does not activate retention. Deployment,
restoration rehearsal, frozen coverage and enabling the job belong to
`g-srs-retain-rollout`; the remaining combined correctness/storage evidence
belongs to `g-srs-retention-gates`. The parent's current decision is M=60 days,
G=1 hour. Its production capacity/latency benchmarks were retired, not passed;
this implementation makes no measured production-lag claim.

## Activity hints

Migration `20260923_01` adds nullable `game_sessions.last_activity_at` and a
`(user_id, last_activity_at)` index. Existing sessions remain unknown rather than
receiving invented activity. Successful game/drill creation, move upload and
revert, opponent fallback and replay, drill progression/termination, grade and
grade retry, and game end pass through `records_session_activity`.

After the handler succeeds, a separate transaction conditionally stamps the
**database** clock, at most once per minute per session. The conditional UPDATE
uses a 1ms lock timeout and a 100ms statement timeout. A dedicated one-slot
PostgreSQL hint pool has zero checkout wait: foreground requests never deadlock
while all hold their ordinary pool slots. This adds at most **one connection per
API process** to the deployment's connection budget; new connection establishment
has a two-second libpq connect timeout. The pool shares the main engine's libpq
keepalives and recycle setting and checks idle connections before reuse.
Hints fail independently of committed
work. The wrapper also tracks local in-flight requests; the evidence worker
tracks its active work through the same counter. These hints are neither an
absence-of-work proof nor evidence-write observations, and do not extend M/G.

## Scheduling contract

- Read actual foldable rows using the fold's owner, M+G, target-pin and effective
  legacy-event-time predicates. Overdue age starts at the latest of session
  M+G, effective event time, and the historically eligible target's last served
  time plus the inclusive 30-day pin window. Future effective event times remain
  ineligible. Already-pruned targeting metadata cannot establish a historical
  pin expiry; without a surviving target, session/event age is a **conservative
  lag upper bound**. It may force cleanup or alert earlier, never later. The fold
  prefix is not a completion watermark.
- Below 12h overdue, require roughly one hour of observed inactivity (61 minutes
  allows for hint coalescing). Missing hints prefer deferral. Recheck before
  preparation, immediately before transfer, and between batches. A new request
  arriving after the final hint check is still protected by the fold interlock.
- At 12h overdue, prefer a 60-second quiet gap with one minute of coalescing
  tolerance and no known local in-flight work. Allow **one** 60-second recheck,
  outside transactions; other users continue while this user waits. At 18h,
  ignore activity immediately. Unknown activity cannot exempt an overdue user.
- Once a forced attempt begins, returning activity cannot stop it. Rotate users
  after every attempt, with at least one second between a user's attempts. Start
  at 25 pairs / 4 distinct blunders; time pressure only reduces batch size.
- Stop the forced visit when fresh lag drops below 12h, or after 100 attempts or
  120 seconds charged to that user: each fold attempt's elapsed time plus its
  required cooldown. Time spent serving other users beyond that cooldown does
  not consume the budget; a multi-user sweep can exceed 120 seconds overall.
  Busy/stale/failed-export attempts count. The preliminary quiet
  preference has its own 60-second bound. Normal visits use the same finite cap.
  Recheck the time budget before export and transfer; an export that finishes
  after the budget does not enter the critical transaction. Already running I/O
  is not forcibly interrupted; the fold's existing critical deadline is unchanged.
- Discovery has no first-N user cutoff. Every discovered user gets a turn before
  revisits; capped users remain eligible for the next hourly sweep. Fresh final
  backlog reads include newly discovered users. Disabled, failed, unknown,
  deferred and capped states are explicit; none are converted into completion.

The individual fold still owns try-advisory locking, state/blunder NOWAIT,
deadlines, atomic transfer and finite recovery. See [RECOVER_SRS_FOLD.md](RECOVER_SRS_FOLD.md).

## Rollout settings and monitoring

After the rollout gates and migration are complete, set
`GHOSTREPLAY_SRS_CLEANUP_JOB_ENABLED=true` on the **single API process** that has
the durable private export volume. Keep the approved export path configured.
Do not also launch a cron copy: the job is an in-process singleton, not a
cross-replica scheduler. A multi-worker/replica deployment must designate one
maintenance process before enabling it. No separate service can expire files it
cannot access.

The thread runs once at startup, then on an hourly cadence with no overlapping
runs or catch-up burst. Stop waits are interruptible; shutdown finishes the
current bounded operation and starts no new attempt, final backlog read or
artifact expiry. The policy's
`cleanup_enabled` remains the independent authority for deletion. The hourly job
continues expiry even with cleanup disabled, and even if the sweep raises.
Turning the whole job off stops both activities; use the documented manual
`fold_srs_opportunities.py expire` path while it is off.

Wire release monitoring to these log events before activating:

- `srs_cleanup_lag_alert` (ERROR): a user's oldest foldable lag upper bound is
  at least 24 hours. It includes the exact pair count and `lag_upper_bound_seconds`; it is not suppressed by
  partial progress, activity, a busy attempt, or the per-user cap.
- `srs_cleanup_backlog_unknown`, `srs_cleanup_user_failed`,
  `srs_cleanup_job_failed`, `srs_cleanup_shutdown_timeout` (ERROR): investigate;
  missing measurement is not a zero backlog.
- `srs_cleanup_sweep` (INFO): disabled state, candidate/batch/deleted counts,
  deferred/capped/alert/error counts. Monitor for at least one successful hourly
  execution; absence of logs is not evidence of a healthy empty backlog.
- `session_activity_hint_failed` (WARNING): optional activity avoidance degraded;
  forced cleanup still runs under the database locks.

Verify the actual deployed commit and settings, an hourly execution and expiry
on the mounted volume, and alert delivery during rollout. The code only emits
the alert events; it does not configure an external alert destination.

## Component verification

```bash
cd backend && source .venv/bin/activate
python -m pytest -W error test_opportunity_cleanup.py test_session_activity.py \
  test_opportunity_compaction.py test_session_evidence_scheduler.py
# With the normal disposable PostgreSQL test URLs configured:
python -m pytest -W error test_opportunity_cleanup_pg.py \
  test_opportunity_compaction_pg.py \
  test_pg_gate_plugin.py::test_manifest_matches_real_pg_gate_collection
```

Virtual-time tests cover exact transitions, continuously active/unknown users,
caps, cooldowns, revisits, multi-user fairness, returning activity, partial
progress and failures. PostgreSQL tests cover the additive migration, real hint
contention and coalescing, activity arriving between export and locking, forced
continuation, and target-pin age accounting. Existing fold tests retain the real
locking/deadline/recovery contract.
