import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  EngineInfo,
  WorkerResponse,
} from '../workers/stockfishMessages'

type EngineStatus = 'booting' | 'ready' | 'error'

type EvaluationOptions = {
  movetime?: number
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
  const [info, setInfo] = useState<EngineInfo | null>(null)
  const [isThinking, setIsThinking] = useState(false)

  useEffect(() => {
    if (typeof SharedArrayBuffer === 'undefined') {
      setStatus('error')
      setError(
        'SharedArrayBuffer is unavailable. Ensure COOP/COEP headers are active and reload the page.',
      )
      return
    }

    const worker = new Worker(
      new URL('../workers/stockfishWorker.ts', import.meta.url),
      { type: 'module' },
    )
    workerRef.current = worker

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
          setInfo(null)
          break
        case 'info':
          if (message.id === activeRequestId.current) {
            setInfo(message.info)
          }
          break
        case 'bestmove': {
          const entry = pendingEvaluations.current.get(message.id)

          if (entry) {
            entry.resolve({ move: message.move, raw: message.raw })
            pendingEvaluations.current.delete(message.id)
          }

          if (message.id === activeRequestId.current) {
            activeRequestId.current = null
            setIsThinking(false)
            setInfo(null)
          }
          break
        }
        case 'error': {
          setStatus('error')
          setError(message.error)
          setIsThinking(false)
          activeRequestId.current = null

          pendingEvaluations.current.forEach((entry) => {
            entry.reject(new Error(message.error))
          })
          pendingEvaluations.current.clear()
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
      pendingEvaluations.current.forEach((entry) => {
        entry.reject(new Error('Stockfish worker disposed'))
      })
      pendingEvaluations.current.clear()
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

      const requestId = createRequestId()
      const payload = {
        type: 'evaluate-position' as const,
        id: requestId,
        fen,
        movetime: options?.movetime ?? 1200,
      }

      const result = new Promise<EvaluationResult>((resolve, reject) => {
        pendingEvaluations.current.set(requestId, { resolve, reject })
      })

      workerRef.current.postMessage(payload)
      return result
    },
    [error, status],
  )

  const resetEngine = useCallback(() => {
    if (!workerRef.current) {
      return
    }

    workerRef.current.postMessage({ type: 'newgame' })
    activeRequestId.current = null
    setIsThinking(false)
    setInfo(null)
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
    evaluatePosition,
    resetEngine,
  }
}
