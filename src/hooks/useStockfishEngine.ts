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

const evalCacheKey = (fen: string, searchmoves?: string[]): string => {
  if (!searchmoves?.length) return fen
  return fen + '|' + [...searchmoves].sort().join(',')
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

export const useStockfishEngine = () => {
  const workerRef = useRef<Worker | null>(null)
  const pendingEvaluations = useRef<Map<string, PendingEntry>>(new Map())
  const activeRequestId = useRef<string | null>(null)
  const [status, setStatus] = useState<EngineStatus>('booting')
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<EngineInfo[]>([])
  const [isThinking, setIsThinking] = useState(false)
  const evalCache = useRef<Map<string, EngineInfo[]>>(new Map())
  const activeCacheKey = useRef<string | null>(null)

  useEffect(() => {
    const worker = new Worker(
      new URL('../workers/stockfishWorker.ts', import.meta.url),
      { type: 'module' },
    )
    workerRef.current = worker
    const pendingMap = pendingEvaluations.current

    const handleMessage = (event: MessageEvent<WorkerResponse>) => {
      const message = event.data

      switch (message.type) {
        case 'booted':
          break
        case 'ready':
          setStatus('ready')
          break
        case 'thinking':
          activeRequestId.current = message.id
          setIsThinking(true)
          setInfo([])
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
          }
          break
        case 'bestmove': {
          const entry = pendingMap.get(message.id)

          if (entry) {
            entry.resolve({ move: message.move, raw: message.raw })
            pendingMap.delete(message.id)
          }

          if (message.id === activeRequestId.current) {
            activeRequestId.current = null
            setIsThinking(false)
            // Cache completed result — capture the key now so the
            // updater doesn't read a stale/mutated ref later.
            const fenToCache = activeCacheKey.current
            if (fenToCache) {
              setInfo((current) => {
                evalCache.current.set(fenToCache, current)
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

          pendingMap.forEach((entry) => {
            entry.reject(new Error(message.error))
          })
          pendingMap.clear()
          break
        }
        case 'log':
          // Logs are forwarded for debugging but intentionally ignored here.
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
    }

    worker.addEventListener('message', handleMessage)
    worker.addEventListener('error', handleError)

    return () => {
      worker.removeEventListener('message', handleMessage)
      worker.removeEventListener('error', handleError)
      pendingMap.forEach((entry) => {
        entry.reject(new Error('Stockfish worker disposed'))
      })
      pendingMap.clear()
      worker.terminate()
      workerRef.current = null
    }
  }, [])

  const evaluatePosition = useCallback(
    (fen: string, options?: EvaluationOptions) => {
      if (status === 'error') {
        return Promise.reject(
          new Error(error ?? 'Stockfish engine unavailable'),
        )
      }

      if (!workerRef.current) {
        return Promise.reject(new Error('Stockfish worker not ready'))
      }

      // Return cached result if available
      const cacheKey = evalCacheKey(fen, options?.searchmoves)
      const cached = evalCache.current.get(cacheKey)
      if (cached) {
        setInfo(cached)
        setIsThinking(false)
        activeCacheKey.current = cacheKey
        return Promise.resolve({ move: cached[0]?.pv?.[0] ?? '', raw: '' })
      }

      setInfo([])
      activeCacheKey.current = cacheKey
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

      workerRef.current.postMessage(payload)
      return result
    },
    [error, status],
  )

  const stopSearch = useCallback(() => {
    if (!workerRef.current) return
    workerRef.current.postMessage({ type: 'command', command: 'stop' })
    activeRequestId.current = null
    setIsThinking(false)
    setInfo([])
  }, [])

  const resetEngine = useCallback(() => {
    if (!workerRef.current) {
      return
    }

    workerRef.current.postMessage({ type: 'newgame' })
    activeRequestId.current = null
    setIsThinking(false)
    setInfo([])
    pendingEvaluations.current.forEach((entry) => {
      entry.reject(new Error('Engine reset'))
    })
    pendingEvaluations.current.clear()
  }, [])

  return {
    status,
    error,
    isThinking,
    info,
    stopSearch,
    evaluatePosition,
    resetEngine,
  }
}
