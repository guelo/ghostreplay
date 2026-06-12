# g-hpw4 — Exactly-once context-correct recording & SRS via outcome channel (F4)

Split from **g-cache-first-resolve** (was Finding F4 + the Outcome channel API section).
Part of epic **g-stable-drill-best**.

**Depends on g-cache-first-resolve (scope A):** this bead consumes the single published
`AnalysisResult` and adds the outcome emission points *inside* the scope-A state machine
(`analyzeMove` → `scheduled`, `resolveAnalysisResult` → `resolved`, `releaseFallback` reject
→ `failed`, `failRequest` → `failed`, plus `markSkipped`). Those methods are built in A.

**Blocks g-h94q:** the durable decision-owner / reducer / outbox work builds on the outcome
channel and terminal frontier introduced here.

## Scope note

Minimum needed for exactly-once recording/SRS under buffered (cache-first) resolution
**while a UI is mounted**. Decision state stays in the React layer (AnalysisEffects /
`ChessGame.tsx` refs) as today; the recording decision calls the existing
`shouldRecordBlunder` (`src/utils/blunder.ts:56`) + `recordBlunder`, and SRS calls the
existing review API. Deeper grading-threshold semantics remain in g-deterministic-grade.
Durable relocation, reducer/outbox, SRS write *ordering*, idempotency keys/migration, and
unmount durability are all DEFERRED to **g-h94q**.

## Finding F4 — regular-game recording/SRS must be exactly-once and context-correct

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
  waiter rejection on supersession (Findings F2/G2, in scope A) is unchanged and independent.
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
  synchronous reset handler (Finding K4, below) bumps `alertEpoch` + clears `alertBuffer`
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

## Outcome channel API (Finding J3 — concrete ownership/wiring)

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
  synchronous turn. (The `resolutionState`/`latestRequestIds`/timer internals come from
  scope A; this bead adds the outcome/frontier/lineage side of `pruneFromMoveIndex`.)
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

## Files to modify

- `src/services/GameAnalysisCoordinator.ts` — add the `AnalysisOutcome` channel
  (`addAnalysisOutcomeListener`/`emitOutcome`/`markSkipped`) replacing
  `addAnalysisResolvedListener`; the synchronous reset channel
  (`addAnalysisResetListener`/`emitReset`, called in `startSession`/`clearSession`); the
  `getEpoch()` snapshot (M3); `pruneFromMoveIndex(k)` outcome/lineage side (M1); the
  `lastRequestIdByMoveIndex` lineage map (L3); the monotonic `committedDecisionIndex`
  boundary (M2). (The emission *points* hook into the scope-A `analyzeMove` /
  `resolveAnalysisResult` / `releaseFallback` / `failRequest` methods.)
- `src/components/chess-game/AnalysisEffects.tsx` — subscribe to `addAnalysisOutcomeListener`:
  SRS immediate per `resolved`, recording via the terminal-aware frontier (using the
  existing `shouldRecordBlunder` + `recordBlunder`), blunder alert/audio via microtask
  coalescing; seed generation from `getEpoch()` and drop stale-generation outcomes; subscribe
  to `addAnalysisResetListener` for synchronous UI reset (F4/H1/H3/I/J/K/M3). Decision state
  stays in component refs. **Remove** the legacy `lastAnalysis`-driven recording/SRS/alert
  effects (`:63,75-116,216`) so they do not double-fire.
- `src/hooks/useChessGameController.ts` — register `BlunderContext` + pending SRS keyed by
  `committed.analysisId` (H2); reorder so context is written before `markSkipped` (K3); mint
  a stable `srsDecisionId` at registration that survives request-id retries (Finding 3 /
  forward-prep for g-h94q).
- `src/hooks/useChessGameLifecycle.ts` — prune-on-revert and clear-on-reset of the context
  map (H2); call `coordinator.pruneFromMoveIndex(newHistory.length)` synchronously inside
  `rewindBoardLocally` next to `prunePendingSrsReviewsFromMoveIndex` (M1).
- Tests: `GameAnalysisCoordinator.test.ts`, `AnalysisEffects.test.tsx`,
  `useChessGameController.test.ts`, `useChessGameLifecycle.test.ts`, a `ChessGame` gameplay
  test.

## Test plan

- **17e. Batched two-index recording (Finding F4):** a single cache batch resolves two
  indices where the earlier is a player blunder — the blunder is recorded exactly once with
  ITS OWN move context.
- **17e-2. Practice-continuation snapshot (Finding 5):** a candidate enqueued while
  `isGameActive && !isPracticeContinuation` is true, then `isPracticeContinuation` flips
  true before the deferred decision runs → recording uses the SNAPSHOTTED composite
  (records); reverse case never records. `shouldRecordBlunder` receives the snapshot, not
  the live store.
- **17f. Out-of-order + failure-hole frontier (H1, I1):** (a) index 3 resolves before
  recordable index 1 → index 1 first recorded; (b) index 2 `failed`/`skipped` between
  resolved 1 and 3 → frontier advances, no deadlock; (c) `analyzeMove()` → `undefined`
  marks `skipped` and unblocks later indices; (d) rewind/replay re-seeds cleanly.
- **17g. Context key + lifecycle (H2):** context looked up by `requestId`; after a takeback
  to ply k, entries with `moveIndex >= k` pruned and not mis-paired on replay; new
  game/drill restart clears the map.
- **17h. Latest-only alert, all results consumed (H3, I2):** two player blunders in one
  batch → exactly ONE alert, for the latest; assert BOTH analyses still consumed (SRS each,
  recording decision each in move order, persistence + annotations reflect both).
- **17h-2. Alert coalescing boundary (J2):** two player-blunder outcomes same synchronous
  turn → one flush → one alert; separate turns → two.
- **17h-3. Context survives buffered resolution (J1):** a `resolved` index buffered behind
  an earlier `pending` slot still has its snapshotted context when the frontier drains;
  `resolved` entries removed at snapshot, `failed`/`skipped` retained for retry migration.
- **17h-4. Outcome channel generation guard (J3):** stale-`generation` outcome dropped by
  AnalysisEffects; `markSkipped` emits `skipped` that advances the frontier.
- **17h-5. Session change resets, not emits (K1):** `startSession`/`clearSession` emit NO
  per-index outcomes; consumer resets frontier/context/alert. Restart/fatal/`clearAnalysis`
  (same generation) DO emit `failed` per unresolved index.
- **17h-6. Deferred alert stale guard (K2):** outcome buffered, then session change /
  revert / unmount bumps `alertEpoch`; queued `flushAlert` fires no alert/audio.
- **17h-6b. Synchronous reset beats the microtask (K4):** queue an alert, call
  `startSession`, flush microtasks WITHOUT flushing React effects → NO alert/audio (the
  synchronous `emitReset` bumped `alertEpoch` first). Same for a direct revert reset.
- **17h-7. markSkipped ordering (K3):** context registered before the synchronous `skipped`
  emission; exactly one terminal outcome per synthetic requestId.
- **17h-8. Supersession blocks the frontier (L1):** index 1 superseded (replacement still
  `pending`), index 2 resolves → index 2 stays blocked until replacement-1 terminal;
  `scheduled` rewinds `nextDecisionIndex` if it had advanced.
- **17h-9. Retry retains original context (L2, L3):** `restartAnalysisWorker()` (emits
  `failed`, context+lineage retained) then `analyzeMove` (emits `scheduled` with
  `previousRequestId`) → retried analysis resolves with ORIGINAL context; old entry deleted
  post-migration; frontier reconsiders. Cover BOTH `failed → scheduled` and
  `skipped → scheduled`. Assert lineage clears on session reset / revert / destroy.
- **17h-10. Revert pruning API (M1):** `pruneFromMoveIndex(k)` from `rewindBoardLocally`
  cancels/clears requests, waiters, cache state, analyses, and lineage for indices `>= k`;
  late callbacks for a pruned index are no-ops.
- **17h-11. Monotonic commit boundary (M2):** index 1 `failed`, index 3 recorded, index 1
  retried recordable → index-3 backend record stands, no second/changed `POST /api/blunder`;
  index 1 only refreshes display/annotation.
- **17h-12. Initial epoch acquisition (M3):** a remounted AnalysisEffects reads `getEpoch()`
  at registration and validates/drops its FIRST outcome by generation without a reset.
- **17h-13. Prune tombstone (N1):** `pruneFromMoveIndex` then a queued worker
  start/stream/result for a pruned id → dropped via `discardedRequestIds`; no `lastAnalysis`
  mutation/spinner; tombstones cleared on worker replacement.
- **17h-14. SRS retry migration (N2):** a retried (`scheduled` + `previousRequestId`) move
  re-keys its pending SRS entry old→new; old entry does not linger; SRS fires once for the
  replacement; SRS not blocked by `committedDecisionIndex`.
- **17h-15..22 — MOVED to g-h94q** (durable owner / reducer / outbox / SRS ordering /
  idempotency / unmount durability / backend migration). Until g-h94q lands, an outcome
  resolving while AnalysisEffects is unmounted is best-effort as today.
- **17i. SRS immediacy/decoupling (I3):** an SRS-targeted result resolves while an earlier
  index is still `pending` → SRS processed immediately (not blocked by the recording
  frontier), exactly once.
- **19. Persistence carries authoritative values (criterion 9):** after a cache/worker
  reordering that resolves from a trusted cache hit, an incremental upload flush →
  `buildSessionMoveUploadsForIndices` payload contains the authoritative best move / evals /
  delta / classification, not the worker's.
- **20. Downstream-effect stability via the OUTCOME CHANNEL (Finding 6b):** recording, SRS,
  and blunder-alert consumers migrated OFF the old `lastAnalysis` selector/effect
  (`AnalysisEffects.tsx:63,75-116,216`) onto `addAnalysisOutcomeListener` — old effects
  REMOVED, not left alongside. Vary source completion order and assert recording decision +
  `processSrsReview` are each invoked exactly once per move with the authoritative result
  via the outcome channel; assert the legacy `lastAnalysis` path no longer triggers
  recording/SRS. `lastAnalysis` itself still exists for board display + variation resolution.

Run targeted (pre-push suite is flaky under parallel load — per project memory, rerun named
files in isolation before trusting a failure):
```
npx vitest run src/services/GameAnalysisCoordinator.test.ts
npx vitest run src/components/chess-game/AnalysisEffects.test.tsx
npx vitest run src/hooks/useChessGameController.test.ts
npx vitest run src/hooks/useChessGameLifecycle.test.ts
npx vitest run src/components/ChessGame.test.tsx
```

## Acceptance criteria

8. Regular-game classification/recording and SRS pass/fail consume the single published
   result exactly once.
9. Session upload and post-game state retain the same best move, delta, and classification
   that drove live behavior.
10. Tests vary source completion order and assert stable recording and SRS outcomes;
    recording/SRS/alert consumers are migrated off the legacy `lastAnalysis` effect onto the
    outcome channel (old path removed).

## Out of scope → g-h94q

Coordinator-lifetime decision-owner relocation; synchronous-reducer + async-idempotent-outbox
split; SRS write *ordering* (apply `pass_streak` in logical order, not arrival order);
per-decision idempotency keys on the wire; durable IndexedDB outbox + retry/backoff +
4xx-terminal; backend unique-constraint migration (`models.py` + Alembic on `srs.py`/blunder
endpoint, Finding 6a); `isReplay`/unmount-durability (N3/O3/P2).
