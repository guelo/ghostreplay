import { Chess } from "chess.js";
import type { AnalysisResult } from "../../../hooks/useMoveAnalysis";
import type {
  AnalysisMove,
  PositionAnalysis,
  SessionMoveUpload,
} from "../../../utils/api";
import type { MoveRecord } from "./movePresentation";
import { MATE_BASE_CP } from "../../../types/analysis";

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
    // Carried verbatim from the analysis result, so a retry or an end-of-game
    // snapshot re-send uploads the identical claim (provenance is idempotent
    // across attempts). Null whenever the result is not honest raw worker output.
    provenance: analysis?.provenance ?? null,
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
 * Fill the analysis race's one safe residual: an unresolved final move whose
 * position is provably terminal — checkmate or any draw (stalemate, threefold
 * repetition, fifty-move, insufficient material). The whole uploaded game is
 * replayed and bound to its FEN chain from the known opening before anything is
 * changed, so terminality is read from the RECONSTRUCTED history rather than
 * the bare final FEN: threefold repetition is undetectable from a lone FEN (the
 * repetition count is not encoded), and the fifty-move clock is only carried
 * because the replay recomputes it. That same replay is the completeness guard
 * — a truncated suffix, an extended history that ended earlier, a skipped ply,
 * or a mismatched FEN all fail closed.
 *
 * Fill values (mover-relative, aligned with the worker's stored convention):
 *   - checkmate -> eval_cp=+MATE_BASE_CP, eval_mate=0, eval_delta=0 (a mating
 *     move is provably unbeatable, so delta 0 is exact);
 *   - terminal draw -> eval_cp=0, eval_mate=null, eval_delta=null. The played
 *     position is fixed at 0, but a draw does NOT prove the move was best
 *     (repetition / stalemate / fifty-move can squander a win), so the delta is
 *     unknown. It is EXPLICITLY nulled — eval_delta is independently nullable
 *     and the resolved-guard only inspects eval_cp/eval_mate, so an unresolved
 *     row can still carry a stale non-null delta that must not survive.
 *
 * Strictly an unresolved-eval fill: a resolved worker analysis (eval_cp or
 * eval_mate non-null on the final row) always wins and is never overridden —
 * including a resolved threefold draw whose worker search produced a nonzero
 * eval. Invalid, partial, extended, or nonterminal input is returned untouched
 * and never throws.
 */
export const fillUnresolvedTerminal = (
  uploads: SessionMoveUpload[],
  expectedStartingFen: string,
): SessionMoveUpload[] => {
  if (uploads.length === 0) return uploads;

  const final = uploads[uploads.length - 1];
  if (final.eval_cp !== null || final.eval_mate !== null) {
    return uploads;
  }
  if (uploads[0].fen_before !== expectedStartingFen) return uploads;

  try {
    const replay = new Chess(expectedStartingFen);
    // The opening itself must not already be terminal. chess.js still permits
    // legal moves from some drawn positions (e.g. a bishop shuffle in K+B vs K),
    // so a one-ply chain from an already-drawn start would replay cleanly and
    // stay game-over at the final row — but the move did NOT reach the terminal,
    // the position was drawn before it. The in-loop early-terminal check only
    // fires AFTER a move and is skipped for a sole (index 0 == last) row, so this
    // must be caught up front. Extends completeness guard (c) — "not terminal
    // before the final row" — back to before the FIRST row (g-terminal-startdraw).
    if (replay.isGameOver()) return uploads;

    for (let index = 0; index < uploads.length; index += 1) {
      const upload = uploads[index];
      if (
        upload.move_number !== Math.floor(index / 2) + 1 ||
        upload.color !== (index % 2 === 0 ? "white" : "black") ||
        upload.fen_before !== replay.fen()
      ) {
        return uploads;
      }

      const move = replay.move(upload.move_san);
      if (!move || move.san !== upload.move_san || replay.fen() !== upload.fen_after) {
        return uploads;
      }
      if (index < uploads.length - 1 && replay.isGameOver()) return uploads;
    }

    if (!replay.isGameOver()) return uploads;

    const filled = uploads.slice();
    filled[filled.length - 1] = replay.isCheckmate()
      ? {
          ...final,
          eval_cp: MATE_BASE_CP,
          eval_mate: 0,
          eval_delta: 0,
          synthetic_terminal_eval: true,
        }
      : {
          ...final,
          eval_cp: 0,
          eval_mate: null,
          eval_delta: null,
          synthetic_terminal_eval: true,
        };
    return filled;
  } catch {
    return uploads;
  }
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
      // Exact evidence keys carried through for display-helper parity with backend
      // responses; the ephemeral drill board passes no sessionId, so the evidence
      // driver never runs here (g-cache-stronger-evals).
      fen_before: upload.fen_before,
      move_uci: upload.move_uci,
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
        // Local worker results are not a backend trusted-position winner, so the
        // honest flag is false. Whether AnalysisBoard re-searches on an untrusted
        // seed is g-54h5's call; this phase only delivers the flag.
        position_trusted: false,
      };
    }
  });

  // Start the review one ply BEFORE the player's last bad move (g-eflo), so the
  // board shows the position they had to choose from rather than the mistake
  // already played. Index -1 is AnalysisBoard's "starting position" sentinel, so
  // a first-ply failure (failedMoveIndex === 0) correctly opens at the start.
  // A null failedMoveIndex is a defensive fallback to the last move.
  const initialMoveIndex =
    moves.length === 0
      ? 0
      : failedMoveIndex === null
        ? moves.length - 1
        : Math.min(Math.max(failedMoveIndex - 1, -1), moves.length - 1);

  return { moves, positionAnalysis, playerColor, initialMoveIndex, sourceSessionId };
};
