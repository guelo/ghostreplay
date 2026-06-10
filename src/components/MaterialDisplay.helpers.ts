export const PIECE_VALUES: Record<string, number> = { p: 1, n: 3, b: 3, r: 5, q: 9 };
export const STARTING_COUNTS: Record<string, number> = {
  p: 8,
  n: 2,
  b: 2,
  r: 2,
  q: 1,
};
export const PIECE_ORDER = ["p", "n", "b", "r", "q"] as const;

export function parseMaterial(fen: string) {
  const placement = fen.split(" ")[0];
  const counts = { w: { ...STARTING_COUNTS }, b: { ...STARTING_COUNTS } };

  // Count pieces remaining on board, then captured = starting - remaining
  const remaining: Record<string, Record<string, number>> = {
    w: { p: 0, n: 0, b: 0, r: 0, q: 0 },
    b: { p: 0, n: 0, b: 0, r: 0, q: 0 },
  };

  for (const ch of placement) {
    if (ch === "/" || (ch >= "1" && ch <= "8")) continue;
    const lower = ch.toLowerCase();
    if (!(lower in PIECE_VALUES)) continue;
    const color = ch === lower ? "b" : "w";
    remaining[color][lower]++;
  }

  // Captured by white = black pieces missing from board
  // Captured by black = white pieces missing from board
  const capturedByWhite: Record<string, number> = {};
  const capturedByBlack: Record<string, number> = {};
  for (const piece of PIECE_ORDER) {
    capturedByWhite[piece] = counts.b[piece] - remaining.b[piece];
    capturedByBlack[piece] = counts.w[piece] - remaining.w[piece];
  }

  return { capturedByWhite, capturedByBlack };
}
