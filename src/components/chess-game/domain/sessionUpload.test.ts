import { describe, it, expect } from "vitest";
import { Chess } from "chess.js";
import { buildDrillAnalysisSnapshot } from "./sessionUpload";
import type { MoveRecord } from "./movePresentation";
import type { AnalysisResult } from "../../../hooks/useMoveAnalysis";
import { STARTING_FEN } from "../config";

// Build a short legal move history: 1.e4 e5 2.Nf3
const buildHistory = (): MoveRecord[] => {
  const chess = new Chess();
  const sans = ["e4", "e5", "Nf3"];
  return sans.map((san) => {
    const move = chess.move(san);
    return { san: move.san, fen: chess.fen(), uci: move.from + move.to };
  });
};

const analysis = (overrides: Partial<AnalysisResult> = {}): AnalysisResult => ({
  id: "a",
  move: "e2e4",
  bestMove: "d2d4",
  bestLine: ["d2d4", "d7d5"],
  bestEval: 30,
  playedEval: 20,
  currentPositionEval: 20,
  playedEvalMate: null,
  currentPositionEvalMate: null,
  moveIndex: 0,
  delta: 10,
  classification: "good",
  blunder: false,
  recordable: false,
  ...overrides,
});

describe("buildDrillAnalysisSnapshot", () => {
  it("retains every ply, including plies with no analysis (null fields)", () => {
    const history = buildHistory();
    // Only index 0 has analysis; indices 1 and 2 are unresolved.
    const analyses = new Map<number, AnalysisResult>([[0, analysis()]]);

    const snap = buildDrillAnalysisSnapshot(
      history,
      analyses,
      STARTING_FEN,
      "white",
      2,
    );

    expect(snap.moves).toHaveLength(3);
    expect(snap.moves[0].move_san).toBe("e4");
    expect(snap.moves[0].eval_cp).toBe(20);
    expect(snap.moves[0].classification).toBe("good");
    // Unanalyzed plies keep san/fen but null analysis fields.
    expect(snap.moves[1].move_san).toBe("e5");
    expect(snap.moves[1].eval_cp).toBeNull();
    expect(snap.moves[1].classification).toBeNull();
    expect(snap.moves[2].eval_cp).toBeNull();
  });

  it("keys position_analysis by fen_before and omits unanalyzed positions", () => {
    const history = buildHistory();
    const analyses = new Map<number, AnalysisResult>([[0, analysis()]]);

    const snap = buildDrillAnalysisSnapshot(
      history,
      analyses,
      STARTING_FEN,
      "white",
      0,
    );

    // Index 0's fen_before is the starting position.
    expect(Object.keys(snap.positionAnalysis)).toEqual([STARTING_FEN]);
    const entry = snap.positionAnalysis[STARTING_FEN];
    expect(entry.best_move_uci).toBe("d2d4");
    expect(entry.best_move_san).toBe("d4");
    expect(entry.best_line_uci).toEqual(["d2d4", "d7d5"]);
  });

  it("clamps initialMoveIndex into range", () => {
    const history = buildHistory();
    const analyses = new Map<number, AnalysisResult>();

    expect(
      buildDrillAnalysisSnapshot(history, analyses, STARTING_FEN, "white", 99)
        .initialMoveIndex,
    ).toBe(2);
    expect(
      buildDrillAnalysisSnapshot(history, analyses, STARTING_FEN, "white", null)
        .initialMoveIndex,
    ).toBe(0);
  });

  it("returns empty moves and initialMoveIndex 0 for empty history", () => {
    const snap = buildDrillAnalysisSnapshot(
      [],
      new Map(),
      STARTING_FEN,
      "black",
      null,
    );
    expect(snap.moves).toHaveLength(0);
    expect(snap.initialMoveIndex).toBe(0);
    expect(snap.playerColor).toBe("black");
  });
});
