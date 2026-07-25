export type AnalyzeMoveMessage = {
  type: 'analyze-move'
  id: string
  fen: string
  move: string
  playerColor: 'white' | 'black'
  moveIndex?: number
  legalMoveCount?: number
  /**
   * Search depth for the root, post-played, and post-best searches. Defaults to
   * 17 when omitted. Both in-game callers now pass `sessionAnalysisDepth()` — the
   * per-device depth, fixed for the whole page session (g-mk1d) — and the
   * analysis-board evidence driver passes 21 (g-cache-stronger-evals).
   *
   * The CALLER owns this value, and therefore owns the provenance claim built
   * from it: the worker no longer needs to echo the depth back, only whether the
   * configured limit was honestly reached (`capFired` below).
   */
  depth?: number
}

export type AnalysisWorkerRequest =
  | AnalyzeMoveMessage
  | { type: 'cancel-analysis'; id: string }
  | { type: 'terminate' }

import type { MoveClassification } from './analysisUtils'

/** Why a search (or a whole analyze-move) stopped. */
export type AnalysisStopReason = 'bestmove' | 'deadline'

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
      /**
       * True when ANY constituent search of this analyze-move was stopped by the
       * shared wall-clock deadline (g-mk1d §1.6). The tuple is then a TRUNCATED
       * search, so the caller must NOT attach a depth claim to it — the row falls
       * back to provenance-less `browser-game-v1`.
       *
       * This is the provenance-honesty signal, NOT `reachedDepth < depth`, which is
       * wrong in both directions: the stop can land just after `info depth N` is
       * reported (reached == requested, still truncated), and a forced mate can
       * finish below N with no cap — the configured limit was honestly satisfied
       * there, so that tuple KEEPS its provenance.
       */
      capFired: boolean
      /**
       * Why this analyze-move ended, folded across its constituent searches:
       * `'deadline'` if ANY was cut short by the shared budget, else
       * `'bestmove'`. Currently determined by `capFired` — it is the readable
       * form of the same fact, kept as its own field so a future third reason
       * (engine abort, stop-grace expiry) can be reported without consumers
       * having to re-interpret a boolean.
       */
      stopReason: AnalysisStopReason
      /** Deepest completed root iteration observed. Diagnostics only. */
      reachedDepth: number | null
    }
  | { type: 'error'; error: string; id?: string }
  | { type: 'log'; message: string }
