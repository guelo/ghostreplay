/**
 * Pure utility functions for move analysis.
 * Extracted from analysisWorker for testability.
 */

import type { EngineScore } from './stockfishMessages'

export const BLUNDER_THRESHOLD = 150

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

export const isBlunder = (delta: number | null): boolean => {
  return delta !== null && delta >= BLUNDER_THRESHOLD
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
  if (delta >= BLUNDER_THRESHOLD) return 'blunder'
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
