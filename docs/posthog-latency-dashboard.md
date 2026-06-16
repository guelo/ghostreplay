# PostHog — per-route latency dashboard (g-e8wz baseline)

This dashboard is the **artifact that unblocks g-e8wz**. It turns the server-side
`api_request` event (emitted per request by `HTTPLoggingMiddleware`, keyed by the
low-cardinality **route template**) into route-level latency distributions and
request-frequency / error-rate baselines.

Build these insights in PostHog (Project → Insights / SQL), then pin them to a
dashboard named **"API latency baseline (g-e8wz)"**. Each query is HogQL over the
`api_request` event.

## Event reference — `api_request`

| Property | Meaning |
|---|---|
| `route` | route template, e.g. `/api/session/{session_id}/moves` (never the concrete path; `unmatched` for 404s) |
| `method` | HTTP method |
| `status_code` | numeric status |
| `duration_ms` | server-measured request duration |
| `ok` | `status_code < 400` |
| `status_class` | `2xx` / `4xx` / `5xx` (`{n}xx` bucket) |
| `request_id` | correlates with the client-side `api_request_client` event and server logs |

`distinct_id` is the backend `user_id` (string) or `anon`.

## Insight 1 — Latency percentiles per route (p50/p95/p99/max)

```sql
SELECT
  properties.route AS route,
  count() AS requests,
  round(quantile(0.50)(toFloat(properties.duration_ms)), 1) AS p50_ms,
  round(quantile(0.95)(toFloat(properties.duration_ms)), 1) AS p95_ms,
  round(quantile(0.99)(toFloat(properties.duration_ms)), 1) AS p99_ms,
  round(max(toFloat(properties.duration_ms)), 1) AS max_ms
FROM events
WHERE event = 'api_request'
  AND timestamp > now() - INTERVAL 7 DAY
GROUP BY route
ORDER BY p95_ms DESC
```

Call out the two hot routes the optimization targets:
`/api/srs/review` and `/api/session/{session_id}/moves`.

## Insight 2 — Request frequency per route (volume)

```sql
SELECT properties.route AS route, count() AS requests
FROM events
WHERE event = 'api_request'
  AND timestamp > now() - INTERVAL 7 DAY
GROUP BY route
ORDER BY requests DESC
```

## Insight 3 — Error rate per route (`status_class`)

```sql
SELECT
  properties.route AS route,
  count() AS total,
  countIf(properties.status_class = '4xx') AS errors_4xx,
  countIf(properties.status_class = '5xx') AS errors_5xx,
  round(100.0 * countIf(properties.ok = false) / count(), 2) AS error_pct
FROM events
WHERE event = 'api_request'
  AND timestamp > now() - INTERVAL 7 DAY
GROUP BY route
ORDER BY error_pct DESC
```

## Insight 4 — p95 latency over time for the hot routes (trend)

```sql
SELECT
  toStartOfHour(timestamp) AS hour,
  properties.route AS route,
  round(quantile(0.95)(toFloat(properties.duration_ms)), 1) AS p95_ms
FROM events
WHERE event = 'api_request'
  AND properties.route IN ('/api/srs/review', '/api/session/{session_id}/moves')
  AND timestamp > now() - INTERVAL 7 DAY
GROUP BY hour, route
ORDER BY hour
```

## Rollout

1. Deploy with `POSTHOG_DISABLED` unset/false and a valid `POSTHOG_PROJECT_TOKEN`.
2. Confirm `api_request` events land in PostHog → Activity, and that
   `distinct_id` matches between a client `api_request_client` and the server
   `api_request` for the same `request_id`/user.
3. Let the baseline accumulate before scheduling the g-e8wz optimization.
