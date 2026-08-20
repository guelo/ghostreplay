# Opening-boundary live-delta monitoring runbook

Step 4 of the `g-je81` rollout: what to watch after
`OPENING_BOUNDARY_PUBLICATION_ENABLED` is turned on, where each number comes
from, and what to do when one moves the wrong way.

This is the **diagnostic** window, not a go/no-go gate. The product decision to
ship live opening-score deltas is settled; these metrics exist to find a dominant
fallback bucket, catch a latency or load regression, and prove the feature is
actually reaching users. Architecture lives in
[`openingscore_final.md`](./openingscore_final.md); this file is operations only.

---

## 0. Verified production configuration

Confirmed 2026-08-19, after rollout steps 1–3:

| Fact | Value | How it was checked |
| --- | --- | --- |
| Railway project / service | `ghostreplay` → `ghostreplay` (production, sfo) | `railway status` |
| Kill switch | `OPENING_BOUNDARY_PUBLICATION_ENABLED = true` | `railway variables --kv` |
| Boundary route live | `/api/session/{session_id}/opening-boundary` present | production `/openapi.json` |
| Backend PostHog | `POSTHOG_PROJECT_TOKEN` set (`phc_…`, 48 chars), `POSTHOG_DISABLED=false`, host `https://us.i.posthog.com` | `railway variables --kv` |
| Frontend PostHog | token `phc_zWZJ…` present in the deployed bundle | `curl https://ghostreplay.vercel.app/assets/index-*.js` |
| Frontend boundary code deployed | `opening_delta_poll_completed` and `game_opening_boundary` present in the lazy `GamePage-*.js` chunk | same |

Both emitters are therefore live. The switch is read **once at backend startup**
(`backend/app/opening_score_delta.py:122`), so any change to it requires a
service restart to take effect.

---

## 1. What emits what

### `opening_boundary_shadow_terminal` — backend, once per session, at terminalization

Emitted from `backend/app/opening_boundary.py:206`, called by
`api/game.py:1034` (`terminal_trigger="game_end"`) and `api/drills.py:687,972`.
Fires only for `opening_phase_protocol_version = 1` sessions, only once per
session (claimed durably on `game_sessions.opening_boundary_shadow_terminal_at`),
and **not** for `ABANDON` results.

| Property | Meaning |
| --- | --- |
| `protocol_version` | always `1` when emitted |
| `session_mode` | game vs drill |
| `terminal_trigger` | `game_end`, `drill_natural_end`, or `accuracy_fail` |
| `raw_candidate_seen` | a probe hint/candidate/verdict/exhaustion existed |
| `proof_verdict` | `not_attempted` (synthesized when unset), `passed`, `capped`, `exhausted`, `wrong_row_count`, `coordinate_mismatch`, `nonstandard_start`, `illegal_or_discontinuous_line` |
| `baseline_ready_at_transition` | before-session baseline was durable |
| `would_have_published` | every publication precondition was met |
| `did_publish` | `opening_middle_ply` was actually written — **the headline metric** |
| `reason` | closed bucket: `no_candidate`, `probe_ack_incomplete`, `continuity_or_cap_refusal`, `exhausted`, `baseline_missing`, `would_publish` |
| `line_revision_zero` | `false` ⇒ post-takeback cohort |
| `ready_to_terminal_lead_ms` | how much earlier than terminal the marker was ready |

Captured with `distinct_id=None`, which the client maps to the shared `"anon"`
person (`backend/app/posthog_client.py:64`). Aggregate-only by design: no session
IDs, chess content, openings, or scores. Unique-user counts on this event are
meaningless — use event counts.

### `opening_delta_poll_completed` — frontend, once per delta poll

Emitted from `src/utils/openingDeltaPoll.ts:348`. Covers both the live boundary
poll and the existing terminal poll; `trigger` separates them:
`game_opening_boundary` / `drill_opening_boundary` are the live path,
everything else is terminal.

Key properties: `outcome` (`fresh`, `attempts_exhausted`, `capacity_evicted`,
`abandoned`), `elapsed_ms`, `attempt_count`, `request_error_count`,
`fresh_on_first_attempt`, `has_renderable_change`,
`session_replaced_before_completion`, `visibility_at_start` / `_at_end` /
`visibility_changed`, `mode`.

This is the **leading** signal — it fires mid-session. The shadow event is
lagging: it only lands when a session terminates.

---

## 2. PostHog dashboard

Create a dashboard named **Opening boundary rollout**. Each tile below is given
as HogQL (PostHog → SQL editor → save as insight), which is exact and
copy-pasteable; the equivalent point-and-click recipe is noted where it is
simpler.

> **If a `= true` comparison returns zero rows**, the property is arriving as a
> JSON string in your project. Swap `properties.x = true` for
> `toBool(properties.x)` in that tile and re-run.

### Chart-style rules for this dashboard

PostHog SQL insights render as Table, Line, Bar, Area, Pie, or Big Number. Four
rules decide every tile below, and they are worth knowing before you start
clicking:

- **A line or stacked bar needs a date column in the `SELECT`.** The snapshot
  queries below deliberately have none — they render as tables and bars. Each
  tile notes the `toDate(timestamp)` variant where a trend is worth having.
- **Never put two units on one plot.** A count and a percentage, or milliseconds
  and a percentage, means a second y-axis, and the alignment between two scales is
  arbitrary — it invents a relationship the data does not contain. Split into two
  tiles instead.
- **Never stack nested subsets.** `published` ⊆ `eligible` ⊆ `terminal_sessions`;
  stacking those segments counts the same session up to three times.
- **Compute shares in SQL, not with a normalize toggle.** It is version-proof and
  the query then documents exactly what the percentage is a percentage *of*.

Skip pie and donut everywhere here. Both part-to-whole tiles have segments that
need comparing against each other, which is the one job a pie is worst at.

Keep the series order stable across tiles so a reader who has learned which line
is which is not repainted when a filter drops a series.

### Tile 1 — Is it on and publishing? (the headline)

```sql
SELECT
  toDate(timestamp)                              AS day,
  count()                                        AS terminal_sessions,
  countIf(properties.would_have_published = true) AS eligible,
  countIf(properties.did_publish = true)          AS published,
  round(100.0 * countIf(properties.did_publish = true) / count(), 1) AS published_pct
FROM events
WHERE event = 'opening_boundary_shadow_terminal'
  AND timestamp > now() - INTERVAL 14 DAY
GROUP BY day
ORDER BY day
```

**Chart: line, three series, unstacked.** The counts are nested subsets, so
overlapping lines let you read both gaps directly while stacking would triple-count.
Leave `published_pct` off this plot — a percentage cannot share an axis with a
count. Give it its own **Big Number** tile at the top of the dashboard, since the
share of sessions that published is the one number the dashboard leads with:

```sql
SELECT round(100.0 * countIf(properties.did_publish = true) / count(), 1) AS published_pct
FROM events
WHERE event = 'opening_boundary_shadow_terminal'
  AND timestamp > now() - INTERVAL 7 DAY
```

Before the switch was enabled, `published` was structurally 0. After enabling it
should track `eligible` closely. **A sustained gap between `eligible > 0` and
`published = 0` means the marker is not being written** — check the switch value
and that the service actually restarted, since it is read once at startup.

### Tile 2 — Fallback mix (where eligibility is lost)

```sql
SELECT
  properties.reason        AS reason,
  properties.session_mode  AS mode,
  count()                  AS sessions
FROM events
WHERE event = 'opening_boundary_shadow_terminal'
  AND timestamp > now() - INTERVAL 7 DAY
GROUP BY reason, mode
ORDER BY sessions DESC
```

**Chart: horizontal bar, sorted descending.** The `reason` names are long and the
job is ranking — which bucket dominates. Drop `mode` from the chart itself and
either read the split in a table below or duplicate the tile filtered per mode;
six reasons × two modes is twelve classes, well past the point where color still
carries meaning.

To watch the mix *drift*, add a day column and chart it as a **stacked bar** —
`reason` is a closed partition, so the segments legitimately sum to the whole:

```sql
SELECT
  toDate(timestamp) AS day,
  properties.reason AS reason,
  count()           AS sessions
FROM events
WHERE event = 'opening_boundary_shadow_terminal'
  AND timestamp > now() - INTERVAL 14 DAY
GROUP BY day, reason
ORDER BY day, sessions DESC
```

UI equivalent: Trends → total count of `opening_boundary_shadow_terminal`,
breakdown by `reason`.

`reason` is a closed set, so this partitions every session exactly once. One
dominant non-`would_publish` bucket is the thing worth fixing:

| Dominant bucket | Reading |
| --- | --- |
| `no_candidate` | sessions end before the Lichess middlegame boundary, or the raw predicate never fires — expected for short games, suspicious if universal |
| `probe_ack_incomplete` | the coordinator never got contiguous upload acks before terminal; upload latency or ack fencing |
| `baseline_missing` | the before-session baseline lost the race to terminal — look at `opening_baseline_job` timings (§4) |
| `continuity_or_cap_refusal` | prefix proof refused: row gaps, nonstandard start, or the probe-ply cap |
| `exhausted` | the divider found no retained middle marker on that revision — legitimate, not a defect |

### Tile 3 — Lead time (what the feature actually buys)

```sql
SELECT
  properties.session_mode AS mode,
  count()                 AS n,
  round(quantile(0.5)(toFloat(properties.ready_to_terminal_lead_ms)) / 1000, 1) AS p50_seconds,
  round(quantile(0.9)(toFloat(properties.ready_to_terminal_lead_ms)) / 1000, 1) AS p90_seconds
FROM events
WHERE event = 'opening_boundary_shadow_terminal'
  AND properties.ready_to_terminal_lead_ms IS NOT NULL
  AND timestamp > now() - INTERVAL 14 DAY
GROUP BY mode
```

**Chart: table** (or a KPI row of stat tiles). This is four numbers; a four-bar
chart is chrome around a table. If you want to catch a collapse early, add
`toDate(timestamp)` and chart p50 alone as a **line** — one series, no legend
needed, the title names it.

This is the head start over terminal-only behavior. If p50 collapses toward zero
the boundary is being proved so late that the feature is cosmetic.

### Tile 4 — Live poll latency and success

```sql
SELECT
  properties.mode AS mode,
  count()         AS polls,
  round(100.0 * countIf(properties.outcome = 'fresh') / count(), 1)                  AS fresh_pct,
  round(100.0 * countIf(properties.fresh_on_first_attempt = true) / count(), 1)      AS first_attempt_pct,
  round(quantile(0.5)(toFloat(properties.elapsed_ms)))                               AS p50_ms,
  round(quantile(0.9)(toFloat(properties.elapsed_ms)))                               AS p90_ms
FROM events
WHERE event = 'opening_delta_poll_completed'
  AND properties.trigger IN ('game_opening_boundary', 'drill_opening_boundary')
  AND timestamp > now() - INTERVAL 7 DAY
GROUP BY mode
```

**Chart: table.** Do not plot this row as one figure — `fresh_pct` and
`first_attempt_pct` are percentages while `p50_ms` and `p90_ms` are milliseconds,
and forcing them onto one plot is exactly the dual-axis mistake. For trends, add a
day column and split into two **line** charts: one for the two latency quantiles,
one for the two rates.

`elapsed_ms` is wall time from poll start to a validated fresh result. Rising p90
with flat `fresh_pct` means the scoped overlay is getting slower; rising
`attempts_exhausted` (Tile 5) means it stopped converging inside the budget.

### Tile 5 — Poll outcome mix and transport errors

```sql
SELECT
  properties.outcome AS outcome,
  count()            AS polls,
  round(avg(toFloat(properties.request_error_count)), 2) AS avg_request_errors,
  round(100.0 * countIf(properties.has_renderable_change = true) / count(), 1) AS renderable_pct
FROM events
WHERE event = 'opening_delta_poll_completed'
  AND properties.trigger IN ('game_opening_boundary', 'drill_opening_boundary')
  AND timestamp > now() - INTERVAL 7 DAY
GROUP BY outcome
ORDER BY polls DESC
```

**Chart: horizontal bar, sorted descending**, plotting `polls` by `outcome` — four
categories, part-to-whole, short names. Keep `avg_request_errors` and
`renderable_pct` as table columns beside it; they are different units and do not
belong on the bar.

`abandoned` is normal and expected — terminalization preempting a live poll is
the designed takeover, as is a takeback or unmount. `attempts_exhausted` and
`capacity_evicted` are the regression signals. `avg_request_errors > 0` points at
the transport, not the lane.

### Tile 6 — Boundary vs terminal takeover

```sql
SELECT
  lane,
  outcome,
  polls,
  round(100.0 * polls / sum(polls) OVER (PARTITION BY lane), 1) AS pct_of_lane
FROM (
  SELECT
    if(properties.trigger IN ('game_opening_boundary', 'drill_opening_boundary'),
       'boundary', 'terminal') AS lane,
    properties.outcome         AS outcome,
    count()                    AS polls
  FROM events
  WHERE event = 'opening_delta_poll_completed'
    AND timestamp > now() - INTERVAL 7 DAY
  GROUP BY lane, outcome
)
ORDER BY lane, polls DESC
```

**Chart: 100% stacked horizontal bar, one bar per lane**, plotting `pct_of_lane`.
The question is whether the outcome *mix* differs between lanes; boundary and
terminal poll volumes are not comparable, so stacking the raw counts would only
show which lane ran more often. The window function normalizes each lane to its
own total — if your PostHog rejects `OVER (PARTITION BY …)`, drop the wrapper and
divide by a scalar subquery per lane instead.

Terminal-lane behavior must be unchanged from before the rollout. If terminal
`fresh` rates or counts move, the live path is interfering with the authoritative
one — that is a rollback trigger, not a tuning problem.

---

## 3. Capture-completeness gate

The bead requires ≥98% telemetry capture completeness before eligibility
percentages mean anything. `capture()` is fire-and-forget and no-ops when
disabled (`backend/app/posthog_client.py:64`), so a missing event proves nothing
on its own — it has to be checked against durable state.

The authoritative denominator is the durable claim column, not another event.

**PostHog numerator** (same window as below):

```sql
SELECT count() FROM events
WHERE event = 'opening_boundary_shadow_terminal'
  AND timestamp >= now() - INTERVAL 7 DAY
```

**Production denominator** — counts only, no per-session content:

```bash
railway connect Postgres    # service name as listed by `railway status --json`
```

```sql
SELECT count(*) AS claimed
FROM game_sessions
WHERE opening_boundary_shadow_terminal_at >= now() - interval '7 days';
```

Completeness = numerator / denominator. Below 98%, fix measurement before
interpreting any rate in §2 — the shortfall is biased by an unknown amount in an
unknown direction. Mind the boundary effects: the two windows must cover the same
interval, and events queued in the SDK at a deploy/restart are flushed by the
lifespan `shutdown()` but can still straddle the edge.

---

## 4. What PostHog cannot see

Scoped-lane load and stage timings are `logger.info` only. Retrieve them from
Railway:

```bash
railway logs --service ghostreplay | grep scoped_opening_delta
railway logs --service ghostreplay | grep opening_baseline_job
```

| Log line | Source | Tells you |
| --- | --- | --- |
| `scoped_opening_delta outcome=… request_count=… candidate_count=… published_count=… session_load_ms=… overlay_ms=… digest_ms=… score_ms=… publish_ms=… total_ms=…` | `opening_score_delta.py:1396` | per-invocation overlay cost and where the time goes; `published_count` vs `request_count` is the lane's own hit rate |
| `opening_baseline_job … source=… mismatch_reason=… snapshot_ms=…` | `opening_score_delta.py:1225` | baseline readiness latency — the upstream gate for `reason = baseline_missing` |
| `opening_baseline_snapshot … source=… snapshot_ms=…` | `opening_score_delta.py:1000` | snapshot cost in isolation |
| `POST /api/session/{id}/opening-boundary 200 12.345ms client=…` | `http_logging.py:24` | proof endpoint latency under the session-row lock (positional `<method> <path> <status> <ms>`, not `key=value`) |

CPU, memory, and queue pressure are Railway's own service metrics in the
dashboard, not application logs.

**If any log-derived figure is going to be reported as a number**, follow
[`railway-log-query-completeness.md`](./railway-log-query-completeness.md)
rather than a plain `grep | wc -l`. A count from a query that silently dropped
records is worse than no count. Eyeballing the logs for a dominant error shape
needs no such ceremony.

---

## 5. Cadence and thresholds

Daily for the first week, then weekly:

1. Tile 1 — `published` tracking `eligible`, and neither collapsing.
2. Tile 5 — `attempts_exhausted` + `capacity_evicted` share flat.
3. Tile 6 — terminal lane unchanged.
4. `scoped_opening_delta total_ms` p90 not climbing; Railway CPU flat.

Escalate to rollback (§6) on: terminal-lane regression, `attempts_exhausted`
becoming a material share of boundary polls, or scoped-lane load visibly
affecting request latency. Everything else — a dominant fallback bucket, weak
lead time — is a fix-forward engineering item, not a rollback.

The bead's diagnostic checkpoint (≥400 protocol-v1 delta-bearing terminal
sessions across ≥14 days, ideally ≥200 games and ≥200 drills) is a reporting
milestone. At n=400 a worst-case proportion carries a ±4.9pp 95% margin; per-mode
at n=200 it is ±6.9pp. Report Wilson intervals for rate metrics and bootstrap
intervals for lead-time quantiles. This checkpoint does **not** gate the feature.

---

## 6. Rollback

```bash
railway variable set OPENING_BOUNDARY_PUBLICATION_ENABLED=false --service ghostreplay
```

Setting a variable triggers a deploy by default, which supplies the restart the
startup-read switch requires. If it was set with `--skip-deploys`, or the deploy
needs forcing, follow with `railway redeploy --service ghostreplay`.

This stops creation of **new** markers. It does not revoke an already-published
marker, which stays readable until takeback or terminalization. Terminal
reconciliation and durable opening evidence are untouched by the live path, so
rollback needs no cache invalidation, no replay migration, and no evidence
rebuild. Invalid switch values fail closed.
