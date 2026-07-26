# Querying Railway logs completely

How to retrieve as complete a set of production log records from Railway over a
time window as the platform permits — what that buys you, what it does **not**
prove, and why each cheaper method is unsound.

This is method, not architecture — it is about the Railway CLI's retrieval
semantics, and it applies to any future measurement taken from production logs.

**Provenance.** Derived and hardened while building
`backend/scripts/report_session_v2_adoption.py` (shipped `7fbe186`, 2026-07-25;
deleted once the `browser-game-v1` retirement turned out to need no adoption
gate — see `g-bgv1-report-fate`). The script's *gate* is gone and should not be
revived; the retrieval reasoning below is what survived it, and it was the
expensive half. Empirical figures are from the one operator run
(`g-bgv1-adoption-run`, 2026-07-25).

---

## 0. The premise

A number computed from a log query that silently lost records is **worse than no
number**: it is biased by an unknown amount in an unknown direction and it looks
authoritative. So the design goal below is to fail **closed** — an unprovable
query aborts the measurement rather than reporting over whatever arrived.

That goal is met for every loss mode Railway lets you *detect*. It is not met
everywhere: two modes (§2.1, §3.1) are undetectable from inside the query, so
they fail **open** by construction — the run reports clean and cannot know
otherwise. Those are assumptions, not checks, and they are the reason this
document does not claim completeness outright.

Two of Railway's loss modes are invisible from inside a filtered query, and one
of them (deployment coverage) cannot be bounded by any inference from what the
API exposes. Those are §1 and §3.

**What follows is not uniformly "proof", and the difference matters.** Sorted by
what each section actually delivers:

| Loss mode | Strength of the defence |
|---|---|
| Single-deployment blind spot (§1) | **Proved**, by exhaustion over a finite, truncation-checked deployment list. |
| Retrieval-limit truncation (§2) | **Proved** *within* a shard — no shard is silently capped. |
| Shard-boundary records (§2.1) | **Assumption.** Unpinned `--since`/`--until` inclusivity can drop a record at the split instant with no trace. Cheaply fixable — see §2.1. |
| Railway's own log shedding (§3.1) | **Assumption, irreducibly fail-open.** The warning's wording is server-side and undocumented as an invariant. |
| Parse/command failures (§4) | **Proved** — every failure is surfaced, none skipped. |
| Retention expiring mid-run (§6.1) | **Detectable, but only if you check for it** — a start-time-only retention check misses it entirely. |

The two assumption rows are stated as such wherever they appear. A measurement
built on this method should carry them forward as caveats rather than inherit the
word "complete".

---

## 1. Deployment coverage — proven by exhaustion, not by inference

**A window is served by more than one container.** A window spanning a deploy has
its earlier half in the *previous* deployment's log stream. `railway logs` with
no positional deployment id silently defaults to the most recent successful
deployment, so the naive query reads one stream and misses the rest.

**Railway exposes nothing to reconstruct a takeover timeline.** `railway
deployment list` returns, per deployment:

- a **status** that is a snapshot of where it *ended up* — `REMOVED` is the
  terminal state both of a container that served for a week and of one cancelled
  thirty seconds into its build;
- a **`created_at` that is when the BUILD started**, not when the container began
  serving. Build + release + healthchecks can take hours;
- **no activation time and no removal time.** The outgoing container also keeps
  running while it drains, still doing work and still emitting log lines.

So there is no sound rule of the form "deployment N's life ends when deployment
N+1 appears". Every narrowing rule tried against this data was refuted:

| Candidate rule | Why it is unsound |
|---|---|
| Stop the backward walk at a status that proves the container ran (`SUCCESS`) | `SUCCESS` says it became live *eventually*, not before the window opened. A deployment created three hours early can spend four of them in build, release and healthchecks — its predecessor owned the opening. |
| Stop at the first candidate that returned no records | The stream is *filtered* (e.g. to one `side_effect=` marker), so it records **gameplay, not lifecycle**. A container serving through a quiet hour and an aborted build look identical. |
| Stop at the deployment that predates the emitter's own deploy | `created_at` is build start while the emitter's deploy time is when it reached production, so the emitter's own deployment sorts *before* that timestamp — and any build created (and aborted) during its rollout ends the walk in front of it. |
| …same rule, second reason | Pre-instrumentation containers were **not silent**: they emitted the same summary line without the new fields. If one drained into the window, those records must surface as *missing-field anomalies* — telemetry that is missing is a completeness failure — and skipping the deployment converts that into invisible absence. |

**What is left is the deployment list itself**, which is finite. So: query
*every* deployment created before the window closed, back through the whole list,
and prove coverage by **exhaustion**.

The concrete plan splits into two sets:

- **`in_range`** — created in `[window_start - takeover_grace, window_end)`.
  Query all of them whatever their status: created after the window closed, a
  deployment cannot have logged inside it; anything else in range might have
  (a late-failing build may still have emitted a line, and an unqueried source is
  an *unquantified* hole, not a known-empty one).
- **`carry_in`** — every older deployment, newest first, **not truncated**.

A *takeover grace* (2h was the default) is a convenience for ordering the walk,
**not a coverage bound** — nothing proves a deployment created within it had
taken over, or that anything created before it had stopped. Both lags stack and
Railway exposes neither.

The cost is real and grows with release history: every deployment created before
the window gets queried, ~3 CLI calls each, most returning nothing. A budget for
cutting the walk short is legitimate *only* if spending it is recorded as a
**coverage gap** rather than waved through. There is no safe default budget.

> If Railway ever exposes per-deployment **activation and removal** times, that
> is the bound to switch to. It is the evidence none of the rules above could
> substitute for.

**The deployment list has its own truncation trap.** `railway deployment list
--limit` defaults to 20 and is capped at 1000 by the CLI. That cap is a
*maximum, not a page size*, and there is no continuation cursor — so a
full-length 1000-record result is indistinguishable from a truncated one, and the
records dropped off the end are the **oldest**, i.e. exactly the deployment
active at the window's start boundary. Always pass `--limit 1000`, and treat
`>= 1000` as possibly-truncated.

---

## 2. The 500-record retrieval limit — shard until nothing saturates

`railway logs` defaults a historical query to **500 records** when `--lines` is
omitted (`cli/src/commands/logs.rs`: `limit: args.lines.or(Some(500))`), the
documented maximum is the same 500, and **there is no "there was more" signal in
the response.**

Therefore:

1. Always pass `--lines` explicitly.
2. Treat a shard returning **exactly** the limit as **AMBIGUOUS, never as
   complete**: halve the time range and re-query both halves, recursively.
3. Trust only leaves that come back *strictly under* the limit.
4. Publish the whole shard tree with the result, so a reader can see the query
   was not silently capped.
5. A range at a minimum sane width (60s) that *still* saturates is an
   unsplittable saturation — at that point the log path cannot prove completeness
   and **no number should be reported**.

### 2.1 LIMITATION — shard boundaries are not proven lossless

The CLI's `--since` / `--until` inclusivity is **not contractually pinned**, and
the recursion above splits both halves at the same instant: it issues
`--since since --until midpoint` and then `--since midpoint --until until`. A
record landing exactly on `midpoint` therefore has four possible fates, and only
three are safe:

| `--since` | `--until` | Record at `midpoint` | Consequence |
|---|---|---|---|
| inclusive | exclusive | right only (left's `--until` excludes it) | clean partition |
| inclusive | inclusive | both halves | duplicate — dedupe needed |
| exclusive | inclusive | left only (right's `--since` excludes it) | clean |
| **exclusive** | **exclusive** | **neither** | **SILENTLY LOST** |

The last row is the dangerous one, and it defeats the proof rather than dents it:
both children come back below the limit and certify themselves complete, so the
loss leaves no trace anywhere in the shard tree.

**Dedupe does not rescue this, and its own identity is weak.** The deleted
implementation keyed records on `(timestamp, message)` — the only fields it was
willing to assume were present. That identity **can collapse genuinely distinct
events**: any two sharing both a timestamp and a message become one. No such
collision was observed. What *was* observed is weaker but points the same way —
§5 records Railway assigning distinct events (with *different* messages) the same
buffered-log timestamp, so the timestamp half of the key separates less than its
resolution suggests, and the identity leans almost entirely on the message.

**What is actually proven, then:** that no shard was silently capped at the
retrieval limit (§2 items 1–5 do establish that). Completeness *across a
boundary* is an assumption until one of these is done:

1. **Pin the semantics empirically, once, and record it** — the cheap fix, and
   the recommended one. But it has a prerequisite, and skipping it makes the probe
   answer the wrong question:

   **First establish the accepted endpoint resolution ε.** A probe at `T ± 1ns`
   only distinguishes inclusivity if Railway *preserves* nanoseconds in a query
   bound. If it rounds or truncates endpoints — to microseconds, milliseconds, or
   whole seconds — then `T` and `T + 1ns` are the *same* bound, both probes
   degenerate to the same query, and the result is ambiguous rather than
   informative. Determine ε by bisection against a known record: widen the offset
   from a bound that clearly excludes `T` until the record's membership flips,
   and take the smallest offset that actually changes the answer. Record ε
   alongside the CLI version.

   **Then probe with that ε.** Issue `--since T --until T+ε` and
   `--since T-ε --until T`; whether `T` comes back tells you each end's
   inclusivity directly. Re-check on CLI upgrade — ε is part of the pinned
   contract, not a constant.
2. **Overlap the shards by WIDENING THE BOUNDS, not by renotating them.** Bracket
   notation buys nothing here — which end is closed is the CLI's decision, not the
   caller's, so writing `[since, midpoint]` changes no query. Real overlap means
   moving the endpoints past each other: query `--until midpoint + δ` on the left
   and `--since midpoint - δ` on the right. **δ must be at least one preserved
   endpoint unit — the same ε as above, not the record timestamps' resolution.**
   Records may be stamped in nanoseconds while bounds are honoured only to the
   millisecond; a δ below ε rounds away and produces no overlap at all while
   looking like it did. That puts the midpoint instant strictly *inside* both
   ranges, so every inclusivity combination returns it at least once. The cost is
   a guaranteed duplicate band, which is only payable with a genuinely stable
   per-record identity — `(timestamp, message)` is not one, so this route needs a
   unique record id the CLI actually emits.
3. **Cross-check the counts** — a saturated parent returned exactly `limit`
   records, so the true count in its range is `>= limit`. If its leaves sum to
   *fewer* than that, records were provably lost. This is a partial detector, not
   a proof: loss is invisible whenever the leaves still sum to `>= limit`.

Until then, state the result as complete **up to boundary semantics** and carry
that forward as a caveat. Note what it does *not* license: **idempotence is not a
defence here.** An idempotent rollup absorbs duplicates, but this failure mode is
an *omission*, and omissions change answers — losing one session's latest-final
record silently moves it to an older verdict or out of the denominator entirely.
Boundary uncertainty is therefore insufficient for an exact count *and*
insufficient for a latest-wins rollup; it is tolerable only where a single
dropped record cannot change the conclusion, and that has to be argued per
measurement rather than assumed.

---

## 3. Railway's own drop warning is excluded by your filter

Railway sheds log lines above **500/sec/replica** and reports that as a log line
of its own:

```
Railway rate limit of 500 logs/sec reached for replica, update your application
to reduce the logging rate. Messages dropped: N
```

Which means **a filtered query cannot surface its own loss signal** — the filter
that selects your records necessarily excludes this warning. So run a *second,
separately-filtered* query per deployment looking for the warning, and fail
closed on any hit.

That side query is the only *available* check on whether the main query lost
records, so it has to be as trustworthy as it can be made:

- **Match forgivingly, and query forgivingly.** Zero records is exactly what a
  clean sample looks like, so a query that fails to retrieve a reworded warning
  fails *open*. Use **one query per marker phrase** (`"Messages dropped"`,
  `"rate limit of"`) and union the results, rather than one OR-ed filter — the
  CLI's filter grammar is not pinned either, and a filter Railway parses as a
  literal string matches nothing, which is the same fail-open in a different
  place. Match parsed lines case-insensitively on either half.
- **A record in that query that is not a drop warning invalidates the
  deployment** rather than being skipped: it means the filter is not selecting
  what the check assumes.
- Re-check the main filter's marker on every parsed record too. If a returned
  record lacks the substring you filtered on, the filter did not do what you
  assume and both the record counts and the loss check are meaningless.

### 3.1 LIMITATION — this check is fail-open, and cannot be made otherwise

Two marker phrases are **hedging, not proof**. Railway documents only a warning
*like* the example above; there is no structured field, no error code, and no
documented invariant that either phrase persists. The wording is **server-side**,
so pinning the CLI version does not pin it, and a rewording that changes *both*
halves produces zero records — indistinguishable from a genuinely clean sample.

Record this as a standing assumption of any measurement that relies on it:

> **Assumed:** that Railway's log-shedding warning still contains "Messages
> dropped" or "rate limit of", and that it is retrievable through `--filter`.
> If Railway rewords it, every run silently reports "no drops detected".

The assumption is cheap to re-validate and worth re-validating whenever the
numbers matter: check the current wording in Railway's
[logging-throughput docs](https://docs.railway.com/observability/logs#logging-throughput)
and record the checked-on date beside the result. Nothing in the retrieval path
can detect the failure for you.

---

## 4. Parse failures are data loss, and the bias has a direction

Every non-blank line of CLI output must be a JSON object (handle both a JSON
array and NDJSON); anything else **fails the run**.

Skipping unparseable lines looks harmless — "it wasn't a log record anyway" — but
a truncated or corrupt record is exactly a record you meant to count, and
dropping it removes an item from the **denominator** without leaving a trace.
Worse, the bias has a direction: odd records skew toward older, longer-running,
legacy-client sessions, so silent skipping pushes an adoption fraction **up**.

Likewise, every Railway invocation failure — non-zero exit, missing binary,
timeout — is one failure class, because they all mean the same thing to the
caller: the records this command should have returned are missing.

---

## 5. Timestamps

- Compare log timestamps as **exact integer nanoseconds**. Floats cannot
  represent an epoch-nanosecond value without ~256ns of rounding, and would
  manufacture ties that do not exist.
- **Railway timestamp collisions are real, not hypothetical.**
  [`release_a_runbook.md`](./release_a_runbook.md) §4 records Railway assigning
  two distinct migration transitions the *same* buffered-log timestamp
  (`2026-07-11T10:33:43.599Z`). Any "latest wins" rollup therefore has an
  order-dependent tiebreak. Detect the collision and fail closed when equal-max
  records disagree; do not let list order pick a winner.

---

## 6. Reproducibility inputs

Record these *in the output*, not just in the operator's head — a run with a
tolerance relaxed to a year is otherwise indistinguishable from one at the
default, and a verdict whose inputs are invisible cannot be independently
doubted:

- **The Railway CLI version.** Pagination and filter semantics above are
  version-dependent. A version lookup that *fails* should block the run rather
  than degrade to "unknown".
- The window, the attested retention, the instrumentation deploy time, any
  freshness tolerance and takeover grace, and **both the start and end time of
  the query run itself** (see §6.1).
- The queried deployment set with statuses, the shard tree, and every anomaly.
- The accepted endpoint resolution ε (§2.1) that the shard bounds rely on.

**Log retention bounds the window, and it must be attested, not optional.** An
optional check is not a check: omitting retention on a window that reaches past
the horizon yields exactly the same confident answer as a window that fits.
Railway's [documented tiers](https://docs.railway.com/observability/logs) are
Hobby/Trial **7 days**, Pro **30**, Enterprise up to **90** — a larger claim
cannot be true of any plan and should be rejected.

*Attested* means read off the project's plan in the dashboard. Probing which
deployments still return records does **not** substitute for it — see §7.

### 6.1 Retention expires DURING the run — check the window against the end, not the start

Checking "does the window fit inside retention?" once, at startup, is a check
against the wrong instant. **The query is not a snapshot.** §1's exhaustive walk
issues roughly three calls for every historical deployment — 27 deployments made
that ~80 calls on this project — and §2 multiplies that by every shard split. The
run takes real time, and retention keeps advancing through all of it.

So a window that fits at query start can have its oldest edge fall off the
retention horizon before the later shards are issued. The records lost that way
are **the oldest ones**, which is the same directional bias as §1 and §4: older
records skew toward legacy clients, so quietly losing them pushes an adoption
fraction *up*. And the loss is invisible — the late shard returns fewer records
and certifies itself complete, exactly as in §2.1.

Concretely:

- **Record `query_started_at` and `query_finished_at`** in the report, not just a
  generation time.
- **Demand headroom, not just fit.** Require
  `window_start >= query_started_at - retention + expected_run_duration + margin`.
  Sizing the expected duration from the deployment count and the observed
  per-call latency is enough; the margin is there because shard splitting makes
  the call count data-dependent and unpredictable in advance.
- **Fail closed on overrun.** If `query_finished_at` lands such that
  `window_start < query_finished_at - retention`, the run consumed its own
  headroom: some of the window was outside retention by the time it was queried.
  Report it as an evidence failure, not a warning — the number is biased by an
  unknown amount in the known direction.
- **Cheap detector, worth having anyway:** re-issue the *oldest* shard's query
  after the walk finishes and compare its record count to the first pass. A drop
  means retention ate records mid-run, whatever the arithmetic predicted.

The same reasoning applies to any window whose start sits near the horizon: the
closer `window_start` is to `now - retention`, the less of the run it can
survive. Prefer windows with slack at the old end over windows that exactly fill
the retention period.

---

## 7. Observations from the one operator run (2026-07-25, this project's Railway service)

Useful as calibration for anyone sizing a future log-based measurement. Not all
rows are measurements — the third column says which is which.

| Item | Value | How it was established |
|---|---|---|
| Two literal query outcomes | deployment created **7.7d** before the query → **0 records**; deployment created **6.9d** before the query → **records present** | Nothing else was measured. This locates no boundary: §1 refutes both `created_at` and an empty filtered stream as evidence about a container's life. |
| Retention value used | **7 days**, adopted as a *working, conservative bound* for sizing windows — plan NOT attested | Chosen because it is the shortest documented tier, so it is the fail-closed choice. Not a measurement (see caveat). |
| Deployments listed and examined | **27** | `railway deployment list --limit 1000` |
| Records returned across them (one `side_effect=` filter) | **581** | Direct query across every deployment examined |
| Distinct sessions | **22** (~3.1/day) | Per-day: 07-21:3, 07-22:7, 07-23:3, 07-24:6, 07-25:3 |
| Distinct users | **5** — user 14 (owner): 475 records, user 120: 52, user 115: 29, user 118: 21, user 85: 4 (= 581) | Grouped from the same 581 records |
| `created_at` vs activation, measured | build start `04:55:17.534Z` → "Starting Container" `04:59:28.784Z` → "Application startup complete" `04:59:32.450Z` (~4m15s) | Read activation from the deployment's *own* log stream; this gap is the §1 problem in miniature. |

> **Caveat on the first two rows — nothing here measures retention, and no
> boundary was located.** The two outcomes rest on exactly the signals §1 spends
> its length refuting: `created_at` (build start, not serving time) and an empty
> *filtered* stream (a record of gameplay, not of lifecycle — a quiet container
> and an aborted build look identical). The older deployment's zero records is
> equally explained by it never having served, having served only briefly, or
> having been idle. So the pair does **not** bracket a horizon; it is two data
> points consistent with a 7-day retention and with several other stories. The 7
> in the second row comes from Railway's shortest documented tier, not from these
> observations — it is the fail-closed number to size a window against, which is
> a legitimate use of an unattested value. Read the plan in the dashboard to get
> the real one.

**The scale lesson.** At ~3.1 distinct sessions/day, a threshold of 200 distinct
sessions needs ~65 days of window against the 7-day working bound — 9× over.
Railway Pro's 30 days would still only reach ~93. *A log-retrieval measurement is
retention-bounded, and no amount of query rigour fixes a traffic shortfall.* If a
criterion needs accumulation beyond the retention horizon, the answer is a
durable observation table (persist one row per observed unit), not a longer
window.

---

## 8. Command shapes that work

```bash
railway link   # once, per project

railway deployment list --json --limit 1000 \
  --service <svc> --environment production

railway logs <deployment_id> --deployment --json \
  --lines 500 --filter '"<marker>"' \
  --since 2026-07-18T00:00:00Z --until 2026-07-25T00:00:00Z
```

- The deployment id is **positional**; omitting it silently defaults to the most
  recent successful deployment — the single-deployment blind spot of §1.
- `--deployment` selects the *application* log stream rather than build / http /
  network logs.
- `--lines` and `--limit` are always explicit, per §2 and §1.
- Re-confirm all of this against your installed CLI (`railway --version`) before
  trusting a run — the semantics above were derived in July 2026, and the
  retrieval limits, filter grammar and JSON shape are all version-dependent. The
  version installed on the dev machine at the time of writing was `railway
  5.28.1`; the operator run of 2026-07-25 did not record its own.
