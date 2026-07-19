import { describe, expect, it } from "vitest";
import {
  buildRootIndex,
  deriveLiveOpeningLineage,
  type LiveOpeningRootIndex,
} from "./deriveLiveLineage";
import type { OpeningRootItem } from "../utils/api";
import fixture from "./__fixtures__/openingChainParity.json";

/**
 * Cross-implementation parity for the played-opening chain walk (g-a5v3).
 *
 * The live cards derive the chain here; the persisted lineage derives it in
 * app/opening_roots.py::played_opening_chain_indexed. This suite and
 * backend/test_opening_chain_parity.py consume the SAME generated fixture
 * (backend/scripts/gen_opening_chain_parity_fixture.py). If one side fails,
 * regenerate the fixture and fix the OTHER side to match — do not weaken the
 * assertion.
 */

const rootIndex = (): LiveOpeningRootIndex =>
  buildRootIndex({
    families: [{ family_name: "all", roots: fixture.roots as OpeningRootItem[] }],
    total_roots: fixture.roots.length,
    total_families: 1,
  });

describe("deriveLiveOpeningLineage — backend parity", () => {
  for (const testCase of fixture.cases) {
    it(testCase.name, () => {
      const moveHistory = testCase.sans.map((san, i) => ({
        san,
        fen: testCase.fens[i],
      }));

      const lineage = deriveLiveOpeningLineage(moveHistory, rootIndex());

      // Assert the crossing index AND the SAN prefix: the prefix is what the
      // card displays, and it is exactly what a wrong index would corrupt.
      expect(
        lineage.map((item) => ({
          opening_key: item.opening_key,
          crossing_index: item.crossingIndex,
          moves: item.moves,
        })),
      ).toEqual(
        testCase.expected.map((e) => ({
          opening_key: e.opening_key,
          crossing_index: e.crossing_index,
          moves: e.moves,
        })),
      );

      // `path` (ancestor keys) must also match the server's projection.
      expect(lineage.map((item) => item.path)).toEqual(
        testCase.expected.map((e) => e.path),
      );
    });
  }

  it("carries root metadata onto each card and leaves scores unhydrated", () => {
    const testCase = fixture.cases.find((c) => c.expected.length > 0)!;
    const moveHistory = testCase.sans.map((san, i) => ({
      san,
      fen: testCase.fens[i],
    }));

    const lineage = deriveLiveOpeningLineage(moveHistory, rootIndex());
    const first = lineage[0];
    const root = fixture.roots.find((r) => r.opening_key === first.opening_key)!;

    expect(first.opening_name).toBe(root.opening_name);
    expect(first.opening_family).toBe(root.opening_family);
    expect(first.eco).toBe(root.eco);
    expect(first.depth).toBe(root.depth);
    // Scores are the server's job — locally derived cards start unscored.
    expect(first.score).toBeNull();
    expect(first.confidence).toBeNull();
    expect(first.sample_size).toBeNull();
  });

  it("dedupes consecutive duplicate positions", () => {
    // No legal move list reaches this branch (consecutive plies differ in
    // side-to-move, so their keys differ); driven synthetically, mirroring
    // test_opening_chain_parity.py::test_consecutive_duplicate_keys_are_deduped.
    const key = fixture.roots[0].opening_key;
    const moveHistory = [
      { san: "a", fen: key },
      { san: "b", fen: key },
      { san: "c", fen: key },
    ];

    const lineage = deriveLiveOpeningLineage(moveHistory, rootIndex());

    expect(lineage).toHaveLength(1);
    expect(lineage[0].crossingIndex).toBe(0);
  });

  it("skips unknown and missing positions without aborting the walk", () => {
    const key = fixture.roots[0].opening_key;
    const moveHistory = [
      { san: "a", fen: "" },
      { san: "b", fen: "not-a-fen" },
      { san: "c", fen: key },
    ];

    const lineage = deriveLiveOpeningLineage(moveHistory, rootIndex());

    expect(lineage.map((i) => i.crossingIndex)).toEqual([2]);
  });

  it("returns an empty index for a missing roots response", () => {
    expect(buildRootIndex(null).size).toBe(0);
    expect(deriveLiveOpeningLineage([{ san: "e4", fen: "x" }], buildRootIndex(null))).toEqual([]);
  });
});
