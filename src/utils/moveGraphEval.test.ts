import { describe, expect, it } from "vitest";
import { isCheckmateFen, whiteCpForMove } from "./moveGraphEval";

// Terminal checkmate FENs (post-move positions):
//  - Scholar's mate: 4.Qxf7# is ply index 6 (even → white mates). Black to move, mated.
const SCHOLARS_MATE_FEN =
  "r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4";
//  - Fool's mate: 2...Qh4# is ply index 3 (odd → black mates). White to move, mated.
const FOOLS_MATE_FEN =
  "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3";
const NORMAL_FEN =
  "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1";

describe("isCheckmateFen", () => {
  it("returns true for a checkmate FEN", () => {
    expect(isCheckmateFen(SCHOLARS_MATE_FEN)).toBe(true);
    expect(isCheckmateFen(FOOLS_MATE_FEN)).toBe(true);
  });

  it("returns false for a normal (non-mate) FEN", () => {
    expect(isCheckmateFen(NORMAL_FEN)).toBe(false);
  });

  it("returns false (no throw) for malformed, empty, or missing FEN", () => {
    expect(isCheckmateFen("not a fen")).toBe(false);
    expect(isCheckmateFen("")).toBe(false);
    expect(isCheckmateFen(null)).toBe(false);
    expect(isCheckmateFen(undefined)).toBe(false);
  });
});

describe("whiteCpForMove", () => {
  // ── Shape 1: evaluated checkmate (eval_cp present) ──
  it("prefers the CP channel, converted to white perspective by ply parity", () => {
    // White delivered mate at even ply → +10000; the fen_after argument is ignored.
    expect(whiteCpForMove(10000, 0, 6, SCHOLARS_MATE_FEN)).toBe(10000);
    // Black delivered mate at odd ply → mover +10000 flips to −10000 for white.
    expect(whiteCpForMove(10000, 0, 3, FOOLS_MATE_FEN)).toBe(-10000);
  });

  // ── Shape 2: mate-only (eval_cp null, eval_mate === 0) ──
  it("falls back to a correctly-signed mate cp when only eval_mate is present", () => {
    expect(whiteCpForMove(null, 0, 6, null)).toBe(10000); // even ply → white
    expect(whiteCpForMove(null, 0, 3, null)).toBe(-10000); // odd ply → black
  });

  // ── Shape 3: truly unevaluated (both channels null) + checkmate FEN ──
  it("synthesizes a terminal checkmate from fenAfter when both eval channels are null", () => {
    // Scholar's mate, even ply → white mates → +10000.
    expect(whiteCpForMove(null, null, 6, SCHOLARS_MATE_FEN)).toBe(10000);
    // Fool's mate, odd ply → black mates → −10000.
    expect(whiteCpForMove(null, null, 3, FOOLS_MATE_FEN)).toBe(-10000);
  });

  it("returns null when both eval channels are null and the FEN is not checkmate", () => {
    expect(whiteCpForMove(null, null, 6, NORMAL_FEN)).toBeNull();
    expect(whiteCpForMove(null, null, 6, null)).toBeNull();
    expect(whiteCpForMove(null, null, 3, "malformed")).toBeNull();
  });

  it("does not synthesize when fenAfter is null even at a checkmate-looking ply", () => {
    // No FEN passed (non-terminal ply) → no synthesis, stays null.
    expect(whiteCpForMove(null, null, 6, null)).toBeNull();
  });
});
