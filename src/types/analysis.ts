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

/**
 * The DYNAMIC half of a `browser-game-v2` cache row's identity (g-mk1d §2.1): what
 * engine artifact and search settings this device actually used for ONE move's own
 * local search. Sent per uploaded move; the server stamps the fixed half (engine
 * name, MultiPV, analyzer protocol, manifest digest) and never accepts a
 * client-sent profile id.
 *
 * Attached ONLY to a raw worker tuple the worker itself declared eligible
 * (`evidenceEligible` — untruncated AND canonically graded; see
 * `workerTupleProvenance`). It is cleared whenever the tuple stops describing
 * that search — a canonical reconciliation rewrite, a time-truncated
 * (`capFired`) search, or a delta-band fallback classification — so a depth
 * claim is never stamped on numbers, or a grade, the claimed search did not
 * produce. See `reconcileTrustedBest`.
 *
 * Self-reported diagnostics by design: forging these can only reorder
 * NON-authoritative browser rows within the browser tier. It can never cross the
 * authority barrier, earn a capability, or touch position truth.
 */
export type BrowserAnalysisProvenance = {
  engine_version: string
  engine_build: string
  eval_file_id: string
  search_limit_type: 'depth'
  search_limit_value: number
  threads: number
  hash_mb: number
}

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
  /** RAW uncapped mover-relative loss (bestEval − playedEval); may be mate pseudo-cp (~10000).
   * Normalized to the 0..1000 display/decision CPL by evalLoss at the boundary (e.g. the
   * DecisionOwner SRS send). */
  delta: number | null
  classification: MoveClassification | null
  blunder: boolean
  recordable: boolean
  /**
   * This device's own search provenance for THIS tuple, or null/absent when the
   * tuple is not honest raw worker output — a cache-sourced result (someone
   * else's search), a canonically reconciled tuple, a time-truncated search, or
   * a search whose grading fell back to the delta band. Uploaded per move;
   * null ⇒ the row is stamped `browser-game-v1` with no strength claim.
   */
  provenance?: BrowserAnalysisProvenance | null
}
