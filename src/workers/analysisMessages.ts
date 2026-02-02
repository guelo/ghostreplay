export type AnalyzeMoveMessage = {
  type: 'analyze-move'
  id: string
  fen: string
  move: string
  movetime?: number
}

export type AnalysisWorkerRequest =
  | AnalyzeMoveMessage
  | { type: 'terminate' }

export type AnalysisWorkerResponse =
  | { type: 'ready' }
  | {
      type: 'analysis'
      id: string
      bestMove: string
      bestEval: number | null
      playedEval: number | null
      delta: number | null
      blunder: boolean
    }
  | { type: 'error'; error: string }
  | { type: 'log'; message: string }
