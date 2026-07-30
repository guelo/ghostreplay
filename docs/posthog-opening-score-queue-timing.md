# PostHog — opening-score queue time vs. compute time (g-score-queue-timing)

This runbook is the **reproducible measurement artifact for g-score-queue-timing**. It
answers one question with production numbers: when an opening-score rebuild happens,
how much of the wall-clock is **scheduler queue time** and how much is **worker compute
time**?

The answer decides which follow-up bead is justified. It is deliberately not obvious
from either number alone: if queue time dominates, making scoring faster is wasted
work; if the queue time is mostly *intentional debounce*, shortening the quiet window
trades convergence latency for more rebuilds; and if it is mostly *post-deadline
dispatch lag*, no debounce change helps at all — that is single-worker head-of-line
blocking.

No scheduler or scorer tuning ships in the instrumentation release. Collect first.

## Where the numbers come from

`OpeningScoreScheduler` (`backend/app/opening_score_scheduler.py`) coalesces enqueues
per `(user_id, player_color)` and runs them on one serialized daemon thread. Each
`_run_one` samples worker start as its **first instruction** and publishes the fixed
queue decomposition in a ContextVar for the duration of the recompute;
`opening_cache._emit_opening_scores_recomputed` folds that snapshot into the **existing**
`opening_scores_recomputed` event. There is no second event and no per-enqueue event.

## Event reference — `opening_scores_recomputed`

Fires **only for an actual rebuild** — never for a `cached` fast return or a
`no_evidence` bail-out.

### Pre-existing properties (unchanged)

| Property | Meaning |
|---|---|
| `duration_ms` | the **narrow** actual-rebuild span measured inside `recompute_opening_scores_if_needed` (freshness snapshot → overlay → durable write) |
| `reason` | dominant trigger: `cache_miss` \| `registry_drift` \| `stale_branch_keys` \| `evidence_change` \| `decay_staleness` |
| `cache_miss`, `registry_drift`, `stale_branch_keys`, `evidence_change`, `decay_staleness` | booleans behind `reason` |
| `batch_size` | named-root row count in the new batch (`null` if the count query failed) |
| `player_color` | `white` \| `black` |

`distinct_id` is the backend `user_id` as a string.

### Timing properties (g-score-queue-timing, timing version 1)

| Property | Meaning |
|---|---|
| `scheduler_timed` | `true` only for a run dispatched by the scheduler. **Every query below must filter on this.** |
| `scheduler_timing_version` | `1`. Bumped if a field's meaning changes, so old shapes can be excluded. |
| `scheduler_run_id` | unique per executed run; correlates the event with the operational completion log record |
| `queue_first_ms` | `worker_started - first_seen` — convergence latency experienced by the **oldest** enqueue folded into this run |
| `queue_last_ms` | `worker_started - last_seen` — latency from the **newest** evidence folded in |
| `coalesce_span_ms` | `last_seen - first_seen` — time spent accumulating the burst. Identity: `queue_first_ms = coalesce_span_ms + queue_last_ms` |
| `deadline_delay_ms` | `final_deadline - first_seen` — **intentional policy delay** after debounce / max-wait / immediate updates |
| `dispatch_lag_ms` | `max(0, worker_started - final_deadline)` — delay **after** the entry was eligible: principally single-worker head-of-line blocking or thread scheduling |
| `worker_compute_ms` | worker start → durable batch (rebuild returned, batch committed, post-commit pruning done), sampled before this event's own `batch_size` query. **This is the worker-cost number for the comparison**, and it is wider than `duration_ms` because it includes worker/session acquisition and the decision preflight |
| `trigger_first` / `trigger_last` | first / last producer folded into the burst — **diagnostics only** |
| `trigger_sources` | deterministically sorted array of **every** producer folded in — **the authoritative cohort gate** |
| `enqueue_count` | how many enqueues this one run represents |
| `immediate` | an `refresh_now` enqueue folded in (sticky), so the entry was due immediately |
| `forced_dispatch` | dispatched **ahead of its deadline** by a shutdown drain or explicit `flush_pending`. **Exclude from steady-state distributions.** |
| `quiet_window_ms` / `max_wait_ms` | the scheduler configuration in force for that run (1500 / 10000 at time of writing) |

`trigger_sources` values (closed `OpeningScoreTrigger` vocabulary):
`cached_score_reader_warm`, `cached_score_reader_cold`, `session_lineage_warm`,
`session_lineage_cold`, `tree_reader_warm`, `tree_reader_bootstrap`,
`tree_status_bootstrap`, `score_delta`, `session_evidence`, `srs_review`.

An unknown source is rejected at the enqueue boundary before any queue mutation, so no
other value can appear here.

### Exclusions — apply to every steady-state query

- `properties.scheduler_timed = true` — a direct/offline/test recompute emits the event
  with `scheduler_timed = false` and **no** queue properties. Without this filter those
  runs silently join the distribution with missing fields.
- `properties.forced_dispatch = false` — shutdown drains and explicit flushes may run
  before their configured deadline, so their queue time is not a debounce observation.
  A forced run that *does* rebuild **is** in this event, carrying
  `forced_dispatch = true`; this filter is what removes it, so omitting the filter
  silently mixes pre-deadline dispatches into the debounce distribution.
- `properties.scheduler_timing_version = 1` — guards against a later field-semantics
  bump. Apply it to **every** query, not just the primary one: a future version-2 event
  would otherwise join a version-1 aggregate with incompatible semantics and no error.

`failed`, `cached`, and `no_evidence` runs are **not** in this event at all (it fires
only on a real rebuild), so neither run frequency nor failure rate can be measured from
it. Read those from the operational log instead:

```
opening_score_recompute_run run_id=… run_outcome=rebuilt|cached|no_evidence|failed …
```

That record is emitted once per executed run with the same `run_id`, carries
`worker_run_ms` (the all-outcome operational duration), and deliberately contains **no**
user ID, opening key, session ID, position, or score.

## Query 1 — primary all-rebuild distribution

```sql
SELECT
  count() AS rebuilds,
  round(quantile(0.50)(toFloat(properties.queue_first_ms)), 1) AS queue_first_p50,
  round(quantile(0.95)(toFloat(properties.queue_first_ms)), 1) AS queue_first_p95,
  round(max(toFloat(properties.queue_first_ms)), 1) AS queue_first_max,
  round(quantile(0.50)(toFloat(properties.queue_last_ms)), 1) AS queue_last_p50,
  round(quantile(0.95)(toFloat(properties.queue_last_ms)), 1) AS queue_last_p95,
  round(quantile(0.50)(toFloat(properties.deadline_delay_ms)), 1) AS deadline_p50,
  round(quantile(0.95)(toFloat(properties.deadline_delay_ms)), 1) AS deadline_p95,
  round(quantile(0.50)(toFloat(properties.dispatch_lag_ms)), 1) AS dispatch_p50,
  round(quantile(0.95)(toFloat(properties.dispatch_lag_ms)), 1) AS dispatch_p95,
  round(max(toFloat(properties.dispatch_lag_ms)), 1) AS dispatch_max,
  round(quantile(0.50)(toFloat(properties.coalesce_span_ms)), 1) AS coalesce_p50,
  round(quantile(0.95)(toFloat(properties.coalesce_span_ms)), 1) AS coalesce_p95,
  round(quantile(0.50)(toFloat(properties.worker_compute_ms)), 1) AS worker_p50,
  round(quantile(0.95)(toFloat(properties.worker_compute_ms)), 1) AS worker_p95,
  round(max(toFloat(properties.worker_compute_ms)), 1) AS worker_max,
  round(quantile(0.50)(toFloat(properties.duration_ms)), 1) AS inner_p50,
  round(quantile(0.95)(toFloat(properties.duration_ms)), 1) AS inner_p95
FROM events
WHERE event = 'opening_scores_recomputed'
  AND properties.scheduler_timed = true
  AND properties.scheduler_timing_version = 1
  AND properties.forced_dispatch = false
  AND isFinite(toFloat(properties.queue_first_ms))
  AND isFinite(toFloat(properties.worker_compute_ms))
  AND timestamp > now() - INTERVAL 7 DAY
```

Always report `rebuilds` (N) **beside** every percentile.

## Query 2 — the g-a5v3 target cohort (cold session-lineage reads)

The bead exists because g-a5v3 moved cold scoring off the live lineage request path.
That cohort is defined by **set membership**, not by burst endpoints:

```sql
SELECT
  count() AS rebuilds,
  round(quantile(0.50)(toFloat(properties.queue_first_ms)), 1) AS queue_first_p50,
  round(quantile(0.95)(toFloat(properties.queue_first_ms)), 1) AS queue_first_p95,
  round(max(toFloat(properties.queue_first_ms)), 1) AS queue_first_max,
  round(quantile(0.50)(toFloat(properties.worker_compute_ms)), 1) AS worker_p50,
  round(quantile(0.95)(toFloat(properties.worker_compute_ms)), 1) AS worker_p95
FROM events
WHERE event = 'opening_scores_recomputed'
  AND properties.scheduler_timed = true
  AND properties.scheduler_timing_version = 1
  AND properties.forced_dispatch = false
  AND has(properties.trigger_sources, 'session_lineage_cold')
  AND timestamp > now() - INTERVAL 7 DAY
```

**Do not** filter this cohort with `trigger_first = 'session_lineage_cold'` or
`trigger_last = 'session_lineage_cold'`. One coalesced run can carry the cold lineage
enqueue between two other producers, and endpoint equality drops exactly those runs —
biasing the target sample toward bursts that happened to begin or end with a lineage
read.

If the cohort is small, report **N, median, max** and say the p95 is not meaningful
rather than quoting an unstable percentile.

## Query 3 — mixed-source diagnostic (proves Query 2's filter is the right one)

This should return a non-zero count in normal traffic. Each row is a run the
endpoint-equality filter would have silently dropped:

```sql
SELECT
  properties.trigger_first AS trigger_first,
  properties.trigger_last AS trigger_last,
  properties.trigger_sources AS trigger_sources,
  count() AS rebuilds
FROM events
WHERE event = 'opening_scores_recomputed'
  AND properties.scheduler_timed = true
  AND properties.scheduler_timing_version = 1
  AND properties.forced_dispatch = false
  AND has(properties.trigger_sources, 'session_lineage_cold')
  AND properties.trigger_first != 'session_lineage_cold'
  AND properties.trigger_last != 'session_lineage_cold'
GROUP BY trigger_first, trigger_last, trigger_sources
ORDER BY rebuilds DESC
```

The behavioral fixture for this query is
`test_opening_recompute_analytics.py::test_mixed_source_run_is_selected_by_set_membership_not_by_endpoints`.

**Validate `has(...)` in the deployed project during rollout.** If that PostHog version
needs an explicit array cast, update this checked-in runbook with the verified
equivalent before collecting results. The semantics must remain *membership in
`trigger_sources`*.

## Query 4 — event-level shares (never compare unrelated aggregate percentiles)

"Is queue time bigger than compute time?" is a per-event question. Comparing a queue p95
against a worker p95 computed over the same set is not the same claim.

```sql
SELECT
  count() AS rebuilds,
  round(100.0 * countIf(
    toFloat(properties.queue_first_ms) > toFloat(properties.worker_compute_ms)
  ) / count(), 1) AS pct_queue_dominated,
  round(quantile(0.50)(
    toFloat(properties.queue_first_ms)
    / (toFloat(properties.queue_first_ms) + toFloat(properties.worker_compute_ms))
  ), 3) AS queue_share_p50,
  round(quantile(0.95)(
    toFloat(properties.queue_first_ms)
    / (toFloat(properties.queue_first_ms) + toFloat(properties.worker_compute_ms))
  ), 3) AS queue_share_p95,
  round(quantile(0.50)(
    toFloat(properties.deadline_delay_ms)
    / nullIf(toFloat(properties.queue_first_ms), 0)
  ), 3) AS policy_share_of_queue_p50
FROM events
WHERE event = 'opening_scores_recomputed'
  AND properties.scheduler_timed = true
  AND properties.scheduler_timing_version = 1
  AND properties.forced_dispatch = false
  AND toFloat(properties.queue_first_ms) + toFloat(properties.worker_compute_ms) > 0
  AND timestamp > now() - INTERVAL 7 DAY
```

`policy_share_of_queue_p50` splits queue time into intentional policy delay versus
post-deadline dispatch lag — the split that selects the follow-up bead.

## Query 5 — segmentation

```sql
SELECT
  properties.reason AS reason,
  properties.player_color AS player_color,
  properties.immediate AS immediate,
  count() AS rebuilds,
  round(quantile(0.50)(toFloat(properties.queue_first_ms)), 1) AS queue_first_p50,
  round(quantile(0.50)(toFloat(properties.worker_compute_ms)), 1) AS worker_p50,
  round(quantile(0.50)(toFloat(properties.enqueue_count)), 1) AS enqueue_count_p50
FROM events
WHERE event = 'opening_scores_recomputed'
  AND properties.scheduler_timed = true
  AND properties.scheduler_timing_version = 1
  AND properties.forced_dispatch = false
  AND timestamp > now() - INTERVAL 7 DAY
GROUP BY reason, player_color, immediate
ORDER BY rebuilds DESC
```

Per-source membership segments, run one `has(...)` value at a time:

```sql
SELECT
  count() AS rebuilds,
  round(quantile(0.50)(toFloat(properties.queue_first_ms)), 1) AS queue_first_p50,
  round(quantile(0.50)(toFloat(properties.worker_compute_ms)), 1) AS worker_p50
FROM events
WHERE event = 'opening_scores_recomputed'
  AND properties.scheduler_timed = true
  AND properties.scheduler_timing_version = 1
  AND properties.forced_dispatch = false
  AND has(properties.trigger_sources, {source})   -- one OpeningScoreTrigger value
  AND timestamp > now() - INTERVAL 7 DAY
```

**Membership segments overlap by construction** — one coalesced run can belong to
several. They do not partition the total and their counts must not be summed.

## Rollout protocol

1. Deploy the instrumentation with **no** scheduler/scorer tuning in the same release.
2. In PostHog Activity, confirm a real scheduled rebuild has
   `scheduler_timing_version = 1`, finite non-negative timing fields, only approved
   `trigger_sources` values, and no raw score/opening/session payload.
3. Exercise or observe one mixed-source run and verify Query 3 includes it.
4. Collect **seven complete days**. If that yields fewer than **30** successful,
   non-forced, scheduler-timed rebuilds, extend to 14 days and explicitly report the
   small sample rather than optimizing from an unstable percentile.
5. Run Queries 1, 2, 4, 5. Report failures and forced drains separately, from the
   `opening_score_recompute_run` log.
6. Append the aggregate table, observation dates, sample counts, deployed commit,
   filters used, and the conclusion to bead `g-score-queue-timing`
   (`bd update --append-notes`, then verify with `bd show`). No per-user rows, no
   individual user IDs.

## Decision rule

| Observation | Follow-up |
|---|---|
| Queue dominated, **policy delay** dominates the queue | scheduler-policy bead (e.g. `g-tune-score-queue`), separately reviewed |
| Queue dominated, **dispatch lag** dominates the queue | head-of-line / capacity bead (e.g. `g-score-worker-hol`) — shortening the debounce would not help |
| **Worker** dominated on actual rebuilds | scoring/persistence profiling bead (e.g. `g-speed-score-run`) |
| Large `coalesce_span_ms` but low `queue_last_ms` | coalescing is doing its job; record that and do **not** "optimize" the oldest-enqueue number by rebuilding more often |

No tuning is implemented under `g-score-queue-timing`. Close it only after the
production report is attached and any evidence-backed follow-up is filed with an
explicit `g-<slug>` ID.
