# PostHog — client-capture outage canary (g-client-event-gap)

This is the **detection signal** for g-client-event-gap. All client-side PostHog
capture died in production on ~2026-07-02 and ran silent for three weeks before
anyone noticed, because nothing watched it: server capture stayed healthy the
whole time, so every dashboard built on `api_request` looked normal.

The canary closes that gap. It compares the client event stream against the
subset of server requests that *should* have produced one.

## Why the naive comparison does not work

The obvious signal — "alert when `api_request` is healthy but `api_request_client`
is ~0" — misfires, because a silent client is sometimes **correct**. The
posthog-js SDK opts capture out on a yes-like Do Not Track (`respect_dnt: true`),
so a user base that asserts DNT produces near-zero client events by design, and
an alert on raw volume would fire forever.

So the denominator is not all server requests — it is the server requests from
clients that were **not** gated. `HTTPLoggingMiddleware._privacy_signals` tags
every `api_request` with the HTTP-header equivalents:

| Property | Source header | Mirrors |
|---|---|---|
| `dnt_signaled` | `DNT: 1` / `DNT: yes` | posthog-js `respect_dnt` opt-out — **the live client gate** |
| `gpc_signaled` | `Sec-GPC: 1` | nothing, as of `f763907` — see below |
| `client_capture_gated` | either of the above | **stale**: still ORs GPC in |

> **The server tag is out of sync with the client as of `f763907`** ("get rid of
> gpc gate"), which removed the `isGpcSignaled()` pre-init bail. The client now
> gates on DNT alone, but `_privacy_signals` still computes
> `client_capture_gated = gpc OR dnt`. A browser that sends `Sec-GPC: 1` without
> `DNT: 1` now captures normally, yet is tagged as gated — so it is wrongly
> excluded from the denominator, biasing `B` low and the ratio high (the canary
> under-fires).
>
> **Use `dnt_signaled = false` as the denominator filter, not
> `client_capture_gated = false`**, until the server tag is realigned. Both
> queries below already do.

`dnt_signaled = false` therefore means "this browser should have been emitting
`api_request_client`". If a healthy volume of those requests arrives and no
client events do, client capture is genuinely broken.

### Content blockers sit inside the denominator, deliberately

DNT and GPC announce themselves in a request header, so the server can subtract
them. A content blocker does not. uBlock Origin drops the `/ingest` POST while
the `/api` calls beside it go through untouched — so that browser is tagged
`dnt_signaled = false`, lands in the denominator, and contributes nothing to the
numerator.

That is the right treatment: blocker-induced loss is real telemetry loss, and it
is invisible to every other signal we have. But it has two consequences. A
healthy ratio is bounded well below 1.0 by the steady-state blocker share, and a
*rising* blocker share looks identical to a code regression on the ratio alone —
so check `vercel.json` and the bundle before concluding either.

The same-origin `/ingest` proxy confers no immunity. It exists for COEP/CORS
(`src/analytics/posthog.ts:14-21`) and merely happens to dodge some blocklists;
uBlock Origin blocked capture straight through it on this app, discovered
2026-07-29.

### What the first read showed (2026-07-28)

The gate rate came back at **~100%**, and it is **DNT, not GPC**, doing the
gating: `pct_gated` tracked `pct_dnt` on every day measured (100.0 / 100.0 /
99.9), while `gpc_signaled` was 0% on three of four days. `respect_dnt: true` is
what silences capture; the GPC pre-init bail was close to a no-op on this
traffic, and was removed outright in `f763907` on the strength of this read.

The breakdown by `distinct_id` explains why. Over five days production traffic
was **two browsers**: one with 3,107 requests (DNT, no GPC) and one with 315
(DNT and GPC), plus 9 anonymous requests of which 8 were verification probes.
The app is unreleased — all of it is developer testing, and both test browsers
had DNT enabled.

So there is no "audience privacy mix" here to reason about.

### Resolution (2026-07-29)

Clearing DNT was necessary but not sufficient. Capture stayed dead until uBlock
Origin was disabled as well — two independent kill switches, either one fatal on
its own, which is exactly why fixing the first changed nothing observable and
looked like a failed fix.

With both cleared the day reads 789 server requests, **789 of them ungated**, and
**783 `api_request_client` events — a capture ratio of 0.992**. Two `final_full`
events landed (`game_end`, `resign`), the first ever captured in production, and
both joined to durable `session_upload_receipt` rows by `client_request_id`.

Treat 0.992 as a **ceiling, not a steady state**: that browser had its blocker
disabled, so its blocker share was zero. No real population will match it.

> These properties ship from commit `5eeee18` (deployed 2026-07-24). They are
> absent on earlier events, so scope any backfill query to `timestamp >=
> '2026-07-24 10:00:00'` — an absent property is not `false`, and older rows
> will silently drop out of the ungated denominator.

## The insight (PostHog UI → Trends)

Build a Trends insight named **"Client capture health (g-client-event-gap)"**
with two series and a formula:

- **A** — event `api_request_client`, count
- **B** — event `api_request`, count, filtered to `dnt_signaled` **is false**
- **Formula** — `A / B`

Interval: **daily**. Date range: last 30 days.

Attach an **alert** on the formula series: notify when the value is **below
0.20**, and require B to be meaningful — PostHog alerts apply to a single
series, so pair it with a second alert (or a filter on the insight) that only
treats the ratio as valid when **B ≥ 200** for the day. Below that volume the
ratio is noise, not an outage.

A daily interval is chosen from measured volume, not habit. Total traffic runs
250–2,041 requests/day (and one day in the five measured had none at all), so
an hourly bucket holds ~10–85 requests and a 6-hour window ~60–500 — a `B ≥ 200`
guard would go unsatisfied most of the time and the canary would sit silent for
the wrong reason. Daily buckets clear the guard on all but the quietest days.
Idle days simply do not evaluate, which is correct for a project whose traffic
is developer testing. Revisit the interval if real traffic ever arrives: at
production volume, hourly buckets detect an outage ~24× sooner.

## The same check as HogQL (manual / backfill)

```sql
SELECT
  toDate(timestamp) AS day,
  countIf(event = 'api_request' AND properties.dnt_signaled = false) AS ungated_requests,
  countIf(event = 'api_request') AS all_requests,
  countIf(event = 'api_request_client') AS client_events,
  round(
    client_events / nullIf(ungated_requests, 0),
    3
  ) AS capture_ratio
FROM events
WHERE event IN ('api_request', 'api_request_client')
  AND timestamp > now() - INTERVAL 30 DAY
GROUP BY day
HAVING ungated_requests >= 200
ORDER BY day
```

`capture_ratio` near zero while `ungated_requests` is healthy is the outage.
`nullIf` keeps a zero-denominator day out of the series rather than turning it
into a division error or a fake zero.

## Threshold calibration

`0.20` is a deliberately loose starting point, not a measured bound. Each
ungated logical request produces exactly one `api_request_client` event and at
least one server `api_request` (retries add server rows but not client rows), so
a healthy ratio is at most 1.0 and should sit well above 0.5. For reference, on
2026-07-01 — the last healthy day before the collapse — client volume was 78% of
*total* server volume (6400 / 8238), and the ungated denominator is strictly
smaller than that, so the true healthy ratio was higher still.

Measured directly against the ungated denominator on 2026-07-29: **783 / 789 =
0.992**, confirming the ~1:1 model. The six unmatched requests are consistent
with server-side retries, which add server rows but not client rows.

**Re-calibrate once client capture is restored and a week of clean data exists.**
Set the threshold to roughly half the observed p10 daily ratio. Until then 0.20
is low enough that only a real outage trips it.

Calibrate from the *observed* ratio, never from the theoretical 1.0: the blocker
share above is a permanent floor under the numerator, and its size here is
unknown — this app has never had a client-capture window that was both ungated
and blocker-free.

Note that 6400 / 8238 came from a pre-release window, so it is the developer's
own testing too, not a production baseline. Use it as a sanity bound on the
shape of the ratio, not as a target.

## What a firing canary means

| Observation | Reading |
|---|---|
| `capture_ratio` ~0, `ungated_requests` healthy | Real outage. Client capture is broken for browsers that never asked to be excluded. Bisect recent frontend commits touching `src/analytics/posthog.ts`, `main.tsx`, or `vercel.json`'s `/ingest` rewrites. |
| `capture_ratio` ~0, `ungated_requests` ~0 | Not an outage — the audience really is asserting GPC/DNT. Nothing to fix client-side; measure the affected outcomes server-side instead. |
| `capture_ratio` sags gradually, no deploy correlates | Likely a drifting content-blocker share, not a regression. Blocked browsers are in the denominator by design (above). Confirm against the bundle/`vercel.json` history before chasing code. |
| `capture_ratio` healthy, a specific route missing | Not this canary's job — a call-site bug, not a capture outage. |
| Both series ~0 | Ingest or traffic outage, not client-specific. The existing `api_request` volume insight covers it. |

## Verifying the tags survive the proxy

Prod browsers reach the API through Vercel's `/api/:match*` rewrite (see
`resolveApiBaseUrl` — a `.vercel.app` host always forces the same-origin `/api`
base), so `Sec-GPC` and `DNT` must survive that hop for the denominator to be
correct. If the proxy stripped them, every request would look ungated and the
canary would over-fire.

**Verified 2026-07-28: the rewrite forwards both headers intact.** Eight probes
(gpc-only / dnt-only / both / neither, each sent through the rewrite and direct
to Railway) returned matching tags on every pair. Re-run this only after a
`vercel.json` change.

To re-check after any `vercel.json` change, send probes with known ids through
both paths and confirm the tags match:

```bash
curl -s -o /dev/null -H 'X-Client-Request-ID: <uuid>' -H 'Sec-GPC: 1' \
  https://ghostreplay.vercel.app/api/__gap_probe          # via the rewrite
curl -s -o /dev/null -H 'X-Client-Request-ID: <other-uuid>' -H 'Sec-GPC: 1' \
  https://ghostreplay-production.up.railway.app/api/__gap_probe   # direct control
```

A 401 is the expected response and is still captured — the middleware is the
outermost layer, so the event is emitted regardless of status. Then look the
probes up by `client_request_id`:

```sql
SELECT properties.client_request_id AS id,
       properties.gpc_signaled, properties.dnt_signaled, properties.client_capture_gated
FROM events
WHERE event = 'api_request'
  AND properties.client_request_id LIKE '9c000000-%'
  AND timestamp > now() - INTERVAL 1 DAY
ORDER BY timestamp
```

The proxied row and its direct control must agree. If they disagree, the header
is being dropped in transit and the canary's denominator cannot be trusted.
