import type { EngineScore } from './stockfishMessages'

/**
 * Ordered score categories: losing mate < cp < winning mate.
 *
 * The category step is what makes winning-mate versus losing-mate total by
 * construction, without ever converting a mate distance into a centipawn
 * surrogate (g-two-search-grade §5.4).
 */
const category = (score: EngineScore): -1 | 0 | 1 => {
  if (score.type !== 'mate') {
    return 0
  }
  return score.value > 0 ? 1 : -1
}

/**
 * The ONE total order over root-frame, mover-relative scores.
 *
 * Returns -1 when `a` is worse than `b`, 1 when better, 0 when equal.
 *
 * Within a category: a shorter winning mate is better, a longer losing mate is
 * better, and a larger CP is better. Winning and losing mates therefore share one
 * rule — the smaller mate distance wins — because +2 beats +5 and -10 beats -1.
 *
 * Deliberately NOT `calculateWinChance`, which collapses every mate to
 * ±CP_CEILING and clamps CP to the same bounds, making it unusable for ordering.
 *
 * Root mate 0 is invalid at this boundary: it means the side to move is already
 * mated, so no legal move exists to order. Callers reject it before comparing
 * (`mate-zero` in `pvSnapshots.ts`).
 *
 * §5.4 names three surfaces for this comparator: worker ordering,
 * restricted-search slot-order validation, and server ingress. Only slot-order
 * validation is wired today; the Python twin and the shared golden fixtures land
 * with the score-model work.
 */
export const compareRootScores = (a: EngineScore, b: EngineScore): -1 | 0 | 1 => {
  const categoryA = category(a)
  const categoryB = category(b)

  if (categoryA !== categoryB) {
    return categoryA < categoryB ? -1 : 1
  }

  if (a.value === b.value) {
    return 0
  }

  // Both mates: the shorter distance is the better score, whichever side is
  // delivering it. Both CP: the larger value is the better score.
  if (categoryA !== 0) {
    return a.value < b.value ? 1 : -1
  }

  return a.value < b.value ? -1 : 1
}
