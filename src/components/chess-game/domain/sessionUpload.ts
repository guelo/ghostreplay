import { Chess } from "chess.js";
import type { AnalysisResult } from "../../../hooks/useMoveAnalysis";
import type {
  AnalysisMove,
  PositionAnalysis,
  SessionMoveUpload,
} from "../../../utils/api";
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
    eval_mate: analysis?.playedEvalMate ?? null,
    best_move_san: analysis
      ? parseUciToSan(fenBeforeMove, analysis.bestMove)
      : null,
    best_move_eval_cp: analysis?.bestEval ?? null,
    eval_delta: analysis?.delta ?? null,
    classification: analysis?.classification ?? null,
    fen_before: fenBeforeMove,
    move_uci: move.uci,
    best_move_uci: analysis?.bestMove ?? null,
    best_line_uci: analysis?.bestLine ?? null,
    decision_source: move.decisionSource ?? null,
    target_blunder_id: move.targetBlunderId ?? null,
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

/**
 * Transient, in-memory snapshot of a just-played drill for the ephemeral
 * /drill-analysis surface. Never persisted, never uploaded — see g-a406.
 */
export interface DrillAnalysisSnapshot {
  moves: AnalysisMove[];
  positionAnalysis: Record<string, PositionAnalysis>;
  playerColor: "white" | "black";
  initialMoveIndex: number;
  /**
   * Identity of the drill session this snapshot describes. Binds the review and
   * any "return to drill" restoration to a single drill so a stale snapshot can
   * never be paired with a different game store state (g-65ve).
   */
  sourceSessionId: string;
  /** Non-blocking notice (e.g. partial analysis) shown on the review surface. */
  warning?: string | null;
}

/**
 * Build an AnalysisBoard-compatible snapshot from live drill state.
 *
 * Every ply is retained (AnalysisBoard uses the array index as the canonical
 * move index), so plies whose engine analysis is still unresolved keep null
 * eval/best/classification fields rather than being omitted or reordered.
 * `positionAnalysis` is keyed by fen_before and only analyzed plies contribute
 * entries — matching server semantics for SessionAnalysis.position_analysis.
 */
export const buildDrillAnalysisSnapshot = (
  history: MoveRecord[],
  analysesByIndex: Map<number, AnalysisResult>,
  startingFen: string,
  playerColor: "white" | "black",
  failedMoveIndex: number | null,
  sourceSessionId: string,
): DrillAnalysisSnapshot => {
  const moves: AnalysisMove[] = [];
  const positionAnalysis: Record<string, PositionAnalysis> = {};

  history.forEach((_, index) => {
    const upload = buildUploadForIndex(history, analysesByIndex, index, startingFen);
    if (!upload) return;

    moves.push({
      move_number: upload.move_number,
      color: upload.color,
      move_san: upload.move_san,
      fen_after: upload.fen_after,
      eval_cp: upload.eval_cp,
      eval_mate: upload.eval_mate,
      best_move_san: upload.best_move_san,
      best_move_eval_cp: upload.best_move_eval_cp,
      eval_delta: upload.eval_delta,
      classification: upload.classification,
    });

    if (analysesByIndex.has(index) && upload.fen_before && upload.best_move_uci) {
      positionAnalysis[upload.fen_before] = {
        best_move_uci: upload.best_move_uci,
        best_move_san: upload.best_move_san,
        best_move_eval_cp: upload.best_move_eval_cp,
        best_line_uci: upload.best_line_uci ?? null,
      };
    }
  });

  const initialMoveIndex =
    moves.length === 0
      ? 0
      : Math.min(Math.max(failedMoveIndex ?? 0, 0), moves.length - 1);

  return { moves, positionAnalysis, playerColor, initialMoveIndex, sourceSessionId };
};
