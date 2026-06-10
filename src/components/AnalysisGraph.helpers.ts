const WINNING_CHANCES_SLOPE = 0.00368208;
const WINNING_CHANCES_MAX_CP = 1000;

/**
 * Match Lichess's analysis graph by plotting bounded winning chances instead of
 * raw centipawns. This keeps large late-game swings visible without a hard
 * centipawn ceiling flattening the chart.
 */
export const cpToWinningChances = (cp: number) => {
  const clamped = Math.max(-WINNING_CHANCES_MAX_CP, Math.min(WINNING_CHANCES_MAX_CP, cp));
  return 2 / (1 + Math.exp(-WINNING_CHANCES_SLOPE * clamped)) - 1;
};
