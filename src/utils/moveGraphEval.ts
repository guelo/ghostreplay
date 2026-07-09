import { Chess } from "chess.js";
import { moverMateToWhiteCp, toWhitePerspective } from "../workers/analysisUtils";

/** Guarded checkmate predicate — false (never throws) on malformed/empty FEN. */
export const isCheckmateFen = (fen: string | null | undefined): boolean => {
  if (!fen) return false;
  try {
    return new Chess(fen).isCheckmate();
  } catch {
    return false;
  }
};

/**
 * White-perspective cp for one plotted move, or null when it has no usable eval.
 * Prefers the CP channel, then a correctly-signed mate cp. When BOTH channels are
 * null, synthesizes a terminal-checkmate point from `fenAfter` (peg to the mating
 * side by ply parity) so an unevaluated final mate never renders on the equal line.
 * Pass `fenAfter` ONLY for the terminal ply — checkmate can only be the last move,
 * and this avoids constructing a Chess for every pending ply during live play.
 */
export const whiteCpForMove = (
  evalCp: number | null,
  evalMate: number | null,
  moveIndex: number,
  fenAfter: string | null,
): number | null => {
  if (evalCp != null) return toWhitePerspective(evalCp, moveIndex);
  const mateCp = moverMateToWhiteCp(evalMate, moveIndex);
  if (mateCp != null) return mateCp; // handles eval_mate === 0 (mate-only shape)
  if (isCheckmateFen(fenAfter)) return moverMateToWhiteCp(0, moveIndex);
  return null;
};
