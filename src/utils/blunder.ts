/**
 * Blunder detection utilities
 */

export type AnalysisResult = {
  move: string
  bestMove: string
  bestEval: number | null
  playedEval: number | null
  delta: number | null
  blunder: boolean
}

export type BlunderContext = {
  fen: string
  pgn: string
  moveSan: string
  moveUci: string // For matching with analysis result
}

export type BlunderCheckParams = {
  analysis: AnalysisResult | null
  context: BlunderContext | null
  sessionId: string | null
  isGameActive: boolean
  alreadyRecorded: boolean
}

/**
 * Determines if a blunder should be recorded to the backend.
 * Returns the blunder data if it should be recorded, null otherwise.
 */
export const shouldRecordBlunder = (
  params: BlunderCheckParams,
): {
  sessionId: string
  pgn: string
  fen: string
  userMove: string
  bestMove: string
  evalBefore: number
  evalAfter: number
} | null => {
  const { analysis, context, sessionId, isGameActive, alreadyRecorded } = params

  // No analysis or not a blunder
  if (!analysis?.blunder) {
    return null
  }

  // No active session
  if (!sessionId || !isGameActive) {
    return null
  }

  // Already recorded first blunder this session
  if (alreadyRecorded) {
    return null
  }

  // No context stored for this analysis
  if (!context) {
    return null
  }

  // Analysis doesn't match the pending move (compare UCI format)
  if (analysis.move !== context.moveUci) {
    return null
  }

  return {
    sessionId,
    pgn: context.pgn,
    fen: context.fen,
    userMove: context.moveSan, // API expects SAN format
    bestMove: analysis.bestMove,
    evalBefore: analysis.bestEval ?? 0,
    evalAfter: analysis.playedEval ?? 0,
  }
}
