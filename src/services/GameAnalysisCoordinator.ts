/**
 * GameAnalysisCoordinator — singleton that owns the gameplay Stockfish worker
 * and all long-lived analysis/upload state. Survives route navigation so that
 * in-flight analysis is never lost when the user navigates from /game to /history.
 */

import type {
  AnalyzeMoveMessage,
  AnalysisWorkerRequest,
  AnalysisWorkerResponse,
} from '../workers/analysisMessages'
import {
  isRecordableFailure,
  isWithinRecordingMoveCap,
  classifyMove,
  gradeDrillMove,
  isTrustedPositionHit,
  isTrustedExactBestHit,
  isTrustedMoveHit,
  hasCpEvalLoss,
  reconcileTrustedBest,
} from '../workers/analysisUtils'
import type { MoveClassification, MoveGrade } from '../workers/analysisUtils'
import { lookupAnalysisCache, uploadSessionMoves } from '../utils/api'
import type { CachedAnalysis, SessionMoveUpload } from '../utils/api'
import { gameAnalysisStore } from '../stores/createAnalysisStore'
import type { AnalysisResult } from '../types/analysis'
import { useGameStore } from '../stores/useGameStore'
import { buildSessionMoveUploadsForIndices } from '../components/chess-game/domain/sessionUpload'
import { STARTING_FEN } from '../components/chess-game/config'
import { DecisionOwner, type DecisionOwnerGameState } from './DecisionOwner'

const createRequestId = () => {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID()
  }
  return Math.random().toString(36).slice(2)
}

const CACHE_LOOKUP_DEBOUNCE_MS = 150
/**
 * Hard cap on how long the trailing cache-lookup debounce may slide under a
 * sustained burst. Without it a continuous stream of moves could defer dispatch
 * (and therefore the cache-response timer) indefinitely. Measured from the first
 * enqueue into an empty batch.
 */
const CACHE_BATCH_MAX_AGE_MS = 400
/**
 * Cache-response window. Started when the lookup is actually dispatched (in
 * flushCacheLookups), not at analyzeMove. If the cache has not answered within
 * this window the buffered worker fallback is released.
 */
const ANALYSIS_RESOLUTION_TIMEOUT_MS = 2500
/**
 * Per-request inactivity watchdog window. Replaces the old fixed total-elapsed
 * deadline: a request fails only after producing NO observable activity (worker
 * `analysis-progress`/started/streaming/result/error) for this window; any
 * activity for it resets the timer. Comfortably above Stockfish's intra-search
 * info cadence plus the per-request reset round-trip, while cutting silent-worker
 * failure from 30s → 8s so drill `waitForAnalysis` recovery does not stall. The
 * watchdog is a POST-ready concept; engine boot is bounded separately by
 * ANALYSIS_BOOT_TIMEOUT_MS.
 */
const ANALYSIS_INACTIVITY_TIMEOUT_MS = 8_000
/**
 * Boot backstop, distinct from the inactivity window. Bounds a silent/hung
 * engine boot: started on worker creation, cleared on `ready`, and on fire it
 * tears the worker down as a fatal start failure (so drill recovery settles
 * instead of hanging). Generous — comfortably above real WASM cold-boot, below
 * the old 30s deadline that used to double as the boot guard.
 */
const ANALYSIS_BOOT_TIMEOUT_MS = 20_000
const INCREMENTAL_UPLOAD_INTERVAL_MS = 3000
const INCREMENTAL_UPLOAD_BATCH_THRESHOLD = 4
const IDLE_SHUTDOWN_MS = 5 * 60 * 1000 // 5 minutes
const RETRY_MAX_DELAY_MS = 30_000

type PendingCacheLookup = {
  requestId: string
  fen: string
  move: string
  moveIndex: number
  playerColor: 'white' | 'black'
  legalMoveCount: number | undefined
}

type AnalysisWaiter = {
  generation: number
  /** Bound at registration so a superseded request cannot fulfil a waiter that
   *  was awaiting a different (older or newer) request for the same index. */
  requestId: string | undefined
  resolve: (result: AnalysisResult) => void
  reject: (error: Error) => void
}

type ReleaseReason = 'cache-miss' | 'untrusted' | 'cache-error' | 'timeout' | 'worker-error'

/**
 * Drill-only "position truth" resolved from a trusted-cache exact-best hit
 * (g-position-analysis Phase 6). A SEPARATE side channel from the published
 * `AnalysisResult` path: it is fed only from CACHE evidence (never the worker)
 * and a position-only hit fulfils it WITHOUT publishing. `positionEvalLossCp` is
 * the backend-derived threshold loss (null unless both grains were trusted, pure
 * CP, and same search strength).
 */
type DrillTruth = { best_move_uci: string; positionEvalLossCp: number | null }

type DrillTruthWaiter = {
  generation: number
  /** Bound at registration so a superseding request rejects a stale waiter. */
  requestId: string
  resolve: (truth: DrillTruth | null) => void
  reject: (error: Error) => void
}

/**
 * The result of `waitForDrillGrade`: the tri-state grade, the best move to
 * surface as a correction suggestion (trusted position truth or honest worker
 * best move — NEVER `?? playedMove`), and which channel produced it.
 */
export type DrillGrade = {
  grade: MoveGrade
  bestMove: string | null
  source: 'position' | 'worker'
}

/**
 * Per-moveIndex resolution state machine. The worker result is buffered while
 * the authoritative cache lookup is still pending; the cache settle performs the
 * single policy decision. Carries the owning requestId so a superseded/older
 * request can never settle a newer one.
 */
type ResolutionEntry = {
  requestId: string
  cacheStatus: 'pending' | 'released'
  releaseReason?: ReleaseReason
  /** Worker finished but cache still pending — held here until cache settles. */
  bufferedWorker?: AnalysisResult
  /** Worker errored (scoped) while cache pending — awaiting cache recovery. */
  workerFailed?: boolean
  /** Captured scoped-error text, used when rejecting waiters. */
  workerError?: string
  /** Per-request inactivity watchdog (armed post-ready; reset by activity). */
  watchdogTimer?: ReturnType<typeof setTimeout>
  /** True once this request has produced its OWN activity. A started request is
   *  reset only by its own activity (worker's serial focus); a not-yet-started
   *  (queued) request is reset by any live worker liveness. */
  started?: boolean
  /** Cache-response window (started at dispatch in flushCacheLookups). */
  cacheTimer?: ReturnType<typeof setTimeout>
}

/**
 * Typed outcome channel (g-hpw4). Every analysis index reaches exactly one
 * terminal outcome (`resolved | failed | skipped`); `scheduled` is the
 * non-terminal (re)open used for supersession/retry. Consumed by AnalysisEffects
 * for exactly-once, context-correct recording / SRS / blunder-alert.
 */
export type AnalysisOutcomeStatus = 'scheduled' | 'resolved' | 'failed' | 'skipped'

export type AnalysisOutcome = {
  /** Monotonic journal sequence (forward-compat for g-hpw4 replay; undrained here). */
  seq: number
  /** sessionGeneration at emit time — consumers drop stale-generation outcomes. */
  generation: number
  /** activeSessionId at emit time. */
  sessionId: string | null
  moveIndex: number
  requestId: string
  status: AnalysisOutcomeStatus
  /** Present on a `scheduled` that supersedes a prior request for this index. */
  previousRequestId?: string
  /** Present iff status === 'resolved'. */
  result?: AnalysisResult
}

/**
 * Narrow read interface the React consumer (AnalysisEffects) depends on. Lets
 * tests substitute a controllable channel without the full coordinator.
 */
export type AnalysisResetInfo = {
  generation: number
  sessionId: string | null
  /** Present on a revert prune: only indices >= this are reset (M1). Absent on a
   *  full session-change reset. */
  fromMoveIndex?: number
}

export interface AnalysisOutcomeSource {
  getEpoch(): { generation: number; sessionId: string | null }
  addAnalysisOutcomeListener(cb: (o: AnalysisOutcome) => void): () => void
  addAnalysisResetListener(cb: (info: AnalysisResetInfo) => void): () => void
}

const makeCacheKey = (fen: string, moveUci: string) => `${fen}::${moveUci}`

// Callers pass the MOVER's color (the side that played the analyzed move), so
// these produce mover-relative values consumed via parity-based
// `toWhitePerspective`. See the perspective note on AnalysisResult.
const toPlayerPerspective = (
  whiteRelativeEval: number | null,
  playerColor: 'white' | 'black',
): number | null => {
  if (whiteRelativeEval === null) return null
  return playerColor === 'white' ? whiteRelativeEval : -whiteRelativeEval
}

/**
 * Convert a white-relative mate count to the mover's perspective by sign-negating
 * the count for black (mirrors `toPlayerPerspective` for the mate channel).
 */
const mateToPlayerPerspective = (
  whiteRelativeMate: number | null,
  playerColor: 'white' | 'black',
): number | null => {
  if (whiteRelativeMate === null) return null
  return playerColor === 'white' ? whiteRelativeMate : -whiteRelativeMate
}

const fromCachedAnalysis = (
  requestId: string,
  cached: CachedAnalysis,
  move: string,
  moveIndex: number,
  playerColor: 'white' | 'black',
  legalMoveCount: number | undefined,
): AnalysisResult => {
  const playedEval = toPlayerPerspective(cached.played_eval, playerColor)
  const bestEval = toPlayerPerspective(cached.best_eval, playerColor)
  const playedEvalMate = mateToPlayerPerspective(cached.played_eval_mate, playerColor)
  // `eval_delta` is the RAW cache evidence (uncapped, may be mate pseudo-cp)
  // retained for blunder/SRS/display on the published path; the normalized 0..1000
  // display/decision CPL is derived downstream by evalLoss (e.g. the DecisionOwner
  // SRS send). It is NOT the drill threshold loss and is left raw here because its
  // two local consumers — classifyMove (win-chance) and isRecordableFailure (≤150
  // threshold) — are both cap-independent. The drill grader reads the
  // backend-derived, same-strength `position_eval_loss_cp` out-of-band (see
  // `waitForDrillGrade`), never this browser-visible snapshot.
  const delta = cached.eval_delta
  const classification = (cached.classification as MoveClassification | null) ?? classifyMove(delta)
  const forced = legalMoveCount !== undefined && legalMoveCount <= 2
  const blunder = !forced && classification === 'blunder'
  const recordable =
    !forced &&
    isRecordableFailure(delta) &&
    isWithinRecordingMoveCap(moveIndex)

  return {
    id: requestId,
    move,
    // The published gate only resolves an `isTrustedPositionHit` row, which
    // guarantees `best_move_uci` is non-null — so use it directly. NEVER fall
    // back to `?? move`: a published result's `bestMove` must be honest position
    // truth, not the played move masquerading as best (the old g-l02q hazard,
    // now owned by the drill-truth side channel for strictness-0 grading).
    bestMove: cached.best_move_uci as string,
    bestLine: cached.best_line_uci ?? null,
    bestEval,
    playedEval,
    currentPositionEval: playedEval,
    playedEvalMate,
    currentPositionEvalMate: playedEvalMate,
    moveIndex,
    delta,
    classification,
    blunder,
    recordable,
  }
}

type UploadState = {
  sessionId: string
  uploadedIndices: Set<number>
  dirtyIndices: Set<number>
  uploadInFlight: boolean
  abortController: AbortController | null
  retryCount: number
  retryTimer: ReturnType<typeof setTimeout> | null
  /** True after the session is finalized — the interval timer is gone,
   *  so the success handler must drain all remaining dirty indices. */
  detached: boolean
  uploadsEnabled: boolean
}

const isAbortError = (err: unknown): boolean =>
  typeof err === 'object' &&
  err !== null &&
  'name' in err &&
  err.name === 'AbortError'

export class GameAnalysisCoordinator {
  // Worker state
  private worker: Worker | null = null
  private pendingMoveIndices = new Map<string, number>()
  private pendingMeta = new Map<string, { moveIndex: number; legalMoveCount: number | undefined }>()
  private latestRequestIds = new Map<number, string>()
  private resolvedIndices = new Set<number>()
  private lastStreamingUpdateMs = 0
  private currentAnalyzingRequestId: string | null = null
  /** True only between the worker's `ready` and its teardown. The inactivity
   *  watchdog is a post-ready concept, so analyzeMove arms it only when this is
   *  true; pre-ready requests are armed when `ready` arrives. NOT the store
   *  status (which a scoped recovery can move independently). */
  private engineReady = false
  /** Backstop for a silent/hung engine boot (ANALYSIS_BOOT_TIMEOUT_MS). Started
   *  on worker creation, cleared on `ready`; fatal on fire. */
  private bootWatchdogTimer: ReturnType<typeof setTimeout> | null = null

  // Cache lookup batching
  private pendingCacheLookups: PendingCacheLookup[] = []
  private cacheFlushTimer: ReturnType<typeof setTimeout> | null = null
  /** Timestamp of the first enqueue into an empty batch (max-batch-age clock). */
  private cacheBatchFirstEnqueuedAt: number | null = null

  // Cache-first resolution state machine (keyed by moveIndex).
  private resolutionState = new Map<number, ResolutionEntry>()
  /** Retained until lifecycle cleanup so a late worker message for a settled
   *  index is never mistaken for a non-indexed request (Finding G1). */
  private requestIdToMoveIndex = new Map<string, number>()

  // Session state — generation monotonically increases on each startSession
  // so in-flight async work from a previous session can be detected and dropped.
  private activeSessionId: string | null = null
  private sessionGeneration = 0
  private uploadState: UploadState | null = null

  // Incremental upload timer
  private incrementalUploadTimer: ReturnType<typeof setTimeout> | null = null

  // Idle shutdown
  private idleTimer: ReturnType<typeof setTimeout> | null = null

  // Outcome channel (g-hpw4) — the single fan-out for recording/SRS/alert.
  private analysisOutcomeListeners = new Set<(o: AnalysisOutcome) => void>()
  private analysisResetListeners = new Set<(info: AnalysisResetInfo) => void>()
  private outcomeSeq = 0
  /** Single source of `previousRequestId` for supersession/retry lineage (L3).
   *  Preserved across same-generation failed cleanup; cleared on reset/prune. */
  private lastRequestIdByMoveIndex = new Map<number, string>()
  /** Synthetic/skipped request ids already emitted, to guard double emission (K3). */
  private skippedRequestIds = new Set<string>()
  /** Pruned (reverted) request ids the live worker may still message about (N1).
   *  Worker handlers drop these FIRST; cleared on worker replacement. */
  private discardedRequestIds = new Set<string>()
  private analysisWaiters = new Map<number, Set<AnalysisWaiter>>()

  // Drill-only "position truth" side channel (g-position-analysis Phase 6),
  // request-bound. A present record means drill truth has SETTLED for that
  // request (`truth` set = trusted exact-best resolved; `truth: null` = no
  // trusted position). Supersession/clear delete stale records, so a present
  // record is always for the current request — the `waitForDrillTruth` fast path
  // relies on that invariant. The waiters fire from the cache `.then`/release
  // paths; they never depend on the worker.
  private drillTruth = new Map<number, { requestId: string; truth: DrillTruth | null }>()
  private drillTruthWaiters = new Map<number, Set<DrillTruthWaiter>>()

  // Coordinator-lifetime recording/SRS decision owner (g-2m0p). Fed every
  // outcome/reset for the singleton's life; the React layer leases UI callbacks
  // onto it via registerUICallbacks. Durable POSTs survive AnalysisEffects unmount.
  private readonly _decisionOwner: DecisionOwner

  constructor() {
    this._decisionOwner = new DecisionOwner({
      getGameState: (): DecisionOwnerGameState => {
        const s = useGameStore.getState()
        return {
          sessionId: s.sessionId,
          isGameActive: s.isGameActive,
          isPracticeContinuation: s.isPracticeContinuation,
          playerColor: s.playerColor,
          moveHistory: s.moveHistory,
        }
      },
    })
    this.addAnalysisOutcomeListener((o) => this._decisionOwner.handleOutcome(o))
    this.addAnalysisResetListener((info) => this._decisionOwner.handleReset(info))
    this._decisionOwner.seedGeneration(this.sessionGeneration)
  }

  get store() {
    return gameAnalysisStore
  }

  /** The coordinator-owned recording/SRS decision owner (g-2m0p). */
  get decisionOwner(): DecisionOwner {
    return this._decisionOwner
  }

  get sessionId() {
    return this.activeSessionId
  }

  /** Synchronous epoch snapshot so a freshly-mounted consumer can validate its
   *  very first outcome by generation without waiting for a reset (M3). */
  getEpoch(): { generation: number; sessionId: string | null } {
    return { generation: this.sessionGeneration, sessionId: this.activeSessionId }
  }

  addAnalysisOutcomeListener(cb: (o: AnalysisOutcome) => void) {
    this.analysisOutcomeListeners.add(cb)
    return () => {
      this.analysisOutcomeListeners.delete(cb)
    }
  }

  addAnalysisResetListener(cb: (info: AnalysisResetInfo) => void) {
    this.analysisResetListeners.add(cb)
    return () => {
      this.analysisResetListeners.delete(cb)
    }
  }

  /** Single fan-out point, stamping current generation/sessionId (+ seq). */
  private emitOutcome(
    o: Omit<AnalysisOutcome, 'seq' | 'generation' | 'sessionId'>,
  ) {
    const outcome: AnalysisOutcome = {
      seq: this.outcomeSeq++,
      generation: this.sessionGeneration,
      sessionId: this.activeSessionId,
      ...o,
    }
    for (const listener of this.analysisOutcomeListeners) {
      listener(outcome)
    }
  }

  /** Synchronously notify reset listeners (K4) — must run in the same task as the
   *  generation bump, before any queued microtask (e.g. a buffered alert flush). */
  private emitReset(fromMoveIndex?: number) {
    const info: AnalysisResetInfo = {
      ...this.getEpoch(),
      ...(fromMoveIndex !== undefined ? { fromMoveIndex } : {}),
    }
    for (const listener of this.analysisResetListeners) {
      listener(info)
    }
  }

  /** Controller-facing: a move whose analyzeMove() returned undefined (synthetic
   *  id) never enters the coordinator's pending maps, so emit its sole terminal
   *  `skipped` outcome here. Guarded against double emission (K3). */
  markSkipped(moveIndex: number, requestId: string) {
    if (this.skippedRequestIds.has(requestId)) return
    this.skippedRequestIds.add(requestId)
    // Leave lineage so a later retry for this index can find its predecessor (L3).
    this.lastRequestIdByMoveIndex.set(moveIndex, requestId)
    this.emitOutcome({ moveIndex, requestId, status: 'skipped' })
  }

  /**
   * Synchronous revert pruning (M1): for every index >= k cancel in-flight
   * requests, clear resolution/timer/cache state, reject waiters, drop store
   * analyses and lineage. Pruned request ids become tombstones (N1) so a
   * worker message already queued for them is dropped, not mistaken for a
   * non-indexed result. Called from rewindBoardLocally; the worker is NOT
   * terminated, so tombstones persist until the next worker replacement.
   */
  pruneFromMoveIndex(k: number) {
    const indices = new Set<number>()
    for (const idx of this.resolutionState.keys()) if (idx >= k) indices.add(idx)
    for (const idx of this.latestRequestIds.keys()) if (idx >= k) indices.add(idx)
    for (const idx of this.lastRequestIdByMoveIndex.keys()) if (idx >= k) indices.add(idx)
    for (const idx of this.resolvedIndices) if (idx >= k) indices.add(idx)

    for (const idx of indices) {
      const entry = this.resolutionState.get(idx)
      if (entry) {
        if (entry.watchdogTimer) clearTimeout(entry.watchdogTimer)
        if (entry.cacheTimer) clearTimeout(entry.cacheTimer)
        this.resolutionState.delete(idx)
      }
      const requestId = this.latestRequestIds.get(idx)
      if (requestId) {
        this.cancelWorkerAnalysis(requestId)
        this.clearActiveAnalysisStateIfCurrent(requestId)
        this.pendingMoveIndices.delete(requestId)
        this.pendingMeta.delete(requestId)
        // Tombstone, do NOT delete from requestIdToMoveIndex (N1).
        this.requestIdToMoveIndex.delete(requestId)
        this.discardedRequestIds.add(requestId)
      }
      const waiters = this.analysisWaiters.get(idx)
      if (waiters) {
        this.analysisWaiters.delete(idx)
        for (const waiter of waiters) waiter.reject(new Error('Analysis reverted'))
      }
      // Drop drill truth (record + waiters) for the reverted index.
      this.rejectAndClearDrillTruth(idx, new Error('Analysis reverted'))
      // Drop the skipped-dedup guard for this index's synthetic id so a replayed
      // move (deterministic `analysis-{idx}-{uci}` id) can emit `skipped` again
      // and re-terminate the frontier slot.
      const lineageId = this.lastRequestIdByMoveIndex.get(idx)
      if (lineageId) this.skippedRequestIds.delete(lineageId)
      this.latestRequestIds.delete(idx)
      this.lastRequestIdByMoveIndex.delete(idx)
      this.resolvedIndices.delete(idx)
      this.store.getState().removeAnalysis(idx)
    }
    // Drop not-yet-dispatched cache lookups for pruned indices.
    if (this.pendingCacheLookups.length > 0) {
      this.pendingCacheLookups = this.pendingCacheLookups.filter(p => p.moveIndex < k)
    }
    // Synchronously notify the consumer to prune its frontier/alert for >= k (K4).
    this.emitReset(k)
  }

  /** Emit `failed` for every still-unresolved index, then the caller clears
   *  resolution state. Used by same-generation termination (restart / fatal /
   *  clearAnalysis) so the recording frontier never strands a `pending` slot.
   *  Lineage (lastRequestIdByMoveIndex) is preserved for an immediate retry (L3). */
  private terminateAllPendingAsFailed() {
    for (const [moveIndex, entry] of this.resolutionState) {
      this.emitOutcome({ moveIndex, requestId: entry.requestId, status: 'failed' })
    }
  }

  private cancelWorkerAnalysis(requestId: string) {
    if (!this.worker) return
    this.worker.postMessage({ type: 'cancel-analysis', id: requestId } satisfies AnalysisWorkerRequest)
  }

  private clearActiveAnalysisStateIfCurrent(requestId: string) {
    if (this.currentAnalyzingRequestId !== requestId) {
      return
    }

    const s = this.store.getState()
    this.currentAnalyzingRequestId = null
    this.lastStreamingUpdateMs = 0
    s.setIsAnalyzing(false)
    s.setAnalyzingMove(null)
    s.setStreamingEval(null)
  }

  private rejectAnalysisWaiters(error: Error) {
    for (const waiters of this.analysisWaiters.values()) {
      for (const waiter of waiters) {
        waiter.reject(error)
      }
    }
    this.analysisWaiters.clear()
  }

  // --- Inactivity watchdog ---

  /** (Re)arm the per-request inactivity timer for an entry. On fire the request
   *  is failed as `inactivity`. */
  private armWatchdog(moveIndex: number, entry: ResolutionEntry) {
    if (entry.watchdogTimer) clearTimeout(entry.watchdogTimer)
    entry.watchdogTimer = setTimeout(() => {
      this.failRequest(moveIndex, entry.requestId, 'inactivity')
    }, ANALYSIS_INACTIVITY_TIMEOUT_MS)
  }

  /** Serial-worker liveness: re-arm the watchdog of every still-pending entry
   *  that has NOT started yet (queued behind the active request). The worker is a
   *  single serial engine, so a live active request's progress proves it is alive
   *  and draining toward those queued requests. Also called directly by `ready`
   *  (no id) to (re)arm everything once the engine is live. */
  private bumpQueuedWatchdogs() {
    for (const [idx, entry] of this.resolutionState) {
      if (entry.started) continue
      if (this.resolvedIndices.has(idx)) continue
      this.armWatchdog(idx, entry)
    }
  }

  /** Reset the watchdog from observed activity for `requestId`. SELF-GUARDING: it
   *  does NOTHING unless `requestId` is the LIVE, CURRENT owner of its index — so
   *  a superseded id (entry now owned by the replacement), a failed id (entry
   *  deleted), a resolved id, or a discarded id is inert and can neither revive
   *  nor fail a newer request, nor keep queued work alive (review finding 1). For
   *  a live request: mark it `started` and re-arm ITS watchdog (own activity),
   *  then bump the queued (not-started) entries (serial-worker liveness). */
  private noteActivity(requestId: string) {
    const idx = this.requestIdToMoveIndex.get(requestId)
    if (idx === undefined) return
    const entry = this.resolutionState.get(idx)
    if (!entry || entry.requestId !== requestId) return
    if (this.latestRequestIds.get(idx) !== requestId) return
    if (this.resolvedIndices.has(idx)) return
    entry.started = true
    this.armWatchdog(idx, entry)
    this.bumpQueuedWatchdogs()
  }

  // --- Drill-truth side channel (Phase 6) ---

  /** Resolve drill-truth waiters bound to (moveIndex, requestId); a waiter for a
   *  different request or stale generation is rejected (it was superseded). */
  private fulfillDrillTruthWaiters(
    moveIndex: number,
    requestId: string,
    truth: DrillTruth | null,
  ) {
    const waiters = this.drillTruthWaiters.get(moveIndex)
    if (!waiters) return
    this.drillTruthWaiters.delete(moveIndex)
    for (const waiter of waiters) {
      if (waiter.generation === this.sessionGeneration && waiter.requestId === requestId) {
        waiter.resolve(truth)
      } else {
        waiter.reject(new Error('Analysis superseded'))
      }
    }
  }

  /** Record settled drill truth (trusted exact-best) for a request and drain its
   *  waiters. A present record is the authoritative, current truth for the index. */
  private recordDrillTruth(moveIndex: number, requestId: string, truth: DrillTruth) {
    this.drillTruth.set(moveIndex, { requestId, truth })
    this.fulfillDrillTruthWaiters(moveIndex, requestId, truth)
  }

  /** Settle drill truth as `null` (no trusted exact-best) for a request UNLESS a
   *  truth record already exists. Called from every terminal cache path so a
   *  drill grade waiter can never hang; idempotent via the existing-record guard.
   *  The null record persists so a later `waitForDrillGrade` still settles fast. */
  private settleDrillTruthNull(moveIndex: number, requestId: string) {
    if (this.drillTruth.has(moveIndex)) return
    this.drillTruth.set(moveIndex, { requestId, truth: null })
    this.fulfillDrillTruthWaiters(moveIndex, requestId, null)
  }

  /** Reject every drill-truth waiter (lifecycle resets); mirrors
   *  `rejectAnalysisWaiters`. Does NOT clear the record map — callers that reset
   *  records (clearAllResolutionState / clearAnalysis / destroy) do that. */
  private rejectDrillTruthWaiters(error: Error) {
    for (const waiters of this.drillTruthWaiters.values()) {
      for (const waiter of waiters) waiter.reject(error)
    }
    this.drillTruthWaiters.clear()
  }

  /** Reject only the drill-truth waiters bound to a specific request and drop the
   *  index's truth record (supersession / per-request failure). */
  private rejectDrillTruthForRequest(moveIndex: number, requestId: string, error: Error) {
    this.drillTruth.delete(moveIndex)
    const waiters = this.drillTruthWaiters.get(moveIndex)
    if (!waiters) return
    const remaining = new Set<DrillTruthWaiter>()
    for (const waiter of waiters) {
      if (waiter.requestId === requestId) waiter.reject(error)
      else remaining.add(waiter)
    }
    if (remaining.size > 0) this.drillTruthWaiters.set(moveIndex, remaining)
    else this.drillTruthWaiters.delete(moveIndex)
  }

  /** Drop an index's truth record and reject ALL its waiters (revert prune). */
  private rejectAndClearDrillTruth(moveIndex: number, error: Error) {
    this.drillTruth.delete(moveIndex)
    const waiters = this.drillTruthWaiters.get(moveIndex)
    if (!waiters) return
    this.drillTruthWaiters.delete(moveIndex)
    for (const waiter of waiters) waiter.reject(error)
  }

  /** Settled-aware read of drill truth for an index. Fast-path resolves a present
   *  record (always current — supersession/clear remove stale ones). With no
   *  record and no current request, resolves `null` (caller falls back to the
   *  worker via `waitForAnalysis`). Otherwise registers a request-bound waiter. */
  private waitForDrillTruth(
    moveIndex: number,
    requestId: string | undefined,
    generation: number,
  ): Promise<DrillTruth | null> {
    const rec = this.drillTruth.get(moveIndex)
    if (rec) return Promise.resolve(rec.truth)
    // No settled record. Resolve null (NOT reject — that is the forbidden
    // waitForAnalysis mirror) when there is no live request for this index:
    // never scheduled (requestId undefined) OR already failed/torn down
    // (requestId no longer pending). The caller delegates to waitForAnalysis,
    // whose own analysisMap fast path returns any settled result and otherwise
    // rejects to recovery — so a waiter that could never settle is never made.
    if (requestId === undefined || !this.pendingMoveIndices.has(requestId)) {
      return Promise.resolve(null)
    }
    return new Promise((resolve, reject) => {
      const waiter: DrillTruthWaiter = { generation, requestId, resolve, reject }
      const waiters = this.drillTruthWaiters.get(moveIndex) ?? new Set<DrillTruthWaiter>()
      waiters.add(waiter)
      this.drillTruthWaiters.set(moveIndex, waiters)
    })
  }

  /**
   * Drill-accuracy grade for a played move (g-position-analysis Phase 6).
   * DRILL-TRUTH FIRST for BOTH strictness tiers: it reads the trusted-cache
   * exact-best truth before ever touching the worker, so it grades from cache
   * even when the PUBLISHED path released to the worker (a post-split move row
   * can be CP-trusted yet carry no publishable `eval_delta`).
   *
   *  - strictness <= 0: exact-best. Truth present -> compare the played move to
   *    `best_move_uci` (no eval needed). Truth null -> worker fallback.
   *  - strictness  > 0: threshold. Truth with a non-null `positionEvalLossCp` ->
   *    grade from that backend-derived loss WITHOUT awaiting the worker (mate /
   *    cross-profile cases already left it null on the backend). Else -> worker.
   *
   * The worker fallback delegates to `waitForAnalysis`, which returns a settled
   * result via its own `analysisMap` fast path and rejects only when nothing is
   * scheduled/pending — so `waitForDrillGrade` works whether called BEFORE or
   * AFTER settlement, and its sole reject path is "no request, no settled data"
   * (handled by the caller's try/catch -> drill recovery).
   */
  async waitForDrillGrade(
    moveIndex: number,
    playedMoveUci: string,
    strictnessCp: number,
  ): Promise<DrillGrade> {
    // Custom preamble (NOT a mirror of waitForAnalysis's pending-state reject):
    // drillTruth / analysisMap may already hold the result after a fast
    // settlement that tore down pending state, so do NOT reject on missing
    // pending state here. The only reject is delegated to waitForAnalysis.
    const generation = this.sessionGeneration
    const requestId = this.latestRequestIds.get(moveIndex)
    const truth = await this.waitForDrillTruth(moveIndex, requestId, generation)

    if (strictnessCp <= 0) {
      if (truth) {
        return {
          grade: gradeDrillMove(null, 0, playedMoveUci === truth.best_move_uci),
          bestMove: truth.best_move_uci,
          source: 'position',
        }
      }
      return this.workerDrillFallback(moveIndex, playedMoveUci, strictnessCp)
    }

    if (truth && truth.positionEvalLossCp !== null) {
      return {
        grade: gradeDrillMove(
          truth.positionEvalLossCp,
          strictnessCp,
          playedMoveUci === truth.best_move_uci,
        ),
        bestMove: truth.best_move_uci,
        source: 'position',
      }
    }
    return this.workerDrillFallback(moveIndex, playedMoveUci, strictnessCp)
  }

  private async workerDrillFallback(
    moveIndex: number,
    playedMoveUci: string,
    strictnessCp: number,
  ): Promise<DrillGrade> {
    const analysis = await this.waitForAnalysis(moveIndex)
    return {
      grade: gradeDrillMove(analysis.delta, strictnessCp, analysis.bestMove === playedMoveUci),
      bestMove: analysis.bestMove,
      source: 'worker',
    }
  }

  // --- Worker lifecycle ---

  private ensureWorker() {
    if (this.worker) return
    this.worker = new Worker(
      new URL('../workers/analysisWorker.ts', import.meta.url),
      { type: 'module' },
    )
    this.worker.addEventListener('message', this.handleWorkerMessage)
    this.worker.addEventListener('error', this.handleWorkerError)
    this.store.getState().resetTransient()
    // A fresh worker is not yet ready: the inactivity watchdog stays disarmed
    // until `ready` arrives, bounded meanwhile by the boot backstop.
    this.engineReady = false
    if (this.bootWatchdogTimer) clearTimeout(this.bootWatchdogTimer)
    this.bootWatchdogTimer = setTimeout(() => {
      this.bootWatchdogTimer = null
      this.handleFatalError('Analysis engine failed to start')
    }, ANALYSIS_BOOT_TIMEOUT_MS)
    this.resetIdleTimer()
  }

  private terminateWorker() {
    if (this.idleTimer) {
      clearTimeout(this.idleTimer)
      this.idleTimer = null
    }
    if (this.bootWatchdogTimer) {
      clearTimeout(this.bootWatchdogTimer)
      this.bootWatchdogTimer = null
    }
    this.engineReady = false
    if (!this.worker) return
    this.worker.removeEventListener('message', this.handleWorkerMessage)
    this.worker.removeEventListener('error', this.handleWorkerError)
    this.worker.terminate()
    this.worker = null
    // The replaced worker can no longer message about pruned requests (N1).
    this.discardedRequestIds.clear()
  }

  private resetIdleTimer() {
    if (this.idleTimer) clearTimeout(this.idleTimer)
    this.idleTimer = setTimeout(() => {
      // Only shut down if no active session and no pending uploads
      if (!this.activeSessionId && !this.hasPendingUploads()) {
        this.terminateWorker()
      }
    }, IDLE_SHUTDOWN_MS)
  }

  private hasPendingUploads(): boolean {
    return this.uploadState !== null && this.uploadState.dirtyIndices.size > 0
  }

  // --- Session lifecycle ---

  startSession(sessionId: string) {
    // If switching sessions, finalize old one
    if (this.activeSessionId && this.activeSessionId !== sessionId) {
      this.finalizeOldSession()
    }

    this.activeSessionId = sessionId
    this.sessionGeneration++
    // Synchronous reset BEFORE clearing pending requests, so a buffered
    // microtask (alert flush) sees the bumped epoch first (K1/K4).
    this.emitReset()
    this.lastRequestIdByMoveIndex.clear()
    this.skippedRequestIds.clear()
    this.rejectAnalysisWaiters(new Error('Analysis session changed'))
    this.rejectDrillTruthWaiters(new Error('Analysis session changed'))
    this.clearAllResolutionState()
    this.resolvedIndices.clear()
    this.pendingMoveIndices.clear()
    this.pendingMeta.clear()
    this.latestRequestIds.clear()
    this.pendingCacheLookups = []
    this.currentAnalyzingRequestId = null
    if (this.cacheFlushTimer) {
      clearTimeout(this.cacheFlushTimer)
      this.cacheFlushTimer = null
    }
    this.lastStreamingUpdateMs = 0
    this.store.getState().clearAll()
    // clearAll doesn't reset status — do it explicitly so a prior worker
    // error doesn't stick across sessions.
    this.store.getState().setStatus('booting')

    // Reset the worker between gameplay sessions so stale queue entries and
    // accumulated Stockfish state do not leak into the next game.
    this.terminateWorker()

    this.uploadState = {
      sessionId,
      uploadedIndices: new Set(),
      dirtyIndices: new Set(),
      uploadInFlight: false,
      abortController: null,
      retryCount: 0,
      retryTimer: null,
      detached: false,
      uploadsEnabled: true,
    }

    this.startIncrementalUploadTimer()
    this.ensureWorker()
  }

  clearSession() {
    this.finalizeOldSession()
    this.activeSessionId = null
    this.sessionGeneration++
    this.emitReset()
    this.lastRequestIdByMoveIndex.clear()
    this.skippedRequestIds.clear()
    this.rejectAnalysisWaiters(new Error('Analysis session cleared'))
    this.rejectDrillTruthWaiters(new Error('Analysis session cleared'))
    this.clearAllResolutionState()
    this.store.getState().clearAll()
    this.resolvedIndices.clear()
    this.pendingMoveIndices.clear()
    this.pendingMeta.clear()
    this.latestRequestIds.clear()
    this.pendingCacheLookups = []
    this.currentAnalyzingRequestId = null
    if (this.cacheFlushTimer) {
      clearTimeout(this.cacheFlushTimer)
      this.cacheFlushTimer = null
    }
    this.lastStreamingUpdateMs = 0
    this.terminateWorker()
  }

  private finalizeOldSession() {
    this.stopIncrementalUploadTimer()

    if (this.uploadState) {
      if (!this.uploadState.uploadsEnabled) {
        this.cancelUploadState(this.uploadState)
        this.uploadState = null
        return
      }

      // Mark the upload state as detached so the in-flight success handler
      // knows to drain ALL remaining dirty indices (not just >= threshold).
      this.uploadState.detached = true

      // Flush remaining dirty uploads for the old session.
      // The payload is frozen at flush time (issue #2 fix), so any pending
      // retry will re-send the old session's data to the old session ID —
      // it cannot accidentally serialize the new session's moves.
      if (this.uploadState.dirtyIndices.size > 0) {
        this.flushIncrementalUpload(this.uploadState)
      }
      // Do NOT cancel the retry timer — let it complete with frozen payload.
      // Detach from coordinator so the retry closure is self-contained.
      this.uploadState = null
    }
  }

  // --- Analysis API ---

  analyzeMove(
    fen: string,
    move: string,
    playerColor: 'white' | 'black',
    moveIndex?: number,
    legalMoveCount?: number,
  ): string | undefined {
    if (this.store.getState().status === 'error') return
    this.ensureWorker()
    if (!this.worker) return

    const id = createRequestId()
    if (moveIndex !== undefined) {
      const previousRequestId = this.latestRequestIds.get(moveIndex)
      if (previousRequestId && previousRequestId !== id) {
        this.cancelWorkerAnalysis(previousRequestId)
      }

      // Supersede any prior resolution state for this index: clear BOTH timers
      // and immediately reject waiters bound to the superseded request so a
      // caller awaiting the old request does not hang (Finding F2).
      const prevEntry = this.resolutionState.get(moveIndex)
      if (prevEntry) {
        if (prevEntry.watchdogTimer) clearTimeout(prevEntry.watchdogTimer)
        if (prevEntry.cacheTimer) clearTimeout(prevEntry.cacheTimer)
        this.rejectWaitersForRequest(moveIndex, prevEntry.requestId, new Error('Analysis superseded'))
      }
      // Drop any prior drill truth for this index (record + waiters) so the
      // fast-path invariant holds: a present drillTruth record is ALWAYS for the
      // current request. Unconditional — an already-RESOLVED index (no prevEntry)
      // can be re-opened here and would otherwise leave a stale record.
      this.rejectAndClearDrillTruth(moveIndex, new Error('Analysis superseded'))

      this.store.getState().removeAnalysis(moveIndex)
      this.pendingMoveIndices.set(id, moveIndex)
      this.pendingMeta.set(id, { moveIndex, legalMoveCount })
      this.latestRequestIds.set(moveIndex, id)
      this.requestIdToMoveIndex.set(id, moveIndex)
      this.resolvedIndices.delete(moveIndex)

      const entry: ResolutionEntry = { requestId: id, cacheStatus: 'pending' }
      this.resolutionState.set(moveIndex, entry)
      // Arm the inactivity watchdog only once the engine is ready; a request
      // scheduled while still booting is armed later by `ready` →
      // bumpQueuedWatchdogs() (engine-boot strategy — the 8s window must not
      // false-fail the first request during a slow cold WASM boot).
      if (this.engineReady) this.armWatchdog(moveIndex, entry)

      // Outcome: supersession/retry lineage (L3). previousRequestId comes from
      // the dedicated lineage map, NOT latestRequestIds (cleared by same-gen
      // cleanup). Emit `scheduled` so the consumer (re)opens the slot to pending
      // under the new id and migrates context/SRS old->new.
      const prevLineageId = this.lastRequestIdByMoveIndex.get(moveIndex)
      this.lastRequestIdByMoveIndex.set(moveIndex, id)
      this.discardedRequestIds.delete(id)
      this.skippedRequestIds.delete(id)
      // A retry overwrites lineage, so clear the predecessor's skip-dedup guard
      // too — otherwise a skipped synthetic id orphaned here would suppress a
      // later replay of that same deterministic id after a prune (P2).
      if (prevLineageId) this.skippedRequestIds.delete(prevLineageId)
      this.emitOutcome({
        moveIndex,
        requestId: id,
        status: 'scheduled',
        ...(prevLineageId && prevLineageId !== id ? { previousRequestId: prevLineageId } : {}),
      })
    }

    const message: AnalyzeMoveMessage = {
      type: 'analyze-move',
      id,
      fen,
      move,
      playerColor,
      ...(moveIndex !== undefined ? { moveIndex } : {}),
      ...(legalMoveCount !== undefined ? { legalMoveCount } : {}),
    }
    this.worker.postMessage(message)

    if (moveIndex !== undefined) {
      this.scheduleCacheLookup({ requestId: id, fen, move, moveIndex, playerColor, legalMoveCount })
    }

    this.resetIdleTimer()
    return id
  }

  restartAnalysisWorker() {
    this.currentAnalyzingRequestId = null
    this.lastStreamingUpdateMs = 0
    // Emit `failed` per unresolved index (same generation) BEFORE teardown so the
    // recording frontier advances; lineage is preserved for an immediate retry.
    this.terminateAllPendingAsFailed()
    this.terminateWorker()
    // Terminating the worker orphans every in-flight request — buffered,
    // pending, and worker-failed alike — so reject all their waiters rather
    // than leaving them to hang until the inactivity watchdog fires (Finding R4).
    this.clearAllResolutionState()
    this.rejectAnalysisWaiters(new Error('Analysis worker restarted'))
    this.rejectDrillTruthWaiters(new Error('Analysis worker restarted'))
    this.pendingMoveIndices.clear()
    this.pendingMeta.clear()
    const s = this.store.getState()
    s.setStatus('booting')
    s.setError(null)
    s.setIsAnalyzing(false)
    s.setAnalyzingMove(null)
    s.setStreamingEval(null)
    this.ensureWorker()
  }

  /** Clear every resolution-state entry and its timers, plus the requestId↔index
   *  tombstone map and the cache-batch age clock. Used by all lifecycle paths. */
  private clearAllResolutionState() {
    for (const entry of this.resolutionState.values()) {
      if (entry.watchdogTimer) clearTimeout(entry.watchdogTimer)
      if (entry.cacheTimer) clearTimeout(entry.cacheTimer)
    }
    this.resolutionState.clear()
    this.requestIdToMoveIndex.clear()
    this.cacheBatchFirstEnqueuedAt = null
    // Drop all settled drill-truth records (waiters are rejected by the callers
    // that also reject analysisWaiters — clearAllResolutionState never strands a
    // waiter because every caller pairs it with rejectDrillTruthWaiters).
    this.drillTruth.clear()
  }

  waitForAnalysis(moveIndex: number): Promise<AnalysisResult> {
    const existing = this.store.getState().analysisMap.get(moveIndex)
    if (existing) {
      return Promise.resolve(existing)
    }
    if (this.store.getState().status === 'error') {
      return Promise.reject(new Error(this.store.getState().error ?? 'Analysis worker unavailable'))
    }
    const requestId = this.latestRequestIds.get(moveIndex)
    if (!requestId) {
      return Promise.reject(new Error('Analysis was not scheduled for this move'))
    }
    if (!this.pendingMoveIndices.has(requestId) && !this.pendingMeta.has(requestId)) {
      return Promise.reject(new Error('Analysis is not pending for this move'))
    }

    const generation = this.sessionGeneration
    return new Promise((resolve, reject) => {
      const waiter: AnalysisWaiter = { generation, requestId, resolve, reject }
      const waiters = this.analysisWaiters.get(moveIndex) ?? new Set<AnalysisWaiter>()
      waiters.add(waiter)
      this.analysisWaiters.set(moveIndex, waiters)
    })
  }

  /**
   * Block until every given move index has settled, or until `budgetMs` elapses
   * — whichever comes first (g-2nrn).
   *
   * NEVER rejects. A timeout, a failed analysis, an index that was never
   * scheduled and a superseded request are all the same outcome to the only
   * caller (the terminal full-history upload): proceed with whatever
   * `analysisMap` holds. Settling is a synchronization barrier here, not a
   * value fetch — the caller re-reads `analysisMap` afterwards — so this
   * deliberately does not expose `waitForAnalysis`'s rejection contract, which
   * rejects SYNCHRONOUSLY for a never-scheduled index and would throw into any
   * caller that did not individually await it.
   *
   * Already-resolved indices cost nothing: no waiter is registered and no timer
   * is armed, so the common fully-settled tail adds zero latency.
   *
   * On expiry every still-pending waiter is DEREGISTERED. `waitForAnalysis`
   * has no timeout of its own, so a plain `Promise.race` at the call site would
   * strand its waiter in `analysisWaiters` for the rest of the session.
   */
  async settleWithin(moveIndices: number[], budgetMs: number): Promise<void> {
    if (budgetMs <= 0) return
    // A dead worker never settles anything; waiting out the full budget would
    // burn terminal-action latency for nothing.
    if (this.store.getState().status === 'error') return

    const analysisMap = this.store.getState().analysisMap
    const pending = moveIndices.filter((idx) => !analysisMap.has(idx))
    if (pending.length === 0) return

    const registered: Array<{ moveIndex: number; waiter: AnalysisWaiter }> = []
    const settled = pending.map(
      (moveIndex) =>
        new Promise<void>((resolve) => {
          const requestId = this.latestRequestIds.get(moveIndex)
          // Never scheduled, or already terminal/superseded: nothing to await.
          if (
            !requestId ||
            (!this.pendingMoveIndices.has(requestId) &&
              !this.pendingMeta.has(requestId))
          ) {
            resolve()
            return
          }
          const waiter: AnalysisWaiter = {
            generation: this.sessionGeneration,
            requestId,
            // Both legs collapse to "settled" — the result is read from
            // analysisMap, and a rejection still means we should stop waiting.
            resolve: () => resolve(),
            reject: () => resolve(),
          }
          const waiters =
            this.analysisWaiters.get(moveIndex) ?? new Set<AnalysisWaiter>()
          waiters.add(waiter)
          this.analysisWaiters.set(moveIndex, waiters)
          registered.push({ moveIndex, waiter })
        }),
    )
    if (registered.length === 0) return

    let budgetTimer: ReturnType<typeof setTimeout> | undefined
    try {
      await Promise.race([
        Promise.all(settled),
        new Promise<void>((resolve) => {
          budgetTimer = setTimeout(resolve, budgetMs)
        }),
      ])
    } finally {
      // Clear unconditionally: a settled tail must not leave a live handle
      // behind (it would hang fake-timer tests and outlive the session).
      if (budgetTimer !== undefined) clearTimeout(budgetTimer)
      for (const { moveIndex, waiter } of registered) {
        this.deregisterWaiter(moveIndex, waiter)
      }
    }
  }

  /** Drop a single waiter WITHOUT settling it, pruning the index entry when it
   *  empties. Used when a bounded wait gives up: the analysis may still resolve
   *  later, and must not fire into an abandoned waiter. */
  private deregisterWaiter(moveIndex: number, waiter: AnalysisWaiter) {
    const waiters = this.analysisWaiters.get(moveIndex)
    if (!waiters) return
    waiters.delete(waiter)
    if (waiters.size === 0) {
      this.analysisWaiters.delete(moveIndex)
    }
  }

  clearAnalysis() {
    this.store.getState().clearAll()
    this.lastStreamingUpdateMs = 0
    // Same-generation termination: fail every unresolved index so the frontier
    // advances; lineage retained for an immediate retry (L3).
    this.terminateAllPendingAsFailed()
    // Reject any unresolved waiters — clearing orphans every in-flight request
    // (Finding R4).
    for (const entry of this.resolutionState.values()) {
      if (entry.watchdogTimer) clearTimeout(entry.watchdogTimer)
      if (entry.cacheTimer) clearTimeout(entry.cacheTimer)
    }
    this.resolutionState.clear()
    this.cacheBatchFirstEnqueuedAt = null
    this.rejectAnalysisWaiters(new Error('Analysis cleared'))
    // clearAnalysis inlines its teardown (no clearAllResolutionState call), so
    // clear the drill-truth records and reject their waiters here too.
    this.drillTruth.clear()
    this.rejectDrillTruthWaiters(new Error('Analysis cleared'))
    this.pendingMoveIndices.clear()
    this.pendingMeta.clear()
    this.latestRequestIds.clear()
    this.resolvedIndices.clear()
    this.pendingCacheLookups = []
    // Intentionally retain requestIdToMoveIndex as a tombstone: clearAnalysis
    // does NOT terminate the worker, so a late worker `analysis` for a now-
    // discarded indexed request must still be recognized as indexed (its entry
    // is gone → dropped), never mistaken for an ad-hoc result that would
    // repopulate lastAnalysis and retrigger subscribers (Finding 1).
    this.currentAnalyzingRequestId = null
    if (this.cacheFlushTimer) {
      clearTimeout(this.cacheFlushTimer)
      this.cacheFlushTimer = null
    }
  }

  // --- Worker message handling ---

  private handleWorkerMessage = (event: MessageEvent<AnalysisWorkerResponse>) => {
    const message = event.data
    const s = this.store.getState()

    switch (message.type) {
      case 'ready':
        // A stray late `ready` (e.g. after the boot-timeout fatal path) must not
        // flip 'error' back to 'ready', re-arm watchdogs, or clear the boot guard
        // on a dead worker. terminateWorker's listener removal isolates a replaced
        // worker, so no separate generation check is needed.
        if (s.status === 'error') break
        this.engineReady = true
        if (this.bootWatchdogTimer) {
          clearTimeout(this.bootWatchdogTimer)
          this.bootWatchdogTimer = null
        }
        s.setStatus('ready')
        // Arm the inactivity watchdog for every request scheduled while booting.
        this.bumpQueuedWatchdogs()
        break
      case 'analysis-started': {
        // Drop late worker messages after a fatal error (Finding F1).
        if (s.status === 'error') break
        // Drop messages for pruned/reverted requests (N1) before any indexed
        // lookup so they never re-show the spinner.
        if (this.discardedRequestIds.has(message.id)) break
        // Gate by request state so a late start after a cache hit cancels does
        // not re-show the spinner (Finding R5), AND require a LIVE resolution
        // entry so a late start after a watchdog/worker failRequest (entry gone,
        // but the id still in latestRequestIds) cannot revive the spinner for a
        // request that already failed.
        const startIdx = this.requestIdToMoveIndex.get(message.id)
        if (startIdx !== undefined) {
          const entry = this.resolutionState.get(startIdx)
          if (
            !entry ||
            entry.requestId !== message.id ||
            this.latestRequestIds.get(startIdx) !== message.id ||
            this.resolvedIndices.has(startIdx)
          ) {
            break
          }
        }
        this.noteActivity(message.id)
        this.currentAnalyzingRequestId = message.id
        s.setIsAnalyzing(true)
        s.setAnalyzingMove(message.move)
        break
      }
      case 'analysis-streaming': {
        if (s.status === 'error') break
        if (this.discardedRequestIds.has(message.id)) break
        this.noteActivity(message.id)
        const streamIdx = this.pendingMoveIndices.get(message.id)
        if (
          streamIdx !== undefined &&
          this.latestRequestIds.get(streamIdx) === message.id &&
          !this.resolvedIndices.has(streamIdx)
        ) {
          const now = performance.now()
          if (now - this.lastStreamingUpdateMs >= 250) {
            this.lastStreamingUpdateMs = now
            this.store.getState().setStreamingEval({ moveIndex: streamIdx, cp: message.cp })
          }
        }
        break
      }
      case 'analysis-progress': {
        // Liveness-only ping (root/post-played/post-best). Drop after a fatal
        // error or for a pruned/reverted request; otherwise reset the watchdog.
        // noteActivity self-guards against stale/superseded/resolved ids.
        if (s.status === 'error') break
        if (this.discardedRequestIds.has(message.id)) break
        this.noteActivity(message.id)
        break
      }
      case 'analysis': {
        // Drop late worker results after a fatal error (Finding F1).
        if (s.status === 'error') {
          this.clearActiveAnalysisStateIfCurrent(message.id)
          break
        }
        // Drop results for pruned/reverted requests (N1) so they never fall into
        // the non-indexed branch and clobber lastAnalysis.
        if (this.discardedRequestIds.has(message.id)) {
          this.clearActiveAnalysisStateIfCurrent(message.id)
          break
        }
        // Activity: reset the watchdog (self-guarded). Matters when the result is
        // buffered behind a still-pending cache so the wait is not killed.
        this.noteActivity(message.id)

        const moveIndex = this.requestIdToMoveIndex.get(message.id)
        if (moveIndex !== undefined) {
          // Known indexed request (possibly already settled). Never treat it as
          // non-indexed \u2014 that would clobber lastAnalysis (Finding G1).
          this.clearActiveAnalysisStateIfCurrent(message.id)
          const entry = this.resolutionState.get(moveIndex)
          if (
            !entry ||
            entry.requestId !== message.id ||
            this.latestRequestIds.get(moveIndex) !== message.id ||
            this.resolvedIndices.has(moveIndex)
          ) {
            // Stale / superseded / already resolved \u2192 drop.
            break
          }

          const result = this.buildWorkerResult(message, moveIndex)
          if (entry.cacheStatus === 'pending') {
            // Hold the worker result until the authoritative cache settles.
            // Keep pendingMoveIndices/pendingMeta so waitForAnalysis still
            // registers a waiter (Finding 3).
            entry.bufferedWorker = result
          } else {
            this.resolveAnalysisResult(moveIndex, result)
            console.log(
              `[Analyst] resolve idx=${moveIndex} source=worker(${entry.releaseReason ?? 'released'})`,
            )
            if (result.blunder && message.delta !== null) {
              console.log(
                `[Analyst] Blunder detected: \u0394${message.delta}cp (best ${message.bestMove}).`,
              )
            }
          }
          break
        }

        // Genuinely non-indexed (ad-hoc) request.
        this.clearActiveAnalysisStateIfCurrent(message.id)
        this.pendingMeta.delete(message.id)
        const result = this.buildWorkerResult(message, null)
        this.store.getState().setLastAnalysis(result)
        if (result.blunder && message.delta !== null) {
          console.log(
            `[Analyst] Blunder detected: \u0394${message.delta}cp (best ${message.bestMove}).`,
          )
        }
        break
      }
      case 'error': {
        if (message.id !== undefined) {
          // Scoped, request-specific failure \u2014 do NOT set global store status
          // error (Finding R2) so a later trusted cache hit can still recover.
          const idx = this.requestIdToMoveIndex.get(message.id)
          if (idx === undefined) break
          const entry = this.resolutionState.get(idx)
          if (!entry || entry.requestId !== message.id) break
          if (this.resolvedIndices.has(idx)) break
          // Activity: reset the watchdog (self-guarded). Matters when the error is
          // held behind a still-pending cache (workerFailed) awaiting recovery.
          this.noteActivity(message.id)
          this.clearActiveAnalysisStateIfCurrent(message.id)
          if (entry.cacheStatus === 'pending') {
            // A trusted cache hit can still resolve this move; wait for the
            // cache to settle before rejecting (rule 7 workerFailed branch).
            entry.workerFailed = true
            entry.workerError = message.error
          } else {
            // Cache already released the fallback (reverse order) and no worker
            // result is coming \u2014 fail immediately rather than waiting for the
            // inactivity watchdog to expire (Finding 1/2).
            this.failRequest(idx, message.id, 'worker-error', message.error)
          }
          break
        }
        // Unscoped / fatal error (engine/bootstrap).
        this.handleFatalError(message.error)
        break
      }
      case 'log':
        console.log(`[Analyst] ${message.message}`)
        break
      default:
        message satisfies never
    }
  }

  private handleWorkerError = (event: ErrorEvent) => {
    this.handleFatalError(event.message || 'Analysis worker error')
  }

  /**
   * Fatal (unscoped) worker failure: set global error status, reject all
   * waiters, and fully tear down resolution state so an in-flight cache `.then`
   * cannot resolve a move after consumers were told analysis failed (Finding
   * G3). Invalidate the worker so queued messages cannot reopen the late-worker
   * bug (Finding F1).
   */
  private handleFatalError(errorText: string) {
    this.currentAnalyzingRequestId = null
    this.lastStreamingUpdateMs = 0
    const s = this.store.getState()
    s.setStatus('error')
    s.setError(errorText)
    s.setIsAnalyzing(false)
    s.setAnalyzingMove(null)
    s.setStreamingEval(null)

    this.terminateAllPendingAsFailed()
    this.clearAllResolutionState()
    this.pendingMoveIndices.clear()
    this.pendingMeta.clear()
    this.pendingCacheLookups = []
    if (this.cacheFlushTimer) {
      clearTimeout(this.cacheFlushTimer)
      this.cacheFlushTimer = null
    }
    this.rejectAnalysisWaiters(new Error(errorText))
    this.rejectDrillTruthWaiters(new Error(errorText))
    this.terminateWorker()
  }

  // --- Resolution ---

  private fulfillWaiters(moveIndex: number, result: AnalysisResult) {
    const waiters = this.analysisWaiters.get(moveIndex)
    if (!waiters) return
    this.analysisWaiters.delete(moveIndex)
    for (const waiter of waiters) {
      if (
        waiter.generation === this.sessionGeneration &&
        (waiter.requestId === undefined || waiter.requestId === result.id)
      ) {
        waiter.resolve(result)
      } else {
        waiter.reject(new Error('Analysis superseded'))
      }
    }
  }

  /** Reject only the waiters bound to a specific request (e.g. on supersession),
   *  leaving waiters for other requests of the same index untouched. */
  private rejectWaitersForRequest(moveIndex: number, requestId: string, error: Error) {
    const waiters = this.analysisWaiters.get(moveIndex)
    if (!waiters) return
    const remaining = new Set<AnalysisWaiter>()
    for (const waiter of waiters) {
      if (waiter.requestId === requestId) {
        waiter.reject(error)
      } else {
        remaining.add(waiter)
      }
    }
    if (remaining.size > 0) {
      this.analysisWaiters.set(moveIndex, remaining)
    } else {
      this.analysisWaiters.delete(moveIndex)
    }
  }

  private buildWorkerResult(
    message: Extract<AnalysisWorkerResponse, { type: 'analysis' }>,
    moveIndex: number | null,
  ): AnalysisResult {
    const meta = moveIndex !== null ? this.pendingMeta.get(message.id) : undefined
    const forced = meta?.legalMoveCount !== undefined && meta.legalMoveCount <= 2
    const blunder = !forced && message.classification === 'blunder'
    const recordable =
      !forced &&
      isRecordableFailure(message.delta) &&
      (moveIndex !== null ? isWithinRecordingMoveCap(moveIndex) : false)

    return {
      id: message.id,
      move: message.move,
      bestMove: message.bestMove,
      bestLine: message.bestLine,
      bestEval: message.bestEval,
      playedEval: message.playedEval,
      currentPositionEval: message.playedEval,
      playedEvalMate: message.playedEvalMate,
      currentPositionEvalMate: message.playedEvalMate,
      moveIndex: moveIndex ?? null,
      delta: message.delta,
      classification: message.classification,
      blunder,
      recordable,
    }
  }

  /**
   * Release the buffered worker fallback once the cache has settled non-trusted
   * (miss / untrusted / error / timeout). Idempotent and id-guarded. Does NOT
   * clear the inactivity watchdog — a released request still awaiting a silent
   * worker stays protected, failing only after the watchdog window of silence.
   */
  private releaseFallback(moveIndex: number, requestId: string, reason: ReleaseReason) {
    const entry = this.resolutionState.get(moveIndex)
    if (!entry || entry.requestId !== requestId) return
    if (this.resolvedIndices.has(moveIndex)) return
    if (entry.cacheStatus === 'released') return
    if (entry.cacheTimer) {
      clearTimeout(entry.cacheTimer)
      entry.cacheTimer = undefined
    }
    entry.cacheStatus = 'released'
    entry.releaseReason = reason

    // The cache settled non-trusted for this request: no exact-best truth is
    // coming, so settle drill truth null (no-op if a position-only hit already
    // recorded truth) and the drill grade falls back to the worker.
    this.settleDrillTruthNull(moveIndex, requestId)

    if (entry.bufferedWorker) {
      this.resolveAnalysisResult(moveIndex, entry.bufferedWorker)
      console.log(`[Analyst] resolve idx=${moveIndex} source=worker(${reason})`)
    } else if (entry.workerFailed) {
      // Worker already errored and no result will ever come.
      this.failRequest(moveIndex, requestId, 'worker-error', entry.workerError)
    }
    // else: leave released; the worker result resolves on arrival, or the
    // inactivity watchdog terminates the request after a window of silence.
  }

  /**
   * Hard no-hang terminator. Rejects the index's waiters, cancels the worker,
   * clears active state, and drops all per-request state + both timers. (g-hpw4
   * will emit a `failed` outcome here so the recording frontier advances.)
   */
  private failRequest(
    moveIndex: number,
    requestId: string,
    reason: 'inactivity' | 'worker-error',
    errorText?: string,
  ) {
    const entry = this.resolutionState.get(moveIndex)
    if (!entry || entry.requestId !== requestId) return
    if (this.resolvedIndices.has(moveIndex)) return

    if (entry.watchdogTimer) clearTimeout(entry.watchdogTimer)
    if (entry.cacheTimer) clearTimeout(entry.cacheTimer)
    this.resolutionState.delete(moveIndex)
    this.pendingMoveIndices.delete(requestId)
    this.pendingMeta.delete(requestId)

    this.rejectWaitersForRequest(moveIndex, requestId, new Error(errorText ?? 'analysis timed out'))
    // A hard terminal failure rejects any drill-truth waiter for this request and
    // drops its record so a drill grade awaiting truth fails to recovery rather
    // than hanging to the inactivity watchdog.
    this.rejectDrillTruthForRequest(moveIndex, requestId, new Error(errorText ?? 'analysis timed out'))
    this.cancelWorkerAnalysis(requestId)
    this.clearActiveAnalysisStateIfCurrent(requestId)
    // Lineage retained so an immediate retry can migrate this index's context (L3).
    this.emitOutcome({ moveIndex, requestId, status: 'failed' })
    console.log(`[Analyst] resolve idx=${moveIndex} source=failed(${reason})`)
  }

  private resolveAnalysisResult(moveIndex: number, result: AnalysisResult) {
    if (this.latestRequestIds.get(moveIndex) !== result.id) return
    if (this.resolvedIndices.has(moveIndex)) return
    this.resolvedIndices.add(moveIndex)

    // Grain-split best reconciliation (g-move-best-icon / g-jfdj): the TRUSTED
    // position grain names the exact best move (drill-truth is recorded before this
    // terminal resolve on every cache/worker path) and that answer wins over the
    // published classification — promoting a played==best result to 'best' (star),
    // and demoting a fallback that wrongly graded a non-best move 'best' down to
    // 'excellent'. Read truth
    // BEFORE settleDrillTruthNull below; the requestId guard rejects a stale record
    // (supersession clears stale truth, but the result.id match is belt-and-braces).
    const truth = this.drillTruth.get(moveIndex)
    const published =
      truth?.truth && truth.requestId === result.id
        ? reconcileTrustedBest(result, truth.truth.best_move_uci)
        : result

    // Terminal: clear per-request state + both timers + pending metadata.
    const entry = this.resolutionState.get(moveIndex)
    if (entry) {
      if (entry.watchdogTimer) clearTimeout(entry.watchdogTimer)
      if (entry.cacheTimer) clearTimeout(entry.cacheTimer)
      this.resolutionState.delete(moveIndex)
    }
    this.pendingMoveIndices.delete(result.id)
    this.pendingMeta.delete(result.id)

    // A terminal resolve from EITHER channel settles drill truth (no-op if a
    // trusted exact-best hit already recorded it). The null record persists so a
    // post-settlement waitForDrillGrade still settles fast (-> worker fallback,
    // which reads the same resolved result via analysisMap).
    this.settleDrillTruthNull(moveIndex, result.id)

    this.store.getState().resolveAnalysis(moveIndex, published)
    this.fulfillWaiters(moveIndex, published)

    // Mark dirty for incremental upload
    if (
      this.uploadState &&
      this.uploadState.uploadsEnabled &&
      this.uploadState.sessionId === this.activeSessionId
    ) {
      this.uploadState.dirtyIndices.add(moveIndex)
      // Trigger immediate upload if threshold reached
      if (this.uploadState.dirtyIndices.size >= INCREMENTAL_UPLOAD_BATCH_THRESHOLD) {
        this.flushIncrementalUpload(this.uploadState)
      }
    }

    this.emitOutcome({ moveIndex, requestId: result.id, status: 'resolved', result: published })
  }

  // --- Cache lookups ---

  private scheduleCacheLookup(lookup: PendingCacheLookup) {
    if (this.pendingCacheLookups.length === 0) {
      this.cacheBatchFirstEnqueuedAt = Date.now()
    }
    this.pendingCacheLookups.push(lookup)
    if (this.cacheFlushTimer !== null) {
      clearTimeout(this.cacheFlushTimer)
    }
    // Trailing debounce, but capped by a mandatory max batch age so a sustained
    // burst cannot defer dispatch (and the cache-response timer) indefinitely.
    const elapsed = this.cacheBatchFirstEnqueuedAt !== null
      ? Date.now() - this.cacheBatchFirstEnqueuedAt
      : 0
    const delay = Math.max(0, Math.min(CACHE_LOOKUP_DEBOUNCE_MS, CACHE_BATCH_MAX_AGE_MS - elapsed))
    this.cacheFlushTimer = setTimeout(() => {
      this.cacheFlushTimer = null
      this.flushCacheLookups()
    }, delay)
  }

  private flushCacheLookups() {
    const batch = this.pendingCacheLookups.splice(0)
    // Reset the max-batch-age clock every time the batch is emptied so the next
    // batch's first request is not seen as already aged (Finding 2).
    this.cacheBatchFirstEnqueuedAt = null
    if (batch.length === 0) return

    // Capture generation so we can discard results if the session changed
    // while the cache lookup was in flight.
    const gen = this.sessionGeneration

    // Start the cache-response window NOW (at dispatch), not at analyzeMove, so
    // a sliding trailing debounce cannot release a request before its lookup is
    // even sent (Finding 5).
    for (const pending of batch) {
      const entry = this.resolutionState.get(pending.moveIndex)
      if (!entry || entry.requestId !== pending.requestId) continue
      if (entry.cacheStatus !== 'pending') continue
      if (entry.cacheTimer) clearTimeout(entry.cacheTimer)
      entry.cacheTimer = setTimeout(() => {
        this.releaseFallback(pending.moveIndex, pending.requestId, 'timeout')
      }, ANALYSIS_RESOLUTION_TIMEOUT_MS)
    }

    const positions = batch.map(p => ({ fen: p.fen, move_uci: p.move }))

    lookupAnalysisCache(positions)
      .then(results => {
        // Session switched — discard stale results
        if (this.sessionGeneration !== gen) return

        for (const pending of batch) {
          const entry = this.resolutionState.get(pending.moveIndex)
          if (!entry || entry.requestId !== pending.requestId) continue
          // A released (timed-out) worker fallback owns the resolution, so a
          // late trusted hit must not win (Finding R3).
          if (entry.cacheStatus !== 'pending') continue
          if (this.resolvedIndices.has(pending.moveIndex)) continue

          const key = makeCacheKey(pending.fen, pending.move)
          const cached = results.get(key)

          // Drill-truth side channel (Phase 6): record exact-best truth from a
          // trusted position hit BEFORE and INDEPENDENT of the published gate. A
          // position-only hit (no move row) feeds the drill but never publishes.
          // Pure side-channel write — it must NOT touch resolutionState, the
          // worker, uploads, or outcomes; the published gate below runs unchanged.
          // settleDrillTruthNull on the terminal paths (release / resolve) covers
          // every non-exact-best case, so a drill grade waiter can never hang.
          if (cached && isTrustedExactBestHit(cached)) {
            this.recordDrillTruth(pending.moveIndex, pending.requestId, {
              best_move_uci: cached.best_move_uci as string,
              positionEvalLossCp: cached.position_eval_loss_cp ?? null,
            })
          }

          if (
            !cached ||
            !isTrustedPositionHit(cached) ||
            !isTrustedMoveHit(cached) ||
            !hasCpEvalLoss(cached)
          ) {
            // Release the worker fallback unless ALL three concerns pass. This
            // is the PUBLISHED-path gate and it stays STRICT (Phase 6 does NOT
            // relax it): regular-game blunder detection / SRS / uploads / the
            // review board all consume this `AnalysisResult` and need a
            // co-computed CP `eval_delta` snapshot.
            //  - isTrustedPositionHit: trusted, renderable best move/PV so
            //    `bestMove`/PV are grain-correct and exact-best is meaningful.
            //  - isTrustedMoveHit: trusted, renderable played evidence
            //    (classification + a played eval of either kind).
            //  - hasCpEvalLoss: the published snapshot needs a finite CP
            //    `eval_delta`; mate-only / post-split move rows lack one and the
            //    worker publishes instead. The DRILL grade no longer depends on
            //    this gate — it reads the backend-derived `position_eval_loss_cp`
            //    via the drill-truth side channel recorded above (Phase 6), so a
            //    release here does NOT block drill grading.
            // Position-untrusted (null best), move-untrusted, and move-trusted
            // mate-only rows all fall back on the published path.
            const reason: ReleaseReason = cached ? 'untrusted' : 'cache-miss'
            this.releaseFallback(pending.moveIndex, pending.requestId, reason)
            continue
          }

          const result = fromCachedAnalysis(
            pending.requestId,
            cached,
            pending.move,
            pending.moveIndex,
            pending.playerColor,
            pending.legalMoveCount,
          )

          if (!this.resolvedIndices.has(pending.moveIndex)) {
            this.resolveAnalysisResult(pending.moveIndex, result)
            this.clearActiveAnalysisStateIfCurrent(pending.requestId)
            this.cancelWorkerAnalysis(pending.requestId)
            console.log(
              `[Analyst] resolve idx=${pending.moveIndex} source=cache(authoritative profile=${cached.analysis_profile_id ?? 'unknown'})`,
            )
            if (result.blunder && result.delta !== null) {
              console.log(
                `[Analyst] Blunder detected (cached): \u0394${result.delta}cp (best ${result.bestMove}).`,
              )
            }
          }
        }
      })
      .catch(() => {
        // Network/lookup error — release the worker fallback for every still-
        // pending move in the batch rather than stranding a buffered result.
        if (this.sessionGeneration !== gen) return
        for (const pending of batch) {
          this.releaseFallback(pending.moveIndex, pending.requestId, 'cache-error')
        }
      })
  }

  // --- Incremental upload ---

  private startIncrementalUploadTimer() {
    this.stopIncrementalUploadTimer()
    this.incrementalUploadTimer = setInterval(() => {
      if (this.uploadState && this.uploadState.dirtyIndices.size > 0) {
        this.flushIncrementalUpload(this.uploadState)
      }
    }, INCREMENTAL_UPLOAD_INTERVAL_MS)
  }

  private stopIncrementalUploadTimer() {
    if (this.incrementalUploadTimer) {
      clearInterval(this.incrementalUploadTimer)
      this.incrementalUploadTimer = null
    }
  }

  private cancelUploadState(state: UploadState) {
    if (state.retryTimer) {
      clearTimeout(state.retryTimer)
      state.retryTimer = null
    }
    if (state.abortController) {
      state.abortController.abort()
      state.abortController = null
    }
    state.uploadInFlight = false
  }

  /**
   * Build and send an upload for dirty indices. The payload is snapshotted
   * once from global state when this is first called for a batch. Retries
   * re-send the same frozen payload so they can never accidentally serialize
   * a different session's moves.
   */
  private flushIncrementalUpload(
    state: UploadState,
    frozenPayload?: SessionMoveUpload[],
    frozenIndices?: Set<number>,
  ) {
    if (!state.uploadsEnabled) return
    if (state.uploadInFlight) return

    // First call for this batch — snapshot from global state
    const indicesToUpload = frozenIndices ?? new Set(state.dirtyIndices)
    if (!frozenIndices) {
      if (state.dirtyIndices.size === 0) return
      state.dirtyIndices.clear()
    }

    const payload = frozenPayload ?? buildSessionMoveUploadsForIndices(
      [...useGameStore.getState().moveHistory],
      new Map(this.store.getState().analysisMap),
      [...indicesToUpload],
      STARTING_FEN,
    )

    if (payload.length === 0) {
      return
    }

    state.uploadInFlight = true
    const controller = typeof AbortController !== 'undefined'
      ? new AbortController()
      : null
    state.abortController = controller

    // Mid-game incremental uploads skip the expensive blunder-opportunity
    // recompute (g-y90g): the current session's own mid-game opportunity events
    // are never consumed during its own play, so only the final/complete upload
    // (uploadFullMoveHistoryBeforeEnd) needs to compute them. Graph upsert +
    // analysis-cache + opening-score recompute still run on every upload.
    uploadSessionMoves(
      state.sessionId,
      payload,
      controller
        ? {
            uploadKind: 'incremental',
            signal: controller.signal,
            recomputeOpportunity: false,
          }
        : { uploadKind: 'incremental', recomputeOpportunity: false },
    )
      .then(() => {
        if (state.abortController === controller) {
          state.abortController = null
        }
        for (const idx of indicesToUpload) {
          state.uploadedIndices.add(idx)
        }
        state.retryCount = 0
        state.uploadInFlight = false
        if (!state.uploadsEnabled) {
          return
        }

        // If more dirty indices accumulated during upload, flush again.
        // When detached (session finalized), drain unconditionally since
        // the interval timer is no longer running.
        if (state.dirtyIndices.size > 0 &&
            (state.detached || state.dirtyIndices.size >= INCREMENTAL_UPLOAD_BATCH_THRESHOLD)) {
          this.flushIncrementalUpload(state)
        }
      })
      .catch((err) => {
        if (state.abortController === controller) {
          state.abortController = null
        }
        state.uploadInFlight = false
        if (!state.uploadsEnabled || isAbortError(err)) {
          return
        }
        console.error('[Coordinator] Incremental upload failed:', err)

        // Retry with exponential backoff, re-using the frozen payload
        state.retryCount++
        const delay = Math.min(
          1000 * Math.pow(2, state.retryCount - 1),
          RETRY_MAX_DELAY_MS,
        )
        if (state.retryTimer) clearTimeout(state.retryTimer)
        state.retryTimer = setTimeout(() => {
          state.retryTimer = null
          this.flushIncrementalUpload(state, payload, indicesToUpload)
        }, delay)
      })
  }

  /**
   * Best-effort flush of already-resolved dirty indices. Does NOT block on
   * worker completion. Called at game-end for final reconciliation.
   */
  async flushPendingUploads(): Promise<void> {
    if (
      !this.uploadState ||
      !this.uploadState.uploadsEnabled ||
      this.uploadState.dirtyIndices.size === 0
    ) {
      return
    }
    this.flushIncrementalUpload(this.uploadState)
  }

  stopSessionUploads() {
    this.stopIncrementalUploadTimer()
    if (!this.uploadState) return
    this.uploadState.uploadsEnabled = false
    this.uploadState.dirtyIndices.clear()
    this.cancelUploadState(this.uploadState)
  }

  // --- Teardown ---

  destroy() {
    this.stopIncrementalUploadTimer()
    if (this.uploadState?.retryTimer) {
      clearTimeout(this.uploadState.retryTimer)
    }
    if (this.cacheFlushTimer) {
      clearTimeout(this.cacheFlushTimer)
    }
    if (this.idleTimer) {
      clearTimeout(this.idleTimer)
    }
    if (this.bootWatchdogTimer) {
      clearTimeout(this.bootWatchdogTimer)
      this.bootWatchdogTimer = null
    }
    this.clearAllResolutionState()
    this.rejectAnalysisWaiters(new Error('Analysis coordinator destroyed'))
    this.rejectDrillTruthWaiters(new Error('Analysis coordinator destroyed'))
    this.pendingMoveIndices.clear()
    this.pendingMeta.clear()
    this.lastRequestIdByMoveIndex.clear()
    this.skippedRequestIds.clear()
    this.discardedRequestIds.clear()
    this.terminateWorker()
    this._decisionOwner.dispose()
    this.analysisOutcomeListeners.clear()
    this.analysisResetListeners.clear()
    this.uploadState = null
    this.activeSessionId = null
  }
}

/** Singleton coordinator instance — lives for the app lifetime. */
export const gameAnalysisCoordinator = new GameAnalysisCoordinator()
