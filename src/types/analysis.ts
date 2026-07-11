/**
 * Neutral, dependency-free analysis domain types.
 *
 * These live here (rather than in the hook layer) so low-level modules like
 * `workers/analysisUtils` can operate on analysis results without importing back
 * up into `hooks/useMoveAnalysis`, which would form a layering cycle. This module
 * imports nothing — it is a leaf that every layer may depend on. The original
 * homes (`analysisUtils` for `MoveClassification`, `useMoveAnalysis` for
 * `AnalysisResult`) re-export these so existing consumers are unaffected.
 */

export type MoveClassification =
  | 'best'
  | 'excellent'
  | 'good'
  | 'inaccuracy'
  | 'mistake'
  | 'blunder'

/** Canonical centipawn magnitude used for terminal mate scores. */
export const MATE_BASE_CP = 10000

export type AnalysisResult = {
  id: string
  move: string
  bestMove: string
  /** Root best-move principal variation (UCI). Starts with bestMove. */
  bestLine?: string[] | null
  bestEval: number | null
  playedEval: number | null
  currentPositionEval: number | null
  // NOTE on perspective: despite the historical "player" naming, every eval
  // below is MOVER-relative — relative to the side that played the analyzed
  // move (callers pass the mover's color as `playerColor`/`analysisColor`, see
  // useChessGameController.commitAppliedMove). This is why downstream code
  // converts to white via parity-based `toWhitePerspective(_, moveIndex)` rather
  // than `playerToWhite(_, userColor)`. Keep that contract when wiring new
  // consumers, or the sign will flip on black moves.
  /** Mover-relative mate count for the played move, null when not a mate. */
  playedEvalMate: number | null
  /** Mover-relative mate count for the current position, null when not a mate. */
  currentPositionEvalMate: number | null
  moveIndex: number | null
  delta: number | null
  classification: MoveClassification | null
  blunder: boolean
  recordable: boolean
}
