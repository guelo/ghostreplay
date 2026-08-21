/**
 * Browser port of the Lichess scalachess game-phase divider.
 *
 * Keep this in sync with backend/app/game_phase.py, the server-side co-authority,
 * via src/utils/__fixtures__/gamePhaseParity.json and the parity tests on both
 * sides. The upstream Divider.scala implementation is MIT licensed; attribution
 * and the full license notice for this derived logic live in game_phase.py.
 */

const STARTING_FEN =
  "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const MAX_PROBE_PLY = 80;
const PIECES = /^[pnbrqkPNBRQK]$/;

type Piece =
  | "p"
  | "n"
  | "b"
  | "r"
  | "q"
  | "k"
  | "P"
  | "N"
  | "B"
  | "R"
  | "Q"
  | "K";
export type Placement = readonly (Piece | null)[];

/** Parse only the piece-placement field of a FEN, failing closed on bad input. */
export function parsePlacement(fen: unknown): Placement | null {
  if (typeof fen !== "string" || fen.trim() === "") return null;
  const placement = fen.trim().split(/\s+/, 1)[0];
  const fenRanks = placement.split("/");
  if (fenRanks.length !== 8) return null;

  const squares: (Piece | null)[] = Array(64).fill(null);
  for (let fenRank = 0; fenRank < fenRanks.length; fenRank += 1) {
    let file = 0;
    let previousWasDigit = false;
    for (const symbol of fenRanks[fenRank]) {
      if (/^[1-8]$/.test(symbol)) {
        if (previousWasDigit) return null;
        file += Number(symbol);
        previousWasDigit = true;
        continue;
      }
      if (!PIECES.test(symbol) || file >= 8) return null;
      const rankFromWhite = 7 - fenRank;
      squares[rankFromWhite * 8 + file] = symbol as Piece;
      file += 1;
      previousWasDigit = false;
    }
    if (file !== 8) return null;
  }
  return squares;
}

export function majorsAndMinors(position: Placement): number {
  let count = 0;
  for (const piece of position) {
    if (
      piece != null &&
      piece.toLowerCase() !== "p" &&
      piece.toLowerCase() !== "k"
    ) {
      count += 1;
    }
  }
  return count;
}

export function backrankSparse(position: Placement): boolean {
  let whiteBackrank = 0;
  let blackBackrank = 0;
  for (let file = 0; file < 8; file += 1) {
    const whitePiece = position[file];
    const blackPiece = position[7 * 8 + file];
    if (whitePiece != null && whitePiece === whitePiece.toUpperCase()) {
      whiteBackrank += 1;
    }
    if (blackPiece != null && blackPiece === blackPiece.toLowerCase()) {
      blackBackrank += 1;
    }
  }
  return whiteBackrank < 4 || blackBackrank < 4;
}

function regionScore(y: number, white: number, black: number): number {
  if (white === 0 && black === 0) return 0;
  if (white === 1 && black === 0) return 1 + (8 - y);
  if (white === 2 && black === 0) return y > 2 ? 2 + (y - 2) : 0;
  if (white === 3 && black === 0) return y > 1 ? 3 + (y - 1) : 0;
  if (white === 4 && black === 0) return y > 1 ? 3 + (y - 1) : 0;
  if (white === 0 && black === 1) return 1 + y;
  if (white === 1 && black === 1) return 5 + Math.abs(4 - y);
  if (white === 2 && black === 1) return 4 + (y - 1);
  if (white === 3 && black === 1) return 5 + (y - 1);
  if (white === 0 && black === 2) return y < 6 ? 2 + (6 - y) : 0;
  if (white === 1 && black === 2) return 4 + (7 - y);
  if (white === 2 && black === 2) return 7;
  if (white === 0 && black === 3) return y < 7 ? 3 + (7 - y) : 0;
  if (white === 1 && black === 3) return 5 + (7 - y);
  if (white === 0 && black === 4) return y < 7 ? 3 + (7 - y) : 0;
  return 0;
}

export function mixedness(position: Placement): number {
  let total = 0;
  for (let y = 0; y <= 6; y += 1) {
    for (let x = 0; x <= 6; x += 1) {
      let white = 0;
      let black = 0;
      const lowerLeft = y * 8 + x;
      for (const offset of [0, 1, 8, 9]) {
        const piece = position[lowerLeft + offset];
        if (piece == null) continue;
        if (piece === piece.toUpperCase()) white += 1;
        else black += 1;
      }
      total += regionScore(y + 1, white, black);
    }
  }
  return total;
}

export function isMiddlegame(position: Placement): boolean {
  return (
    majorsAndMinors(position) <= 10 ||
    backrankSparse(position) ||
    mixedness(position) > 150
  );
}

/** Return the absolute ply where the opening ends, or null when it is unknown. */
export function openingPlyCount(
  postMoveFens: readonly (string | null | undefined)[],
): number | null {
  const positions: Placement[] = [];
  for (const fen of [STARTING_FEN, ...postMoveFens.slice(0, MAX_PROBE_PLY)]) {
    const position = parsePlacement(fen);
    if (position == null) return null;
    positions.push(position);
  }

  const middle = positions.findIndex(isMiddlegame);
  if (middle < 0) return null;

  const end = positions.findIndex((position) => majorsAndMinors(position) <= 6);
  if (end >= 0 && !(middle < end)) return null;
  return middle;
}
