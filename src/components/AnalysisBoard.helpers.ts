import { Chess } from "chess.js";
import type { AnalysisMove } from "../utils/api";
import { mateToCp } from "../workers/analysisUtils";
import type { MoveClassification } from "../workers/analysisUtils";
import type { EngineInfo } from "../workers/stockfishMessages";
import { CLASSIFICATION_ICON } from "./MoveRow.helpers";

type MoveSquares = { from: string; to: string };

export type MainLineMoveDetails = {
  fenBefore: string;
  playedSquares: MoveSquares | null;
  bestSquares: MoveSquares | null;
};

/** Convert a SAN move to its start/end squares using the supplied position. */
export const sanToSquares = (
  fen: string,
  san: string,
): MoveSquares | null => {
  try {
    const tempChess = new Chess(fen);
    const result = tempChess.move(san);
    if (!result) return null;
    return { from: result.from, to: result.to };
  } catch {
    return null;
  }
};

export const buildMainLineMoveDetails = (
  moves: AnalysisMove[],
  startingFen: string,
): MainLineMoveDetails[] => {
  return moves.map((move, index) => {
    // Prefer the exact wire `fen_before`; reconstruct only for legacy sessions
    // whose wire field is null.
    const fenBefore =
      move.fen_before ??
      (index === 0 ? startingFen : moves[index - 1]?.fen_after ?? startingFen);
    const playedSquares = sanToSquares(fenBefore, move.move_san);
    const bestSquares =
      move.best_move_san && move.best_move_san !== move.move_san
        ? sanToSquares(fenBefore, move.best_move_san)
        : null;

    return {
      fenBefore,
      playedSquares,
      bestSquares,
    };
  });
};

const uciToSquares = (uci: string) => ({
  startSquare: uci.slice(0, 2),
  endSquare: uci.slice(2, 4),
});

const BEST_MOVE_ARROW_COLOR = "rgba(59, 130, 246, 1.00)";

/**
 * Blue arrow for the 2nd/3rd lines, opacity fading as centipawn loss grows.
 * Caps at 0.8 (the best move is a separate, solid 1.0) so alternatives stay
 * visibly distinct from the best move even when their evals are nearly equal.
 */
export const engineArrowColor = (cpLoss: number): string => {
  const clamped = Math.max(0, cpLoss);
  const opacity = Math.max(0.05, Math.min(0.75, 0.75 - clamped / 100));
  return `rgba(59, 130, 246, ${opacity.toFixed(2)})`;
};

const DEFAULT_BLUE_ARROW = "rgba(59, 130, 246, 0.45)";

type MoveArrow = { startSquare: string; endSquare: string; color: string };

/** Convert an EngineScore to a single number (side-to-move relative). */
export const scoreToNum = (s: EngineInfo["score"]): number | null => {
  if (!s) return null;
  return s.type === "cp" ? s.value : mateToCp(s.value);
};

/** Pure function: build engine line arrows with strength-based styling. */
export function buildEngineArrows(
  lines: EngineInfo[],
): MoveArrow[] {
  if (lines.length === 0) return [];
  const scores = lines.map((l) => scoreToNum(l?.score));
  const bestScore = scores.find((s) => s !== null) ?? null;

  const seen = new Set<string>();
  const result: MoveArrow[] = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line?.pv?.[0]) continue;
    const squares = uciToSquares(line.pv[0]);
    const key = `${squares.startSquare}-${squares.endSquare}`;
    if (seen.has(key)) continue;
    seen.add(key);

    let color: string;
    if (i === 0) {
      color = BEST_MOVE_ARROW_COLOR;
    } else if (bestScore !== null && scores[i] !== null) {
      color = engineArrowColor(bestScore - scores[i]!);
    } else {
      color = DEFAULT_BLUE_ARROW;
    }

    result.push({ ...squares, color });
  }
  return result;
}

export type BoardEvalIcon = {
  icon: string;
  title: string;
  classification: MoveClassification;
  left: string;
  top: string;
};

/**
 * Compute the on-board eval-icon badge for the current move's destination
 * square. Pure (no DOM): positions are expressed as percentages of the board
 * frame, where each square is 12.5%. Returns null when no badge should show
 * ("good"/null classification, missing icon, or invalid square).
 */
export const computeBoardEvalIcon = ({
  square,
  classification,
  boardOrientation,
}: {
  square: string | null;
  classification: MoveClassification | null | undefined;
  boardOrientation: "white" | "black";
}): BoardEvalIcon | null => {
  if (!square || classification == null || classification === "good") {
    return null;
  }
  const iconData = CLASSIFICATION_ICON[classification];
  if (!iconData) return null;

  const file = square.charCodeAt(0) - 97; // a=0 … h=7
  const rank = parseInt(square[1] ?? "", 10); // 1 … 8
  if (file < 0 || file > 7 || !(rank >= 1 && rank <= 8)) return null;

  let squareLeft: number;
  let squareTop: number;
  let isRightEdge: boolean;
  if (boardOrientation === "white") {
    squareLeft = file * 12.5;
    squareTop = (8 - rank) * 12.5;
    isRightEdge = file === 7;
  } else {
    squareLeft = (7 - file) * 12.5;
    squareTop = (rank - 1) * 12.5;
    isRightEdge = file === 0;
  }

  // Badge diameter is 5% of the board (≈40px at a 100px square). The center
  // sits ~1% (≈8px) inward from the square's top-right corner so the badge
  // protrudes "somewhat outside". On the visual right edge, mirror to the
  // top-left corner. Clamp the center Y to the radius so the top row isn't
  // clipped at the board's top.
  const centerX = isRightEdge ? squareLeft + 1 : squareLeft + 11.5;
  const centerY = Math.max(squareTop + 1, 2.5);

  return {
    icon: iconData.icon,
    title: iconData.title,
    classification,
    left: `${centerX}%`,
    top: `${centerY}%`,
  };
};
