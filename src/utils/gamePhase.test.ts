import { describe, expect, it } from "vitest";
import { openingPlyCount, parsePlacement } from "./gamePhase";

const STARTING_FEN =
  "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const MIDDLEGAME_FEN =
  "rnbqkbnr/pppppppp/8/8/8/RNB2BNR/PPPPPPPP/3QK3 w - - 0 1";

describe("parsePlacement", () => {
  it.each([
    null,
    undefined,
    "",
    "8/8/8/8/8/8/8",
    "8/8/8/8/8/8/8/8/8",
    "7/8/8/8/8/8/8/8",
    "8P/8/8/8/8/8/8/8",
    "44/8/8/8/8/8/8/8",
    "7x/8/8/8/8/8/8/8",
    "8/8/8/8/8/8/8/7Kx",
  ])("returns null rather than throwing for malformed input %#", (fen) => {
    expect(() => parsePlacement(fen)).not.toThrow();
    expect(parsePlacement(fen)).toBeNull();
  });

  it("accepts structurally valid placement without requiring legal kings", () => {
    expect(parsePlacement("8/8/8/8/8/8/8/8")).not.toBeNull();
  });
});

describe("openingPlyCount", () => {
  it("fails closed when a malformed or missing FEN occurs in the scanned line", () => {
    expect(openingPlyCount([STARTING_FEN, "bad", MIDDLEGAME_FEN])).toBeNull();
    expect(openingPlyCount([STARTING_FEN, null, MIDDLEGAME_FEN])).toBeNull();
  });

  it("does not scan for a crossing beyond the 80-ply probe cap", () => {
    const fens = Array<string>(200).fill(STARTING_FEN);
    fens[100] = MIDDLEGAME_FEN;
    expect(openingPlyCount(fens)).toBeNull();
  });
});
