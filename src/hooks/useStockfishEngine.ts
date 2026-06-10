import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  EngineInfo,
  WorkerResponse,
} from '../workers/stockfishMessages'

type EngineStatus = 'booting' | 'ready' | 'error'

type EvaluationOptions = {
  movetime?: number
  depth?: number
  multipv?: number
  searchmoves?: string[]
}

type UseStockfishEngineOptions = {
  enabled?: boolean
}

type EvaluationSignature = Pick<
  EvaluationOptions,
  'movetime' | 'depth' | 'multipv' | 'searchmoves'
>

const evalCacheKey = (fen: string, options?: EvaluationSignature): string => {
  const searchmoves = options?.searchmoves?.length
    ? [...options.searchmoves].sort().join(',')
    : ''

  return [
    fen,
    `movetime=${options?.movetime ?? ''}`,
    `depth=${options?.depth ?? ''}`,
    `multipv=${options?.multipv ?? 1}`,
    `searchmoves=${searchmoves}`,
  ].join('|')
}

type EvaluationResult = {
  move: string
  raw: string
}

type PendingEntry = {
  resolve: (value: EvaluationResult) => void
  reject: (error: Error) => void
}

const createRequestId = () => {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID()
  }

  return Math.random().toString(36).slice(2)
}

const rejectPendingEvaluations = (
  pending: Map<string, PendingEntry>,
  error: Error,
) => {
  pending.forEach((entry) => {
    entry.reject(error)
  })
  pending.clear()
}

export const useStockfishEngine = (options: UseStockfishEngineOptions = {}) => {
  const enabled = options.enabled ?? true
  const workerRef = useRef<Worker | null>(null)
  const pendingEvaluations = useRef<Map<string, PendingEntry>>(new Map())
  const activeRequestId = useRef<string | null>(null)
  const [status, setStatus] = useState<EngineStatus>('booting')
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<EngineInfo[]>([])
  const [infoFen, setInfoFen] = useState<string | null>(null)
  const [isThinking, setIsThinking] = useState(false)
  const evalCache = useRef<Map<string, EngineInfo[]>>(new Map())
  const activeCacheKey = useRef<string | null>(null)
  const requestFenById = useRef<Map<string, string>>(new Map())
  const requestCacheKeyById = useRef<Map<string, string>>(new Map())

  useEffect(() => {
    // Reset transient engine output whenever the enabled state flips so a
    // re-enabled engine never briefly exposes the previous session's lines.
    activeRequestId.current = null
    activeCacheKey.current = null
    requestFenById.current.clear()
    requestCacheKeyById.current.clear()
    /* eslint-disable react-hooks/set-state-in-effect */
    setInfo([])
    setInfoFen(null)
    setIsThinking(false)
    /* eslint-enable react-hooks/set-state-in-effect */

    if (!enabled) {
      return
    }

    const worker = new Worker(
      new URL('../workers/stockfishWorker.ts', import.meta.url),
      { type: 'module' },
    )
    workerRef.current = worker
    const pendingMap = pendingEvaluations.current

    if (import.meta.env.DEV) {
      ;(window as unknown as { __sf?: (cmd: string) => void }).__sf = (
        cmd: string,
      ) => worker.postMessage({ type: 'command', command: cmd })
    }

    const handleMessage = (event: MessageEvent<WorkerResponse>) => {
      const message = event.data

      switch (message.type) {
        case 'booted':
          break
        case 'ready':
          setStatus('ready')
          break
        case 'thinking':
          if (!requestFenById.current.has(message.id)) {
            break
          }
          activeRequestId.current = message.id
          setIsThinking(true)
          setInfo([])
          setInfoFen(null)
          break
        case 'info':
          // Only update PV lines for info messages that contain an actual
          // principal variation.  Stockfish emits status-only info lines
          // (e.g. "info depth 15 currmove e2e4 currmovenumber 1") that
          // lack both multipv and pv.  Without this guard the default
          // multipv→0 mapping overwrites slot 0 with a pv-less object,
          // causing the blue "best move" arrow to vanish mid-search.
          if (message.id === activeRequestId.current && message.info.pv) {
            setInfo((prev) => {
              const idx = (message.info.multipv ?? 1) - 1
              const next = [...prev]
              next[idx] = message.info
              return next
            })
            setInfoFen(requestFenById.current.get(message.id) ?? null)
          }
          break
        case 'bestmove': {
          const entry = pendingMap.get(message.id)

          if (entry) {
            entry.resolve({ move: message.move, raw: message.raw })
            pendingMap.delete(message.id)
          }
          requestFenById.current.delete(message.id)
          const cacheKey = requestCacheKeyById.current.get(message.id) ?? null
          requestCacheKeyById.current.delete(message.id)

          if (message.id === activeRequestId.current) {
            activeRequestId.current = null
            setIsThinking(false)
            if (cacheKey) {
              setInfo((current) => {
                evalCache.current.set(cacheKey, current)
                return current
              })
            }
          }
          break
        }
        case 'error': {
          setStatus('error')
          setError(message.error)
          setIsThinking(false)
          activeRequestId.current = null
          requestFenById.current.clear()
          requestCacheKeyById.current.clear()
          setInfoFen(null)

          rejectPendingEvaluations(pendingMap, new Error(message.error))
          break
        }
        case 'log':
          console.log(`[StockfishEngine] ${message.line}`)
          break
        default:
          message satisfies never
      }
    }

    const handleError = (event: ErrorEvent) => {
      setStatus('error')
      setError(event.message)
      setIsThinking(false)
      activeRequestId.current = null
      requestFenById.current.clear()
      requestCacheKeyById.current.clear()
      setInfoFen(null)
    }

    worker.addEventListener('message', handleMessage)
    worker.addEventListener('error', handleError)

    return () => {
      worker.removeEventListener('message', handleMessage)
      worker.removeEventListener('error', handleError)
      rejectPendingEvaluations(pendingMap, new Error('Stockfish worker disposed'))
      requestFenById.current.clear()
      requestCacheKeyById.current.clear()
      worker.terminate()
      workerRef.current = null
      if (import.meta.env.DEV) {
        delete (window as unknown as { __sf?: (cmd: string) => void }).__sf
      }
    }
  }, [enabled])

  const evaluatePosition = useCallback(
    (fen: string, options?: EvaluationOptions) => {
      if (!enabled) {
        return Promise.reject(new Error('Stockfish engine disabled'))
      }

      if (status === 'error') {
        return Promise.reject(
          new Error(error ?? 'Stockfish engine unavailable'),
        )
      }

      if (!workerRef.current) {
        return Promise.reject(new Error('Stockfish worker not ready'))
      }

      // Return cached result if available
      const cacheKey = evalCacheKey(fen, options)
      const cached = evalCache.current.get(cacheKey)
      if (cached) {
        if (workerRef.current) {
          workerRef.current.postMessage({ type: 'command', command: 'stop' })
        }
        activeRequestId.current = null
        requestFenById.current.clear()
        requestCacheKeyById.current.clear()
        rejectPendingEvaluations(
          pendingEvaluations.current,
          new Error('Stockfish evaluation superseded'),
        )
        setInfo(cached)
        setInfoFen(fen)
        setIsThinking(false)
        activeCacheKey.current = cacheKey
        return Promise.resolve({ move: cached[0]?.pv?.[0] ?? '', raw: '' })
      }

      setInfo([])
      setInfoFen(null)
      activeRequestId.current = null
      activeCacheKey.current = cacheKey
      requestFenById.current.clear()
      requestCacheKeyById.current.clear()
      rejectPendingEvaluations(
        pendingEvaluations.current,
        new Error('Stockfish evaluation superseded'),
      )
      const requestId = createRequestId()
      const payload = {
        type: 'evaluate-position' as const,
        id: requestId,
        fen,
        movetime: options?.movetime ?? 1200,
        depth: options?.depth,
        multipv: options?.multipv,
        searchmoves: options?.searchmoves,
      }

      const result = new Promise<EvaluationResult>((resolve, reject) => {
        pendingEvaluations.current.set(requestId, { resolve, reject })
      })

      requestFenById.current.set(requestId, fen)
      requestCacheKeyById.current.set(requestId, cacheKey)
      workerRef.current.postMessage(payload)
      return result
    },
    [enabled, error, status],
  )

  const stopSearch = useCallback(() => {
    if (workerRef.current) {
      workerRef.current.postMessage({ type: 'command', command: 'stop' })
    }
    activeRequestId.current = null
    requestFenById.current.clear()
    requestCacheKeyById.current.clear()
    rejectPendingEvaluations(
      pendingEvaluations.current,
      new Error('Stockfish evaluation stopped'),
    )
    setIsThinking(false)
    setInfo([])
    setInfoFen(null)
  }, [])

  const resetEngine = useCallback(() => {
    if (workerRef.current) {
      workerRef.current.postMessage({ type: 'newgame' })
    }

    activeRequestId.current = null
    requestFenById.current.clear()
    requestCacheKeyById.current.clear()
    setIsThinking(false)
    setInfo([])
    setInfoFen(null)
    rejectPendingEvaluations(pendingEvaluations.current, new Error('Engine reset'))
  }, [])

  return {
    status,
    error,
    isThinking,
    info,
    infoFen,
    stopSearch,
    evaluatePosition,
    resetEngine,
  }
}
