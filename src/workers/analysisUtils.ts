/**
 * Pure utility functions for move analysis.
 * Extracted from analysisWorker for testability.
 */

import type { EngineScore } from './stockfishMessages'

export const BLUNDER_THRESHOLD = 50
export const MOVE_LIST_BLUNDER_THRESHOLD = 150
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
 * Determines if a move is a blunder based on eval drop and position context.
 * The threshold scales with how lost the position already is:
 * - In equal positions (±200cp): base threshold of 50cp
 * - When already losing badly: threshold increases (losing 50cp more when down 500 is less meaningful)
 * - preEval is from the player's perspective (positive = advantage)
 */
export const isBlunder = (delta: number | null, preEval?: number | null): boolean => {
  if (delta === null || delta < BLUNDER_THRESHOLD) {
    return false
  }

  if (preEval === null || preEval === undefined) {
    return delta >= BLUNDER_THRESHOLD
  }

  // Scale threshold: when already losing, require a bigger drop to flag as blunder.
  // At eval 0: threshold = 50. At eval -500: threshold = 100. At eval -1000: threshold = 150.
  const disadvantage = Math.max(-preEval, 0)
  const scaledThreshold = BLUNDER_THRESHOLD + disadvantage * 0.1
  return delta >= scaledThreshold
}

export const isWithinRecordingMoveCap = (
  moveIndex: number | null | undefined,
): boolean => {
  if (moveIndex === null || moveIndex === undefined || moveIndex < 0) {
    return false
  }
  return moveIndex < RECORDING_MOVE_CAP_PLY
}

export type MoveClassification = 'blunder' | 'inaccuracy' | 'good' | 'great' | 'best'

export type SessionMoveClassification =
  | 'best'
  | 'excellent'
  | 'good'
  | 'inaccuracy'
  | 'mistake'
  | 'blunder'

export const classifyMove = (delta: number | null): MoveClassification | null => {
  if (delta === null) return null
  if (delta >= MOVE_LIST_BLUNDER_THRESHOLD) return 'blunder'
  if (delta >= 50) return 'inaccuracy'
  if (delta < 0) return 'great'
  if (delta === 0) return 'best'
  return 'good'
}

export const classifySessionMove = (
  delta: number | null,
): SessionMoveClassification | null => {
  if (delta === null) return null

  const normalizedDelta = Math.max(delta, 0)
  if (normalizedDelta === 0) return 'best'
  if (normalizedDelta <= 10) return 'excellent'
  if (normalizedDelta <= 50) return 'good'
  if (normalizedDelta <= 100) return 'inaccuracy'
  if (normalizedDelta <= 149) return 'mistake'
  return 'blunder'
}

export const ANNOTATION_SYMBOL: Record<MoveClassification, string> = {
  blunder: '??',
  inaccuracy: '?!',
  good: '',
  great: '!',
  best: '!',
}
