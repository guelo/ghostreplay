import type { AnalysisMove } from './api';

export type ClassKey = 'blunder' | 'mistake' | 'inaccuracy';
/** A single class, or 'all' — the header selection that spans every class. */
export type ClassSelector = ClassKey | 'all';
/** `avgCpl` is null iff `avgCplCount === 0` — no evaluated move for that side. A genuine 0 is perfect play. */
export type SideStats = Record<ClassKey, { count: number; indices: number[] }> & { avgCpl: number | null; avgCplCount: number };
export type StatSelection = { side: 'player' | 'opponent'; cls: ClassSelector } | null;

/** One highlighted move on the analysis graph, carrying its own classification color. */
export type HighlightDot = { index: number; classification: ClassKey };
export type HighlightedMoves = { dots: HighlightDot[] };

export const CLASS_KEYS: ClassKey[] = ['blunder', 'mistake', 'inaccuracy'];

/**
 * Expand a stats selection into the sorted list of highlight dots it covers.
 * Shared by the highlight-set and the click-cycle-set so they can't drift.
 * A single-class selection yields that class's indices (unchanged cell behavior);
 * cls === 'all' unions every class. Classes are disjoint (one classification per
 * move) so no dedup is needed; the result is sorted ascending by move index.
 */
export function selectionDots(
  sideStats: { player: SideStats; opponent: SideStats },
  sel: StatSelection,
): HighlightDot[] {
  if (!sel) return [];
  const stats = sel.side === 'player' ? sideStats.player : sideStats.opponent;
  const classes: ClassKey[] = sel.cls === 'all' ? CLASS_KEYS : [sel.cls];
  return classes
    .flatMap((cls) => stats[cls].indices.map((index) => ({ index, classification: cls })))
    .sort((a, b) => a.index - b.index);
}

export function computeSideStats(
  moves: AnalysisMove[],
  playerColor: 'white' | 'black',
): { player: SideStats; opponent: SideStats } {
  const makeSide = (): SideStats => ({
    blunder: { count: 0, indices: [] },
    mistake: { count: 0, indices: [] },
    inaccuracy: { count: 0, indices: [] },
    avgCpl: null,
    avgCplCount: 0,
  });
  const player = makeSide();
  const opponent = makeSide();

  let playerDeltaSum = 0, playerDeltaCount = 0;
  let opponentDeltaSum = 0, opponentDeltaCount = 0;

  for (let i = 0; i < moves.length; i++) {
    const m = moves[i];
    const isPlayer = m.color === playerColor;
    const side = isPlayer ? player : opponent;
    const cls = m.classification as ClassKey | null;
    if (cls && cls in side) {
      side[cls].count++;
      side[cls].indices.push(i);
    }
    if (m.eval_delta != null) {
      if (isPlayer) {
        playerDeltaSum += m.eval_delta;
        playerDeltaCount++;
      } else {
        opponentDeltaSum += m.eval_delta;
        opponentDeltaCount++;
      }
    }
  }
  player.avgCpl = playerDeltaCount > 0 ? Math.round(playerDeltaSum / playerDeltaCount) : null;
  opponent.avgCpl = opponentDeltaCount > 0 ? Math.round(opponentDeltaSum / opponentDeltaCount) : null;
  player.avgCplCount = playerDeltaCount;
  opponent.avgCplCount = opponentDeltaCount;

  return { player, opponent };
}
