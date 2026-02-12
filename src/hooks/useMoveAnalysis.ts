import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  AnalyzeMoveMessage,
  AnalysisWorkerResponse,
} from '../workers/analysisMessages'

const createRequestId = () => {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID()
  }

  return Math.random().toString(36).slice(2)
}

type AnalysisStatus = 'booting' | 'ready' | 'error'

export type AnalysisResult = {
  id: string
  move: string
  bestMove: string
  bestEval: number | null
  playedEval: number | null
  currentPositionEval: number | null
  moveIndex: number | null
  delta: number | null
  blunder: boolean
}

export const useMoveAnalysis = () => {
  const workerRef = useRef<Worker | null>(null)
  const [status, setStatus] = useState<AnalysisStatus>('booting')
  const [error, setError] = useState<string | null>(null)
  const [lastAnalysis, setLastAnalysis] = useState<AnalysisResult | null>(null)
  const [isAnalyzing, setIsAnalyzing] = useState(false)
  const [analyzingMove, setAnalyzingMove] = useState<string | null>(null)
  const [analysisMap, setAnalysisMap] = useState<Map<number, AnalysisResult>>(new Map())
  // Maps request IDs to move indices so we can file results into analysisMap
  const pendingMoveIndices = useRef<Map<string, number>>(new Map())

  useEffect(() => {
    const worker = new Worker(
      new URL('../workers/analysisWorker.ts', import.meta.url),
      { type: 'module' },
    )
    workerRef.current = worker

    const handleMessage = (event: MessageEvent<AnalysisWorkerResponse>) => {
      const message = event.data

      switch (message.type) {
        case 'ready':
          setStatus('ready')
          break
        case 'analysis-started':
          setIsAnalyzing(true)
          setAnalyzingMove(message.move)
          break
        case 'analysis': {
          setIsAnalyzing(false)
          setAnalyzingMove(null)
          const moveIndex = pendingMoveIndices.current.get(message.id)
          if (moveIndex !== undefined) {
            pendingMoveIndices.current.delete(message.id)
          }

          const result: AnalysisResult = {
            id: message.id,
            move: message.move,
            bestMove: message.bestMove,
            bestEval: message.bestEval,
            playedEval: message.playedEval,
            currentPositionEval: message.playedEval,
            moveIndex: moveIndex ?? null,
            delta: message.delta,
            blunder: message.blunder,
          }
          setLastAnalysis(result)
          if (moveIndex !== undefined) {
            setAnalysisMap(prev => {
              const next = new Map(prev)
              next.set(moveIndex, result)
              return next
            })
          }
          if (message.blunder && message.delta !== null) {
            console.log(
              `[Analyst] Blunder detected: Δ${message.delta}cp (best ${message.bestMove}).`,
            )
          }
          break
        }
        case 'error':
          setStatus('error')
          setError(message.error)
          setIsAnalyzing(false)
          setAnalyzingMove(null)
          break
        case 'log':
          console.log(`[Analyst] ${message.message}`)
          break
        default:
          message satisfies never
      }
    }

    const handleError = (event: ErrorEvent) => {
      setStatus('error')
      setError(event.message)
    }

    worker.addEventListener('message', handleMessage)
    worker.addEventListener('error', handleError)

    return () => {
      worker.removeEventListener('message', handleMessage)
      worker.removeEventListener('error', handleError)
      worker.terminate()
      workerRef.current = null
    }
  }, [])

  const analyzeMove = useCallback(
    (fen: string, move: string, playerColor: 'white' | 'black', moveIndex?: number) => {
      if (status === 'error') {
        return
      }

      if (!workerRef.current) {
        return
      }

      const id = createRequestId()
      if (moveIndex !== undefined) {
        pendingMoveIndices.current.set(id, moveIndex)
      }

      const message: AnalyzeMoveMessage = {
        type: 'analyze-move',
        id,
        fen,
        move,
        playerColor,
        ...(moveIndex !== undefined ? { moveIndex } : {}),
      }

      workerRef.current.postMessage(message)
    },
    [status],
  )

  const clearAnalysis = useCallback(() => {
    setLastAnalysis(null)
    setAnalysisMap(new Map())
    pendingMoveIndices.current.clear()
  }, [])

  return {
    status,
    error,
    lastAnalysis,
    analysisMap,
    isAnalyzing,
    analyzingMove,
    analyzeMove,
    clearAnalysis,
  }
}
