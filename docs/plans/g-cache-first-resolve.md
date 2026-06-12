# g-cache-first-resolve — Make indexed-move analysis authoritative

## Context

Part of epic **g-stable-drill-best**. The bug surfaced in drill mode at 0cp strictness:
after `1.b4 e5` repeated attempts reported e4, a3, then Bb2 as the required best move.
Root cause for *this* child bead: for every indexed move the frontend starts local
Stockfish (worker) analysis **and** a cache lookup (`POST /api/analysis/lookup`)
concurrently, and `resolvedIndices` accepts whichever finishes first
(first-write-wins). The losing result is silently dropped. Because `bestMove`,
`eval_delta`, `classification`, `blunder`, and `recordable` all flow from this single
result into drill grading, regular-game classification/recording, SRS pass/fail,
session upload, and post-game display, the outcome can flip based on network/worker
timing.

This bead makes the **trusted cache result authoritative** and the local worker a
**buffered fallback**, so resolution no longer depends on completion order. It does
*not* change cache quality policy, thresholds, precompute, or worker reset commands
(those are other beads).

**Trust predicate (corrected):** `canResolveCachedAnalysis` (analysisUtils.ts:224) only
checks structural completeness. The backend now returns explicit quality metadata —
`source`, `analysis_profile_id`, `engine_version`, `engine_build`, `evidence_contract_id`,
and a computed **`authoritative: bool`** (`backend/app/api/analysis.py`,
`CachedAnalysisResult` / `_is_authoritative`, set true only when the row's identity
fields match an active authoritative profile). A structurally complete but
non-authoritative row (e.g. a `browser-game-v1` upload) must NOT override the worker.
So the trust gate is:
```
isTrustedCacheHit(cached) = cached.authoritative === true && canResolveCachedAnalysis(cached)
```
This requires extending the frontend `CachedAnalysis` type (`src/utils/api.ts:641`) with
the new fields (at minimum `authoritative`, plus the profile/engine/contract fields for
the diagnostic log).

The same first-write-wins logic exists in two places that must stay in lockstep:
- `src/services/GameAnalysisCoordinator.ts` (gameplay path; drill + regular game)
- `src/hooks/useMoveAnalysis.ts` (other analysis consumers; also owns variation/what-if)

## Current behavior (verified)

- `analyzeMove(...)` posts a worker `analyze-move` message **and**, for indexed moves,
  calls `scheduleCacheLookup` (debounced 150ms batch). Coordinator: lines 332–373.
- Worker `analysis` message → builds `AnalysisResult` → `resolveAnalysisResult` immediately
  (Coordinator 458–514). Cache flush → `resolveAnalysisResult` + `cancelWorkerAnalysis`
  (Coordinator 590–641).
- `resolvedIndices` Set is the only arbiter / idempotency guard; `resolveAnalysisResult`
  (542–575) early-returns if already resolved, then writes the store (`resolveAnalysis`,
  which also sets `lastAnalysis`), fans out to `analysisWaiters` (drill grading via
  `waitForAnalysis`), marks upload dirty, and calls `analysisResolvedListeners`.
  Note: AnalysisEffects (recording / SRS / blunder alert) does NOT subscribe to
  `analysisResolvedListeners`; it reads the store's `lastAnalysis` via a Zustand
  selector/effect (`AnalysisEffects.tsx:63,216`). The listener set is a separate channel.
- `useMoveAnalysis` mirrors this with `resolvedIndices` ref + `resolveAnalysis` (145–155,
  worker path 262–321, cache path 157–197). Variation analyses keyed by ply/FEN, not
  moveIndex — they are worker-only and out of scope.

## Design: buffer worker, let cache decide

Introduce an explicit per-moveIndex resolution state machine so the worker result is
**held** until the authoritative cache lookup settles. Keep `resolvedIndices` as the
terminal idempotency guard.

### New per-request state (both files) — request-scoped

All resolution state is keyed by moveIndex but **carries the owning requestId** so a
superseded/older request can never settle a newer one (Finding 2). A single map:
```
resolutionState: Map<number /*moveIndex*/, {
  requestId: string
  cacheStatus: 'pending' | 'released'
  releaseReason?: 'cache-miss' | 'untrusted' | 'cache-error' | 'timeout' | 'worker-error'
  bufferedWorker?: AnalysisResult     // worker done but cache still pending
  workerFailed?: boolean              // worker errored; awaiting cache (Finding 4)
  workerError?: string                // captured scoped-error text, used when rejecting waiters
  deadlineTimer?: ReturnType<typeof setTimeout>  // total-analysis hard deadline (Finding 2)
  cacheTimer?: ReturnType<typeof setTimeout>     // cache-response window, started at dispatch (Finding 5)
}>
```
Plus a `requestIdToMoveIndex: Map<string, number>` retained until lifecycle cleanup
(Finding G1), and a `requestId` field added to `AnalysisWaiter` (Finding G2). The hook
keeps the equivalents as refs.
**Every** event (worker `analysis`, worker `error`, cache hit, cache miss, cache `.catch`,
timeout) must look up `resolutionState.get(idx)` and bail unless `entry.requestId ===
incomingRequestId`. In the coordinator that requestId comes from `latestRequestIds`/the
worker message id; in the hook it must be added explicitly:

- **Hook gap (Finding 2):** `PendingCacheLookup` (`useMoveAnalysis.ts:49`) has no
  requestId and the hook has no latest-request guard at all. Add `requestId` to
  `PendingCacheLookup`, add a `latestRequestIds: Map<number,string>` ref, set it in
  `analyzeMove`, and gate every resolution on it — matching the coordinator's existing
  `latestRequestIds` contract (`GameAnalysisCoordinator.ts:345-353`).

**Two distinct deadlines (Findings 2 & 5).** A single timer started in `analyzeMove` does
NOT actually bound resolution, for two reasons:
- **Finding 5 (cache timer must not start before the lookup is sent).** The cache batch
  uses a *trailing* debounce that is reset on every new request
  (`GameAnalysisCoordinator.ts:584`), so under a sustained burst the batch dispatch keeps
  sliding while a per-request timer started in `analyzeMove` ticks down — older requests
  could be "released" before their lookup is even sent. Fix requires **BOTH (not either/or —
  Finding 1):** (a) the **cache-response timer starts only when the lookup is actually
  dispatched** (`flushCacheLookups`), not in `analyzeMove`; AND (b) a **mandatory maximum
  batch age** `CACHE_BATCH_MAX_AGE_MS` (propose **400ms**) caps the trailing debounce so a
  continuous burst cannot defer dispatch indefinitely — without it, dispatch (and therefore
  the cache timer) could be postponed until the 8s total deadline incorrectly fails a result
  the cache would have served. Implement (b) by recording the batch's first-enqueue timestamp
  and forcing a flush when `now - firstEnqueued >= CACHE_BATCH_MAX_AGE_MS` even if new
  requests keep arriving. Store this in `cacheBatchFirstEnqueuedAt` and set it **only when
  enqueuing into an empty batch**; **reset it to `null` every time the batch is emptied** —
  on dispatch in `flushCacheLookups` (after the `splice`) AND on any lifecycle cleanup that
  drops `pendingCacheLookups` (`startSession`/`clearSession`/`clearAnalysis`/`destroy`/fatal
  teardown). Otherwise a stale timestamp makes the *next* batch's first request look already
  aged and force-flush immediately (Finding 2). The cache timer governs only "cache didn't
  answer in time → release the worker fallback" (`releaseFallback(..., 'timeout')`).
- **Finding 2 (a stalled worker must still terminate, not hang forever).** `releaseFallback`
  with no buffered worker and no worker error leaves the entry `released` and
  `waitForAnalysis` pending **forever** if the worker never emits — contradicting the
  no-hang contract and SPEC.md:1011 ("Worker crash/timeout → skip evaluation for that move").
  So add a **separate total-analysis deadline** `ANALYSIS_TOTAL_DEADLINE_MS` (propose
  **8000ms**, started in `analyzeMove`) that, when it fires for a still-unresolved index,
  **terminates the request as `failed`**: rejects its `analysisWaiters` ("analysis timed
  out"), emits a `failed` outcome (so the recording frontier advances — never deadlocks),
  and clears `resolutionState`/meta. This is the real no-hang guarantee; the cache-response
  timer only chooses the source when results DO arrive.
  - **Queue-time policy (Finding 3, accepted).** The worker drains serially
    (`analysisWorker.ts:254`) and fast play intentionally queues requests (SPEC §6.4.6
    "evaluations queue; moves are processed in order"), so the 8s deadline **includes time
    spent waiting in the worker queue** — a deep backlog could cancel a still-queued request
    as `failed` before the engine ever starts it. We **accept this**: a `failed` outcome for
    a buried request is consistent with SPEC.md:1011 ("worker timeout → skip evaluation; do
    not flag as blunder"), and `failed` advances the frontier rather than mis-recording. The
    8s value is chosen to comfortably exceed a normal backlog at realistic move cadence. Add
    a **backlog test** (test 10d): enqueue many moves so later indices sit in the queue past
    the deadline → those indices terminate `failed` (skip), earlier indices resolve normally,
    and nothing is mis-recorded or hangs. (A future split of queue-wait vs active-analysis
    deadlines is out of scope for this bead.)

Constants: `ANALYSIS_RESOLUTION_TIMEOUT_MS` (cache-response, propose **2500ms**, started at
dispatch) and `ANALYSIS_TOTAL_DEADLINE_MS` (hard terminate, propose **8000ms**, started at
`analyzeMove`) — both comfortably above the 150ms debounce + a normal lookup/worker round
trip, below anything that would let drill grading (`waitForAnalysis`) hang noticeably.

### Worker message protocol change (Finding R1 — prerequisite for request-scoping)

The worker `error` message has no request id (`analysisMessages.ts:42` — `{ type:
'error'; error: string }`), so a per-request worker failure cannot be attributed to a
moveIndex. Change:
- Add an **optional** `id?: string` to the `error` message type.
- In `analysisWorker.ts` the per-request failure path (`drainQueue` `.catch`, ~`:281`)
  has `request` in scope — emit `{ type: 'error', id: request.id, error }`.
- **Engine/bootstrap/fatal failures stay unscoped** (no `id`) and remain global-fatal.
- Consumers branch on `message.id`: present → request-scoped (rule 6 below); absent →
  today's global error path (set store status `error`, reject all waiters).

Every rule below first resolves `entry = resolutionState.get(idx)` and **returns unless
`entry?.requestId === incomingRequestId`** (Finding 2), and is a no-op if
`resolvedIndices.has(idx)` (terminal idempotency guard). **Cache callbacks (hit, miss,
`.catch`) additionally require `entry.cacheStatus === 'pending'`** (Finding R3): once a
timeout has flipped the entry to `'released'`, a late authoritative cache hit must NOT
win — the released worker fallback owns the resolution. (A late trusted hit arriving
before the worker finishes simply does nothing; the worker result resolves per rule 2.)

1. **Schedule (`analyzeMove`, indexed):** create/replace `resolutionState[idx] =
   {requestId: id, cacheStatus: 'pending', deadlineTimer}` where `deadlineTimer` fires the
   **total-analysis deadline** (Finding 2) → `failRequest(idx, id, 'deadline')`. The
   **cache-response timer is NOT started here** (Finding 5) — it is started in
   `flushCacheLookups` when the batch is dispatched. Replacing supersedes the prior request:
   clear both its timers first. (Worker + cache lookup both fire as today.)

2. **Worker `analysis` arrives (indexed, id matches):**
   - if `entry.cacheStatus === 'pending'` → store `entry.bufferedWorker = result`; do NOT
     resolve. Still call `clearActiveAnalysisStateIfCurrent(id)` so the spinner clears.
     **Do NOT delete `pendingMeta`/`pendingMoveIndices` yet** (Finding 3 — see below).
   - else (`released`) → `resolveAnalysisResult(idx, result)` (terminal; clears entry/meta).

3. **Cache flush result (per move in batch, id matches latest):**
   - **trusted hit** = `isTrustedCacheHit(cached)` (`authoritative && canResolveCachedAnalysis`):
     `resolveAnalysisResult(idx, cacheResult)`, then `cancelWorkerAnalysis(id)`,
     `clearActiveAnalysisStateIfCurrent(id)`, delete entry + meta. Cache wins; buffered
     worker discarded. **This path also recovers a worker-failed request** (Finding 4).
   - **miss / no row / not trusted (`authoritative === false` or incomplete)** →
     `releaseFallback(idx, id, 'untrusted'|'cache-miss')`.

4. **Cache lookup `.catch` (network/error)** → for every still-pending move in the batch
   whose id still matches, `releaseFallback(idx, id, 'cache-error')`. (Today's `.catch`
   is a no-op comment — this is the fix that stops a hung/failed lookup from stranding a
   buffered worker result, or rejecting `waitForAnalysis` forever.)

5. **Timeout fires** → `releaseFallback(idx, id, 'timeout')`.

6. **Worker `error` (Findings 4, R1, R2):** the current handler rejects *all* waiters and
   sets store status `error` immediately (`GameAnalysisCoordinator.ts:515-524`). New
   behavior, branching on whether the message carries an `id`:
   - **Scoped error** (`message.id` present, matches the entry): set `entry.workerFailed =
     true`, **capture `entry.workerError = message.error`** (so the later reject carries the
     real text — Finding 1), and **DO NOT set global store status `error`** (Finding R2 — otherwise
     `waitForAnalysis` would reject everything via the `status === 'error'` check at `:393`,
     defeating cache recovery). Then branch on `cacheStatus`:
     - **`cacheStatus === 'pending'`** → DO NOT reject yet; a trusted cache hit (rule 3) can
       still resolve this move. Waiters are rejected only when its cache settles non-trusted
       (rule 7's `workerFailed` branch).
     - **`cacheStatus === 'released'`** (cache already missed/released the fallback — the
       reverse order, **Finding 2**) → **fail immediately**: there is no buffered worker and
       none is coming, so call `failRequest(idx, id, 'worker-error', message.error)` now
       (reject waiters with `message.error`, emit `failed`, clear state) **rather than waiting
       for the 8s total deadline**. (Rule 7
       only re-fires on a cache settle, which has already happened, so the error path must
       finalize here.) Add the reverse-order test (cache-miss-first → scoped worker error →
       immediate `failed`, no deadline wait).
   - **Unscoped/fatal error** (no `id` — engine/bootstrap, or `handleWorkerError`):
     set store status `error`, reject all waiters, **and fully tear down resolution state
     (Finding G3):** `clearTimeout` **both `entry.deadlineTimer` and `entry.cacheTimer`** for
     every entry (Finding 4 — never just one), drop all `resolutionState`
     entries, buffered worker results, `pendingMoveIndices`/`pendingMeta`,
     `requestIdToMoveIndex`, and the `pendingCacheLookups` batch + `cacheFlushTimer`.
     Otherwise an in-flight cache `.then` could resolve a move and `setLastAnalysis`
     *after* consumers were told analysis failed. Because the cache callback re-checks
     `resolutionState.get(idx)` (now empty) and `cacheStatus === 'pending'`, a late hit
     becomes a no-op. (The captured-`gen` guard already discards it across a session
     change; this covers a fatal error within the same session.)
     - **Worker epoch (Finding F1):** clearing `requestIdToMoveIndex` while the worker
       listener is still live re-opens the late-worker bug — a queued `analysis` becomes
       "non-indexed" and overwrites `lastAnalysis`. So fatal cleanup must also
       **invalidate the worker**: in the coordinator call `terminateWorker()` (removes the
       message listener; recovery is the existing `restartAnalysisWorker`). As a uniform
       belt-and-suspenders that also covers the per-mount hook worker, **drop every worker
       `analysis`/`analysis-started`/`analysis-streaming` message when store
       `status === 'error'`** until an explicit restart clears it. Test fatal error
       followed by late worker **start / stream / result** (not only late cache).
   - **Scoped variation error (hook, Finding G4):** a scoped `error` whose id is in
     `pendingVariationPlies` clears just that variation's streaming state and map entry —
     it must not trip the fatal global path.

7. **`releaseFallback(idx, id, reason)`** (idempotent, id-guarded; reason ∈
   `cache-miss|untrusted|cache-error|timeout`): clear+delete the cache-response timer,
   set `cacheStatus='released'`, `releaseReason=reason`. **Does NOT clear the total-analysis
   deadline timer** (Finding 2) — that still guards a never-arriving worker.
   - if `bufferedWorker` present → `resolveAnalysisResult(idx, bufferedWorker)` (terminal;
     also clears the deadline timer).
   - else if `workerFailed` → reject this move's `analysisWaiters` with `entry.workerError`
     (the captured scoped-error text — Finding 1), emit a `failed` outcome, delete entry +
     meta + deadline timer (no result will ever come).
   - else → leave `released`; rule 2 resolves the worker result when it arrives. **If it
     never arrives, the total-analysis deadline (rule 8) terminates the request.**

8. **`failRequest(idx, id, reason, errorText?)`** — `reason ∈ 'deadline' | 'worker-error'`
   (widen beyond `'deadline'`; it is also called from rule 6's released-then-error path —
   Finding 1). The hard no-hang stop. If the index is still unresolved under this id: reject
   its `analysisWaiters` with `errorText ?? "analysis timed out"`, emit a `failed` outcome
   (frontier advances — never deadlocks),
   `cancelWorkerAnalysis(id)`/`clearActiveAnalysisStateIfCurrent(id)`, and delete entry +
   meta + both timers. Mirrors SPEC.md:1011 (worker timeout → skip evaluation; do not flag).

**Finding R5 — gate late `analysis-started`/`analysis-streaming` by request state.**
After a trusted cache hit cancels a worker, an already-queued `analysis-started`
(`:438`, currently unconditional) or `analysis-streaming` message can still arrive and
re-set `isAnalyzing`/`analyzingMove`, leaving a stuck spinner. Gate `analysis-started` so
it only sets active state when its `id` is still the latest request for an UNRESOLVED
index (mirror the guard `analysis-streaming` already uses at `:444-449`:
`latestRequestIds.get(idx) === id && !resolvedIndices.has(idx)`). For a cache-resolved or
canceled request, drop the message. **Hook variation exception (Finding G4):** in
`useMoveAnalysis`, non-indexed variation requests are tracked in `pendingVariationPlies`,
not `resolutionState`. The new indexed gate must NOT suppress them — accept
`analysis-started`/`streaming` when `message.id` is in `pendingVariationPlies` (preserving
today's variation UI). Order the handler: indexed-guarded path → variation path →
otherwise drop.

**Finding 3 — keep metadata until terminal resolution.** Today the worker `analysis`
handler deletes `pendingMoveIndices`/`pendingMeta` (`GameAnalysisCoordinator.ts:461-466`),
which would make a later `waitForAnalysis` reject (`:396-402`) even though the result is
buffered. Fix: only delete those maps at **terminal** resolution
(`resolveAnalysisResult`, or `releaseFallback`'s reject branch). While buffered, the
request stays "pending" so `waitForAnalysis` registers a waiter that the eventual
`resolveAnalysisResult` fulfills. (Alternatively, teach `waitForAnalysis` to consult
`resolutionState` — keeping metadata is the smaller change and is preferred.)

**Finding G1 — settled-request tombstone (late worker after a cache win must not become
non-indexed).** When a trusted cache hit resolves and we delete `pendingMoveIndices`, a
later worker `analysis` for that same requestId has `moveIndex === undefined` and falls
into the non-indexed branch (`:502-505` / hook `:309-313`), which calls `setLastAnalysis`
and **clobbers `lastAnalysis`** with the discarded worker result. Fix: keep a
`requestIdToMoveIndex: Map<string, number>` (or a `settledRequestIds: Set<string>`)
populated for every indexed request and retained until lifecycle cleanup. In the worker
`analysis`/`analysis-started`/`streaming` handlers, before the non-indexed fallback,
check this map: if the id belongs to a known (now settled) index, **drop the message** —
never treat a known-indexed request as non-indexed. Clear the map in the same lifecycle
methods that clear `resolutionState`. (A canceled worker normally won't emit, but the
message can already be in the queue when the cancel posts.)

**Finding G2 — bind waiters to requestId.** `AnalysisWaiter` (`:49-53`) carries only
`generation`; on a superseded index an old waiter would resolve with the NEW request's
result, double-firing gameplay continuation (drill grade / `waitForAnalysis` callers at
`ChessGame.tsx:686,1006`). Add `requestId` to `AnalysisWaiter`, captured from
`latestRequestIds.get(moveIndex)` when `waitForAnalysis` registers. In
`resolveAnalysisResult` (and the reject paths), only resolve waiters whose
`requestId === result.id` **and** `generation === sessionGeneration`; reject the rest with
a "superseded" error. This also means a waiter registered against an already-superseded
request rejects instead of silently attaching.

**Finding F2 — reject superseded waiters immediately.** In `analyzeMove`'s supersession
branch (`:344-353`, where `previousRequestId` is canceled), do not wait for the
replacement to resolve — **immediately reject any `analysisWaiters` for that moveIndex
whose `requestId === previousRequestId`** with the "superseded" error. Otherwise a caller
awaiting the old request hangs until timeout if the replacement stalls. (The replacement's
own callers register fresh waiters against the new requestId.)

`resolveAnalysisResult` / `resolveAnalysis` stay the single publication point: it writes
the store (`resolveAnalysis` also sets `lastAnalysis`, store:61), fans out to
`analysisWaiters` and emits a `resolved` **outcome** (the outcome channel **replaces** the
old `analysisResolvedListeners`), and marks upload dirty — exactly once. So no downstream
consumer can observe both sources.

### Cleanup & invalidation (Finding 5)

Map clearing alone is insufficient: an in-flight cache lookup promise can resolve *after*
teardown and mutate the shared store. Both call sites already snapshot a generation/gen
for this — reuse and extend it:

- **Coordinator:** cache `.then`/`.catch` already capture `gen = sessionGeneration` and
  bail on mismatch (`:596-602`). Extend that same guard to the new release/timeout paths,
  and in every lifecycle method (`startSession`, `clearSession`, `clearAnalysis`,
  `destroy`, **and `restartAnalysisWorker`** — omitted from the original plan) clear
  `resolutionState` and `clearTimeout` **both `entry.deadlineTimer` and `entry.cacheTimer`**
  on every entry (Finding 4).
- **Restart/teardown orphans every in-flight request, not just `workerFailed` (Finding
  R4):** `restartAnalysisWorker` (`:375`), `clearAnalysis`, and `destroy` terminate the
  worker, so EVERY unresolved request loses its worker — buffered, pending, and
  `workerFailed` alike. Each must **reject the `analysisWaiters` of every unresolved
  index** (with a clear "analysis worker restarted/cleared" error) and clear
  `resolutionState`/meta, rather than leaving waiters to hang until timeout. (Resubmission
  is out of scope; callers re-request on the next move.) `startSession`/`clearSession`
  already call `rejectAnalysisWaiters` — extend that to also drop the new state.
- **Hook:** add a `mountTokenRef`/generation counter incremented on unmount (the effect
  cleanup at `:355` only terminates the worker today). Every async cache callback captures
  the token at schedule time and **no-ops if it changed**, preventing post-unmount store
  mutation. `clearAnalysis` clears the new maps and timers.
- **Superseded indices:** `analyzeMove` replacing `resolutionState[idx]` must
  `clearTimeout` **both** the old entry's `deadlineTimer` and `cacheTimer` (Finding 4) and
  drop its buffer before re-scheduling (already cancels the prior worker requestId in the
  coordinator).

### Notes specific to useMoveAnalysis

Same state machine via `useRef` maps, plus the new `latestRequestIds` ref and mount token
(Findings 2 & 5). The worker `analysis` handler already branches on `moveIndex !==
undefined`; the variation (ply/FEN) branch is untouched and stays worker-only. The hook
has no `cancelWorkerAnalysis`/waiter machinery, so "cache wins" is just: skip the buffered/
incoming worker result. Keep the precedence contract identical to the coordinator.

**Finding R6 — preserve request identity.** The hook's `fromCachedAnalysis`
(`:88-125`) currently calls `createRequestId()` for the result `id`. Change it to accept
the originating request id (now available on `PendingCacheLookup`) so the cache result's
`id` matches its request, consistent with the coordinator's `fromCachedAnalysis`
(which already takes `requestId`).

## Files to modify

- `src/utils/api.ts` — extend `CachedAnalysis` (`:641`) with `authoritative` (+ `source`,
  `analysis_profile_id`, `engine_version`, `engine_build`, `evidence_contract_id`). Backend
  already returns these (`backend/app/api/analysis.py` `CachedAnalysisResult`).
- `src/workers/analysisMessages.ts` — add optional `id?: string` to the `error` message.
- `src/workers/analysisWorker.ts` — emit `id: request.id` on the per-request failure path
  (~`:281`); leave engine/bootstrap failures unscoped.
- `src/workers/analysisUtils.ts` — add `isTrustedCacheHit(cached)` =
  `authoritative === true && canResolveCachedAnalysis(cached)` (keep
  `canResolveCachedAnalysis` as the structural sub-check).
- `src/services/GameAnalysisCoordinator.ts` — primary; rules 1–7, metadata-retention fix,
  worker-error change, lifecycle/`restartAnalysisWorker` cleanup.
- `src/hooks/useMoveAnalysis.ts` — mirror precedence contract; add `latestRequestIds` ref +
  mount token; requestId on `PendingCacheLookup`.
- `src/services/GameAnalysisCoordinator.ts` — adds the **total-analysis deadline**
  (`failRequest`, Finding 2) and the **dispatch-time cache-response timer** (Finding 5) for
  the state machine. (The `AnalysisOutcome` channel / reset channel / `getEpoch` /
  `pruneFromMoveIndex` / lineage map / `committedDecisionIndex` are added by **g-hpw4**,
  hooking into the `analyzeMove`/`resolveAnalysisResult`/`releaseFallback`/`failRequest`
  methods built here.)
- Tests: `GameAnalysisCoordinator.test.ts`, `useMoveAnalysis.test.ts`,
  `analysisWorker.test.ts`, `analysisUtils.test.ts`.

No new dependencies. Reuse existing `fromCachedAnalysis`, `resolveAnalysisResult`,
`cancelWorkerAnalysis`, `clearActiveAnalysisStateIfCurrent`.

## Split-off scope (dependent beads)

The following sat in earlier revisions of this plan and have been split out into dependent
beads (both blocked by this one):

- **g-86pd** — *Free variation pending state on analysis failure (F3)*: the
  `useVariationTree.rejectPending` + `onVariationError` reject channel. See
  `docs/plans/g-variation-reject.md`. (The scoped-variation-error and fatal-teardown hooks
  it attaches to are built here.)
- **g-hpw4** — *Exactly-once context-correct recording & SRS via outcome channel (F4)*: the
  `AnalysisOutcome` channel, terminal-aware recording frontier, SRS-immediate consumer,
  microtask alert coalescing, `BlunderContext` keying, `pruneFromMoveIndex`, lineage map,
  and `committedDecisionIndex`. See `docs/plans/g-outcome-recording.md`. This bead exposes
  the emission *points* (`analyzeMove`/`resolveAnalysisResult`/`releaseFallback`/`failRequest`)
  but does NOT build the channel or the React consumer.
- **g-h94q** — durable decision-owner / reducer / outbox / SRS ordering / idempotency /
  backend migration / unmount durability (blocked by g-hpw4).

## Diagnostics

Add a single concise log at each resolution noting moveIndex + chosen source + decision
path + profile, e.g. `[Analyst] resolve idx=3 source=cache(authoritative,
profile=linux-sf18-d24)`, `source=worker(cache-miss)`, `source=worker(untrusted
profile=browser-game-v1)`, `source=worker(timeout)`, `source=worker(cache-error)`,
`source=worker(worker-error)`. Reuse the existing console.log style and the new
`analysis_profile_id`/`authoritative` fields.

## Test plan (acceptance criteria 1–7)

Add tests that drive both completion orders by controlling when the mocked
`lookupAnalysisCache` promise resolves vs when the worker `analysis` message is posted:

1. **Worker-first, then trusted cache hit** → only the cache result is published
   (assert store/`resolveAnalysis` called once with cache values; `cancel-analysis`
   posted to worker).
2. **Worker-first, then cache miss / not-`canResolveCachedAnalysis` / lookup rejects /
   timeout** → only the worker result is published (use fake timers for the timeout case).
3. **Authoritative gate (Finding 1):** structurally-complete-but-`authoritative:false`
   row → treated as a miss; the worker fallback is published, NOT the cache row.
   `authoritative:true` complete row → cache wins.
4. **Cache-first (trusted) then late worker `analysis`** → cache wins, late worker message
   is a no-op (id mismatch / already resolved).
5. **Stale/superseded (Finding 2):** re-`analyzeMove` same idx with a new requestId before
   resolution; the OLD request's worker/cache hit/miss/error/timeout must not settle the
   new request. Add the matching hook test (requestId guard + `latestRequestIds`).
6. **Worker-error recovery (Finding 4):** worker errors while cache pending → (a) trusted
   cache hit still resolves the move and drill grading; (b) cache miss/error/timeout
   rejects that move's `waitForAnalysis` and cleans up.
7. **`waitForAnalysis` after buffered worker (Finding 3):** worker completes (buffered) →
   call `waitForAnalysis(idx)` → must register and later resolve (not reject) once cache
   settles; assert no `'Analysis is not pending'` rejection.
8. **Lifecycle/invalidation (Finding 5):** `startSession` / `clearSession` /
   `clearAnalysis` / `destroy` / `restartAnalysisWorker` clear state and timers; a cache
   promise resolving after the hook unmounts (stale mount token) does NOT mutate the store.
9. **Drill grading (`waitForAnalysis`)** resolves exactly once with the authoritative
   result regardless of completion order, including the timeout-fallback path (no hang).
10. **Timeout → late authoritative hit → worker (Finding R3):** fake-timers; the
    cache-response timer fires `releaseFallback` first, then a trusted cache hit arrives — it
    must be ignored (`cacheStatus === 'released'` guard); the worker result resolves the move.
10b. **Total-analysis deadline terminates a stalled worker (Finding 2):** fake-timers; cache
    misses (releaseFallback, `released`) AND the worker NEVER emits → at
    `ANALYSIS_TOTAL_DEADLINE_MS`, `failRequest` rejects `waitForAnalysis` ("analysis timed
    out"), emits a `failed` outcome (frontier advances), and clears state — no hang
    (SPEC.md:1011). Assert the deadline timer also clears on normal terminal resolution.
10c. **Cache-response timer starts at dispatch (Finding 5):** under a sustained burst where
    the trailing debounce keeps resetting, an older request is NOT released before its batch
    is dispatched; the cache-response window begins when `flushCacheLookups` sends the lookup.
10c-2. **Mandatory max batch age (Finding 1):** with `analyzeMove` called continuously faster
    than `CACHE_LOOKUP_DEBOUNCE_MS`, the batch is STILL force-flushed at
    `CACHE_BATCH_MAX_AGE_MS` (first-enqueue age) — assert dispatch happens (and the cache
    timer starts) even though the trailing debounce never settles, so a buffered result is
    not failed by the total deadline.
10c-3. **Consecutive batches reset the age clock (Finding 2):** dispatch one batch (which
    resets `cacheBatchFirstEnqueuedAt` to null), then enqueue a second batch — assert the
    second batch is NOT force-flushed immediately but waits its own debounce/max-age window;
    likewise a lifecycle cleanup that empties the batch resets the timestamp.
10d. **Worker-queue backlog (Finding 3, accepted policy):** enqueue many moves so later
    indices sit in the serial worker queue past `ANALYSIS_TOTAL_DEADLINE_MS` → those indices
    terminate `failed` (skip), earlier indices resolve normally; nothing is mis-recorded and
    nothing hangs.
10e. **Cache-miss-first then scoped worker error (Finding 2, reverse order):** cache misses
    (`releaseFallback` → `released`, no buffered worker), THEN a scoped worker `error`
    arrives → `failRequest` fires **immediately** (reject waiters + `failed` outcome), NOT
    after the 8s deadline.
10f. **Both timers cleared (Finding 4):** on terminal resolution, supersession, fatal
    teardown, and every lifecycle clear, assert BOTH `deadlineTimer` and `cacheTimer` are
    cleared (no leaked timer fires after the entry is gone).
11. **Late `analysis-started`/`analysis-streaming` after cache resolution (Finding R5):**
    cache-first resolves + cancels, then a queued `analysis-started`/streaming for the
    same id arrives — `isAnalyzing`/`analyzingMove`/`streamingEval` must NOT be re-set.
12. **Restart/clear orphans (Finding R4):** with unresolved requests pending,
    `restartAnalysisWorker` / `clearAnalysis` / `destroy` reject all their `waitForAnalysis`
    promises (no hang) and clear state/timers.
13. **Scoped vs fatal worker error (Findings R1, R2):** a worker `error` WITH `id` while
    cache pending does NOT set store status `error` and lets a later trusted hit resolve;
    an `error` WITHOUT `id` sets status `error` and rejects all waiters.
15. **Late worker after cache win stays indexed (Finding G1):** trusted cache resolves idx,
    then a late worker `analysis` for that requestId arrives — it must be dropped, NOT
    written to `lastAnalysis` via the non-indexed branch.
16. **Superseded waiter (Finding G2):** register `waitForAnalysis(idx)`, then
    `analyzeMove` the same idx (new requestId) and resolve it — the OLD waiter rejects
    (superseded), only the new request's caller resolves; assert gameplay continuation
    fires once.
17. **Fatal error then late cache hit (Finding G3):** unscoped worker `error` rejects
    waiters and tears down state; a cache `.then` resolving afterward must NOT call
    `setLastAnalysis`/`resolveAnalysis`.
17b. **Fatal error then late WORKER start/stream/result (Finding F1):** after an unscoped
    error, queued worker `analysis-started`/`streaming`/`analysis` messages are dropped
    (`status === 'error'` guard) and never overwrite `lastAnalysis` or re-set the spinner.
17c. **Superseded waiter rejects immediately (Finding F2):** `waitForAnalysis(idx)`, then
    `analyzeMove(idx)` supersedes — the old promise rejects synchronously at supersession,
    without waiting for the new request.
18. **Variation UI preserved (Finding G4, hook):** a variation (`pendingVariationPlies`)
    `analysis-started`/`streaming` still sets variation streaming state; a scoped
    variation `error` clears only that variation, not global status.

Run targeted (pre-push suite is flaky under parallel load — per project memory, rerun
named files in isolation before trusting a failure):
```
npx vitest run src/services/GameAnalysisCoordinator.test.ts
npx vitest run src/hooks/useMoveAnalysis.test.ts
npx vitest run src/workers/analysisWorker.test.ts
npx vitest run src/workers/analysisUtils.test.ts
```
(Adjust paths to whichever of these suites exist; the worker and analysisUtils suites are
touched by the protocol change and `isTrustedCacheHit`. The AnalysisEffects / outcome-channel
recording-frontier tests live in g-hpw4; the variation reject-channel tests in g-86pd.)

## Out of scope (other beads)

Cache quality/profile metadata & comparator (g-guard-cache-writes, done), canonical
precompute (g-canonical-precomp, done), threshold/grading semantics
(g-deterministic-grade), cache repair (g-repair-drill-cache), and per-position worker
engine reset.

**Durable decision-owner work → g-h94q (dependent bead).** Relocating recording/SRS decision
state to a coordinator-lifetime owner, the synchronous-reducer + async-idempotent-outbox
split, SRS write *ordering* (apply `pass_streak` in logical order, not request-arrival
order — serialize by `blunder_id` or recompute from `BlunderReview` history), per-decision
idempotency IDs, durable (IndexedDB) outbox storage + retry/backoff + 4xx-terminal policy,
the backend unique-constraint change (`models.py` + Alembic migration on `srs.py`/blunder
endpoint), and the unmount-durability/`isReplay` semantics all live in g-h94q. This bead keeps
recording/SRS correct **while a UI is mounted** using the existing `shouldRecordBlunder` +
`recordBlunder`/SRS APIs; g-h94q makes it durable across unmount and crash-safe.
