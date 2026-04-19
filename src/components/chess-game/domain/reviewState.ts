import { Chess } from "chess.js";
import { normalize_fen } from "../../../utils/fen";

type PlayerColor = "white" | "black";

const normalizeReviewFen = (fen: string): string => {
  try {
    // Canonicalize backend/raw FEN before comparing so equivalent positions
    // still match when en-passant is omitted by chess.js for unreachable files.
    return normalize_fen(new Chess(fen).fen());
  } catch {
    return normalize_fen(fen);
  }
};

export const hasReviewTargetAtFen = (
  blunderReviewId: number | null,
  blunderTargetFen: string | null,
  currentFen: string,
): blunderReviewId is number =>
  blunderReviewId !== null &&
  blunderTargetFen !== null &&
  normalizeReviewFen(blunderTargetFen) === normalizeReviewFen(currentFen);

export const canArmReviewTarget = (
  targetBlunderId: number | null,
  targetFen: string | null,
  sideToMove: PlayerColor,
  playerColor: PlayerColor,
): targetBlunderId is number =>
  targetBlunderId !== null &&
  targetFen !== null &&
  sideToMove === playerColor;
