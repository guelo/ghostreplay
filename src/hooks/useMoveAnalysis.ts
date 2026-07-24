import { useCallback, useEffect, useRef } from 'react'
import type {
  AnalyzeMoveMessage,
  AnalysisWorkerResponse,
} from '../workers/analysisMessages'
import { isRecordableFailure, isWithinRecordingMoveCap, classifyMove, isTrustedPositionHit, isTrustedExactBestHit, isTrustedMoveHit, hasCpEvalLoss, reconcileTrustedBest } from '../workers/analysisUtils'
import { lookupAnalysisCache } from '../utils/api'
import type { CachedAnalysis } from '../utils/api'
import type { AnalysisStore } from '../stores/createAnalysisStore'
// AnalysisResult now lives in the neutral types module; re-exported here so its
// existing consumers (stores, domain helpers, useVariationTree) are unaffected.
import type { AnalysisResult, MoveClassification } from '../types/analysis'

export type { AnalysisResult }

const createRequestId = () => {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID()
  }

  return Math.random().toString(36).slice(2)
}

const CACHE_LOOKUP_DEBOUNCE_MS = 150
const CACHE_BATCH_MAX_AGE_MS = 400
const ANALYSIS_RESOLUTION_TIMEOUT_MS = 2500
// Matches GameAnalysisCoordinator: a per-request inactivity window replaces the
// old fixed total deadline. A request (indexed OR variation) fails only after
// producing no observable activity for this window; any activity resets it, so a
// slow-but-progressing depth-17 analysis survives while a silent/hung worker
// still fails promptly. Armed only post-`ready` (see ANALYSIS_BOOT_TIMEOUT_MS).
const ANALYSIS_INACTIVITY_TIMEOUT_MS = 8_000
// Boot backstop, distinct from the inactivity window: bounds a silent/hung engine
// boot (cleared on `ready`, fatal on fire) so the tight 8s window never has to
// tolerate a slow cold WASM start.
const ANALYSIS_BOOT_TIMEOUT_MS = 20_000

type PendingCacheLookup = {
  requestId: string
  fen: string
  move: string
  moveIndex: number
  playerColor: 'white' | 'black'
  legalMoveCount: number | undefined
}

type ReleaseReason = 'cache-miss' | 'untrusted' | 'cache-error' | 'timeout' | 'worker-error'

type ResolutionEntry = {
  requestId: string
  cacheStatus: 'pending' | 'released'
  releaseReason?: ReleaseReason
  bufferedWorker?: AnalysisResult
  workerFailed?: boolean
  /** Per-request inactivity watchdog (armed post-ready; reset by activity). */
  watchdogTimer?: ReturnType<typeof setTimeout>
  /** True once this request produced its OWN activity — then reset only by its
   *  own activity; a queued (not-started) entry is reset by any worker liveness. */
  started?: boolean
  cacheTimer?: ReturnType<typeof setTimeout>
}

const makeCacheKey = (fen: string, moveUci: string) => `${fen}::${moveUci}`

/**
 * Convert a white-relative eval to the given color's perspective. Callers pass
 * the MOVER's color (see AnalysisResult perspective note), so the result is
 * mover-relative.
 */
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

/**
 * Build an AnalysisResult from a cached entry, recomputing the blunder flag
 * from game context.
 */
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
  const delta = cached.eval_delta

  // Use classification from cache if available, fall back to legacy delta-based
  const classification = (cached.classification as MoveClassification | null) ?? classifyMove(delta)
  const forced = legalMoveCount !== undefined && legalMoveCount <= 2
  const blunder = !forced && classification === 'blunder'
  const recordable =
    !forced &&
    isRecordableFailure(delta) &&
    isWithinRecordingMoveCap(moveIndex)

  return {
    // Preserve the originating request id so the cache result is attributable
    // to its request (Finding R6), matching the coordinator.
    id: requestId,
    move,
    bestMove: cached.best_move_uci ?? move,
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

export const useMoveAnalysis = (
  store: AnalysisStore,
  onVariationError?: (id: string) => void,
) => {
  // Held in a ref so the worker effect / clearAnalysis can call the latest
  // callback without re-subscribing the worker or recreating clearAnalysis.
  const onVariationErrorRef = useRef(onVariationError)
  onVariationErrorRef.current = onVariationError
  const workerRef = useRef<Worker | null>(null)
  // Maps request IDs to move indices so we can file results into analysisMap
  const pendingMoveIndices = useRef<Map<string, number>>(new Map())
  // Maps request IDs to metadata needed for deriving recordable/blunder in the response handler
  const pendingMeta = useRef<Map<string, { moveIndex: number; legalMoveCount: number | undefined }>>(new Map())
  // Maps request IDs to the absolute ply + FEN of an in-flight what-if analysis
  const pendingVariationPlies = useRef<Map<string, { ply: number; fen: string }>>(new Map())
  // Per-variation inactivity watchdog timers (variation requests are not tracked
  // in resolutionState, so they need their own watchdog that also cancels the
  // worker request — otherwise a missing readyok stalls the worker queue).
  const variationWatchdogTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  // Variation request ids that have produced their OWN activity. A started
  // variation is reset only by its own activity (so a silent started variation
  // still fails after its window); a not-started one is reset by worker liveness.
  const variationStarted = useRef<Set<string>>(new Set())
  // Tombstones for variation ids torn down by FAILURE while the worker stays
  // alive (timeout/cancel via failVariation, scoped variation error, clearAnalysis).
  // Variation ids are never in requestIdToMoveIndex, so a late worker `analysis`
  // for one would otherwise fall through to the non-indexed branch and clobber
  // lastAnalysis (and a late `analysis-started` would revive the spinner). Handlers
  // drop tombstoned ids. Cleared on worker teardown (fatal / unmount), where the
  // 'error' status guard + worker termination already cover late messages.
  const discardedVariationIds = useRef<Set<string>>(new Set())
  // Throttle streaming eval updates to avoid excessive rerenders
  const lastStreamingUpdateMs = useRef(0)
  // Which request currently owns the global analyzing/streaming transient state,
  // so a stale result cannot clear the spinner of a live newer request.
  const currentAnalyzingRequestId = useRef<string | null>(null)

  // Race tracking: which moveIndices have been resolved (by either source)
  const resolvedIndices = useRef<Set<number>>(new Set())

  // Cache-first resolution state machine (mirrors GameAnalysisCoordinator).
  const resolutionState = useRef<Map<number, ResolutionEntry>>(new Map())
  // Latest request id per index — guards every resolution against superseded
  // requests (Finding 2). The hook had no such guard before.
  const latestRequestIds = useRef<Map<number, string>>(new Map())
  // Exact-best truth side channel (g-49e2): records the TRUSTED position's
  // best_move_uci for an index so the terminal resolve can promote a played move
  // that equals it to the best-move star, even when the published path fell back
  // to the worker (which under-rates it). Minimal mirror of the coordinator's
  // drillTruth — only the best move is needed here; the requestId guards against a
  // stale record promoting a superseded request. Cleared on the same
  // supersession/reset paths as the resolution state.
  const exactBestTruth = useRef<Map<number, { requestId: string; bestUci: string }>>(new Map())
  // Retained until lifecycle cleanup so a late worker message for a settled
  // index is never mistaken for a non-indexed request (Finding G1).
  const requestIdToMoveIndex = useRef<Map<string, number>>(new Map())
  // Incremented on unmount; async cache callbacks captured at schedule time
  // no-op if it changed, preventing post-unmount store mutation (Finding 5).
  const mountToken = useRef(0)
  // True only between the worker's `ready` and its teardown. The inactivity
  // watchdog is post-ready, so analyzeMove arms it only when this is true;
  // pre-ready requests are armed when `ready` arrives (engine-boot strategy).
  const engineReadyRef = useRef(false)
  // Boot backstop for a silent/hung engine boot. Started in the worker effect,
  // cleared on `ready`/fatal-error/cleanup; on fire it runs the fatal teardown.
  const bootWatchdogTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Debounced cache lookup batch
  const pendingCacheLookups = useRef<PendingCacheLookup[]>([])
  const cacheFlushTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const cacheBatchFirstEnqueuedAt = useRef<number | null>(null)

  // Terminal publication point — single place that writes the store.
  const resolveAnalysis = useCallback(
    (moveIndex: number, result: AnalysisResult) => {
      if (latestRequestIds.current.get(moveIndex) !== result.id) return false
      if (resolvedIndices.current.has(moveIndex)) return false
      resolvedIndices.current.add(moveIndex)

      // Grain-split best reconciliation (g-49e2 / g-jfdj, same root cause as
      // g-move-best-icon): the trusted POSITION grain names the exact best move,
      // and that answer wins over the published move-grain/worker classification.
      // Promotes a played==best result to 'best' (star), and demotes a fallback
      // that wrongly graded a non-best move 'best' down to the 'excellent' floor.
      // The requestId guard rejects a stale record from a superseded request
      // (which is also cleared on supersession, so this is belt-and-braces).
      const truth = exactBestTruth.current.get(moveIndex)
      const published =
        truth && truth.requestId === result.id
          ? reconcileTrustedBest(result, truth.bestUci)
          : result

      const entry = resolutionState.current.get(moveIndex)
      if (entry) {
        if (entry.watchdogTimer) clearTimeout(entry.watchdogTimer)
        if (entry.cacheTimer) clearTimeout(entry.cacheTimer)
        resolutionState.current.delete(moveIndex)
      }
      pendingMoveIndices.current.delete(result.id)
      pendingMeta.current.delete(result.id)
      exactBestTruth.current.delete(moveIndex)
      store.getState().resolveAnalysis(moveIndex, published)
      return true
    },
    [store],
  )

  const clearActiveAnalysisStateIfCurrent = useCallback((requestId: string) => {
    if (currentAnalyzingRequestId.current !== requestId) return
    currentAnalyzingRequestId.current = null
    lastStreamingUpdateMs.current = 0
    const s = store.getState()
    s.setIsAnalyzing(false)
    s.setAnalyzingMove(null)
    s.setStreamingEval(null)
  }, [store])

  // Tell the worker to abandon a request so a stalled search/reset cannot keep
  // the worker's serial queue blocked. Harmless when the request already
  // finished (the worker drops unknown ids).
  const cancelWorkerRequest = useCallback((requestId: string) => {
    workerRef.current?.postMessage({ type: 'cancel-analysis', id: requestId })
  }, [])

  // Hard no-hang terminator: drop all per-request state + both timers AND cancel
  // the worker request, so a stalled worker (e.g. a missing readyok) cannot keep
  // the queue blocked. It must also clear the spinner if this request still owns
  // it, or isAnalyzing stays stuck (Finding 1).
  const failRequest = useCallback(
    (moveIndex: number, requestId: string) => {
      const entry = resolutionState.current.get(moveIndex)
      if (!entry || entry.requestId !== requestId) return
      if (resolvedIndices.current.has(moveIndex)) return
      if (entry.watchdogTimer) clearTimeout(entry.watchdogTimer)
      if (entry.cacheTimer) clearTimeout(entry.cacheTimer)
      resolutionState.current.delete(moveIndex)
      exactBestTruth.current.delete(moveIndex)
      pendingMoveIndices.current.delete(requestId)
      pendingMeta.current.delete(requestId)
      cancelWorkerRequest(requestId)
      clearActiveAnalysisStateIfCurrent(requestId)
    },
    [clearActiveAnalysisStateIfCurrent, cancelWorkerRequest],
  )

  const clearVariationTimer = useCallback((requestId: string) => {
    const timer = variationWatchdogTimers.current.get(requestId)
    if (timer) clearTimeout(timer)
    variationWatchdogTimers.current.delete(requestId)
    variationStarted.current.delete(requestId)
  }, [])

  const clearAllVariationTimers = useCallback(() => {
    for (const timer of variationWatchdogTimers.current.values()) {
      clearTimeout(timer)
    }
    variationWatchdogTimers.current.clear()
    variationStarted.current.clear()
  }, [])

  // No-hang terminator for what-if (variation) requests: cancel the worker
  // request and drop its streaming/transient state when the deadline elapses.
  const failVariation = useCallback(
    (requestId: string) => {
      clearVariationTimer(requestId)
      if (!pendingVariationPlies.current.has(requestId)) return
      pendingVariationPlies.current.delete(requestId)
      // Tombstone so a late worker result/start for this canceled variation is
      // dropped, not mistaken for a fresh ad-hoc analysis (clobbering lastAnalysis).
      discardedVariationIds.current.add(requestId)
      cancelWorkerRequest(requestId)
      store.getState().setVariationStreamingEval(null)
      clearActiveAnalysisStateIfCurrent(requestId)
      // Free the variation tree's pending entry so the timed-out FEN can be
      // re-requested (Finding F3); otherwise it stays stranded like a scoped
      // error would.
      onVariationErrorRef.current?.(requestId)
    },
    [store, clearVariationTimer, cancelWorkerRequest, clearActiveAnalysisStateIfCurrent],
  )

  // --- Inactivity watchdog (mirrors GameAnalysisCoordinator) ---

  // (Re)arm an indexed request's inactivity timer. On fire the request is failed
  // (no-hang). Looks the entry up by index and verifies the requestId still owns
  // it, so a stale call is inert.
  const armWatchdog = useCallback(
    (moveIndex: number, requestId: string) => {
      const entry = resolutionState.current.get(moveIndex)
      if (!entry || entry.requestId !== requestId) return
      if (entry.watchdogTimer) clearTimeout(entry.watchdogTimer)
      const token = mountToken.current
      entry.watchdogTimer = setTimeout(() => {
        if (mountToken.current !== token) return
        failRequest(moveIndex, requestId)
      }, ANALYSIS_INACTIVITY_TIMEOUT_MS)
    },
    [failRequest],
  )

  // (Re)arm a variation request's inactivity timer. Callers ensure the variation
  // is still pending; on fire it fails the variation (cancels the worker request).
  const armVariationWatchdog = useCallback(
    (requestId: string) => {
      const existing = variationWatchdogTimers.current.get(requestId)
      if (existing) clearTimeout(existing)
      const token = mountToken.current
      const timer = setTimeout(() => {
        if (mountToken.current !== token) return
        failVariation(requestId)
      }, ANALYSIS_INACTIVITY_TIMEOUT_MS)
      variationWatchdogTimers.current.set(requestId, timer)
    },
    [failVariation],
  )

  // Serial-worker liveness: the single engine is alive and draining, so re-arm
  // every NOT-started indexed entry and NOT-started variation (those queued
  // behind the active request). A STARTED request is reset only by its own
  // activity, so it is skipped here. Also called by `ready` to arm everything.
  const noteWorkerLiveness = useCallback(() => {
    for (const [idx, entry] of resolutionState.current) {
      if (entry.started) continue
      if (resolvedIndices.current.has(idx)) continue
      armWatchdog(idx, entry.requestId)
    }
    for (const requestId of pendingVariationPlies.current.keys()) {
      if (variationStarted.current.has(requestId)) continue
      armVariationWatchdog(requestId)
    }
  }, [armWatchdog, armVariationWatchdog])

  // Reset the watchdog from observed activity for `requestId`. SELF-GUARDED: it
  // returns early — doing NOTHING, NOT calling noteWorkerLiveness — unless the id
  // is either (a) a live current indexed request or (b) a still-pending variation.
  // So a late progress for a superseded/resolved indexed id or a deleted/terminal
  // variation id is inert and cannot re-arm unrelated queued work (the variation
  // ids are deleted on every terminal/timeout path, so this guard is essential).
  const noteActivity = useCallback(
    (requestId: string) => {
      const idx = requestIdToMoveIndex.current.get(requestId)
      if (idx !== undefined) {
        // Indexed id (possibly a stale tombstone). Only a live current owner
        // re-arms; otherwise return without touching liveness.
        const entry = resolutionState.current.get(idx)
        if (
          entry &&
          entry.requestId === requestId &&
          latestRequestIds.current.get(idx) === requestId &&
          !resolvedIndices.current.has(idx)
        ) {
          entry.started = true
          armWatchdog(idx, requestId)
          noteWorkerLiveness()
        }
        return
      }
      if (pendingVariationPlies.current.has(requestId)) {
        variationStarted.current.add(requestId)
        armVariationWatchdog(requestId)
        noteWorkerLiveness()
      }
    },
    [armWatchdog, armVariationWatchdog, noteWorkerLiveness],
  )

  // Release the buffered worker fallback once the cache settles non-trusted.
  const releaseFallback = useCallback(
    (moveIndex: number, requestId: string, reason: ReleaseReason) => {
      const entry = resolutionState.current.get(moveIndex)
      if (!entry || entry.requestId !== requestId) return
      if (resolvedIndices.current.has(moveIndex)) return
      if (entry.cacheStatus === 'released') return
      if (entry.cacheTimer) {
        clearTimeout(entry.cacheTimer)
        entry.cacheTimer = undefined
      }
      entry.cacheStatus = 'released'
      entry.releaseReason = reason
      if (entry.bufferedWorker) {
        resolveAnalysis(moveIndex, entry.bufferedWorker)
      } else if (entry.workerFailed) {
        failRequest(moveIndex, requestId)
      }
      // else: worker result resolves on arrival, or the deadline terminates it.
    },
    [resolveAnalysis, failRequest],
  )

  const flushCacheLookups = useCallback(() => {
    const batch = pendingCacheLookups.current.splice(0)
    cacheBatchFirstEnqueuedAt.current = null
    if (batch.length === 0) return

    const token = mountToken.current

    // Start the cache-response window at dispatch (Finding 5).
    for (const pending of batch) {
      const entry = resolutionState.current.get(pending.moveIndex)
      if (!entry || entry.requestId !== pending.requestId) continue
      if (entry.cacheStatus !== 'pending') continue
      if (entry.cacheTimer) clearTimeout(entry.cacheTimer)
      entry.cacheTimer = setTimeout(() => {
        if (mountToken.current !== token) return
        releaseFallback(pending.moveIndex, pending.requestId, 'timeout')
      }, ANALYSIS_RESOLUTION_TIMEOUT_MS)
    }

    const positions = batch.map(p => ({ fen: p.fen, move_uci: p.move }))

    lookupAnalysisCache(positions)
      .then(results => {
        if (mountToken.current !== token) return
        for (const pending of batch) {
          const entry = resolutionState.current.get(pending.moveIndex)
          if (!entry || entry.requestId !== pending.requestId) continue
          if (entry.cacheStatus !== 'pending') continue
          if (resolvedIndices.current.has(pending.moveIndex)) continue

          const key = makeCacheKey(pending.fen, pending.move)
          const cached = results.get(key)

          // Exact-best truth side channel (g-49e2): record the trusted position's
          // best move BEFORE the published gate / releaseFallback, so a played
          // move that equals it is promoted to the best-move star at the terminal
          // resolve even when this row is move-untrusted and falls back to the
          // worker (which under-rates it). Pure side-channel write — it must NOT
          // touch resolutionState, the worker, or outcomes; the gate below runs
          // unchanged.
          if (cached && isTrustedExactBestHit(cached)) {
            exactBestTruth.current.set(pending.moveIndex, {
              requestId: pending.requestId,
              bestUci: cached.best_move_uci as string,
            })
          }

          if (
            !cached ||
            !isTrustedPositionHit(cached) ||
            !isTrustedMoveHit(cached) ||
            !hasCpEvalLoss(cached)
          ) {
            // Release the worker fallback unless ALL three concerns pass:
            // trusted+renderable POSITION (best move/PV), trusted+renderable
            // MOVE evidence, and a CP eval-loss the current grader can use.
            // `hasCpEvalLoss` is the TRANSITIONAL gate that keeps move-trusted
            // mate-only rows on the worker until Phase 6 (epic g-l02q).
            const reason: ReleaseReason = cached ? 'untrusted' : 'cache-miss'
            releaseFallback(pending.moveIndex, pending.requestId, reason)
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

          if (resolveAnalysis(pending.moveIndex, result)) {
            // The authoritative result wins; the discarded worker may still be
            // running (and could stall). Cancel the worker request so it cannot
            // block the serial queue, and clear the spinner now since cache
            // resolution also cleared the deadline timer (mirrors the
            // coordinator).
            cancelWorkerRequest(pending.requestId)
            clearActiveAnalysisStateIfCurrent(pending.requestId)
            console.log(
              `[Analyst] resolve idx=${pending.moveIndex} source=cache(authoritative profile=${cached.analysis_profile_id ?? 'unknown'})`,
            )
            if (result.blunder && result.delta !== null) {
              console.log(
                `[Analyst] Blunder detected (cached): Δ${result.delta}cp (best ${result.bestMove}).`,
              )
            }
          }
        }
      })
      .catch(() => {
        if (mountToken.current !== token) return
        for (const pending of batch) {
          releaseFallback(pending.moveIndex, pending.requestId, 'cache-error')
        }
      })
  }, [resolveAnalysis, releaseFallback, clearActiveAnalysisStateIfCurrent, cancelWorkerRequest])

  // `keepTombstones` retains requestIdToMoveIndex so that, when the worker stays
  // alive across the clear (clearAnalysis), a late indexed result is still
  // recognized as a discarded indexed request and dropped — never mistaken for
  // an ad-hoc result that would repopulate lastAnalysis (Finding 1). Fatal
  // teardown terminates the worker, so it can drop the tombstones too.
  const clearResolutionState = useCallback((keepTombstones = false) => {
    for (const entry of resolutionState.current.values()) {
      if (entry.watchdogTimer) clearTimeout(entry.watchdogTimer)
      if (entry.cacheTimer) clearTimeout(entry.cacheTimer)
    }
    resolutionState.current.clear()
    latestRequestIds.current.clear()
    exactBestTruth.current.clear()
    if (!keepTombstones) {
      requestIdToMoveIndex.current.clear()
    }
    pendingCacheLookups.current.length = 0
    cacheBatchFirstEnqueuedAt.current = null
  }, [])

  const scheduleCacheLookup = useCallback(
    (lookup: PendingCacheLookup) => {
      if (pendingCacheLookups.current.length === 0) {
        cacheBatchFirstEnqueuedAt.current = Date.now()
      }
      pendingCacheLookups.current.push(lookup)

      if (cacheFlushTimer.current !== null) {
        clearTimeout(cacheFlushTimer.current)
      }
      const elapsed = cacheBatchFirstEnqueuedAt.current !== null
        ? Date.now() - cacheBatchFirstEnqueuedAt.current
        : 0
      const delay = Math.max(0, Math.min(CACHE_LOOKUP_DEBOUNCE_MS, CACHE_BATCH_MAX_AGE_MS - elapsed))
      cacheFlushTimer.current = setTimeout(() => {
        cacheFlushTimer.current = null
        flushCacheLookups()
      }, delay)
    },
    [flushCacheLookups],
  )

  useEffect(() => {
    // Reset worker-lifecycle state for this mount (preserves analysisMap
    // so that a singleton store survives remount without data loss).
    store.getState().resetTransient()

    // These ref containers are allocated once by useRef and never reassigned,
    // so capturing them for this mount is identical to reading `.current` in
    // the cleanup below — it just avoids the stale-ref lint warning.
    const pendingVariations = pendingVariationPlies.current
    const resolution = resolutionState.current
    const exactBest = exactBestTruth.current
    const variationTimers = variationWatchdogTimers.current
    const startedVariations = variationStarted.current
    const discardedVariations = discardedVariationIds.current

    const worker = new Worker(
      new URL('../workers/analysisWorker.ts', import.meta.url),
      { type: 'module' },
    )
    workerRef.current = worker

    // Shared fatal teardown for an unscoped worker error, a worker ErrorEvent, or
    // a silent/hung boot (boot watchdog): error status, drop transient/streaming
    // state, free pending variations, clear resolution + variation state, and
    // invalidate the worker so a late `ready` cannot revive it (Finding 3).
    const runFatalTeardown = (errorText: string) => {
      const s = store.getState()
      s.setStatus('error')
      s.setError(errorText)
      s.setIsAnalyzing(false)
      s.setAnalyzingMove(null)
      s.setStreamingEval(null)
      s.setVariationStreamingEval(null)
      lastStreamingUpdateMs.current = 0
      currentAnalyzingRequestId.current = null
      engineReadyRef.current = false
      if (bootWatchdogTimer.current) {
        clearTimeout(bootWatchdogTimer.current)
        bootWatchdogTimer.current = null
      }
      for (const requestId of pendingVariationPlies.current.keys()) {
        onVariationErrorRef.current?.(requestId)
      }
      pendingVariationPlies.current.clear()
      clearAllVariationTimers()
      clearResolutionState()
      // The worker is terminated below and the 'error' status guard now drops any
      // late message, so the variation tombstones are moot — clear them to bound
      // the set's growth across a session.
      discardedVariationIds.current.clear()
      if (workerRef.current) {
        workerRef.current.terminate()
        workerRef.current = null
      }
      mountToken.current++
    }

    // A fresh worker is not yet ready: keep the inactivity watchdog disarmed until
    // `ready`, bounded meanwhile by the boot backstop (engine-boot strategy). A
    // silent/hung boot runs the fatal teardown so recovery settles, not hangs.
    engineReadyRef.current = false
    if (bootWatchdogTimer.current) clearTimeout(bootWatchdogTimer.current)
    const bootToken = mountToken.current
    bootWatchdogTimer.current = setTimeout(() => {
      if (mountToken.current !== bootToken) return
      runFatalTeardown('Analysis engine failed to start')
    }, ANALYSIS_BOOT_TIMEOUT_MS)

    const handleMessage = (event: MessageEvent<AnalysisWorkerResponse>) => {
      const message = event.data
      const s = store.getState()

      switch (message.type) {
        case 'ready':
          // A stray late `ready` must not flip a fatal error back to ready and
          // reopen the ad-hoc path (Finding 3), nor re-arm watchdogs / clear the
          // boot guard on a dead worker. A genuine remount resets status to
          // 'booting' (resetTransient), so legitimate recovery still works.
          if (s.status === 'error') break
          engineReadyRef.current = true
          if (bootWatchdogTimer.current) {
            clearTimeout(bootWatchdogTimer.current)
            bootWatchdogTimer.current = null
          }
          s.setStatus('ready')
          // Arm the inactivity watchdog for every request scheduled while booting.
          noteWorkerLiveness()
          break
        case 'analysis-started': {
          // Drop late worker messages after a fatal error (Finding F1).
          if (s.status === 'error') break
          // Drop a late start for a failed/canceled variation so it cannot revive
          // the spinner after its watchdog already fired (P2).
          if (discardedVariationIds.current.has(message.id)) break
          // Gate indexed requests by request state (Finding R5), but accept
          // variation requests which are tracked separately (Finding G4). Require a
          // LIVE resolution entry so a late start after a watchdog/worker
          // failRequest (entry gone, id still in latestRequestIds) cannot revive
          // the spinner for a request that already failed (P2).
          const startIdx = requestIdToMoveIndex.current.get(message.id)
          if (startIdx !== undefined) {
            const entry = resolutionState.current.get(startIdx)
            if (
              !entry ||
              entry.requestId !== message.id ||
              latestRequestIds.current.get(startIdx) !== message.id ||
              resolvedIndices.current.has(startIdx)
            ) {
              break
            }
          }
          // Activity: reset the watchdog (self-guarded; routes to the indexed or
          // variation own-re-arm + serial-worker liveness).
          noteActivity(message.id)
          currentAnalyzingRequestId.current = message.id
          s.setIsAnalyzing(true)
          s.setAnalyzingMove(message.move)
          break
        }
        case 'analysis-streaming': {
          if (s.status === 'error') break
          // Activity: reset the watchdog (self-guarded; routes to indexed re-arm
          // or variation own-re-arm).
          noteActivity(message.id)
          const streamIdx = pendingMoveIndices.current.get(message.id)
          // Guard by latestRequestIds so a superseded request's stream cannot
          // update the replacement index (Finding 2).
          if (
            streamIdx !== undefined &&
            latestRequestIds.current.get(streamIdx) === message.id &&
            !resolvedIndices.current.has(streamIdx)
          ) {
            // This (latest) request owns the global transient state, so its own
            // terminal result is allowed to clear it later.
            currentAnalyzingRequestId.current = message.id
            const now = performance.now()
            if (now - lastStreamingUpdateMs.current >= 250) {
              lastStreamingUpdateMs.current = now
              store.getState().setStreamingEval({ moveIndex: streamIdx, cp: message.cp })
            }
            break
          }
          // What-if (variation) analyses are tracked by ply + FEN, not moveIndex
          const streamVar = pendingVariationPlies.current.get(message.id)
          if (streamVar !== undefined) {
            const now = performance.now()
            if (now - lastStreamingUpdateMs.current >= 250) {
              lastStreamingUpdateMs.current = now
              store.getState().setVariationStreamingEval({
                ply: streamVar.ply,
                fen: streamVar.fen,
                cp: message.cp,
              })
            }
          }
          break
        }
        case 'analysis-progress': {
          // Liveness-only ping (root/post-played/post-best phases). Reset the
          // watchdog; noteActivity self-guards against stale/superseded/resolved
          // indexed ids and deleted/terminal variation ids.
          if (s.status === 'error') break
          noteActivity(message.id)
          break
        }
        case 'analysis': {
          // Drop late worker results after a fatal error (Finding F1).
          if (s.status === 'error') break
          // Drop a late result for a failed/canceled variation: its id is no longer
          // in pendingVariationPlies, so without this it would fall through to the
          // non-indexed branch and clobber lastAnalysis with a stale result (P1).
          if (discardedVariationIds.current.has(message.id)) {
            clearActiveAnalysisStateIfCurrent(message.id)
            break
          }

          // Clear any in-flight variation streaming for this request; the
          // resolved result now lives in the variation analysis cache.
          if (pendingVariationPlies.current.has(message.id)) {
            clearVariationTimer(message.id)
            pendingVariationPlies.current.delete(message.id)
            s.setVariationStreamingEval(null)
          }

          const moveIndex = requestIdToMoveIndex.current.get(message.id)
          if (moveIndex !== undefined) {
            // Known indexed request (possibly already settled). Clear the
            // spinner only if THIS request owns it, so a stale result cannot
            // clear a live newer request's transient state (Finding 2).
            clearActiveAnalysisStateIfCurrent(message.id)
            const entry = resolutionState.current.get(moveIndex)
            if (
              !entry ||
              entry.requestId !== message.id ||
              latestRequestIds.current.get(moveIndex) !== message.id ||
              resolvedIndices.current.has(moveIndex)
            ) {
              break
            }
            // Activity: reset the watchdog (self-guarded). Matters when the result
            // is buffered behind a still-pending cache so the wait is not killed.
            noteActivity(message.id)

            const meta = pendingMeta.current.get(message.id)
            const forced = meta?.legalMoveCount !== undefined && meta.legalMoveCount <= 2
            const blunder = !forced && message.classification === 'blunder'
            const recordable =
              !forced &&
              isRecordableFailure(message.delta) &&
              isWithinRecordingMoveCap(moveIndex)

            const result: AnalysisResult = {
              id: message.id,
              move: message.move,
              bestMove: message.bestMove,
              bestLine: message.bestLine,
              bestEval: message.bestEval,
              playedEval: message.playedEval,
              currentPositionEval: message.playedEval,
              playedEvalMate: message.playedEvalMate,
              currentPositionEvalMate: message.playedEvalMate,
              moveIndex,
              delta: message.delta,
              classification: message.classification,
              blunder,
              recordable,
            }

            if (entry.cacheStatus === 'pending') {
              // Hold until the authoritative cache settles (Finding 3).
              entry.bufferedWorker = result
            } else {
              resolveAnalysis(moveIndex, result)
              if (blunder && message.delta !== null) {
                console.log(
                  `[Analyst] Blunder detected: Δ${message.delta}cp (best ${message.bestMove}).`,
                )
              }
            }
            break
          }

          // Genuinely non-indexed (variation / ad-hoc) request.
          clearActiveAnalysisStateIfCurrent(message.id)
          pendingMeta.current.delete(message.id)
          const result: AnalysisResult = {
            id: message.id,
            move: message.move,
            bestMove: message.bestMove,
            bestLine: message.bestLine,
            bestEval: message.bestEval,
            playedEval: message.playedEval,
            currentPositionEval: message.playedEval,
            playedEvalMate: message.playedEvalMate,
            currentPositionEvalMate: message.playedEvalMate,
            moveIndex: null,
            delta: message.delta,
            classification: message.classification,
            blunder: message.classification === 'blunder',
            recordable: false,
          }
          store.getState().setLastAnalysis(result)
          if (result.blunder && message.delta !== null) {
            console.log(
              `[Analyst] Blunder detected: Δ${message.delta}cp (best ${message.bestMove}).`,
            )
          }
          break
        }
        case 'error': {
          if (message.id !== undefined) {
            // Scoped variation error: clear only that variation's streaming
            // state, not the global status (Finding G4). The worker stopped for
            // this request, so clear the spinner if it owns it (Finding 1).
            if (pendingVariationPlies.current.has(message.id)) {
              clearVariationTimer(message.id)
              pendingVariationPlies.current.delete(message.id)
              // Tombstone so a racey late result/start for this errored variation
              // cannot clobber lastAnalysis or revive the spinner (P1/P2).
              discardedVariationIds.current.add(message.id)
              s.setVariationStreamingEval(null)
              clearActiveAnalysisStateIfCurrent(message.id)
              // Free the variation tree's pending entry so this FEN can be
              // re-requested (Finding F3); otherwise it stays stranded forever.
              onVariationErrorRef.current?.(message.id)
              break
            }
            // Scoped indexed error.
            const idx = requestIdToMoveIndex.current.get(message.id)
            if (idx === undefined) break
            const entry = resolutionState.current.get(idx)
            if (!entry || entry.requestId !== message.id) break
            if (resolvedIndices.current.has(idx)) break
            // Activity: reset the watchdog (self-guarded). Matters when the error
            // is held behind a still-pending cache (workerFailed) awaiting recovery.
            noteActivity(message.id)
            // The worker has stopped for this request either way, so clear the
            // spinner if it still owns it (failRequest also does this).
            clearActiveAnalysisStateIfCurrent(message.id)
            if (entry.cacheStatus === 'pending') {
              entry.workerFailed = true
            } else {
              failRequest(idx, message.id)
            }
            break
          }
          // Unscoped / fatal error — shared teardown (also clears the boot timer
          // and invalidates the worker so a late `ready` cannot revive it).
          runFatalTeardown(message.error)
          break
        }
        case 'log':
          console.log(`[Analyst] ${message.message}`)
          break
        default:
          message satisfies never
      }
    }

    const handleError = (event: ErrorEvent) => {
      runFatalTeardown(event.message)
    }

    worker.addEventListener('message', handleMessage)
    worker.addEventListener('error', handleError)

    return () => {
      worker.removeEventListener('message', handleMessage)
      worker.removeEventListener('error', handleError)
      worker.terminate()
      workerRef.current = null
      engineReadyRef.current = false
      if (bootWatchdogTimer.current) {
        clearTimeout(bootWatchdogTimer.current)
        bootWatchdogTimer.current = null
      }
      // Invalidate any in-flight async cache callbacks so they cannot mutate
      // the store after unmount (Finding 5), and clear timers. This bumps the
      // live counter on purpose — a captured copy would not invalidate anything.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      mountToken.current++
      // Free every still-pending variation entry so a remount can re-request
      // those FENs (Finding F3).
      for (const requestId of pendingVariations.keys()) {
        onVariationErrorRef.current?.(requestId)
      }
      pendingVariations.clear()
      for (const entry of resolution.values()) {
        if (entry.watchdogTimer) clearTimeout(entry.watchdogTimer)
        if (entry.cacheTimer) clearTimeout(entry.cacheTimer)
      }
      resolution.clear()
      exactBest.clear()
      for (const timer of variationTimers.values()) {
        clearTimeout(timer)
      }
      variationTimers.clear()
      startedVariations.clear()
      discardedVariations.clear()
      if (cacheFlushTimer.current !== null) {
        clearTimeout(cacheFlushTimer.current)
        cacheFlushTimer.current = null
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const analyzeMove = useCallback(
    (fen: string, move: string, playerColor: 'white' | 'black', moveIndex?: number, legalMoveCount?: number, variationPly?: number, variationFen?: string): string | undefined => {
      if (store.getState().status === 'error') {
        return
      }

      if (!workerRef.current) {
        return
      }

      const id = createRequestId()
      if (moveIndex !== undefined) {
        // Supersede any prior resolution state for this index (Findings 2 & F2).
        const prevEntry = resolutionState.current.get(moveIndex)
        if (prevEntry) {
          if (prevEntry.watchdogTimer) clearTimeout(prevEntry.watchdogTimer)
          if (prevEntry.cacheTimer) clearTimeout(prevEntry.cacheTimer)
          // Cancel the superseded worker request: dropping its watchdog here
          // would otherwise leave a reset-stalled old request blocking the
          // worker queue, with only the replacement's watchdog firing later.
          cancelWorkerRequest(prevEntry.requestId)
        }
        pendingMoveIndices.current.set(id, moveIndex)
        pendingMeta.current.set(id, { moveIndex, legalMoveCount })
        latestRequestIds.current.set(moveIndex, id)
        requestIdToMoveIndex.current.set(id, moveIndex)
        resolvedIndices.current.delete(moveIndex)
        // Drop any stale exact-best truth for this index so a superseded
        // request's record can't promote this new request (g-49e2).
        exactBestTruth.current.delete(moveIndex)

        resolutionState.current.set(moveIndex, { requestId: id, cacheStatus: 'pending' })
        // Arm the inactivity watchdog only once the engine is ready; a request
        // scheduled while booting is armed when `ready` arrives (engine-boot
        // strategy — the 8s window must not false-fail during a slow cold boot).
        if (engineReadyRef.current) armWatchdog(moveIndex, id)
      } else if (variationPly !== undefined && variationFen !== undefined) {
        pendingVariationPlies.current.set(id, { ply: variationPly, fen: variationFen })
        // Variation requests are not in resolutionState, so give them their own
        // inactivity watchdog that cancels the worker request (a missing readyok
        // would otherwise block the worker queue indefinitely). Armed post-ready
        // like indexed requests; a pre-ready variation is armed by `ready`.
        if (engineReadyRef.current) armVariationWatchdog(id)
      }

      // Fire the worker (existing path)
      const message: AnalyzeMoveMessage = {
        type: 'analyze-move',
        id,
        fen,
        move,
        playerColor,
        ...(moveIndex !== undefined ? { moveIndex } : {}),
        ...(legalMoveCount !== undefined ? { legalMoveCount } : {}),
      }
      workerRef.current.postMessage(message)

      // Race: also fire a cache lookup
      if (moveIndex !== undefined) {
        scheduleCacheLookup({ requestId: id, fen, move, moveIndex, playerColor, legalMoveCount })
      }

      return id
    },
    [store, scheduleCacheLookup, armWatchdog, armVariationWatchdog, cancelWorkerRequest],
  )

  const clearAnalysis = useCallback(() => {
    store.getState().clearAll()
    lastStreamingUpdateMs.current = 0
    currentAnalyzingRequestId.current = null
    // Cancel every in-flight worker request (indexed + variation) before
    // dropping their bookkeeping: clearAnalysis does not terminate the worker,
    // so an abandoned reset-stalled request would otherwise block its queue.
    for (const requestId of pendingMoveIndices.current.keys()) {
      cancelWorkerRequest(requestId)
    }
    for (const requestId of pendingVariationPlies.current.keys()) {
      cancelWorkerRequest(requestId)
      // Tombstone: clearAnalysis does not terminate the worker, so a late result
      // for this canceled variation must be dropped, not treated as ad-hoc (P1).
      discardedVariationIds.current.add(requestId)
      // Free the variation tree's pending entry so the FEN can be re-requested
      // after the clear (Finding F3).
      onVariationErrorRef.current?.(requestId)
    }
    pendingMoveIndices.current.clear()
    pendingMeta.current.clear()
    pendingVariationPlies.current.clear()
    clearAllVariationTimers()
    resolvedIndices.current.clear()
    // Keep the requestId→index tombstones: clearAnalysis does not terminate the
    // worker, so a late indexed result must still be dropped, not treated as
    // ad-hoc (Finding 1).
    clearResolutionState(true)
    if (cacheFlushTimer.current !== null) {
      clearTimeout(cacheFlushTimer.current)
      cacheFlushTimer.current = null
    }
  }, [store, clearResolutionState, clearAllVariationTimers, cancelWorkerRequest])

  return { analyzeMove, clearAnalysis }
}
