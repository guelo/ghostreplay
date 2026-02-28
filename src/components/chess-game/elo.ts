import { MAIA_ELO_BINS } from "./config";

/** Compute expected Elo stakes (win/loss deltas) for the difficulty selector. */
export function eloStakes(
  playerRating: number,
  opponentRating: number,
  isProvisional: boolean,
): { winDelta: number; lossDelta: number } {
  const k = isProvisional ? 40 : 20;
  const expected =
    1.0 / (1.0 + 10.0 ** ((opponentRating - playerRating) / 400.0));
  return {
    winDelta: Math.round(k * (1 - expected)),
    lossDelta: Math.round(k * (0 - expected)),
  };
}

/** Gaussian-sample a difficulty bin near the user's Elo (sigma controls spread). */
export function sampleEloBin(
  userElo: number,
  sigma = 125,
): (typeof MAIA_ELO_BINS)[number] {
  const weights = MAIA_ELO_BINS.map((bin) =>
    Math.exp(-((userElo - bin) ** 2) / (2 * sigma ** 2)),
  );
  const total = weights.reduce((a, b) => a + b, 0);
  let r = Math.random() * total;
  for (let i = 0; i < weights.length; i++) {
    r -= weights[i];
    if (r <= 0) return MAIA_ELO_BINS[i];
  }
  return MAIA_ELO_BINS[MAIA_ELO_BINS.length - 1];
}
