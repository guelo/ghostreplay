/**
 * Variant A — adaptive two-search grading (g-two-search-grade §3.1), MINIMAL.
 *
 *     position fen F           ; go depth N+1  -> B, root score, root PV
 *     position fen F moves P   ; go depth N    -> played score
 *
 * The root search spends one ply naming `B`, leaving about `N` continuation
 * plies — equal to the post-played search's horizon. The post-played search is
 * KEPT when `P === B` (§3.1): it supplies the resulting-position eval and the
 * `analysis-streaming` updates, and the same search then serves as the post-best
 * score, since it is a search of the same position.
 *
 * MINIMAL, on purpose (g-grade-kill-gate): §5 normalization and §6 resolution
 * arrive whole in g-grade-variant-b. Until then this arm DECLINES to grade a
 * `P !== B` row — `postBestScore: null`, so the shared tail emits
 * `canonical: false` with no classification. Declining costs nothing the kill
 * gate measures: both searches still run exactly as §3.1 specifies, so latency
 * — the only thing that bead gates on — is unaffected, and no cross-frame
 * comparison is ever written to a committed JSONL. Converting inline instead
 * would be a SIGN inversion plus a mate-distance error, which is precisely what
 * §5 exists to get right.
 */

import type { CandidateContext, CandidateOutcome, CandidateProtocol } from './contract'

/**
 * §2's invariant: an analyze-move's `go depth` stays BELOW this.
 *
 * The candidates peak at `MAX_DEVICE_DEPTH + 1 = 18`, so any future tier raise
 * is bounded to 19. Depth 21 belongs to the visible analysis-board path, which
 * never selects a candidate arm — and if it somehow did, `N + 1 = 22` would be a
 * protocol nobody asked for. Throwing makes the runner record a per-row error
 * instead of measuring it.
 */
export const MAX_ANALYZE_MOVE_DEPTH_EXCLUSIVE = 21

export const variantA: CandidateProtocol = {
  arm: 'variantA',
  run: async (context: CandidateContext): Promise<CandidateOutcome> => {
    const rootDepth = context.requestedDepth + 1
    if (rootDepth >= MAX_ANALYZE_MOVE_DEPTH_EXCLUSIVE) {
      throw new Error(
        `variantA root depth ${rootDepth} violates the analyze-move depth invariant ` +
          `(§2: go depth must stay below ${MAX_ANALYZE_MOVE_DEPTH_EXCLUSIVE})`,
      )
    }

    const root = await context.search([], { depth: rootDepth })
    context.checkCanceled()

    const bestMove = root.bestmove
    if (!bestMove || bestMove === '(none)') {
      // No legal move: the shared tail owns the early return, exactly as it does
      // for the current protocol.
      return {
        bestMove: bestMove || '(none)',
        rootPv: null,
        continuationPv: null,
        postPlayedScore: null,
        postBestScore: null,
        capFired: root.capFired,
        reachedDepth: root.reachedDepth,
      }
    }

    // A terminal played move has an exact score and nothing to search.
    const terminalPlayedScore = context.terminalScoreAfterMove(context.playedMove)
    let postPlayedScore = terminalPlayedScore
    let playedPv: string[] | null = null
    let capFired = root.capFired

    if (!terminalPlayedScore) {
      const played = await context.search([context.playedMove], {
        depth: context.requestedDepth,
        onInfo: context.streamPlayed,
      })
      capFired = capFired || played.capFired
      postPlayedScore = played.score
      playedPv = played.pv
    }
    context.checkCanceled()

    const isBestMove = context.playedMove === bestMove

    return {
      bestMove,
      rootPv: root.pv,
      // Only meaningful when the searched position IS the position after `B`.
      continuationPv: isBestMove ? playedPv : null,
      postPlayedScore,
      // Same position, same search — not a second measurement of it. Null when
      // `P !== B`: see the module note on declining to grade.
      postBestScore: isBestMove ? postPlayedScore : null,
      capFired,
      reachedDepth: root.reachedDepth,
    }
  },
}
