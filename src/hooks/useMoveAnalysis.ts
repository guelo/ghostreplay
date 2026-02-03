import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  AnalysisWorkerResponse,
} from '../workers/analysisMessages'

const createRequestId = () => {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID()
  }

  return Math.random().toString(36).slice(2)
}

type AnalysisStatus = 'booting' | 'ready' | 'error'

type AnalyzeMoveOptions = {
  movetime?: number
}

type AnalysisResult = {
  id: string
  move: string
  bestMove: string
  bestEval: number | null
  playedEval: number | null
  currentPositionEval: number | null
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
        case 'analysis':
          setIsAnalyzing(false)
          setAnalyzingMove(null)
          setLastAnalysis({
            id: message.id,
            move: message.move,
            bestMove: message.bestMove,
            bestEval: message.bestEval,
            playedEval: message.playedEval,
            currentPositionEval: message.playedEval,
            delta: message.delta,
            blunder: message.blunder,
          })
          if (message.blunder && message.delta !== null) {
            console.log(
              `[Analyst] Blunder detected: Δ${message.delta}cp (best ${message.bestMove}).`,
            )
          }
          break
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
    (
      fen: string,
      move: string,
      playerColor: 'white' | 'black',
      options?: AnalyzeMoveOptions,
    ) => {
      if (status === 'error') {
        return
      }

      if (!workerRef.current) {
        return
      }

      workerRef.current.postMessage({
        type: 'analyze-move',
        id: createRequestId(),
        fen,
        move,
        playerColor,
        movetime: options?.movetime,
      })
    },
    [status],
  )

  return {
    status,
    error,
    lastAnalysis,
    isAnalyzing,
    analyzingMove,
    analyzeMove,
  }
}
