import type { AnalysisMove } from './api';

export type ClassKey = 'blunder' | 'mistake' | 'inaccuracy';
export type SideStats = Record<ClassKey, { count: number; indices: number[] }> & { avgCpl: number };
export type StatSelection = { side: 'player' | 'opponent'; cls: ClassKey } | null;

export const CLASS_KEYS: ClassKey[] = ['blunder', 'mistake', 'inaccuracy'];

export function computeSideStats(
  moves: AnalysisMove[],
  playerColor: 'white' | 'black',
): { player: SideStats; opponent: SideStats } {
  const makeSide = (): SideStats => ({
    blunder: { count: 0, indices: [] },
    mistake: { count: 0, indices: [] },
    inaccuracy: { count: 0, indices: [] },
    avgCpl: 0,
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
  player.avgCpl = playerDeltaCount > 0 ? Math.round(playerDeltaSum / playerDeltaCount) : 0;
  opponent.avgCpl = opponentDeltaCount > 0 ? Math.round(opponentDeltaSum / opponentDeltaCount) : 0;

  return { player, opponent };
}
