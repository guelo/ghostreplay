import { Chess } from "chess.js";
import type { AnalysisResult } from "../../../hooks/useMoveAnalysis";
import type { SessionMoveUpload } from "../../../utils/api";
import type { MoveRecord } from "./movePresentation";

export const parseUciToSan = (
  fenBeforeMove: string,
  uciMove: string,
): string | null => {
  if (!uciMove || uciMove === "(none)" || uciMove.length < 4) {
    return null;
  }

  try {
    const replay = new Chess(fenBeforeMove);
    const from = uciMove.slice(0, 2);
    const to = uciMove.slice(2, 4);
    const promotion = uciMove.slice(4) || undefined;
    const result = replay.move({ from, to, promotion });
    return result?.san ?? null;
  } catch {
    return null;
  }
};

const buildUploadForIndex = (
  history: MoveRecord[],
  analysesByIndex: Map<number, AnalysisResult>,
  index: number,
  startingFen: string,
): SessionMoveUpload | null => {
  const move = history[index];
  if (!move) return null;

  const analysis = analysesByIndex.get(index);
  const fenBeforeMove =
    index === 0
      ? startingFen
      : (history[index - 1]?.fen ?? startingFen);

  return {
    move_number: Math.floor(index / 2) + 1,
    color: index % 2 === 0 ? "white" : "black",
    move_san: move.san,
    fen_after: move.fen,
    eval_cp: analysis?.playedEval ?? null,
    eval_mate: null,
    best_move_san: analysis
      ? parseUciToSan(fenBeforeMove, analysis.bestMove)
      : null,
    best_move_eval_cp: analysis?.bestEval ?? null,
    eval_delta: analysis?.delta ?? null,
    classification: analysis?.classification ?? null,
    fen_before: fenBeforeMove,
    move_uci: move.uci,
    best_move_uci: analysis?.bestMove ?? null,
  };
};

export const buildSessionMoveUploads = (
  history: MoveRecord[],
  analysesByIndex: Map<number, AnalysisResult>,
  startingFen: string,
): SessionMoveUpload[] => {
  return history.map((_, index) =>
    buildUploadForIndex(history, analysesByIndex, index, startingFen)!,
  );
};

/**
 * Build upload payloads for specific move indices only.
 * Used by incremental uploads to avoid rebuilding the full move list.
 */
export const buildSessionMoveUploadsForIndices = (
  history: MoveRecord[],
  analysesByIndex: Map<number, AnalysisResult>,
  indices: number[],
  startingFen: string,
): SessionMoveUpload[] => {
  const results: SessionMoveUpload[] = [];
  for (const index of indices) {
    const upload = buildUploadForIndex(history, analysesByIndex, index, startingFen);
    if (upload) results.push(upload);
  }
  return results;
};
