/**
 * Normalize a FEN string for position comparison by stripping the halfmove
 * clock and fullmove number (fields 5-6), keeping only piece placement,
 * active color, castling rights, and en passant square (fields 1-4).
 *
 * Matches the backend `normalize_fen` logic in app/fen.py.
 */
export function normalize_fen(fen: string): string {
  return fen.split(" ").slice(0, 4).join(" ");
}
