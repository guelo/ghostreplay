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
- `src/hooks/useVariationTree.ts` — add `rejectPending(id)` (Finding F3).
- `src/components/AnalysisBoard.tsx` — wire `onVariationError` → `rejectPending` (F3).
- `src/components/chess-game/AnalysisEffects.tsx` — subscribe to `addAnalysisOutcomeListener`
  (React): SRS immediate per `resolved`, recording via the terminal-aware frontier (using the
  existing `shouldRecordBlunder` + `recordBlunder`), blunder alert/audio via microtask
  coalescing; seed generation from `getEpoch()` and drop stale-generation outcomes; subscribe
  to `addAnalysisResetListener` for synchronous UI reset (F4/H1/H3/I/J/K/M3). Decision state
  stays in component refs as today.
- `src/services/GameAnalysisCoordinator.ts` — adds the `AnalysisOutcome` channel
  (`addAnalysisOutcomeListener`/`emitOutcome`/`markSkipped`) replacing
  `addAnalysisResolvedListener`, the synchronous reset channel
  (`addAnalysisResetListener`/`emitReset`, called in `startSession`/`clearSession`), the
  `getEpoch()` snapshot (M3), `pruneFromMoveIndex(k)` (M1), the `lastRequestIdByMoveIndex`
  lineage map (L3), the monotonic `committedDecisionIndex` boundary (M2), the **total-analysis
  deadline** (`failRequest`, Finding 2), and the **dispatch-time cache-response timer**
  (Finding 5).
- **DEFERRED to g-h94q (NOT this bead):** relocating `BlunderContext`/pending-SRS/
  `blunderRecorded`/frontier off `ChessGame.tsx` onto a coordinator-lifetime owner; the
  synchronous-reducer + async-idempotent-outbox split; SRS write *ordering* + per-decision
  idempotency IDs + durable (IndexedDB) outbox storage + retry/4xx policy; the
  `backend/app/api/srs.py` + blunder-endpoint idempotency change (which also needs `models.py`
  + an Alembic migration — Finding 6a; note it is the blunder endpoint, NOT `api/analysis.py`).
- `src/hooks/useChessGameController.ts` — register `BlunderContext` + pending SRS keyed by
  `committed.analysisId` (H2); mint a stable `srsDecisionId` at registration that survives
  request-id retries (Finding 3 — used by g-h94q's idempotency, harmless to add now).
- `src/hooks/useChessGameLifecycle.ts` — prune-on-revert and clear-on-reset of the context
  map (H2); call `coordinator.pruneFromMoveIndex(newHistory.length)` synchronously inside
  `rewindBoardLocally` next to `prunePendingSrsReviewsFromMoveIndex` (M1).
- Tests: `GameAnalysisCoordinator.test.ts`, `useMoveAnalysis.test.ts`,
  `useVariationTree.test.ts`, `AnalysisBoard` test, `useChessGameController.test.ts`,
  `useChessGameLifecycle.test.ts`, a `ChessGame`/AnalysisEffects gameplay test (see below).

No new dependencies. Reuse existing `fromCachedAnalysis`, `resolveAnalysisResult`,
`cancelWorkerAnalysis`, `clearActiveAnalysisStateIfCurrent`.

## Adjacent consumer fixes (required for correctness)

### Finding F3 — variation failures must free `useVariationTree` pending state

On a scoped variation error (or fatal teardown) the hook clears `pendingVariationPlies`,
but `useVariationTree.pendingRequestsRef` (`:195-205`) still holds the entry, so
`hasPendingForFen` (AnalysisBoard `:1056`) permanently blocks retrying that FEN. Add a
**reject channel**:
- Add `rejectPending(requestId)` to `useVariationTree` — deletes from `pendingRequestsRef`
  and bumps `analysisCacheVersion` so `hasPendingForFen` re-evaluates.
- Give `useMoveAnalysis` an optional `onVariationError?(id: string)` callback. Invoke it
  for any `pendingVariationPlies` id on a scoped variation error AND for every still-pending
  variation id during fatal teardown / `clearAnalysis` / unmount. AnalysisBoard wires it to
  `rejectPending` (parallel to how it wires `resolvePending` via `lastAnalysis` at `:971`).
- Variation resolution still flows through `lastAnalysis` → AnalysisBoard `:971`
  `resolvePending`; only the failure path is new.

### Finding F4 — regular-game recording/SRS must be exactly-once and context-correct

Recording is a React effect over the single `lastAnalysis` value
(`AnalysisEffects.tsx:75`) matched against one mutable `pendingAnalysisContextRef`
(`useChessGameController.ts:237`). When a **cache batch resolves two indices
synchronously**, `lastAnalysis` is overwritten twice before React re-runs the effect, so
an intermediate player blunder can be skipped, and the surviving `lastAnalysis` can be
paired with the wrong move's context (the ref holds only the latest committed move).

Three consumers with different ordering needs — keep them separate:

**(a) SRS — immediate, request-targeted, NOT behind the frontier (Finding I3).**
`processSrsReview` (`:118-135`) is already keyed by `analysis.id` and self-validates
`pendingReview.moveIndex === analysis.moveIndex`, so it is inherently exactly-once and
move-correct. Keep it driven directly off the synchronous subscription per resolved result
(the existing subscribe at `:217`-area), processing each result immediately. Do not delay
it behind unrelated earlier analyses — that only adds latency and couples it to frontier
correctness.

**(b) Recording / first-blunder — terminal-aware ordered frontier (Findings H1, I1).**
"First recordable blunder per session" is a move-order decision, so it needs ordering —
but a *result-only* contiguous map **deadlocks** on any index that terminates without an
`AnalysisResult`: a scoped worker failure + cache miss, fatal cleanup, supersession, or
`analyzeMove()` returning `undefined` (controller then mints a synthetic
`analysis-{idx}-{uci}` id at `useChessGameController.ts:152` for which no request ever
resolves). Each frontier slot holds `{ requestId, status }` where status is
`pending | resolved | failed | skipped`. **`nextDecisionIndex` advances across every
TERMINAL status (`resolved|failed|skipped`)**, blocking only on `pending` slots. Only
`resolved` slots are blunder candidates; the others just unblock the frontier.

**Preserve the existing eligibility helper (Finding 3).** The recording decision for a
`resolved` candidate MUST run through the existing `shouldRecordBlunder` helper
(`src/utils/blunder.ts:56`) with the snapshotted context — do NOT re-implement its guards.
It already enforces: `analysis.recordable`, active+owned session (`isGameActive`), the
first-blunder-per-session flag (`alreadyRecorded`/`blunderRecordedRef`), a stored context,
the move-zero exclusion (`context.moveIndex === 0`), the 10-full-move recording cap
(`isWithinRecordingMoveCap`), and move/context UCI matching (`analysis.move ===
context.moveUci`). The frontier only decides *ordering and which candidate is first*; the
helper decides *eligibility*. **Practice-continuation (Finding 5):** today the effective
flag passed as `isGameActive` is `isGameActive && !isPracticeContinuation`
(`AnalysisEffects.tsx:81`, reading `useGameStore`). This composite MUST be **snapshotted into
the frontier slot at the time the candidate is enqueued** (the value can change before the
deferred/out-of-order decision runs) and passed unchanged to `shouldRecordBlunder` — do NOT
read the live store at decision time, and do NOT drop the `!isPracticeContinuation` term.
Snapshot and test **every** `shouldRecordBlunder` input (including this composite) rather than
dropping any.

- **Supersession is request-terminal, not index-terminal (Finding L1).** A slot keyed by
  moveIndex must NOT advance when the old request is superseded by a replacement *still
  pending for that same index* (otherwise: index 1 superseded, index 2 resolves → frontier
  wrongly drains past 1 while replacement-1 is in flight). So **supersession is modeled as
  a non-terminal `scheduled` transition, not a `superseded` terminal.** `analyzeMove`
  (indexed) emits a **`scheduled`** outcome `{ moveIndex, requestId, previousRequestId? }`
  before the worker/cache fire (`previousRequestId` comes from the dedicated lineage map
  below, NOT `latestRequestIds`). The consumer **(re)opens the slot to
  `pending` under the new `requestId`**, migrates context (below), and — if
  `nextDecisionIndex` had already advanced past this moveIndex — **rewinds it back to this
  index** so the replacement's eventual result is reconsidered (respecting
  `blunderRecordedRef` idempotency). The terminal `superseded` status is removed entirely;
  waiter rejection on supersession (Findings F2/G2) is unchanged and independent.
- **Outcome channel:** the coordinator publishes a typed outcome (see *Outcome channel
  API* below). Frontier (re)opens on `scheduled` and advances on `resolved|failed|skipped`.
- **Monotonic decision-commit boundary (Finding M2).** A `scheduled` rewind of
  `nextDecisionIndex` is safe for *display/annotation*, but the **first-blunder recording
  side effect is irreversible** (`POST /api/blunder`). So recording uses a SEPARATE
  **monotonic `committedDecisionIndex`** that only ever increases: once the recording
  decision for an index is *committed* (recorded, or definitively passed-over), that
  boundary advances and **never reopens**. Rule: `scheduled` may rewind the *display*
  frontier, but a replacement whose `moveIndex < committedDecisionIndex` **cannot trigger a
  new recording effect** — it only refreshes display/annotation. **SRS is NOT gated by this
  boundary** (Finding N2): SRS is request-targeted, immediate, and independently
  exactly-once via its own `id`+`moveIndex` self-validation; `committedDecisionIndex` is
  recording-only.
  Concretely this bounds the failure→retry race: if index 1 `failed`, index 3 was recorded
  (`committedDecisionIndex` past 3), then index 1 is retried and comes back recordable, the
  index-3 backend record **stands** and index 1 does not retroactively re-record. This is
  an accepted, documented limitation (drill-steering retry is rare and `blunderRecordedRef`
  is first-only per session). Add this exact test (index 1 fail → index 3 recorded → index 1
  retried recordable → assert no second or changed backend record).
- **Context snapshot + retention (Findings J1, L2).** A `resolved` result may sit buffered
  behind an earlier `pending` slot, so its decision runs later. **Snapshot the
  `BlunderContext` into the frontier slot at resolution time** (the `resolved` outcome
  carries `result`; pair it with its context immediately), then the `resolved` move's
  `requestId`→context map entry is removed. **`failed`/`skipped` context entries are
  RETAINED (not evicted immediately)** until the next session reset or revert-prune — this
  is what lets a retry/replacement migrate the original move's context (Finding L2). The
  retained set is bounded (≤ one per move index per session). On a `scheduled` outcome with
  `previousRequestId`, **migrate the context entry old→new id** (if present), so the
  replacement resolves with the original move's `fen`/`pgn`/`moveSan`/`moveUci`.
- Test: out-of-order resolution (index 3 before recordable index 1) → index 1 recorded;
  **failure hole** (index 2 fails/skips between resolved 1 and 3) → frontier still advances
  past 2; rewind/replay re-seeds the frontier without deadlock.

**(c) Blunder alert flash/toast/arrows + audio — latest-only, microtask-coalesced
(Findings H3, I2, J2; user-confirmed).** "Same batch" is defined explicitly via a
**microtask coalescing queue**, since outcome listeners fire once per result: as
player-blunder outcomes arrive, push the `{moveIndex, context-snapshot}` into an
`alertBuffer` and, if not already scheduled, `queueMicrotask(flushAlert)`. `flushAlert`
runs once after the synchronous outcome burst, selects the **highest-moveIndex** buffered
player blunder, fires a single `setBlunderAlert`/flash + one `playRandomBlunderAudio`, and
clears the buffer. Outcomes emitted in the **same synchronous turn** (e.g. one batched
cache resolution loop) share one microtask → one alert; outcomes in **separate turns**
schedule separate microtasks → separate alerts. This is the visual layer ONLY — every
result is still fully consumed by (a) SRS, (b) recording, persistence (upload), and move
annotations. Test both the same-turn coalesce and the across-turn non-coalesce.
- **Deferred-flush stale guard (Finding K2):** an outcome can be valid when buffered, then
  a session change / revert / unmount happens before its microtask runs. Capture an
  `alertEpoch` (a counter/ref) when scheduling, and at the top of `flushAlert` **recheck
  the current epoch** — bail (drop the buffered alert + audio) if it advanced. The
  synchronous reset handler (Finding K4, above) bumps `alertEpoch` + clears `alertBuffer`
  on session change and revert/takeback; unmount does the same in effect cleanup.
  Test: outcome buffered → reset (epoch bump) → microtask runs → **no** alert/audio fires.

**requestId-keyed contexts (Finding H2), shared by (a)–(c).** Key the context map by
**`requestId`** (== `AnalysisResult.id`); the commit path already exposes
`committed.analysisId` (`useChessGameController.ts:~145`). The map is
`Map<requestId, BlunderContext>` (each entry also carrying `moveIndex`), looked up by the
resolved result's `id` so a blunder is always paired with its own move's
`fen`/`pgn`/`moveSan`/`moveUci`. **In this bead this map (and the pending-SRS map +
`blunderRecorded` flag + frontier) stays in the React layer (AnalysisEffects /
`ChessGame.tsx:259` refs) as today** — written at commit by the controller; relocating it to
a coordinator-lifetime owner is deferred to g-h94q.
Lifecycle: **prune on revert/takeback** (`pruneFromMoveIndex`, drop entries with
`moveIndex >=` revert ply and reset the frontier); **clear on every reset path** (new game,
drill restart, session clear). Per Finding J1, a `resolved` move's context is snapshotted
into its frontier slot at resolution, so the map entry is removed then; `failed|skipped`
entries are RETAINED (Finding L2) until session reset or revert-prune so a retry/replacement
can migrate them.

### Outcome channel API (Finding J3 — concrete ownership/wiring)

One typed channel on the coordinator, replacing the ad-hoc `addAnalysisResolvedListener`:
```ts
type AnalysisOutcomeStatus = 'scheduled' | 'resolved' | 'failed' | 'skipped'
type AnalysisOutcome = {
  seq: number                   // monotonic journal sequence (N3 replay cursor)
  generation: number            // sessionGeneration at emit time
  sessionId: string | null      // activeSessionId at emit time
  moveIndex: number
  requestId: string
  status: AnalysisOutcomeStatus
  previousRequestId?: string    // present on 'scheduled' that supersedes a prior request
  result?: AnalysisResult       // present iff status === 'resolved'
}
// In THIS bead the channel is consumed by AnalysisEffects (React): SRS-immediate,
// recording-frontier, and microtask alert all read this subscription, using the existing
// shouldRecordBlunder + recordBlunder + SRS API. emitOutcome is the single fan-out point.
// (The `seq` field + journal/reducer/outbox plumbing is reserved for g-h94q and is not
// built here; emitOutcome may still stamp seq for forward-compat but nothing drains it.)
addAnalysisOutcomeListener(cb: (o: AnalysisOutcome) => void): () => void  // React-consumed
private emitOutcome(o: AnalysisOutcome): void   // single fan-out point
markSkipped(moveIndex: number, requestId: string): void  // controller-facing
// Synchronous, non-indexed lifecycle reset (Finding K4):
addAnalysisResetListener(cb: (info: { generation: number; sessionId: string | null }) => void): () => void
private emitReset(): void   // fan-out called synchronously
// Synchronous epoch snapshot for atomic registration (Finding M3):
getEpoch(): { generation: number; sessionId: string | null }
// Synchronous revert pruning (Finding M1):
pruneFromMoveIndex(k: number): void
// NOTE: decisionOwner.reset/prune + registerDecisionCallbacks (the coordinator-lifetime
// durable owner + cleanup lease) are DEFERRED to g-h94q, not part of this bead.
```
- **Initial epoch acquisition (Finding M3).** A reset listener only observes *future*
  resets, so a freshly mounted/remounted AnalysisEffects has no generation to validate its
  first outcome against. `getEpoch()` returns the current `{generation, sessionId}`
  synchronously; the consumer reads it **atomically at the moment it registers**
  (`addAnalysisOutcomeListener`/`addAnalysisResetListener` in the same `useEffect`), seeding
  its `currentGeneration` so the very first outcome is generation-checked without waiting
  for another reset.
- **Synchronous revert pruning (Finding M1).** Consumer-side reset alone cannot prune
  coordinator-owned state. `pruneFromMoveIndex(k)` — called **synchronously from
  `rewindBoardLocally`** (`useChessGameLifecycle.ts:348`, alongside the existing
  `prunePendingSrsReviewsFromMoveIndex`) — for every index `>= k`: cancels in-flight worker
  requests, clears `resolutionState` (+ timers/buffers), rejects matching `analysisWaiters`
  ("reverted"), drops `latestRequestIds`/`pendingMoveIndices`/`pendingMeta`, removes
  resolved analyses from the store, and clears `lastRequestIdByMoveIndex`. The consumer's
  frontier/context/alert reset for revert (Finding K4 reset handler) runs in the same
  synchronous turn.
  - **Tombstones, not deletion, for `requestIdToMoveIndex` (Finding N1).** Prune happens
    while `sessionGeneration` and the worker listener are still live, so a worker message
    already queued for a pruned request would otherwise see `requestIdToMoveIndex` empty,
    fall into the non-indexed branch, and hit `setLastAnalysis` (`:461`/`:504`). Do NOT
    delete those ids on prune — move them into a **`discardedRequestIds: Set<string>`
    tombstone set** retained until the worker is actually replaced
    (`terminateWorker`/`restartAnalysisWorker`/session reset/`destroy`). The worker
    `analysis`/`analysis-started`/`analysis-streaming` handlers check this set FIRST and
    drop the message (never treat a known-but-discarded request as non-indexed). Test:
    prune → queued worker start/stream/result → no `lastAnalysis` mutation, no spinner.
- **Emission points** (all funnel through `emitOutcome`, stamping current
  `generation`/`sessionId`):
  - `analyzeMove` (indexed) → `scheduled` (with `previousRequestId` from the lineage map
    below). Emitted before worker/cache fire so the consumer's slot is `pending` under the
    new id and context migrates old→new. This is the **only** representation of
    supersession; there is no terminal `superseded`.
  - `resolveAnalysisResult` → `resolved` (with `result`). This **replaces** the old
    `analysisResolvedListeners` fan-out — delete that listener set and its
    `addAnalysisResolvedListener` registration so there is exactly one channel (no dual fan-out).
  - `releaseFallback` reject branch (`workerFailed` + non-trusted settle) → `failed`.
  - **Same-generation termination only — Restart / fatal / `clearAnalysis`** → `failed`
    (worker gone) for **each** still-unresolved index, so the frontier never strands a
    `pending` slot. These keep `sessionGeneration` unchanged, so the stamp is valid.
    Context for these `failed` indices is RETAINED (Finding L2) so an immediately-following
    retry `scheduled` can migrate it.
  - **`markSkipped`** — called by the controller when `analyzeMove()` returns `undefined`
    (synthetic `analysis-{idx}-{uci}` id); emits `skipped`. The controller owns detection,
    the coordinator owns emission, so all outcomes share one stamped channel.
- **Retry path (Finding L2):** the drill-steering retry calls
  `coordinator.analyzeMove(...)` directly (`ChessGame.tsx:1599`) after
  `restartAnalysisWorker()`, ignoring the returned id and registering no context. With the
  above design this is correct *without* call-site bookkeeping: `restartAnalysisWorker`
  emits `failed` for the index (context retained), then `analyzeMove` emits `scheduled`
  with `previousRequestId` = the failed request's id, so the consumer re-opens the slot to
  `pending`, rewinds the frontier to that index, and migrates the original context to the
  new id. The retry resolves with the original move's context. (No change needed at
  `:1599` beyond confirming `restartAnalysisWorker`→`analyzeMove` order; the synthetic-id
  concern does not apply here since `analyzeMove` returns a real id after restart.)
- **`markSkipped` ordering (Finding K3):** `analyzeMove()` runs early in `commitAppliedMove`
  (`useChessGameController.ts:145`) while the player context is registered later (`:237`).
  Reorder so the synthetic request's `BlunderContext` is written into the
  `Map<requestId, BlunderContext>` **before** synchronously calling `markSkipped`, so the
  consumer's frontier slot is created with its moveIndex/context consistently. Guarantee
  **exactly one terminal outcome per requestId**: a synthetic id never enters the
  coordinator's pending maps, so no real outcome can follow; guard `markSkipped` against
  double emission for the same id.
- **Session-change paths do NOT emit per-index outcomes; they emit a synchronous reset
  (Findings K1, K4).** `startSession`/`clearSession` bump
  `sessionGeneration`/`activeSessionId` *before* clearing pending requests (`:251-253`), so
  any cleanup outcome would be stamped with the NEW generation and mis-attributed. Instead,
  **immediately after the generation bump they call `emitReset()`**, which **synchronously**
  invokes `addAnalysisResetListener` callbacks. A React effect watching session state is
  insufficient — effects run *after* queued microtasks, so a buffered `flushAlert` would
  fire before the effect reset (violating K2). The synchronous reset listener runs during
the same task as the bump, before any microtask. The `emitReset()` listener (in
AnalysisEffects) clears the component's frontier/context maps + `alertBuffer` and increments
`alertEpoch` (so any already-queued `flushAlert` bails). **Revert** triggers
`pruneFromMoveIndex(k)` + the same UI reset handler. Reserve `emitOutcome` for same-generation
termination (above). (In g-h94q this same reset also drives a coordinator-lifetime
`decisionOwner.reset/prune` so durable state resets without a mounted UI; not needed here
because the decision state IS the mounted UI's refs.)
- **Retry lineage map (Finding L3).** `previousRequestId` cannot be sourced from
  `latestRequestIds`: same-generation cleanup (`restartAnalysisWorker`/`clearAnalysis`)
  clears it, and `markSkipped` (synthetic controller-side ids) never populates it. Add a
  dedicated **`lastRequestIdByMoveIndex: Map<number, string>`** on the coordinator that is
  the single source of `previousRequestId`:
  - **Set** the entry on every `scheduled` emission AND inside `markSkipped` (so a skipped
    move still leaves lineage for a later retry).
  - **Preserved across same-generation `failed` cleanup** — restart/fatal/`clearAnalysis`
    must NOT clear it (unlike `latestRequestIds`/`resolutionState`), so a retry immediately
    after a failure can still find its predecessor.
  - **Read** when building a `scheduled` outcome: `previousRequestId =
    lastRequestIdByMoveIndex.get(moveIndex)` (then overwrite with the new id).
  - **Cleared** on session reset (`startSession`/`clearSession`), revert/takeback pruning
    (drop entries with `moveIndex >=` revert ply), and `destroy`.
  - After the consumer **successfully migrates** context old→new on a `scheduled` with
    `previousRequestId`, it **deletes the old context entry** (the migration is one-shot).
  This removes all dependence on `latestRequestIds` cleanup timing.
- **Migrate pending SRS on `scheduled` too (Finding N2).** `pendingSrsReviewRef` is keyed by
  `requestId` (`useChessGameController.ts:221`, read at `AnalysisEffects.tsx:123`). A retried
  result has a NEW id, so without migration the old SRS review stays pending forever and the
  replacement's SRS never fires. On a `scheduled` outcome with `previousRequestId`, the
  consumer must **re-key the pending SRS entry old→new** alongside the BlunderContext (same
  one-shot migration). SRS stays **immediate** (consumer a) and is NOT gated by
  `committedDecisionIndex` — that boundary is **recording-specific** (irreversible
  `POST /api/blunder`) and must not delay or block SRS, which is independently exactly-once
  via its `id`+`moveIndex` self-validation. (Alternative if a move is abandoned rather than
  retried: cancel the pending SRS entry on its `failed`/prune — but migration is the
  retry-correct path.)
- **Mint `srsDecisionId` at registration (forward-prep for g-h94q; no backend change here,
  Finding 6a).** This bead **mints a stable `srsDecisionId` (a fresh uuid) when the pending
  SRS review is REGISTERED** (`useChessGameController.ts:221`, stored on the
  `PendingSrsReview`) and **preserves it across request-id retries** (carry it through the N2
  old→new re-key migration unchanged — the decision ID identifies the *logical review*, not
  the request). That is the full extent of the idempotency work IN THIS BEAD: it is cheap,
  harmless, and lets g-h94q use it later. **The idempotency *key on the wire* and the backend
  unique constraint / dedupe are DEFERRED to g-h94q** — this bead makes no `srs.py`/blunder
  endpoint or migration change. Rationale for the decision-ID over `${sessionId}:${moveIndex}`
  (recorded here so g-h94q inherits it): a takeback/replay can legitimately create two
  distinct same-ply reviews, which a ply-derived key would wrongly dedupe. (Tests for backend
  dedupe live in g-h94q; this bead only asserts the id is minted and survives an N2 retry.)
- **In-scope ownership (this bead): AnalysisEffects (React) consumes the outcome channel.**
  The recording-frontier (consumer b), SRS-immediate (consumer a), and the microtask alert
  buffer (consumer c) are all driven from an `addAnalysisOutcomeListener` subscription
  registered in an AnalysisEffects `useEffect` (torn down on unmount). The decision state
  (`Map<requestId, BlunderContext>`, pending-SRS map, `blunderRecordedRef`, the frontier +
  `committedDecisionIndex`) lives in component refs as it does today; the recording decision
  calls the existing `shouldRecordBlunder` + `recordBlunder`, and SRS calls the existing
  review API. **No coordinator-lifetime relocation, no journal/cursor/reducer/outbox, no
  remount replay, and no backend idempotency change are in this bead.**
- **Generation guard:** the consumer seeds its `currentGeneration` from `getEpoch()` at
  registration (M3) and **drops any outcome whose `generation` ≠ current** before touching
  the frontier/alert; the synchronous `emitReset` (K4) clears its frontier/context/alert on
  session change, and `pruneFromMoveIndex` (M1) prunes on revert. This is the in-bead
  correctness boundary while a UI is mounted.
- **Durability across unmount is DEFERRED to g-h94q.** Relocating the decision state to a
  coordinator-lifetime owner, the synchronous-reducer + async-idempotent-outbox split,
  SRS write *ordering* (pass_streak applied in logical order, not arrival order), per-decision
  idempotency IDs, durable (IndexedDB) outbox storage + retry/backoff + 4xx-terminal policy,
  the backend unique-constraint migration, and the `isReplay`/unmount-suppression semantics
  all move to the dependent bead **g-h94q**. Until that lands, an outcome that resolves while
  AnalysisEffects is unmounted is handled best-effort exactly as today (the subscription is
  torn down on unmount); this bead does not regress that behavior.

**Scope note:** minimum needed for exactly-once recording/SRS under buffered resolution;
deeper grading-threshold semantics remain in g-deterministic-grade.

## Diagnostics

Add a single concise log at each resolution noting moveIndex + chosen source + decision
path + profile, e.g. `[Analyst] resolve idx=3 source=cache(authoritative,
profile=linux-sf18-d24)`, `source=worker(cache-miss)`, `source=worker(untrusted
profile=browser-game-v1)`, `source=worker(timeout)`, `source=worker(cache-error)`,
`source=worker(worker-error)`. Reuse the existing console.log style and the new
`analysis_profile_id`/`authoritative` fields.

## Test plan (acceptance criteria 1–10)

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
17d. **Variation error frees retry (Finding F3):** a scoped variation `error` calls
    `onVariationError`→`rejectPending`, after which `hasPendingForFen(fen)` is false and the
    FEN can be re-requested.
17e. **Batched two-index recording (Finding F4):** a single cache batch resolves two
    indices where the earlier is a player blunder — assert the blunder is recorded exactly
    once with ITS OWN move context.
17e-2. **Practice-continuation snapshot (Finding 5):** a candidate is enqueued while
    `isGameActive && !isPracticeContinuation` is true, then `isPracticeContinuation` flips
    true before the deferred/out-of-order decision runs → the recording decision uses the
    SNAPSHOTTED composite (records), and the reverse (enqueued during practice continuation →
    flag snapshot false → never records) — `shouldRecordBlunder` receives the snapshot, not
    the live store value.
17f. **Out-of-order + failure-hole frontier (Findings H1, I1):** (a) index 3 resolves
    before recordable index 1 → index 1 is the first recorded blunder; (b) index 2
    terminates as `failed`/`skipped` between resolved 1 and 3 → the frontier
    advances past 2 and does NOT deadlock; (c) `analyzeMove()` returning `undefined`
    (synthetic id) marks the index `skipped` and unblocks later indices; (d) rewind/replay
    re-seeds the frontier cleanly.
17g. **Context key + lifecycle (Finding H2):** context is looked up by `requestId`; after
    a takeback to ply k, entries with `moveIndex >= k` are pruned and not mis-paired on
    replay; a new game / drill restart clears the map.
17h. **Latest-only alert, all results consumed (Findings H3, I2 — user-confirmed):** two
    player blunders resolve in one batch → exactly ONE alert (flash/toast/arrows + audio)
    is displayed, for the latest blunder; assert BOTH analyses are still consumed downstream
    (SRS processed for each, recording decision evaluated for each in move order, persistence
    payload + move annotations reflect both).
17h-2. **Alert coalescing boundary (Finding J2):** two player-blunder outcomes in the SAME
    synchronous turn → one microtask flush → one alert/audio; the same two outcomes in
    SEPARATE turns → two flushes → two alerts.
17h-3. **Context survives buffered resolution (Finding J1):** a `resolved` index buffered
    behind an earlier `pending` slot still has its snapshotted context when the frontier
    drains (recording/alert get the right `fen`/`pgn`/`moveSan`); `resolved` map entries
    are removed at snapshot while `failed`/`skipped` contexts are retained for retry
    migration until reset/prune.
17h-4. **Outcome channel generation guard (Finding J3):** an outcome stamped with a stale
    `generation` (session changed) is dropped by AnalysisEffects and does not mutate the
    frontier/recording; `markSkipped` emits a `skipped` outcome that advances the frontier.
17h-5. **Session change resets, not emits (Finding K1):** `startSession`/`clearSession`
    (which bump generation before clearing requests) emit NO per-index outcomes; the
    consumer resets its frontier/context/alert buffer on the generation change. Restart/
    fatal/`clearAnalysis` (same generation) DO emit `failed` per unresolved index.
17h-6. **Deferred alert stale guard (Finding K2):** a player-blunder outcome is buffered,
    then a session change / revert / unmount bumps `alertEpoch`; the queued `flushAlert`
    runs and fires no alert/audio.
17h-6b. **Synchronous reset beats the microtask (Finding K4):** queue an alert, call
    `startSession`, then flush microtasks **without** flushing React effects — assert NO
    alert/audio (the synchronous `emitReset` listener bumped `alertEpoch` before the
    microtask ran). Same assertion for a direct revert reset.
17h-7. **markSkipped ordering (Finding K3):** context is registered before the synchronous
    `skipped` emission; exactly one terminal outcome per synthetic requestId (no double
    emission, no later real outcome).
17h-8. **Supersession blocks the frontier (Finding L1):** index 1 superseded (replacement
    still `pending`), index 2 resolves → index 2 stays blocked; frontier does NOT drain
    past 1 until the replacement-1 reaches a terminal status. `scheduled` rewinds
    `nextDecisionIndex` if it had advanced.
17h-9. **Retry retains original context (Findings L2, L3):** `restartAnalysisWorker()`
    (emits `failed`, context + lineage retained) then `analyzeMove` (emits `scheduled` with
    `previousRequestId` from `lastRequestIdByMoveIndex`) → the retried analysis resolves
    with the ORIGINAL move's `fen`/`pgn`/`moveSan` context; the old context entry is
    deleted post-migration; the frontier reconsiders that index. Cover BOTH
    `failed → scheduled` and `skipped → scheduled` lineage (the latter proving `markSkipped`
    populated the map). Assert lineage clears on session reset / revert / destroy.
17h-10. **Revert pruning API (Finding M1):** `pruneFromMoveIndex(k)` from
    `rewindBoardLocally` cancels/clears requests, waiters, cache state, analyses, and
    lineage for indices `>= k`; a late worker/cache callback for a pruned index is a no-op.
17h-11. **Monotonic commit boundary (Finding M2):** index 1 `failed`, index 3 recorded,
    index 1 retried and resolves recordable → the index-3 backend record stands, no second
    or changed `POST /api/blunder`; index 1 only refreshes display/annotation.
17h-12. **Initial epoch acquisition (Finding M3):** a remounted AnalysisEffects reads
    `getEpoch()` at registration and correctly validates/drops its FIRST outcome by
    generation without waiting for a reset.
17h-13. **Prune tombstone (Finding N1):** `pruneFromMoveIndex` then a queued worker
    `analysis-started`/`streaming`/`analysis` for a pruned id → dropped via
    `discardedRequestIds`; no `lastAnalysis` mutation / spinner; tombstones cleared on
    worker replacement.
17h-14. **SRS retry migration (Finding N2):** a retried (`scheduled` + `previousRequestId`)
    move re-keys its pending SRS entry old→new; the old entry does not linger and SRS fires
    once for the replacement; SRS is not blocked by `committedDecisionIndex`.
17h-15..22 (durable owner / reducer / outbox / SRS ordering / idempotency / unmount
    durability / backend migration) — **MOVED to g-h94q.** Not tested in this bead; until
    g-h94q lands, an outcome resolving while AnalysisEffects is unmounted is handled
    best-effort exactly as today (subscription torn down on unmount).
17i. **SRS immediacy/decoupling (Finding I3):** an SRS-targeted result resolves while an
    earlier index is still `pending` → SRS is processed immediately (not blocked by the
    recording frontier), exactly once.
18. **Variation UI preserved (Finding G4, hook):** a variation (`pendingVariationPlies`)
    `analysis-started`/`streaming` still sets variation streaming state; a scoped
    variation `error` clears only that variation, not global status.
19. **Persistence carries authoritative values (criterion 9):** after a cache/worker
    reordering that resolves from a trusted cache hit, trigger an incremental upload flush
    and assert the built `SessionMoveUpload` payload (via
    `buildSessionMoveUploadsForIndices`) contains the authoritative best move / evals /
    delta / classification, not the worker's.
20. **Downstream-effect stability via the OUTCOME CHANNEL (Finding 6b — corrected):** the
    recording, SRS, and blunder-alert consumers are **migrated off the old `lastAnalysis`
    Zustand selector/effect (`AnalysisEffects.tsx:63,75-116,216`) onto the new
    `addAnalysisOutcomeListener` subscription** — the old `lastAnalysis`-driven blunder
    effect, `processSrsReview` selector trigger, and alert effect **must be REMOVED, not left
    alongside the new channel**, or both fire and double-process (record/SRS twice). The test:
    vary source completion order and assert the recording decision + `processSrsReview` are
    each invoked **exactly once** per move with the authoritative result **via the outcome
    channel**, and assert the legacy `lastAnalysis` effect path no longer triggers
    recording/SRS (e.g. it is deleted / no listener on that selector for those effects).
    `lastAnalysis` itself still exists for board display and variation resolution
    (AnalysisBoard `:971`); only the recording/SRS/alert *consumers* move. Listener/waiter
    once-only assertions remain coordinator-internal checks, not proof of recording/SRS.

Run targeted (pre-push suite is flaky under parallel load — per project memory, rerun
named files in isolation before trusting a failure):
```
npx vitest run src/services/GameAnalysisCoordinator.test.ts
npx vitest run src/hooks/useMoveAnalysis.test.ts
npx vitest run src/workers/analysisWorker.test.ts
npx vitest run src/workers/analysisUtils.test.ts
npx vitest run src/components/chess-game/AnalysisEffects.test.tsx
npx vitest run src/hooks/useVariationTree.test.ts
npx vitest run src/components/AnalysisBoard.test.tsx
npx vitest run src/hooks/useChessGameController.test.ts
npx vitest run src/hooks/useChessGameLifecycle.test.ts
npx vitest run src/components/ChessGame.test.tsx
```
(Adjust paths to whichever of these suites exist; the worker, analysisUtils, and
AnalysisEffects suites are touched by the protocol change, `isTrustedCacheHit`, and the
downstream-stability/ordered-frontier work; the variation-tree, AnalysisBoard, controller,
lifecycle, and ChessGame suites cover the F3/H2 context-map and reject-channel changes.)

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
