export type AnalyzeMoveMessage = {
  type: 'analyze-move'
  id: string
  fen: string
  move: string
  playerColor: 'white' | 'black'
  moveIndex?: number
  legalMoveCount?: number
}

export type AnalysisWorkerRequest =
  | AnalyzeMoveMessage
  | { type: 'cancel-analysis'; id: string }
  | { type: 'terminate' }

import type { MoveClassification } from './analysisUtils'

export type AnalysisWorkerResponse =
  | { type: 'ready' }
  | { type: 'analysis-started'; id: string; move: string }
  | { type: 'analysis-streaming'; id: string; cp: number; depth: number }
  | {
      type: 'analysis'
      id: string
      move: string
      bestMove: string
      bestEval: number | null
      playedEval: number | null
      delta: number | null
      classification: MoveClassification | null
    }
  | { type: 'error'; error: string }
  | { type: 'log'; message: string }
