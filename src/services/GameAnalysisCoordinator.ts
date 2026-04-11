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
} from '../workers/analysisUtils'
import type { MoveClassification } from '../workers/analysisUtils'
import { lookupAnalysisCache, uploadSessionMoves } from '../utils/api'
import type { CachedAnalysis, SessionMoveUpload } from '../utils/api'
import { gameAnalysisStore } from '../stores/createAnalysisStore'
import type { AnalysisResult } from '../hooks/useMoveAnalysis'
import { useGameStore } from '../stores/useGameStore'
import { buildSessionMoveUploadsForIndices } from '../components/chess-game/domain/sessionUpload'
import { STARTING_FEN } from '../components/chess-game/config'

const createRequestId = () => {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID()
  }
  return Math.random().toString(36).slice(2)
}

const CACHE_LOOKUP_DEBOUNCE_MS = 150
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

const makeCacheKey = (fen: string, moveUci: string) => `${fen}::${moveUci}`

const toPlayerPerspective = (
  whiteRelativeEval: number | null,
  playerColor: 'white' | 'black',
): number | null => {
  if (whiteRelativeEval === null) return null
  return playerColor === 'white' ? whiteRelativeEval : -whiteRelativeEval
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
    bestMove: cached.best_move_uci ?? move,
    bestEval,
    playedEval,
    currentPositionEval: playedEval,
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
  retryCount: number
  retryTimer: ReturnType<typeof setTimeout> | null
  /** True after the session is finalized — the interval timer is gone,
   *  so the success handler must drain all remaining dirty indices. */
  detached: boolean
}

export class GameAnalysisCoordinator {
  // Worker state
  private worker: Worker | null = null
  private pendingMoveIndices = new Map<string, number>()
  private pendingMeta = new Map<string, { moveIndex: number; legalMoveCount: number | undefined }>()
  private latestRequestIds = new Map<number, string>()
  private resolvedIndices = new Set<number>()
  private lastStreamingUpdateMs = 0
  private currentAnalyzingRequestId: string | null = null

  // Cache lookup batching
  private pendingCacheLookups: PendingCacheLookup[] = []
  private cacheFlushTimer: ReturnType<typeof setTimeout> | null = null

  // Session state — generation monotonically increases on each startSession
  // so in-flight async work from a previous session can be detected and dropped.
  private activeSessionId: string | null = null
  private sessionGeneration = 0
  private uploadState: UploadState | null = null

  // Incremental upload timer
  private incrementalUploadTimer: ReturnType<typeof setTimeout> | null = null

  // Idle shutdown
  private idleTimer: ReturnType<typeof setTimeout> | null = null

  // Listeners for analysis resolution (used by AnalysisEffects etc.)
  private onAnalysisResolved: ((moveIndex: number, result: AnalysisResult) => void) | null = null

  get store() {
    return gameAnalysisStore
  }

  get sessionId() {
    return this.activeSessionId
  }

  setOnAnalysisResolved(cb: ((moveIndex: number, result: AnalysisResult) => void) | null) {
    this.onAnalysisResolved = cb
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
    this.resetIdleTimer()
  }

  private terminateWorker() {
    if (this.idleTimer) {
      clearTimeout(this.idleTimer)
      this.idleTimer = null
    }
    if (!this.worker) return
    this.worker.removeEventListener('message', this.handleWorkerMessage)
    this.worker.removeEventListener('error', this.handleWorkerError)
    this.worker.terminate()
    this.worker = null
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
      retryCount: 0,
      retryTimer: null,
      detached: false,
    }

    this.startIncrementalUploadTimer()
    this.ensureWorker()
  }

  clearSession() {
    this.finalizeOldSession()
    this.activeSessionId = null
    this.sessionGeneration++
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
      this.store.getState().removeAnalysis(moveIndex)
      this.pendingMoveIndices.set(id, moveIndex)
      this.pendingMeta.set(id, { moveIndex, legalMoveCount })
      this.latestRequestIds.set(moveIndex, id)
      this.resolvedIndices.delete(moveIndex)
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

  clearAnalysis() {
    this.store.getState().clearAll()
    this.lastStreamingUpdateMs = 0
    this.pendingMoveIndices.clear()
    this.pendingMeta.clear()
    this.latestRequestIds.clear()
    this.resolvedIndices.clear()
    this.pendingCacheLookups = []
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
        s.setStatus('ready')
        break
      case 'analysis-started':
        this.currentAnalyzingRequestId = message.id
        s.setIsAnalyzing(true)
        s.setAnalyzingMove(message.move)
        break
      case 'analysis-streaming': {
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
      case 'analysis': {
        this.clearActiveAnalysisStateIfCurrent(message.id)

        const moveIndex = this.pendingMoveIndices.get(message.id)
        if (moveIndex !== undefined) {
          this.pendingMoveIndices.delete(message.id)
        }
        const meta = this.pendingMeta.get(message.id)
        this.pendingMeta.delete(message.id)

        if (
          moveIndex !== undefined &&
          (
            this.latestRequestIds.get(moveIndex) !== message.id ||
            this.resolvedIndices.has(moveIndex)
          )
        ) {
          break
        }

        const forced = meta?.legalMoveCount !== undefined && meta.legalMoveCount <= 2
        const blunder = !forced && message.classification === 'blunder'
        const recordable =
          !forced &&
          isRecordableFailure(message.delta) &&
          (moveIndex !== undefined ? isWithinRecordingMoveCap(moveIndex) : false)

        const result: AnalysisResult = {
          id: message.id,
          move: message.move,
          bestMove: message.bestMove,
          bestEval: message.bestEval,
          playedEval: message.playedEval,
          currentPositionEval: message.playedEval,
          moveIndex: moveIndex ?? null,
          delta: message.delta,
          classification: message.classification,
          blunder,
          recordable,
        }

        if (moveIndex !== undefined) {
          this.resolveAnalysisResult(moveIndex, result)
        } else {
          this.store.getState().setLastAnalysis(result)
        }

        if (blunder && message.delta !== null) {
          console.log(
            `[Analyst] Blunder detected: \u0394${message.delta}cp (best ${message.bestMove}).`,
          )
        }
        break
      }
      case 'error':
        this.currentAnalyzingRequestId = null
        this.lastStreamingUpdateMs = 0
        s.setStatus('error')
        s.setError(message.error)
        s.setIsAnalyzing(false)
        s.setAnalyzingMove(null)
        s.setStreamingEval(null)
        break
      case 'log':
        console.log(`[Analyst] ${message.message}`)
        break
      default:
        message satisfies never
    }
  }

  private handleWorkerError = (event: ErrorEvent) => {
    const s = this.store.getState()
    s.setStatus('error')
    s.setError(event.message)
  }

  // --- Resolution ---

  private resolveAnalysisResult(moveIndex: number, result: AnalysisResult) {
    if (this.latestRequestIds.get(moveIndex) !== result.id) return
    if (this.resolvedIndices.has(moveIndex)) return
    this.resolvedIndices.add(moveIndex)
    this.store.getState().resolveAnalysis(moveIndex, result)

    // Mark dirty for incremental upload
    if (this.uploadState && this.uploadState.sessionId === this.activeSessionId) {
      this.uploadState.dirtyIndices.add(moveIndex)
      // Trigger immediate upload if threshold reached
      if (this.uploadState.dirtyIndices.size >= INCREMENTAL_UPLOAD_BATCH_THRESHOLD) {
        this.flushIncrementalUpload(this.uploadState)
      }
    }

    this.onAnalysisResolved?.(moveIndex, result)
  }

  // --- Cache lookups ---

  private scheduleCacheLookup(lookup: PendingCacheLookup) {
    this.pendingCacheLookups.push(lookup)
    if (this.cacheFlushTimer !== null) {
      clearTimeout(this.cacheFlushTimer)
    }
    this.cacheFlushTimer = setTimeout(() => {
      this.cacheFlushTimer = null
      this.flushCacheLookups()
    }, CACHE_LOOKUP_DEBOUNCE_MS)
  }

  private flushCacheLookups() {
    const batch = this.pendingCacheLookups.splice(0)
    if (batch.length === 0) return

    // Capture generation so we can discard results if the session changed
    // while the cache lookup was in flight.
    const gen = this.sessionGeneration
    const positions = batch.map(p => ({ fen: p.fen, move_uci: p.move }))

    lookupAnalysisCache(positions)
      .then(results => {
        // Session switched — discard stale results
        if (this.sessionGeneration !== gen) return

        for (const pending of batch) {
          if (pending.moveIndex === undefined) continue
          if (this.latestRequestIds.get(pending.moveIndex) !== pending.requestId) continue
          if (this.resolvedIndices.has(pending.moveIndex)) continue

          const key = makeCacheKey(pending.fen, pending.move)
          const cached = results.get(key)
          if (!cached) continue

          const result = fromCachedAnalysis(
            pending.requestId,
            cached,
            pending.move,
            pending.moveIndex,
            pending.playerColor,
            pending.legalMoveCount,
          )

          if (!this.resolvedIndices.has(pending.moveIndex)) {
            console.log(
              `[Analyst] Cache hit for move ${pending.move} at index ${pending.moveIndex}`,
            )
            this.resolveAnalysisResult(pending.moveIndex, result)
            this.clearActiveAnalysisStateIfCurrent(pending.requestId)
            this.cancelWorkerAnalysis(pending.requestId)
            if (result.blunder && result.delta !== null) {
              console.log(
                `[Analyst] Blunder detected (cached): \u0394${result.delta}cp (best ${result.bestMove}).`,
              )
            }
          }
        }
      })
      .catch(() => {
        // Cache miss — worker will handle it
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

    uploadSessionMoves(state.sessionId, payload)
      .then(() => {
        for (const idx of indicesToUpload) {
          state.uploadedIndices.add(idx)
        }
        state.retryCount = 0
        state.uploadInFlight = false

        // If more dirty indices accumulated during upload, flush again.
        // When detached (session finalized), drain unconditionally since
        // the interval timer is no longer running.
        if (state.dirtyIndices.size > 0 &&
            (state.detached || state.dirtyIndices.size >= INCREMENTAL_UPLOAD_BATCH_THRESHOLD)) {
          this.flushIncrementalUpload(state)
        }
      })
      .catch((err) => {
        console.error('[Coordinator] Incremental upload failed:', err)
        state.uploadInFlight = false

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
    if (!this.uploadState || this.uploadState.dirtyIndices.size === 0) return
    this.flushIncrementalUpload(this.uploadState)
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
    this.terminateWorker()
    this.uploadState = null
    this.activeSessionId = null
  }
}

/** Singleton coordinator instance — lives for the app lifetime. */
export const gameAnalysisCoordinator = new GameAnalysisCoordinator()
