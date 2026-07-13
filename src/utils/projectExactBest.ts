import { Chess } from 'chess.js'
import type { AnalysisMove, PositionAnalysis } from './api'
import type { AnalysisResult } from '../types/analysis'
import { isTrustedExactBestHit, reconcileTrustedBest } from '../workers/analysisUtils'

/**
 * Standard chess starting position. Must match AnalysisBoard's own default
 * `STARTING_FEN` (the FEN `buildMainLineMoveDetails` assumes for ply 0) — the
 * game-review path never passes a custom `startingFen`, so this is always the
 * `fen_before` of the first move.
 */
const STARTING_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'

/** Derive a played move's UCI (e.g. "e2e4", "e7e8q") from its SAN in a position. */
const sanToUci = (fenBefore: string, san: string): string | null => {
  try {
    const chess = new Chess(fenBefore)
    const m = chess.move(san)
    return m ? m.from + m.to + (m.promotion ?? '') : null
  } catch {
    return null
  }
}

/**
 * Game-review exact-best mirror (g-kfxj). The finished-game review screens render
 * `m.classification` verbatim from the backend session export, so a played move that
 * equals the TRUSTED position best can show the on-board best-arrow yet miss its own
 * gold "best" star when the stored row came from a weaker/earlier run than the
 * position best the backend now serves. The live (`GameAnalysisCoordinator`) and
 * interactive (`useMoveAnalysis`) paths already self-correct through
 * `reconcileTrustedBest`; this projects the same PROMOTION outcome onto the static
 * export so the review path agrees with itself.
 *
 * BOTH review pages (`GameAnalysisPage` and `HistoryPage`, g-22t8.2) project at their
 * own page seam and feed the result to every consumer — `useGameReviewStats` and
 * `AnalysisBoard` alike — so the displayed counts/Avg CPL agree with the board on the
 * promotions THIS helper makes. It says nothing about the board's other best stars:
 * `AnalysisBoard`'s re-annotation overlay (`upgraded`) sits above this layer and can
 * still star a move the page stats count as played, which is intended (board-only
 * grain). `AnalysisBoard` re-projects below that overlay (idempotent, so a no-op for
 * the two review pages) to cover callers that hand it unprojected moves.
 *
 * Gating mirrors `isTrustedExactBestHit` (`position_trusted === true` AND
 * `best_move_uci != null`) so untrusted legacy-seed bests cannot define exact-best
 * (the g-position-analysis invariant). Only the PROMOTION direction fires: we run
 * the shipped helper ONLY for a move whose played UCI already equals the trusted
 * best, so the helper's DEMOTION branch never applies here (the export does not
 * over-grade). Genuinely non-best moves and moves with no trusted position entry
 * pass through by identity (referentially unchanged, so React memoization holds).
 *
 * Single-sources the promotion fields by probing `reconcileTrustedBest` with a
 * minimal `AnalysisResult` rather than restating "classification → best, loss → 0"
 * here. On promotion we also repoint `best_move_san` at the played move so the
 * per-move played-vs-best arrows (AnalysisBoard `arrows`/`bestSquares`) don't draw a
 * contradictory "you should have played X" against the new best star.
 */
export const projectExactBest = (
  moves: AnalysisMove[],
  positionAnalysis: Record<string, PositionAnalysis> | undefined,
  startingFen: string = STARTING_FEN,
): AnalysisMove[] => {
  if (!positionAnalysis) return moves

  return moves.map((move, index) => {
    // Prefer the exact wire fields (g-cache-stronger-evals); fall back to chain
    // reconstruction / SAN parsing ONLY for legacy sessions whose wire fields are
    // null. The backend keys positionAnalysis by the original full `fen_before`, so
    // the wire value is also the correct lookup key.
    const fenBefore =
      move.fen_before ??
      (index === 0 ? startingFen : moves[index - 1]?.fen_after ?? startingFen)
    const entry = positionAnalysis[fenBefore]
    if (!entry || !isTrustedExactBestHit(entry)) return move

    const playedUci = move.move_uci ?? sanToUci(fenBefore, move.move_san)
    if (!playedUci || playedUci !== entry.best_move_uci) return move

    // Played move IS the trusted best → PROMOTION. Build a minimal probe so the
    // outcome ("classification: best, delta: 0") stays single-sourced in the helper.
    const probe: AnalysisResult = {
      id: '',
      move: playedUci,
      bestMove: entry.best_move_uci,
      bestLine: null,
      bestEval: null,
      playedEval: move.eval_cp,
      currentPositionEval: null,
      playedEvalMate: move.eval_mate,
      currentPositionEvalMate: null,
      moveIndex: index,
      delta: move.eval_delta,
      classification: move.classification,
      blunder: false,
      recordable: false,
    }
    const reconciled = reconcileTrustedBest(probe, entry.best_move_uci)
    // Already 'best' → helper returns the probe by reference: nothing to change.
    if (reconciled === probe) return move

    return {
      ...move,
      classification: reconciled.classification,
      eval_delta: reconciled.delta,
      best_move_san: move.move_san,
    }
  })
}
