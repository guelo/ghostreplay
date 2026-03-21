import { useCallback, useEffect, useRef } from 'react'
import type {
  AnalyzeMoveMessage,
  AnalysisWorkerResponse,
} from '../workers/analysisMessages'
import { isRecordableFailure, isWithinRecordingMoveCap, classifyMove } from '../workers/analysisUtils'
import type { MoveClassification } from '../workers/analysisUtils'
import { lookupAnalysisCache } from '../utils/api'
import type { CachedAnalysis } from '../utils/api'
import type { AnalysisStore } from '../stores/createAnalysisStore'

const createRequestId = () => {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID()
  }

  return Math.random().toString(36).slice(2)
}

export type AnalysisResult = {
  id: string
  move: string
  bestMove: string
  bestEval: number | null
  playedEval: number | null
  currentPositionEval: number | null
  moveIndex: number | null
  delta: number | null
  classification: MoveClassification | null
  blunder: boolean
  recordable: boolean
}

const CACHE_LOOKUP_DEBOUNCE_MS = 150

type PendingCacheLookup = {
  fen: string
  move: string
  moveIndex: number
  playerColor: 'white' | 'black'
  legalMoveCount: number | undefined
}

const makeCacheKey = (fen: string, moveUci: string) => `${fen}::${moveUci}`

/**
 * Convert a white-relative eval to player-relative.
 */
const toPlayerPerspective = (
  whiteRelativeEval: number | null,
  playerColor: 'white' | 'black',
): number | null => {
  if (whiteRelativeEval === null) return null
  return playerColor === 'white' ? whiteRelativeEval : -whiteRelativeEval
}

/**
 * Build an AnalysisResult from a cached entry, recomputing the blunder flag
 * from game context.
 */
const fromCachedAnalysis = (
  cached: CachedAnalysis,
  move: string,
  moveIndex: number,
  playerColor: 'white' | 'black',
  legalMoveCount: number | undefined,
): AnalysisResult => {
  const playedEval = toPlayerPerspective(cached.played_eval, playerColor)
  const bestEval = toPlayerPerspective(cached.best_eval, playerColor)
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
    id: createRequestId(),
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

export const useMoveAnalysis = (store: AnalysisStore) => {
  const workerRef = useRef<Worker | null>(null)
  // Maps request IDs to move indices so we can file results into analysisMap
  const pendingMoveIndices = useRef<Map<string, number>>(new Map())
  // Maps request IDs to metadata needed for deriving recordable/blunder in the response handler
  const pendingMeta = useRef<Map<string, { moveIndex: number; legalMoveCount: number | undefined }>>(new Map())
  // Throttle streaming eval updates to avoid excessive rerenders
  const lastStreamingUpdateMs = useRef(0)

  // Race tracking: which moveIndices have been resolved (by either source)
  const resolvedIndices = useRef<Set<number>>(new Set())

  // Debounced cache lookup batch
  const pendingCacheLookups = useRef<PendingCacheLookup[]>([])
  const cacheFlushTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const resolveAnalysis = useCallback(
    (moveIndex: number, result: AnalysisResult) => {
      if (resolvedIndices.current.has(moveIndex)) {
        return false
      }
      resolvedIndices.current.add(moveIndex)
      store.getState().resolveAnalysis(moveIndex, result)
      return true
    },
    [store],
  )

  const flushCacheLookups = useCallback(() => {
    const batch = pendingCacheLookups.current.splice(0)
    if (batch.length === 0) return

    const positions = batch.map(p => ({ fen: p.fen, move_uci: p.move }))

    lookupAnalysisCache(positions)
      .then(results => {
        for (const pending of batch) {
          if (pending.moveIndex === undefined) continue
          if (resolvedIndices.current.has(pending.moveIndex)) continue

          const key = makeCacheKey(pending.fen, pending.move)
          const cached = results.get(key)
          if (!cached) continue

          const result = fromCachedAnalysis(
            cached,
            pending.move,
            pending.moveIndex,
            pending.playerColor,
            pending.legalMoveCount,
          )

          if (resolveAnalysis(pending.moveIndex, result)) {
            console.log(
              `[Analyst] Cache hit for move ${pending.move} at index ${pending.moveIndex}`,
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
        // Cache miss — worker will handle it
      })
  }, [resolveAnalysis])

  const scheduleCacheLookup = useCallback(
    (lookup: PendingCacheLookup) => {
      pendingCacheLookups.current.push(lookup)

      if (cacheFlushTimer.current !== null) {
        clearTimeout(cacheFlushTimer.current)
      }
      cacheFlushTimer.current = setTimeout(() => {
        cacheFlushTimer.current = null
        flushCacheLookups()
      }, CACHE_LOOKUP_DEBOUNCE_MS)
    },
    [flushCacheLookups],
  )

  useEffect(() => {
    // Reset worker-lifecycle state for this mount (preserves analysisMap
    // so that a singleton store survives remount without data loss).
    store.getState().resetTransient()

    const worker = new Worker(
      new URL('../workers/analysisWorker.ts', import.meta.url),
      { type: 'module' },
    )
    workerRef.current = worker

    const handleMessage = (event: MessageEvent<AnalysisWorkerResponse>) => {
      const message = event.data
      const s = store.getState()

      switch (message.type) {
        case 'ready':
          s.setStatus('ready')
          break
        case 'analysis-started':
          s.setIsAnalyzing(true)
          s.setAnalyzingMove(message.move)
          break
        case 'analysis-streaming': {
          const streamIdx = pendingMoveIndices.current.get(message.id)
          if (streamIdx !== undefined && !resolvedIndices.current.has(streamIdx)) {
            const now = performance.now()
            if (now - lastStreamingUpdateMs.current >= 250) {
              lastStreamingUpdateMs.current = now
              store.getState().setStreamingEval({ moveIndex: streamIdx, cp: message.cp })
            }
          }
          break
        }
        case 'analysis': {
          s.setIsAnalyzing(false)
          s.setAnalyzingMove(null)
          s.setStreamingEval(null)
          lastStreamingUpdateMs.current = 0
          const moveIndex = pendingMoveIndices.current.get(message.id)
          if (moveIndex !== undefined) {
            pendingMoveIndices.current.delete(message.id)
          }
          const meta = pendingMeta.current.get(message.id)
          pendingMeta.current.delete(message.id)

          // If cache already resolved this moveIndex, skip the worker result
          if (moveIndex !== undefined && resolvedIndices.current.has(moveIndex)) {
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
            resolveAnalysis(moveIndex, result)
          } else {
            store.getState().setLastAnalysis(result)
          }

          if (blunder && message.delta !== null) {
            console.log(
              `[Analyst] Blunder detected: Δ${message.delta}cp (best ${message.bestMove}).`,
            )
          }
          break
        }
        case 'error':
          s.setStatus('error')
          s.setError(message.error)
          s.setIsAnalyzing(false)
          s.setAnalyzingMove(null)
          break
        case 'log':
          console.log(`[Analyst] ${message.message}`)
          break
        default:
          message satisfies never
      }
    }

    const handleError = (event: ErrorEvent) => {
      const s = store.getState()
      s.setStatus('error')
      s.setError(event.message)
    }

    worker.addEventListener('message', handleMessage)
    worker.addEventListener('error', handleError)

    return () => {
      worker.removeEventListener('message', handleMessage)
      worker.removeEventListener('error', handleError)
      worker.terminate()
      workerRef.current = null
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const analyzeMove = useCallback(
    (fen: string, move: string, playerColor: 'white' | 'black', moveIndex?: number, legalMoveCount?: number): string | undefined => {
      if (store.getState().status === 'error') {
        return
      }

      if (!workerRef.current) {
        return
      }

      const id = createRequestId()
      if (moveIndex !== undefined) {
        pendingMoveIndices.current.set(id, moveIndex)
        pendingMeta.current.set(id, { moveIndex, legalMoveCount })
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
        scheduleCacheLookup({ fen, move, moveIndex, playerColor, legalMoveCount })
      }

      return id
    },
    [store, scheduleCacheLookup],
  )

  const clearAnalysis = useCallback(() => {
    store.getState().clearAll()
    lastStreamingUpdateMs.current = 0
    pendingMoveIndices.current.clear()
    pendingMeta.current.clear()
    resolvedIndices.current.clear()
    pendingCacheLookups.current.length = 0
    if (cacheFlushTimer.current !== null) {
      clearTimeout(cacheFlushTimer.current)
      cacheFlushTimer.current = null
    }
  }, [store])

  return { analyzeMove, clearAnalysis }
}
