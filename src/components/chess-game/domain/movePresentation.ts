import type React from "react";
import { Chess } from "chess.js";
import type { AnalysisResult } from "../../../hooks/useMoveAnalysis";
import { toWhitePerspective } from "../../../workers/analysisUtils";
import { STARTING_FEN } from "../config";

export type MoveRecord = {
  san: string;
  fen: string; // Position after this move
  uci: string;
  decisionSource?: "ghost_path" | "backend_engine" | "local_fallback";
  targetBlunderId?: number | null;
};

export type BlunderAlert = {
  moveSan: string;
  moveUci: string;
  bestMoveUci: string;
  bestMoveSan: string;
  delta: number;
  moveIndex: number;
  sourceFen: string;
  shouldRewind: boolean;
};

export type ReviewFailInfo = {
  userMoveSan: string;
  bestMoveSan: string;
  userMoveUci: string;
  bestMoveUci: string;
  evalLoss: number;
  moveIndex: number;
};

export type MoveArrow = {
  startSquare: string;
  endSquare: string;
  color: string;
};

export const fenBeforeMove = (
  moveHistory: MoveRecord[],
  moveIndex: number,
): string => {
  if (moveIndex <= 0) {
    return STARTING_FEN;
  }
  return moveHistory[moveIndex - 1]?.fen ?? STARTING_FEN;
};

export const sanForUciMove = (sourceFen: string, moveUci: string): string => {
  try {
    const tempChess = new Chess(sourceFen);
    const from = moveUci.slice(0, 2);
    const to = moveUci.slice(2, 4);
    const promotion = moveUci.slice(4) || undefined;
    const appliedMove = tempChess.move({ from, to, promotion });
    return appliedMove?.san ?? moveUci;
  } catch {
    return moveUci;
  }
};

export const buildBlunderAlert = ({
  moveHistory,
  moveIndex,
  moveSan,
  moveUci,
  bestMoveUci,
  delta,
  shouldRewind = false,
}: {
  moveHistory: MoveRecord[];
  moveIndex: number;
  moveSan: string;
  moveUci: string;
  bestMoveUci: string;
  delta: number;
  shouldRewind?: boolean;
}): BlunderAlert => {
  const sourceFen = fenBeforeMove(moveHistory, moveIndex);
  return {
    moveSan,
    moveUci,
    bestMoveUci,
    bestMoveSan: sanForUciMove(sourceFen, bestMoveUci),
    delta,
    moveIndex,
    sourceFen,
    shouldRewind,
  };
};

export const deriveLastMoveSquares = (
  moveHistory: MoveRecord[],
  viewIndex: number | null,
): Record<string, React.CSSProperties> => {
  const idx = viewIndex === null ? moveHistory.length - 1 : viewIndex;
  if (idx < 0 || idx >= moveHistory.length) return {};

  const uci = moveHistory[idx].uci;
  const from = uci.slice(0, 2);
  const to = uci.slice(2, 4);
  const style: React.CSSProperties = { background: "rgba(255, 255, 0, 0.4)" };
  return { [from]: style, [to]: style };
};

export const deriveAnnotatedMoves = (
  moveHistory: MoveRecord[],
  analysisMap: Map<number, AnalysisResult>,
) => {
  return moveHistory.map((m, i) => {
    const analysis = analysisMap.get(i);
    return {
      san: m.san,
      classification: analysis?.classification ?? undefined,
      eval:
        analysis?.playedEval != null
          ? toWhitePerspective(analysis.playedEval, i)
          : undefined,
    };
  });
};

export const deriveBlunderArrows = (
  reviewFailModal: ReviewFailInfo | null,
  blunderAlert: BlunderAlert | null,
): MoveArrow[] => {
  if (reviewFailModal) {
    return [
      {
        startSquare: reviewFailModal.userMoveUci.slice(0, 2),
        endSquare: reviewFailModal.userMoveUci.slice(2, 4),
        color: "rgba(248, 113, 113, 0.8)",
      },
      {
        startSquare: reviewFailModal.bestMoveUci.slice(0, 2),
        endSquare: reviewFailModal.bestMoveUci.slice(2, 4),
        color: "rgba(52, 211, 153, 0.8)",
      },
    ];
  }

  if (!blunderAlert) return [];

  return [
    {
      startSquare: blunderAlert.moveUci.slice(0, 2),
      endSquare: blunderAlert.moveUci.slice(2, 4),
      color: "rgba(248, 113, 113, 0.8)",
    },
    {
      startSquare: blunderAlert.bestMoveUci.slice(0, 2),
      endSquare: blunderAlert.bestMoveUci.slice(2, 4),
      color: "rgba(52, 211, 153, 0.8)",
    },
  ];
};
