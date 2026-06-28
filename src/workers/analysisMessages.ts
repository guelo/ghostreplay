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
  /**
   * Per-search liveness ping for the inactivity watchdog. Emitted from ANY of an
   * analysis's searches (root / post-played / post-best) — the engine-line level,
   * not `onInfo` — so the previously-silent root and post-best phases surface
   * activity too. Worker-throttled. Distinct from `analysis-streaming`, which is
   * display-only and post-played; this carries NO eval/depth and is consumed
   * solely to reset the per-request inactivity watchdog.
   */
  | { type: 'analysis-progress'; id: string }
  | {
      type: 'analysis'
      id: string
      move: string
      bestMove: string
      /**
       * Root best-move principal variation as UCI moves. Validated to start
       * with `bestMove`; falls back to `[bestMove]` when the captured PV is
       * empty or does not begin with the final bestmove.
       */
      bestLine: string[]
      bestEval: number | null
      playedEval: number | null
      /** Player-relative mate count for the best move, null when not a mate. */
      bestEvalMate: number | null
      /** Player-relative mate count for the played move, null when not a mate. */
      playedEvalMate: number | null
      delta: number | null
      classification: MoveClassification | null
      /**
       * False when the classification came from the legacy delta-band fallback
       * (classifyMove) rather than the canonical win-chance model
       * (classifyMoveAdvanced), or when the position yielded no best move.
       * Diagnostics only — not persisted (profile propagation is a follow-up).
       */
      canonical: boolean
    }
  | { type: 'error'; error: string; id?: string }
  | { type: 'log'; message: string }
