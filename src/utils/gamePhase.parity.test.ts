import { describe, expect, it } from "vitest";
import fixture from "./__fixtures__/gamePhaseParity.json";
import {
  backrankSparse,
  isMiddlegame,
  majorsAndMinors,
  mixedness,
  openingPlyCount,
  parsePlacement,
} from "./gamePhase";

/**
 * Cross-implementation parity for backend/app/game_phase.py and the browser
 * marker. Regenerate with backend/scripts/gen_game_phase_parity_fixture.py;
 * fix the drifting implementation rather than weakening these assertions.
 */
describe("gamePhase — backend parity", () => {
  for (const position of fixture.positions) {
    it(`matches predicates for ${position.fen}`, () => {
      const parsed = parsePlacement(position.fen);
      expect(parsed).not.toBeNull();
      if (parsed == null) throw new Error("fixture FEN did not parse");
      expect(majorsAndMinors(parsed)).toBe(position.majors_and_minors);
      expect(backrankSparse(parsed)).toBe(position.backrank_sparse);
      expect(mixedness(parsed)).toBe(position.mixedness);
      expect(isMiddlegame(parsed)).toBe(position.is_middlegame);
    });
  }

  for (const line of fixture.lines) {
    it(line.name, () => {
      expect(openingPlyCount(line.fens)).toBe(line.opening_ply_count);
    });
  }
});
