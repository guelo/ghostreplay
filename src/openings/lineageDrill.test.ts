import { Chess } from "chess.js";
import { describe, expect, it } from "vitest";
import type { OpeningLineageItem } from "../utils/api";
import { normalize_fen } from "../utils/fen";
import { lineageDrillRoute, lineageDrillSelection } from "./lineageDrill";

function card(moves: string[]): OpeningLineageItem {
  const board = new Chess();
  moves.forEach((move) => board.move(move));
  return {
    opening_key: board.fen(), opening_name: "Queen's Gambit Declined", opening_family: "Queen's Gambit",
    eco: "D30", depth: 2, moves, path: ["not a move"], score: null, confidence: 0,
    coverage: null, sample_size: 0, game_count: 0,
  };
}

const english = card(["c4", "e6", "Nc3", "Nf6", "d4", "d5"]);
const qgd = card(["d4", "d5", "c4", "e6", "Nc3", "Nf6"]);

describe("lineage drill route", () => {
  it("uses the selected occurrence prefix independently of registry depth/path and move clocks", () => {
    const line = ["c2c4", "e7e6", "b1c3", "g8f6", "d2d4", "d7d5"];
    expect(lineageDrillRoute(english, 1)).toEqual({ line, reason: null });
    expect(lineageDrillRoute(qgd, 1).line).toEqual(["d2d4", "d7d5", "c2c4", "e7e6", "b1c3", "g8f6"]);
    expect(normalize_fen(english.opening_key)).toBe(normalize_fen(qgd.opening_key));
    expect(lineageDrillRoute({ ...english, opening_key: normalize_fen(english.opening_key) + " 22 9" }, 1).line).toEqual(line);
    const earlier = card(["c4", "e6"]);
    expect(lineageDrillRoute(earlier, 1).line).toEqual(line.slice(0, 2));
    expect(lineageDrillSelection(english, line).opening.depth).toBe(2);
  });

  it("converts castling and promotion to UCI", () => {
    expect(lineageDrillRoute(card(["e4", "e5", "Nf3", "Nc6", "Bc4", "Nf6", "O-O"]), 1).line?.at(-1)).toBe("e1g1");
    expect(lineageDrillRoute(card(["a4", "h5", "a5", "h4", "a6", "h3", "axb7", "hxg2", "bxa8=Q"]), 1).line?.at(-1)).toBe("b7a8q");
  });

  it.each([
    ["empty", { ...english, moves: [] }, 1],
    ["illegal", { ...english, moves: ["e5"] }, 1],
    ["null move", card(["--"]), 1],
    ["mismatch", { ...english, moves: ["e4"] }, 1],
    ["midgame", english, 3],
    ["overlong", { ...english, moves: Array(81).fill("e4") }, 1],
    ["repeated", card(["Nf3", "Nf6", "Ng1", "Ng8"]), 1],
  ] satisfies Array<[string, OpeningLineageItem, number]>)("rejects %s prefixes", (_, item, startPly) => {
    expect(lineageDrillRoute(item, startPly)).toEqual({ line: null, reason: expect.any(String) });
  });
});
