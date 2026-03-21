/**
 * Pure utility functions for move analysis.
 * Extracted from analysisWorker for testability.
 */

import type { EngineScore } from './stockfishMessages'

export const RECORDABLE_FAILURE_THRESHOLD_CP = 50
export const RECORDING_MOVE_CAP_FULL_MOVES = 10
const RECORDING_MOVE_CAP_PLY = RECORDING_MOVE_CAP_FULL_MOVES * 2

export type ParsedInfo = {
  score: EngineScore
}

export const parseInfo = (line: string): ParsedInfo | null => {
  if (!line.startsWith('info')) {
    return null
  }

  const tokens = line.split(' ')
  const scoreIndex = tokens.indexOf('score')

  if (scoreIndex === -1) {
    return null
  }

  const scoreType = tokens[scoreIndex + 1]
  const scoreValue = Number(tokens[scoreIndex + 2])

  if (Number.isNaN(scoreValue) || (scoreType !== 'cp' && scoreType !== 'mate')) {
    return null
  }

  return {
    score: {
      type: scoreType,
      value: scoreValue,
    },
  }
}

export const mateToCp = (movesToMate: number) => {
  const mateBase = 10000
  const mateDecay = 10
  // mate 0 means the side to move is checkmated (lost)
  if (movesToMate === 0) return -mateBase
  const sign = movesToMate >= 0 ? 1 : -1
  return sign * (mateBase - Math.abs(movesToMate) * mateDecay)
}

export const normalizeScore = (score: EngineScore | null, sideToMove: 'w' | 'b') => {
  if (!score) {
    return null
  }

  const raw = score.type === 'cp' ? score.value : mateToCp(score.value)
  const sign = sideToMove === 'w' ? 1 : -1
  return raw * sign
}

export const toWhitePerspective = (
  moverPerspectiveEval: number | null,
  moveIndex: number | null | undefined,
) => {
  if (moverPerspectiveEval === null || moveIndex === null || moveIndex === undefined) {
    return moverPerspectiveEval
  }

  return moveIndex % 2 === 0 ? moverPerspectiveEval : -moverPerspectiveEval
}

/** Convert a player-perspective eval to white perspective. */
export const playerToWhite = (
  playerPerspectiveEval: number | null,
  playerColor: 'white' | 'black',
): number | null => {
  if (playerPerspectiveEval === null) return null
  return playerColor === 'white' ? playerPerspectiveEval : -playerPerspectiveEval
}

export const scoreForPlayer = (
  score: EngineScore | null,
  sideToMove: 'w' | 'b',
  playerColor: 'white' | 'black',
) => {
  const whitePerspective = normalizeScore(score, sideToMove)
  if (whitePerspective === null) {
    return null
  }
  return playerColor === 'white' ? whitePerspective : -whitePerspective
}

export const getSideToMove = (fen: string) => {
  const parts = fen.split(' ')
  const active = parts[1]
  if (active === 'w' || active === 'b') {
    return active
  }
  return null
}

/**
 * Computes bestEval, playedEval, and delta from post-move search scores.
 *
 * Both scores must come from positions AFTER their respective moves (i.e. from
 * the opponent-to-move perspective). Using the pre-move minimax eval as bestEval
 * is unreliable because independent WASM Stockfish searches reach different
 * depths, inflating the delta (e.g. the 1.e4 false-blunder: minimax +97cp vs
 * post-move +29cp for the same resulting position).
 */
export const computeAnalysisResult = (input: {
  bestMove: string
  playedMove: string
  postPlayedScore: EngineScore | null
  postBestScore: EngineScore | null
  sideToMove: 'w' | 'b'
  playerColor: 'white' | 'black'
}): { bestEval: number | null; playedEval: number | null; delta: number | null } => {
  const opponentToMove = input.sideToMove === 'w' ? 'b' : 'w'
  const playedEval = scoreForPlayer(input.postPlayedScore, opponentToMove, input.playerColor)
  const bestEval = input.bestMove === input.playedMove
    ? playedEval
    : scoreForPlayer(input.postBestScore, opponentToMove, input.playerColor)

  const delta = bestEval !== null && playedEval !== null ? bestEval - playedEval : null
  return { bestEval, playedEval, delta }
}

/**
 * Determines if a move is a recordable failure (for blunder recording and SRS
 * review pass/fail). Uses a simple fixed threshold — no context-aware scaling.
 */
export const isRecordableFailure = (delta: number | null): boolean => {
  if (delta === null) return false
  return delta >= RECORDABLE_FAILURE_THRESHOLD_CP
}

export const isWithinRecordingMoveCap = (
  moveIndex: number | null | undefined,
): boolean => {
  if (moveIndex === null || moveIndex === undefined || moveIndex < 0) {
    return false
  }
  return moveIndex < RECORDING_MOVE_CAP_PLY
}

export type MoveClassification =
  | 'best'
  | 'excellent'
  | 'good'
  | 'inaccuracy'
  | 'mistake'
  | 'blunder'

/**
 * @deprecated Use classifyMoveAdvanced for new code. Kept as fallback for
 * legacy cache entries that lack a `classification` value.
 */
export const classifyMove = (
  delta: number | null,
): MoveClassification | null => {
  if (delta === null) return null

  const normalizedDelta = Math.max(delta, 0)
  if (normalizedDelta === 0) return 'best'
  if (normalizedDelta <= 10) return 'excellent'
  if (normalizedDelta <= 50) return 'good'
  if (normalizedDelta <= 100) return 'inaccuracy'
  if (normalizedDelta <= 149) return 'mistake'
  return 'blunder'
}

// ── Win-chance classifier (Lichess logistic model) ──────────────────

export const WIN_CHANCE_MULTIPLIER = -0.00368208
export const CP_CEILING = 1000

/**
 * Converts an engine score to a win chance between -1.0 and 1.0,
 * normalized to white's perspective.
 */
export const calculateWinChance = (
  score: EngineScore,
  pov: 'white' | 'black',
): number => {
  const whiteValue = pov === 'white' ? score.value : -score.value

  const cp =
    score.type === 'mate'
      ? (whiteValue >= 0 ? CP_CEILING : -CP_CEILING)
      : Math.max(-CP_CEILING, Math.min(CP_CEILING, whiteValue))

  return 2 / (1 + Math.exp(WIN_CHANCE_MULTIPLIER * cp)) - 1
}

/**
 * Detects mate transitions: blundering into being mated (MateCreated)
 * or throwing away a winning mate (MateLost). Returns a severity-adjusted
 * classification or null if no mate event occurred.
 *
 * Both scores share the same `scorePov` (the perspective they were reported from).
 * `mover` is the color that played the move being classified.
 */
export const checkMateEvents = (
  prevScore: EngineScore,
  nextScore: EngineScore,
  scorePov: 'white' | 'black',
  mover: 'white' | 'black',
): MoveClassification | null => {
  // Convert to mover POV
  const flipPrev = mover === scorePov ? 1 : -1
  const mPv = prevScore.value * flipPrev
  const mNv = nextScore.value * flipPrev

  // MateCreated: cp → losing mate (blundered into being mated)
  if (prevScore.type === 'cp' && nextScore.type === 'mate' && mNv < 0) {
    if (mPv < -999) return 'inaccuracy'
    if (mPv < -700) return 'mistake'
    return 'blunder'
  }

  // MateLost: winning mate → cp or losing mate
  if (
    prevScore.type === 'mate' &&
    mPv > 0 &&
    (nextScore.type === 'cp' || (nextScore.type === 'mate' && mNv < 0))
  ) {
    const resCp = nextScore.type === 'cp' ? mNv : -1000
    if (resCp > 999) return 'inaccuracy'
    if (resCp > 700) return 'mistake'
    return 'blunder'
  }

  return null
}

/**
 * Advanced move classifier using the Lichess logistic win-chance model.
 *
 * Both `prevScore` and `nextScore` are from post-move positions where the
 * opponent is to move, sharing the same `scorePov`.
 */
export const classifyMoveAdvanced = (input: {
  prevScore: EngineScore
  nextScore: EngineScore
  scorePov: 'white' | 'black'
  mover: 'white' | 'black'
  isBestMove: boolean
}): MoveClassification => {
  const { prevScore, nextScore, scorePov, mover, isBestMove } = input

  if (isBestMove) return 'best'

  const mateResult = checkMateEvents(prevScore, nextScore, scorePov, mover)
  if (mateResult) return mateResult

  const prevWc = calculateWinChance(prevScore, scorePov)
  const nextWc = calculateWinChance(nextScore, scorePov)

  const drop = mover === 'white' ? -(nextWc - prevWc) : (nextWc - prevWc)

  if (drop >= 0.30) return 'blunder'
  if (drop >= 0.20) return 'mistake'
  if (drop >= 0.10) return 'inaccuracy'
  if (drop >= 0.02) return 'good'
  return 'excellent'
}
