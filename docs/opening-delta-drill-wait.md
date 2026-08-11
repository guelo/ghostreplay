# Opening-score drill-repeat wait telemetry

This document describes the client events used to evaluate the player-visible
wait between ending a drill and repeating it. The initial behavior uses the
entire opening-delta poll as the gate: a matching repeat action unlocks when the
poll reports fresh data, exhausts its attempts, or is evicted by the client work
cap.

The first request is immediate. With 28 attempts and 27 retry sleeps of 1.5
seconds, the nominal first-to-last request-start span is **40.5 seconds**. If
every request reaches its 4-second timeout, the complete bound is **152.5
seconds** (`28 × 4s + 27 × 1.5s`), before any extra delay from browser
suspension. Start/end visibility properties make hidden-tab observations
separately filterable.

## Event reference

### `opening_delta_poll_completed`

Emitted exactly once for each newly-created poll loop. A second caller for the
same session joins the existing loop and does not emit another event.

| Property | Meaning |
|---|---|
| `trigger` | `drill_accuracy_fail`, `drill_natural_end`, `game_end`, `game_resign`, or `game_revert` |
| `mode` | `drill` or `game`, derived from the trigger |
| `outcome` | `fresh`, `attempts_exhausted`, `abandoned`, or `capacity_evicted` |
| `elapsed_ms` | Monotonic time from loop creation through finalization |
| `attempt_count` | Number of score-delta GETs started |
| `request_error_count` | Rejected or timed-out requests, excluding an explicit loop abort |
| `fresh_on_first_attempt` | Whether attempt one returned `is_fresh: true` |
| `session_replaced_before_completion` | Whether another session owned the game store at finalization |
| `has_renderable_change` | Whether the fresh response contains a visible score badge |
| `visibility_at_start`, `visibility_at_end` | Browser visibility state at the two timing boundaries |
| `visibility_changed` | Whether those two states differ |

### `drill_again_blocked`

Emitted for every pointer, touch, keyboard, or programmatic activation attempt
while the current session's opening delta is pending. Attempts are deliberately
not debounced.

| Property | Meaning |
|---|---|
| `surface` | `drill_stop` or `post_game` |
| `trigger` | Trigger from the active poll, or `unknown` during the narrow terminal-finalization race |
| `wait_elapsed_ms` | Elapsed poll time at activation, or `null` in that same race |
| `input_method` | `pointer`, `keyboard`, or `programmatic` |
| `visibility` | Browser visibility state at activation |

The existing `drill_again_clicked` event describes successful activation. It
now also carries `surface` and `opening_delta_state_at_click` (`fresh`,
`unavailable`, or `not_applicable`).

None of the completion or blocked-attempt properties contains a session/user
identifier, opening key/name, score, delta, error message, or other
high-cardinality/user-derived value. That privacy choice also means the two
event streams can be compared only in aggregate, not joined into individual
waits.

## Example PostHog queries

Use a deployment-bounded date range for every query. Client capture can be
disabled by privacy settings or content blockers, so interpret these as the
observed capture cohort rather than all players.

### Fresh drill wait distribution

Keep visible-tab and hidden-tab results separate. This query is the visible-only
view; remove the two visibility predicates and group by them for diagnosis.

```sql
SELECT
  properties.trigger AS trigger,
  count() AS polls,
  round(quantile(0.50)(toFloat(properties.elapsed_ms)), 1) AS p50_ms,
  round(quantile(0.90)(toFloat(properties.elapsed_ms)), 1) AS p90_ms,
  round(quantile(0.95)(toFloat(properties.elapsed_ms)), 1) AS p95_ms,
  round(max(toFloat(properties.elapsed_ms)), 1) AS max_ms
FROM events
WHERE event = 'opening_delta_poll_completed'
  AND properties.mode = 'drill'
  AND properties.outcome = 'fresh'
  AND properties.visibility_at_start = 'visible'
  AND properties.visibility_at_end = 'visible'
  AND timestamp > now() - INTERVAL 14 DAY
GROUP BY trigger
ORDER BY trigger
```

### Completion outcomes

```sql
SELECT
  properties.trigger AS trigger,
  properties.outcome AS outcome,
  count() AS polls
FROM events
WHERE event = 'opening_delta_poll_completed'
  AND properties.mode = 'drill'
  AND timestamp > now() - INTERVAL 14 DAY
GROUP BY trigger, outcome
ORDER BY trigger, polls DESC
```

### Blocked-attempt count

```sql
SELECT
  properties.surface AS surface,
  properties.input_method AS input_method,
  count() AS attempts
FROM events
WHERE event = 'drill_again_blocked'
  AND timestamp > now() - INTERVAL 14 DAY
GROUP BY surface, input_method
ORDER BY attempts DESC
```

### Wait already experienced at a blocked attempt

Exclude null snapshots and hidden-tab attempts from the primary rhythm read;
inspect them in separate breakdowns rather than silently mixing them in.

```sql
SELECT
  properties.surface AS surface,
  count() AS attempts,
  round(quantile(0.50)(toFloat(properties.wait_elapsed_ms)), 1) AS p50_ms,
  round(quantile(0.90)(toFloat(properties.wait_elapsed_ms)), 1) AS p90_ms,
  round(quantile(0.95)(toFloat(properties.wait_elapsed_ms)), 1) AS p95_ms,
  round(max(toFloat(properties.wait_elapsed_ms)), 1) AS max_ms
FROM events
WHERE event = 'drill_again_blocked'
  AND properties.wait_elapsed_ms IS NOT NULL
  AND properties.visibility = 'visible'
  AND timestamp > now() - INTERVAL 14 DAY
GROUP BY surface
ORDER BY surface
```

These aggregates inform the manual keep/tune/revert decision; they do not
define a pre-rollout sample threshold or replace playing the repeated drill
flow.
