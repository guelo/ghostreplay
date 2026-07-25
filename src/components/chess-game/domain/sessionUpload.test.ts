import { describe, it, expect } from "vitest";
import { Chess } from "chess.js";
import {
  buildDrillAnalysisSnapshot,
  buildSessionMoveUploads,
  fillUnresolvedTerminal,
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

const uploadsFor = (
  sans: string[],
  startingFen: string = STARTING_FEN,
): SessionMoveUpload[] => {
  const chess = new Chess(startingFen);
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

// Mark the final ply unresolved — the analysis-race residual the fill exists to
// patch: null the worker eval so the resolved-guard does not short-circuit.
const withUnresolvedFinal = (
  uploads: SessionMoveUpload[],
): SessionMoveUpload[] => {
  const last = uploads.length - 1;
  uploads[last] = { ...uploads[last], eval_cp: null, eval_mate: null };
  return uploads;
};

// Fields a terminal DRAW fill must produce: played eval 0, no mate, and — unlike
// checkmate — an explicitly-null delta (a draw does not prove the move was best).
const DRAW_FILL = {
  eval_cp: 0,
  eval_mate: null,
  eval_delta: null,
  synthetic_terminal_eval: true,
};

// A repeating knight cycle returns to the start each 4 plies; the 3rd occurrence
// (8 plies) is threefold. Bare-FEN detection would MISS it — the count is not in
// the FEN — but the chain replay reconstructs the history and detects it.
const THREEFOLD_LINE = ["Nf3", "Nf6", "Ng1", "Ng8", "Nf3", "Nf6", "Ng1", "Ng8"];
// Custom starts for the two draws that need a tailored position. Both round-trip
// through chess.js, so uploads[0].fen_before === the expectedStartingFen passed.
const FIFTY_MOVE_START = "8/8/4k3/8/8/4K3/8/1R6 w - - 99 60"; // one quiet move -> clock 100
const INSUFFICIENT_START = "k7/8/8/5n2/8/3B4/8/4K3 w - - 0 1"; // Bxf5 -> K+B vs K

describe("per-row browser provenance", () => {
  const provenance = {
    engine_version: "18",
    engine_build: "a".repeat(64),
    eval_file_id: `nn-9067e33176e8.nnue:${"9".repeat(64)}`,
    search_limit_type: "depth" as const,
    search_limit_value: 20,
    threads: 1,
    hash_mb: 128,
  };

  it("copies each analysis's own provenance onto its upload row", () => {
    // Per-ROW, not per-request: the deferred scheduler coalesces per slot with
    // last-write-wins, so each surviving slot must carry its OWN claim.
    const history = buildHistory();
    const analyses = new Map([
      [0, analysis({ provenance })],
      [1, analysis({ provenance: null })],
    ]);
    const uploads = buildSessionMoveUploads(history, analyses, STARTING_FEN);
    expect(uploads[0].provenance).toEqual(provenance);
    expect(uploads[1].provenance).toBeNull();
  });

  it("uploads no provenance for an unanalyzed move", () => {
    const history = buildHistory();
    const uploads = buildSessionMoveUploads(history, new Map(), STARTING_FEN);
    expect(uploads.every((u) => u.provenance === null)).toBe(true);
  });

  it("re-sends the identical claim, so retries are idempotent", () => {
    const history = buildHistory();
    const analyses = new Map([[0, analysis({ provenance })]]);
    const first = buildSessionMoveUploads(history, analyses, STARTING_FEN);
    const retry = buildSessionMoveUploads(history, analyses, STARTING_FEN);
    expect(retry[0].provenance).toEqual(first[0].provenance);
  });

  it("leaves a synthetic terminal fill provenance-free", () => {
    // A deterministic terminal score was never searched, so it must not carry a
    // search claim (the backend skips these rows entirely).
    const uploads = withUnresolvedFinal(uploadsFor(["f3", "e5", "g4", "Qh4"]));
    const filled = fillUnresolvedTerminal(uploads, STARTING_FEN);
    const last = filled[filled.length - 1];
    expect(last.synthetic_terminal_eval).toBe(true);
    expect(last.provenance ?? null).toBeNull();
  });
});

describe("fillUnresolvedTerminal", () => {
  it("fills only an unresolved final checkmating ply with terminal provenance", () => {
    const uploads = uploadsFor(["f3", "e5", "g4", "Qh4#"]);
    uploads[2] = { ...uploads[2], eval_cp: null, eval_mate: null };
    uploads[3] = { ...uploads[3], eval_cp: null, eval_mate: null, eval_delta: null };

    const result = fillUnresolvedTerminal(uploads, STARTING_FEN);

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
    expect(fillUnresolvedTerminal(uploads, STARTING_FEN)).toBe(uploads);

    const mateOnly = uploads.slice();
    mateOnly[3] = { ...mateOnly[3], eval_cp: null, eval_mate: 1 };
    expect(fillUnresolvedTerminal(mateOnly, STARTING_FEN)).toBe(mateOnly);
  });

  it.each([
    ["empty", () => [] as SessionMoveUpload[]],
    ["nonterminal", () => uploadsFor(["e4", "e5"]).map((u, i, a) => i === a.length - 1 ? { ...u, eval_cp: null } : u)],
    ["truncated mate line", () => uploadsFor(["f3", "e5", "g4"]).map((u, i, a) => i === a.length - 1 ? { ...u, eval_cp: null } : u)],
  ])("leaves %s input unchanged", (_name, makeUploads) => {
    const uploads = makeUploads();
    expect(fillUnresolvedTerminal(uploads, STARTING_FEN)).toBe(uploads);
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
    expect(() => fillUnresolvedTerminal(uploads, STARTING_FEN)).not.toThrow();
    expect(fillUnresolvedTerminal(uploads, STARTING_FEN)).toBe(uploads);
  });

  it("rejects a legal truncated suffix that does not start at the known opening", () => {
    const full = uploadsFor(["f3", "e5", "g4", "Qh4#"]);
    const suffix = [{ ...full[3], eval_cp: null, eval_mate: null }];
    expect(fillUnresolvedTerminal(suffix, STARTING_FEN)).toBe(suffix);
  });

  it("rejects a game that was already terminal before its final row", () => {
    const uploads = uploadsFor([
      "Nf3", "Nf6", "Ng1", "Ng8",
      "Nf3", "Nf6", "Ng1", "Ng8", // threefold repetition here
      "f3", "e5", "g4", "Qh4#",
    ]);
    uploads[uploads.length - 1] = { ...uploads[uploads.length - 1], eval_cp: null };
    expect(fillUnresolvedTerminal(uploads, STARTING_FEN)).toBe(uploads);
  });

  it("does not throw when the exact expected starting FEN is malformed", () => {
    const uploads = uploadsFor(["f3", "e5", "g4", "Qh4#"]);
    uploads[0] = { ...uploads[0], fen_before: "not a fen" };
    uploads[3] = { ...uploads[3], eval_cp: null };
    expect(() => fillUnresolvedTerminal(uploads, "not a fen")).not.toThrow();
    expect(fillUnresolvedTerminal(uploads, "not a fen")).toBe(uploads);
  });

  it.each([
    [
      "stalemate",
      [
        "e3", "a5", "Qh5", "Ra6", "Qxa5", "h5", "Qxc7", "Rah6", "h4", "f6",
        "Qxd7+", "Kf7", "Qxb7", "Qd3", "Qxb8", "Qh7", "Qxc8", "Kg6", "Qe6",
      ],
      STARTING_FEN,
    ],
    ["threefold repetition", THREEFOLD_LINE, STARTING_FEN],
    ["the fifty-move rule", ["Rb5"], FIFTY_MOVE_START],
    ["insufficient material", ["Bxf5"], INSUFFICIENT_START],
  ])(
    "fills an unresolved terminal-draw final ply for %s",
    (_name, sans, start) => {
      const uploads = withUnresolvedFinal(uploadsFor(sans, start));

      const result = fillUnresolvedTerminal(uploads, start);

      expect(result).not.toBe(uploads);
      // Earlier plies are never touched — only the terminal ply is synthesized.
      expect(result.slice(0, -1)).toEqual(uploads.slice(0, -1));
      expect(result[result.length - 1]).toEqual(
        expect.objectContaining(DRAW_FILL),
      );
    },
  );

  it("clears a stale non-null eval_delta when filling a terminal draw", () => {
    const uploads = uploadsFor(THREEFOLD_LINE);
    const last = uploads.length - 1;
    // Unresolved eval, but a STALE non-null delta the builder left behind. The
    // resolved-guard inspects only eval_cp/eval_mate, so this row still fills —
    // and the draw branch must EXPLICITLY null the delta, not leave the 42.
    uploads[last] = {
      ...uploads[last],
      eval_cp: null,
      eval_mate: null,
      eval_delta: 42,
    };

    const result = fillUnresolvedTerminal(uploads, STARTING_FEN);

    expect(result[last].eval_delta).toBeNull();
    expect(result[last]).toEqual(expect.objectContaining(DRAW_FILL));
  });

  it("never overwrites a resolved threefold-drawn final row", () => {
    // A repetition draw whose worker analysis DID resolve carries a real, nonzero
    // search eval (the worker runs a full search — no terminal short-circuit for
    // threefold). The resolved-guard keeps it; the fill does not rewrite it to 0.
    const uploads = uploadsFor(THREEFOLD_LINE);
    const last = uploads.length - 1;
    uploads[last] = { ...uploads[last], eval_cp: 37, eval_mate: null };

    const result = fillUnresolvedTerminal(uploads, STARTING_FEN);

    expect(result).toBe(uploads);
    expect(result[last].eval_cp).toBe(37);
    expect(result[last]).not.toHaveProperty("synthetic_terminal_eval");
  });

  it("distinguishes a threefold repetition from the same placement without the repetition history", () => {
    const cycle = ["Nf3", "Nf6", "Ng1", "Ng8"];
    // A: two full cycles reach the 3rd occurrence -> threefold -> drawn.
    const threefold = withUnresolvedFinal(uploadsFor([...cycle, ...cycle]));
    const drawn = fillUnresolvedTerminal(threefold, STARTING_FEN);
    expect(drawn[drawn.length - 1]).toEqual(expect.objectContaining(DRAW_FILL));

    // B: one cycle reaches the SAME board placement, but only the 2nd occurrence
    // — not yet a draw. Terminality is read from the replayed history, so B is
    // NOT stamped despite the identical final placement.
    const notYet = withUnresolvedFinal(uploadsFor(cycle));
    expect(fillUnresolvedTerminal(notYet, STARTING_FEN)).toBe(notYet);
  });

  it("fails closed when a fifty-move draw's final halfmove clock is altered", () => {
    const uploads = withUnresolvedFinal(uploadsFor(["Rb5"], FIFTY_MOVE_START));
    const last = uploads.length - 1;
    // The fifty-move rule fires at exactly clock 100; a stale row that reports
    // clock 99 for the same placement must not be stamped. The replay produces
    // 100, so the exact fen_after binding rejects the row and the fill no-ops.
    const fields = uploads[last].fen_after.split(" ");
    fields[4] = "99";
    uploads[last] = { ...uploads[last], fen_after: fields.join(" ") };

    expect(fillUnresolvedTerminal(uploads, FIFTY_MOVE_START)).toBe(uploads);
  });

  it("fails closed for a continuation from an already-terminal starting FEN", () => {
    // K+B vs K is insufficient material — already game-over — yet chess.js still
    // allows a bishop shuffle. A one-ply chain from it replays cleanly and stays
    // game-over at the final row, but the move did not REACH the terminal, so it
    // must no-op. The in-loop early-terminal check is skipped for the sole row,
    // so the up-front start-terminal guard is what catches it (g-terminal-startdraw).
    const TERMINAL_START = "4k3/8/8/8/8/8/8/2B1K3 w - - 0 1";
    const uploads = withUnresolvedFinal(uploadsFor(["Bd2"], TERMINAL_START));

    expect(fillUnresolvedTerminal(uploads, TERMINAL_START)).toBe(uploads);
    expect(uploads[uploads.length - 1]).not.toHaveProperty("synthetic_terminal_eval");
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
