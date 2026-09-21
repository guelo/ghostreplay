# Phase 0 measurement — bounded terminal barrier for opening-baseline capture

This is the **measurement artifact for `g-baseline-barrier` Phase 0**. It answers the
one question the bead's design made a hard gate: when a supported terminal session
ends with `GameSession.opening_score_baseline IS NULL` *only* because its start-state
batch was still converging, **how much longer would the request have had to wait** for
that convergence to land?

The design's ceiling is **100 ms**. The measured floor is **4.3 s**.

**Verdict: no-build.** Gate 4 fails on all three of its sub-conditions, one of them by
a factor of 43. Per the design ("if the optimistic p10 exceeds 100 ms, close no-build
immediately; do not write Phase-1 waiter code merely to confirm it"), no barrier,
waiter, configuration or rollout code is justified. The correct NULL degradation
stays.

Phase 0 itself ships unchanged — the telemetry in `d7a3236` is what produced these
numbers and is what any future re-measurement would use.

---

## 1. Observation boundary

| | |
|---|---|
| Instrumentation commit | `d7a3236` "Instrument terminal baseline convergence", authored 2026-08-20 |
| Its own Railway deployment | `a9ca2028`, created 2026-08-20T21:00:29Z |
| Production revision at analysis time | `7df8f1a` (deployment `0b920193`, created 2026-09-20T11:15:40Z) — `d7a3236` is an ancestor |
| Alembic | repository head `20260919_05`, a descendant of the design's named `20260809_01` |
| Analysis run | 2026-09-21T00:16Z |
| **Operational-log window actually retrievable** | **2026-09-14T02:11:54Z → 2026-09-20T23:29:53Z (6.9 days)** |
| Convergence probes inside it | 14, landing on 2 calendar days (4 on 09-14, 10 on 09-19) |

The telemetry has been in production continuously since 2026-08-20, i.e. ~31 days.
The *analysable* window is far shorter, and not for a query reason — see §2.

### 1.1 Deploy / restart boundaries inside the window

Both schedulers and the probe registry are process-local, so every deploy is a
censored safe miss. Deployments created on or after the instrumentation deploy:

```
2026-08-20T21:00:29Z  a9ca2028  d7a3236   <- instrumentation
2026-08-21T04:38:26Z  3f246648  1cd74678
2026-08-21T05:45:52Z  c46c6261  ceefc6b0
2026-08-21T05:53:21Z  9dab6914  af8dcc6c
2026-08-25T11:06:03Z  176b450b  50b9f43c
2026-09-12T06:37:39Z  a0eb61ac  9737fd96
2026-09-19T05:47:43Z  ee4765a6  4cf4174b   <- 10 deploys in ~28h follow
2026-09-19T06:55:33Z  7a68cee3  1b875f09
2026-09-19T07:03:15Z  39b23ae9  ae4dd5bd
2026-09-19T08:01:20Z  76bef8e8  9675c12c
2026-09-19T08:09:49Z  5e03d2ef  408e9faa
2026-09-19T08:22:18Z  9436a31a  cfd74a44
2026-09-19T08:44:43Z  3f4cf804  66996786
2026-09-19T09:37:05Z  f01448d7  3d6217d8
2026-09-19T09:52:45Z  6c241752  8ca810a9
2026-09-19T10:09:16Z  8cf1be4e  31b89dc3
2026-09-20T06:13:39Z  afa64780  1a49a198
2026-09-20T06:51:44Z  a6c26ea6  5f48d42e
2026-09-20T07:35:10Z  052665bb  8ca5ad78
2026-09-20T10:18:47Z  8133a901  af26a0ca
2026-09-20T11:15:40Z  0b920193  7df8f1a   <- serving at analysis time
```

Deployment list retrieved with `--limit 1000`; 261 records returned, so it is **not**
truncated (`docs/railway-log-query-completeness.md` §1).

---

## 2. What limits the window is Railway log retention, not the query

The convergence record is an **operational log line**, by design: §0.3 of the bead
deliberately keeps user and colour off it and publishes only an opaque
`convergence_probe_id`, which is joined to the PostHog terminal event. That choice is
still right for privacy, but it binds the measurement to Railway's log retention.

Empirically, retention is **7 days**. Every deployment created before 2026-09-12
returns **zero** records for the same filtered query that returns hundreds from later
deployments — including `50b9f43c`, which served continuously from 2026-08-25 to
2026-09-12 and certainly emitted them:

```
8c68fbb9  created=2026-08-20T06:24:20Z rows=   0     # pre-instrumentation carry-in
d7a3236   created=2026-08-20T21:00:29Z rows=   0
1cd74678  created=2026-08-21T04:38:26Z rows=   0
ceefc6b0  created=2026-08-21T05:45:52Z rows=   0
af8dcc6c  created=2026-08-21T05:53:21Z rows=   0
50b9f43c  created=2026-08-25T11:06:03Z rows=   0     # served 18 days, all expired
9737fd96  created=2026-09-12T06:37:39Z rows= 468
4cf4174b  created=2026-09-19T05:47:43Z rows=  53
1b875f09  created=2026-09-19T06:55:33Z rows=   0
ae4dd5bd  created=2026-09-19T07:03:15Z rows=   6
9675c12c  created=2026-09-19T08:01:20Z rows=   2
408e9faa  created=2026-09-19T08:09:49Z rows=   0
cfd74a44  created=2026-09-19T08:22:18Z rows=   4
66996786  created=2026-09-19T08:44:43Z rows=  79
3d6217d8  created=2026-09-19T09:37:05Z rows=  61
8ca810a9  created=2026-09-19T09:52:45Z rows= 181
31b89dc3  created=2026-09-19T10:09:16Z rows= 410
1a49a198  created=2026-09-20T06:13:39Z rows=   0
5f48d42e  created=2026-09-20T06:51:44Z rows=   0
8ca5ad78  created=2026-09-20T07:35:10Z rows=   0
af26a0ca  created=2026-09-20T10:18:47Z rows=   0
7df8f1a   created=2026-09-20T11:15:40Z rows=  10
```

**This is a standing design/infrastructure mismatch, not a one-off.** The bead requires
"at least seven complete days"; the log-side half of the evidence can never exceed
seven days at all. Any future re-measurement of this kind needs the convergence record
routed somewhere with longer retention — or accepted as a rolling 7-day sample. It did
not change this verdict, because the effect size is ~43×, but a close call could not
have been decided from this source.

### 2.1 Retrieval completeness

Method per `docs/railway-log-query-completeness.md`:

* **Deployment coverage by exhaustion** over the 22 deployments above — every
  deployment created at or after the instrumentation deploy, plus one pre-instrumentation
  carry-in (`8c68fbb9`), queried whatever its status.
* **`--lines` always explicit at 500.** The largest single query returned **468**;
  nothing reached the limit, so no result is ambiguous and none needed splitting. The
  recursive-halving path (1 s widened bounds, abort on unsplittable saturation) was
  armed but never fired, so the shard tree here is 22 flat leaves, one per deployment.
* **Dedupe on `(timestamp, message)`** across deployments: 1274 rows in, 1274 unique.
* **Zero query errors**; no deployment was skipped for budget, so there is no coverage
  gap to declare.

The two irreducibly fail-open modes named in that document (shard-boundary inclusivity,
Railway's own shedding) are carried forward as assumptions here too. Neither is load-
bearing: no shard was split, so there are no internal boundaries to lose a record at.

---

## 3. Gate 4 — remaining convergence lag

Population: every included terminal transition observed as `missing_with_watermark`
whose whole-score key was `pending` or `inflight` at terminal observation. That is
exactly the set for which `probe_terminal_recompute` registers an observer
(`backend/app/opening_score_scheduler.py`), so the record set *is* the gate
population, not a sample of it.

**n = 14 resolved probes, 0 censored, all `disposition=rebuilt`, none
`forced_dispatch`.** Dataset: `docs/opening_baseline_barrier_phase0.jsonl`.

Those 14 fall on only two calendar days — 4 on 2026-09-14 and 10 on 2026-09-19 — which
is what play volume in this window looks like, not a retrieval artefact. It clears the
gate's ≥10 precondition and nothing more; it would not support a close call.

| Statistic | Measured | Gate | |
|---|---|---|---|
| resolved uncensored probes | 14 | ≥ 10 | ✅ sample sufficient |
| `completion_lag_ms` p10 (nearest-rank) | **612 ms** | ≤ 100 ms | ❌ |
| `completion_lag_ms` p10 (interpolated) | **354 ms** | ≤ 100 ms | ❌ |
| share completing within 100 ms | **1 / 14 = 7.1 %** | ≥ 10 % | ❌ |
| count completing within 100 ms | **1** | ≥ 2 | ❌ |
| pending optimistic-lower-bound p10 | **4 316 ms** | ≤ 100 ms | ❌ **43×** |

Split by state at terminal observation:

| state | n | min | p10 | p50 | max | ≤100 ms |
|---|---|---|---|---|---|---|
| `inflight` | 9 | 96 ms | 96 ms | 3 490 ms | 10 565 ms | 1 |
| `pending` | 5 | 6 621 ms | 6 621 ms | 11 124 ms | 15 065 ms | 0 |

The `pending` optimistic lower bound — deadline remainder + worker run time, which
deliberately *omits* head-of-line and push-fill cost and so is a floor, not an estimate —
ranges **4 316 ms to 13 474 ms**. Its **minimum** is 43× the budget. There is no
sub-100 ms mass in the pending population and no plausible measurement refinement that
would create any: the whole-score worker alone runs 3.6–22.8 s.

The single sub-100 ms observation (96.0 ms) is an `inflight` probe that happened to land
96 ms before a run that had already been executing for 7.6 s completed. A 100 ms barrier
recovers that case and essentially nothing else.

### 3.1 Bias direction

Probes resolve only within one process lifetime, and an observer lost to a deploy or
restart emits nothing at all — invisible censoring. Ten deploys landed inside a 28-hour
stretch of this window. A probe that would have taken *longer* is more likely to be lost
that way, so the surviving sample is biased **toward short lags**. The true distribution
is at least as bad as measured, which only strengthens the conclusion.

---

## 4. Supporting distributions (operational log, same window)

630 `opening_baseline_scheduler_attempt` records over **240 unique sessions**, and 630
matching `opening_baseline_job` completions.

| Attempt no. | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|
| jobs | 240 | 125 | 116 | 93 | 45 | 9 | 2 |

`retry_budget_exhausted=True` never occurred (0 / 630). The bounded retry path is
working as intended and is **not** a contributing cause.

Job outcome by source:

| source | n |
|---|---|
| `skipped_stale` | 387 |
| `cached_fresh` | 116 |
| `already_set` | 110 |
| `not_active` | 9 |
| `watermark_mismatch` | 5 (all `mismatch_reason=seq`) |
| `raced_evidence_or_already_set` | 3 |

Terminal-session classification — the split the design added so supported residuals can
be separated from abandon/off-route/unrelated drift:

| classification | n | notes |
|---|---|---|
| `active` | 598 | session still running; not a residual |
| `supported_drill_accuracy_fail` | 29 | the supported terminal residual |
| `unrelated_evidence_drift` | 3 | excluded cause, as designed |

Every supported terminal residual in the window came from **drill accuracy-fail**. None
came from `game_end`, `converted_drill_end`, or `drill_natural_end`. If the residual
were ever worth attacking, that is where it lives — and it is one route, not three.

---

## 5. Gates 1–3 (prevalence) — PostHog

Gate 4's failure is sufficient to close no-build on its own, and the design says so
explicitly. The prevalence numbers are still worth recording as aggregate evidence. They
live only in PostHog (the terminal observation rides `game_ended` / `drill_failed` /
`drill_natural_end` via fire-and-forget `capture`), so run these in the PostHog UI and
paste the tables into the bead.

Denominator rule: **filter on field presence, never on event name.** Off-route drill
failure also emits `drill_failed` and intentionally carries no observation.

### 5.1 Prevalence by terminal kind (gates 1 and 2)

```sql
SELECT
  properties.terminal_kind          AS terminal_kind,
  properties.opening_baseline_state AS baseline_state,
  count()                           AS n
FROM events
WHERE event IN ('game_ended', 'drill_failed', 'drill_natural_end')
  AND timestamp >= toDateTime('2026-08-21 00:00:00')
  AND timestamp <  toDateTime('2026-09-21 00:00:00')
  AND properties.opening_baseline_state IS NOT NULL
  AND properties.terminal_kind IS NOT NULL
GROUP BY terminal_kind, baseline_state
ORDER BY terminal_kind, baseline_state
```

Gate 1: `missing_with_watermark` ≥ 10. Gate 2: its share of the total ≥ 2 % **and** the
95 % Wilson lower bound > 1 %.

### 5.2 Recompute state among the misses (gate 3)

```sql
SELECT
  properties.opening_recompute_state         AS recompute_state,
  properties.opening_baseline_scheduler_state AS baseline_scheduler_state,
  count()                                    AS n
FROM events
WHERE event IN ('game_ended', 'drill_failed', 'drill_natural_end')
  AND timestamp >= toDateTime('2026-08-21 00:00:00')
  AND timestamp <  toDateTime('2026-09-21 00:00:00')
  AND properties.opening_baseline_state = 'missing_with_watermark'
GROUP BY recompute_state, baseline_scheduler_state
ORDER BY n DESC
```

Gate 3: ≥ 50 % of `missing_with_watermark` observed with the whole-score recompute
`pending` or `inflight`.

### 5.3 Attempt / age shape of the misses

```sql
SELECT
  properties.opening_baseline_attempts_bucket AS attempts_bucket,
  properties.session_age_bucket               AS age_bucket,
  count()                                     AS n
FROM events
WHERE event IN ('game_ended', 'drill_failed', 'drill_natural_end')
  AND timestamp >= toDateTime('2026-08-21 00:00:00')
  AND timestamp <  toDateTime('2026-09-21 00:00:00')
  AND properties.opening_baseline_state = 'missing_with_watermark'
GROUP BY attempts_bucket, age_bucket
ORDER BY n DESC
```

### 5.4 Route latency baseline (context only, since no barrier ships)

```sql
SELECT
  properties.route AS route,
  count()          AS requests,
  round(quantile(0.50)(toFloat(properties.duration_ms)), 1) AS p50_ms,
  round(quantile(0.95)(toFloat(properties.duration_ms)), 1) AS p95_ms,
  round(quantile(0.99)(toFloat(properties.duration_ms)), 1) AS p99_ms
FROM events
WHERE event = 'api_request'
  AND timestamp >= toDateTime('2026-08-21 00:00:00')
  AND timestamp <  toDateTime('2026-09-21 00:00:00')
  AND properties.route IN (
    '/api/game/end',
    '/api/drills/{session_id}/fail',
    '/api/drills/{session_id}/natural-end'
  )
GROUP BY route
ORDER BY requests DESC
```

### 5.5 Joining a probe back to its terminal event

Only needed if the convergence sample is ever re-examined; the opaque id is the join key
and carries no identity.

```sql
SELECT
  properties.convergence_probe_id    AS probe_id,
  timestamp,
  properties.terminal_kind           AS terminal_kind,
  properties.opening_recompute_state AS recompute_state,
  properties.session_age_bucket      AS age_bucket
FROM events
WHERE event IN ('game_ended', 'drill_failed', 'drill_natural_end')
  AND properties.convergence_probe_id IS NOT NULL
  AND timestamp >= toDateTime('2026-09-14 00:00:00')
ORDER BY timestamp
```

---

## 6. Conclusion

The barrier's premise was that the remaining convergence time, once the g-f3m4
push-fill path exists, might be short enough to wait out inside a latency budget the
`/end` path can afford. It is not. The remaining time is dominated by the whole-score
recompute itself — 3.6 to 22.8 s of worker run time in this window — and the 1.5 s
debounce quiet window sits on top of it for the `pending` half. A 100 ms wait cannot
reach that mass; a wait that could would be two orders of magnitude outside what
`g-mxeo` moved off the request path in the first place.

So the design's first terminal outcome applies: **leave the correct NULL degradation in
place.** The session that ends before its start-state batch converges shows no diff,
which is a true statement about what could be proven.

The Phase-0 instrumentation that produced these numbers now has no consumer: the
convergence-probe registry costs a scheduler-lock snapshot on every included terminal
request and may emit one expired-observer record synchronously. Whether to retire it or
keep it as a standing residual monitor is tracked separately in `g-retire-barrier-probes`.

If the residual is ever revisited, the lead is §4: it is a single route
(drill accuracy-fail), and the lever that would matter is whole-score recompute cost or
debounce placement — not a terminal wait.
