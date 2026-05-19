import { Chess } from "chess.js";

/**
 * Normalize a FEN string for position comparison by stripping the halfmove
 * clock and fullmove number (fields 5-6), keeping only piece placement,
 * active color, castling rights, and legal en passant square (fields 1-4).
 *
 * Matches the backend `normalize_fen` logic in app/fen.py.
 */
export function normalize_fen(fen: string): string {
  try {
    return new Chess(fen).fen().split(" ").slice(0, 4).join(" ");
  } catch {
    return fen.split(" ").slice(0, 4).join(" ");
  }
}
