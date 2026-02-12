export type AnalyzeMoveMessage = {
  type: 'analyze-move'
  id: string
  fen: string
  move: string
  playerColor: 'white' | 'black'
  moveIndex?: number
}

export type AnalysisWorkerRequest =
  | AnalyzeMoveMessage
  | { type: 'terminate' }

export type AnalysisWorkerResponse =
  | { type: 'ready' }
  | { type: 'analysis-started'; id: string; move: string }
  | {
      type: 'analysis'
      id: string
      move: string
      bestMove: string
      bestEval: number | null
      playedEval: number | null
      delta: number | null
      blunder: boolean
    }
  | { type: 'error'; error: string }
  | { type: 'log'; message: string }
