import { Chess } from 'chess.js'
import { isWithinRecordingMoveCap } from '../workers/analysisUtils'
import type { RecordBlunderRequest } from './api'

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
  recordable: boolean
}

export type BlunderContext = {
  fen: string
  pgn: string
  moveSan: string
  moveUci: string // For matching with analysis result
  moveIndex: number
}

export type BlunderCheckParams = {
  analysis: AnalysisResult | null
  context: BlunderContext | null
  sessionId: string | null
  isGameActive: boolean
  alreadyRecorded: boolean
}

/** Candidate evaluation inputs: the recording decision minus the
 * `alreadyRecorded` gate, which is the DecisionOwner's concern (g-am9p). */
export type BlunderCandidateParams = Omit<BlunderCheckParams, 'alreadyRecorded'>

const uciToSan = (fen: string, uciMove: string): string | null => {
  if (!uciMove || uciMove === '(none)' || uciMove.length < 4) {
    return null
  }

  try {
    const board = new Chess(fen)
    const move = board.move({
      from: uciMove.slice(0, 2),
      to: uciMove.slice(2, 4),
      promotion: uciMove.slice(4) || undefined,
    })
    return move?.san ?? null
  } catch {
    return null
  }
}

/**
 * Evaluate whether an analysis result is a recordable blunder candidate,
 * returning the exact `POST /api/blunder` body (without `idempotency_key`,
 * which the caller stamps) or null. This is the eligibility logic shared by
 * `shouldRecordBlunder` and the DecisionOwner; it does NOT apply the
 * `alreadyRecorded` gate — that is the owner's responsibility.
 */
export const evaluateBlunderCandidate = (
  params: BlunderCandidateParams,
): RecordBlunderRequest | null => {
  const { analysis, context, sessionId, isGameActive } = params

  // No analysis or not recordable
  if (!analysis?.recordable) {
    return null
  }

  // No active session
  if (!sessionId || !isGameActive) {
    return null
  }

  // No context stored for this analysis
  if (!context) {
    return null
  }

  // Skip the very first move — ghost mode can never steer back to the
  // starting position, so recording it is pointless.
  if (context.moveIndex === 0) {
    return null
  }

  // Only record automatic blunders in the first 10 full moves.
  if (!isWithinRecordingMoveCap(context.moveIndex)) {
    return null
  }

  // Analysis doesn't match the pending move (compare UCI format)
  if (analysis.move !== context.moveUci) {
    return null
  }

  return {
    session_id: sessionId,
    pgn: context.pgn,
    fen: context.fen,
    user_move: context.moveSan, // API expects SAN format
    best_move: uciToSan(context.fen, analysis.bestMove) ?? analysis.bestMove,
    eval_before: analysis.bestEval ?? 0,
    eval_after: analysis.playedEval ?? 0,
  }
}

/**
 * Determines if a blunder should be recorded to the backend.
 * Returns the blunder data if it should be recorded, null otherwise.
 *
 * Thin wrapper over {@link evaluateBlunderCandidate} that adds the
 * `alreadyRecorded` gate and returns the legacy camelCase shape its callers
 * already consume.
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
  // Already recorded first blunder this session
  if (params.alreadyRecorded) {
    return null
  }

  const candidate = evaluateBlunderCandidate(params)
  if (!candidate) {
    return null
  }

  return {
    sessionId: candidate.session_id,
    pgn: candidate.pgn,
    fen: candidate.fen,
    userMove: candidate.user_move,
    bestMove: candidate.best_move,
    evalBefore: candidate.eval_before,
    evalAfter: candidate.eval_after,
  }
}
