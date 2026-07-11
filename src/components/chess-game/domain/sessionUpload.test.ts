import { describe, it, expect } from "vitest";
import { Chess } from "chess.js";
import {
  buildDrillAnalysisSnapshot,
  fillUnresolvedTerminalMate,
} from "./sessionUpload";
import type { MoveRecord } from "./movePresentation";
import type { AnalysisResult } from "../../../hooks/useMoveAnalysis";
import { STARTING_FEN } from "../config";
import type { SessionMoveUpload } from "../../../utils/api";

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

const uploadsFor = (sans: string[]): SessionMoveUpload[] => {
  const chess = new Chess(STARTING_FEN);
  return sans.map((san, index) => {
    const fenBefore = chess.fen();
    const move = chess.move(san);
    return {
      move_number: Math.floor(index / 2) + 1,
      color: index % 2 === 0 ? "white" : "black",
      move_san: move.san,
      fen_before: fenBefore,
      fen_after: chess.fen(),
      move_uci: move.from + move.to + (move.promotion ?? ""),
      eval_cp: 12,
      eval_mate: null,
      best_move_san: null,
      best_move_eval_cp: null,
      eval_delta: 1,
      classification: "good",
      best_move_uci: null,
      best_line_uci: null,
      decision_source: null,
      target_blunder_id: null,
    };
  });
};

describe("fillUnresolvedTerminalMate", () => {
  it("fills only an unresolved final checkmating ply with terminal provenance", () => {
    const uploads = uploadsFor(["f3", "e5", "g4", "Qh4#"]);
    uploads[2] = { ...uploads[2], eval_cp: null, eval_mate: null };
    uploads[3] = { ...uploads[3], eval_cp: null, eval_mate: null, eval_delta: null };

    const result = fillUnresolvedTerminalMate(uploads, STARTING_FEN);

    expect(result).not.toBe(uploads);
    expect(result[2]).toBe(uploads[2]);
    expect(result[2].eval_cp).toBeNull();
    expect(result[3]).toEqual(expect.objectContaining({
      eval_cp: 10000,
      eval_mate: 0,
      eval_delta: 0,
      synthetic_terminal_eval: true,
    }));
  });

  it("never overwrites a resolved final row", () => {
    const uploads = uploadsFor(["f3", "e5", "g4", "Qh4#"]);
    expect(fillUnresolvedTerminalMate(uploads, STARTING_FEN)).toBe(uploads);

    const mateOnly = uploads.slice();
    mateOnly[3] = { ...mateOnly[3], eval_cp: null, eval_mate: 1 };
    expect(fillUnresolvedTerminalMate(mateOnly, STARTING_FEN)).toBe(mateOnly);
  });

  it.each([
    ["empty", () => [] as SessionMoveUpload[]],
    ["nonterminal", () => uploadsFor(["e4", "e5"]).map((u, i, a) => i === a.length - 1 ? { ...u, eval_cp: null } : u)],
    ["truncated mate line", () => uploadsFor(["f3", "e5", "g4"]).map((u, i, a) => i === a.length - 1 ? { ...u, eval_cp: null } : u)],
  ])("leaves %s input unchanged", (_name, makeUploads) => {
    const uploads = makeUploads();
    expect(fillUnresolvedTerminalMate(uploads, STARTING_FEN)).toBe(uploads);
  });

  it.each([
    ["wrong starting FEN", (u: SessionMoveUpload[]) => { u[0] = { ...u[0], fen_before: u[1].fen_before }; }],
    ["mismatched after FEN", (u: SessionMoveUpload[]) => { u[1] = { ...u[1], fen_after: u[0].fen_after }; }],
    ["illegal SAN", (u: SessionMoveUpload[]) => { u[2] = { ...u[2], move_san: "Qh9" }; }],
    ["missing interior ply", (u: SessionMoveUpload[]) => { u.splice(2, 1); }],
    ["different final halfmove clock", (u: SessionMoveUpload[]) => {
      const fields = u[u.length - 1].fen_after.split(" ");
      fields[4] = String(Number(fields[4]) + 1);
      u[u.length - 1] = { ...u[u.length - 1], fen_after: fields.join(" ") };
    }],
  ])("fails closed for %s", (_name, corrupt) => {
    const uploads = uploadsFor(["f3", "e5", "g4", "Qh4#"]);
    uploads[uploads.length - 1] = { ...uploads[uploads.length - 1], eval_cp: null };
    corrupt(uploads);
    expect(() => fillUnresolvedTerminalMate(uploads, STARTING_FEN)).not.toThrow();
    expect(fillUnresolvedTerminalMate(uploads, STARTING_FEN)).toBe(uploads);
  });

  it("rejects a legal truncated suffix that does not start at the known opening", () => {
    const full = uploadsFor(["f3", "e5", "g4", "Qh4#"]);
    const suffix = [{ ...full[3], eval_cp: null, eval_mate: null }];
    expect(fillUnresolvedTerminalMate(suffix, STARTING_FEN)).toBe(suffix);
  });

  it("rejects a game that was already terminal before its final row", () => {
    const uploads = uploadsFor([
      "Nf3", "Nf6", "Ng1", "Ng8",
      "Nf3", "Nf6", "Ng1", "Ng8", // threefold repetition here
      "f3", "e5", "g4", "Qh4#",
    ]);
    uploads[uploads.length - 1] = { ...uploads[uploads.length - 1], eval_cp: null };
    expect(fillUnresolvedTerminalMate(uploads, STARTING_FEN)).toBe(uploads);
  });

  it("does not throw when the exact expected starting FEN is malformed", () => {
    const uploads = uploadsFor(["f3", "e5", "g4", "Qh4#"]);
    uploads[0] = { ...uploads[0], fen_before: "not a fen" };
    uploads[3] = { ...uploads[3], eval_cp: null };
    expect(() => fillUnresolvedTerminalMate(uploads, "not a fen")).not.toThrow();
    expect(fillUnresolvedTerminalMate(uploads, "not a fen")).toBe(uploads);
  });
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
      "sess-1",
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
      "sess-1",
    );

    // Index 0's fen_before is the starting position.
    expect(Object.keys(snap.positionAnalysis)).toEqual([STARTING_FEN]);
    const entry = snap.positionAnalysis[STARTING_FEN];
    expect(entry.best_move_uci).toBe("d2d4");
    expect(entry.best_move_san).toBe("d4");
    expect(entry.best_line_uci).toEqual(["d2d4", "d7d5"]);
    // Locally-built seeds are not a backend trusted-position winner (g-54h5).
    expect(entry.position_trusted).toBe(false);
  });

  it("starts one ply before the bad move, clamped into range (g-eflo)", () => {
    const history = buildHistory(); // 3 plies, so last index is 2
    const analyses = new Map<number, AnalysisResult>();

    // Out-of-range failedMoveIndex still clamps to the last move.
    expect(
      buildDrillAnalysisSnapshot(history, analyses, STARTING_FEN, "white", 99, "sess-1")
        .initialMoveIndex,
    ).toBe(2);
    // null failedMoveIndex is a defensive fallback to the last move.
    expect(
      buildDrillAnalysisSnapshot(history, analyses, STARTING_FEN, "white", null, "sess-1")
        .initialMoveIndex,
    ).toBe(2);
    // Bad move at index 2 -> open at index 1 (the position moved from).
    expect(
      buildDrillAnalysisSnapshot(history, analyses, STARTING_FEN, "white", 2, "sess-1")
        .initialMoveIndex,
    ).toBe(1);
    // Bad move is the first ply -> -1 == starting position sentinel.
    expect(
      buildDrillAnalysisSnapshot(history, analyses, STARTING_FEN, "white", 0, "sess-1")
        .initialMoveIndex,
    ).toBe(-1);
  });

  it("returns empty moves and initialMoveIndex 0 for empty history", () => {
    const snap = buildDrillAnalysisSnapshot(
      [],
      new Map(),
      STARTING_FEN,
      "black",
      null,
      "sess-1",
    );
    expect(snap.moves).toHaveLength(0);
    expect(snap.initialMoveIndex).toBe(0);
    expect(snap.playerColor).toBe("black");
  });
});
