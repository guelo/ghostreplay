import { describe, expect, it } from "vitest";
import {
  buildRootIndex,
  deriveLiveOpeningLineage,
  mergeServerLineage,
  mergeServerLineageState,
  type LiveOpeningLineageItem,
} from "./deriveLiveLineage";
import type { OpeningLineageItem, OpeningRootItem } from "../utils/api";
import fixture from "./__fixtures__/openingChainParity.json";

/**
 * The merge contract (g-a5v3): locally-derived cards own STRUCTURE, the server
 * owns SCORES only. These tests pin the rules that keep a slow or shorter
 * server response from ever removing or corrupting a visible card.
 */

const roots = () =>
  buildRootIndex({
    families: [{ family_name: "all", roots: fixture.roots as OpeningRootItem[] }],
    total_roots: fixture.roots.length,
    total_families: 1,
  });

const caseByName = (name: string) => fixture.cases.find((c) => c.name === name)!;

function localFor(name: string): LiveOpeningLineageItem[] {
  const testCase = caseByName(name);
  return deriveLiveOpeningLineage(
    testCase.sans.map((san, i) => ({ san, fen: testCase.fens[i] })),
    roots(),
  );
}

/** A server row carrying scores for the crossing whose SAN prefix is `moves`. */
function serverItem(
  openingKey: string,
  moves: string[],
  score: number | null,
): OpeningLineageItem {
  return {
    opening_key: openingKey,
    opening_name: "server-name-should-be-ignored",
    opening_family: "server-family-should-be-ignored",
    eco: "ZZ9",
    depth: 99,
    score,
    confidence: 0.7,
    coverage: 0.4,
    sample_size: 12,
    game_count: 3,
    path: [],
    moves,
  };
}

describe("mergeServerLineage", () => {
  it("hydrates scores without disturbing name, path, or SAN prefix", () => {
    const local = localFor("linear walk through nested roots");
    const target = local[1];
    const merged = mergeServerLineage(local, [
      serverItem(target.opening_key, target.moves, 73),
    ]);

    expect(merged[1].score).toBe(73);
    expect(merged[1].sample_size).toBe(12);
    // Structure stays local — the server's name/depth/path are NOT adopted.
    expect(merged[1].opening_name).toBe(target.opening_name);
    expect(merged[1].depth).toBe(target.depth);
    expect(merged[1].path).toEqual(target.path);
    expect(merged[1].moves).toEqual(target.moves);
  });

  it("keeps every local card when the server lineage is shorter", () => {
    const local = localFor("linear walk through nested roots");
    expect(local.length).toBeGreaterThan(1);

    const merged = mergeServerLineage(local, [
      serverItem(local[0].opening_key, local[0].moves, 50),
    ]);

    // The core immediacy guarantee: a lagging server response can never make a
    // visible card disappear.
    expect(merged).toHaveLength(local.length);
    expect(merged[0].score).toBe(50);
    expect(merged[1].score).toBeNull();
  });

  it("keeps every local card when the server lineage is empty or absent", () => {
    const local = localFor("linear walk through nested roots");
    expect(mergeServerLineage(local, [])).toEqual(local);
    expect(mergeServerLineage(local, null)).toEqual(local);
    expect(mergeServerLineage(local, undefined)).toEqual(local);
  });

  it("hydrates two crossings of the SAME root independently", () => {
    // The reason the merge key is (opening_key, crossingIndex) and not
    // opening_key alone: a key-only merge would copy one row's score onto both
    // crossings and collapse their distinct prefixes.
    const local = localFor("non-consecutive repeated root retains both crossings");
    const repeated = local.filter(
      (item) => item.opening_key === local[0].opening_key,
    );
    expect(repeated).toHaveLength(2);
    expect(repeated[0].crossingIndex).not.toBe(repeated[1].crossingIndex);

    // Server scores only the SECOND crossing.
    const merged = mergeServerLineage(local, [
      serverItem(repeated[1].opening_key, repeated[1].moves, 88),
    ]);
    const mergedRepeats = merged.filter(
      (item) => item.opening_key === local[0].opening_key,
    );

    expect(mergedRepeats[0].score).toBeNull();
    expect(mergedRepeats[1].score).toBe(88);
    // Prefixes stay distinct — the later crossing keeps its longer prefix.
    expect(mergedRepeats[0].moves).toEqual(repeated[0].moves);
    expect(mergedRepeats[1].moves).toEqual(repeated[1].moves);
    expect(mergedRepeats[1].moves.length).toBeGreaterThan(
      mergedRepeats[0].moves.length,
    );
  });

  it("ignores a server row whose crossing index has no local counterpart", () => {
    const local = localFor("linear walk through nested roots");
    const merged = mergeServerLineage(local, [
      serverItem(local[0].opening_key, ["e4", "e5", "Nf3"], 42),
    ]);
    expect(merged.map((i) => i.score)).toEqual(local.map(() => null));
  });

  it("preserves referential identity when nothing hydrates", () => {
    // Consumers memoize on the lineage array; a no-op poll must not re-render.
    const local = localFor("linear walk through nested roots");
    expect(mergeServerLineage(local, [serverItem("unknown-key", [], 10)])).toBe(
      local,
    );
  });
});

describe("mergeServerLineageState pending occurrences", () => {
  it("marks every local occurrence pending before the server lineage arrives", () => {
    const local = localFor("linear walk through nested roots");

    const state = mergeServerLineageState(local, []);
    expect(state.lineage).toBe(local);
    expect(state.pendingScoreOccurrences.map(({ index }) => index)).toEqual(
      local.map((_, index) => index),
    );
  });

  it("leaves only unmatched occurrences pending, including when a match has a null score", () => {
    const local = localFor("linear walk through nested roots");
    const matched = local[0];

    const state = mergeServerLineageState(local, [
      serverItem(matched.opening_key, matched.moves, null),
    ]);
    const pending = new Set(
      state.pendingScoreOccurrences.map(({ index }) => index),
    );

    expect(pending.has(0)).toBe(false);
    expect([...pending]).toEqual(
      local.slice(1).map((_, index) => index + 1),
    );
    expect(state.lineage[0].score).toBeNull();
  });

  it("resolves repeated opening keys by occurrence rather than by key alone", () => {
    const local = localFor("non-consecutive repeated root retains both crossings");
    const repeatedIndices = local.flatMap((item, index) =>
      item.opening_key === local[0].opening_key ? [index] : [],
    );
    expect(repeatedIndices).toHaveLength(2);
    const later = local[repeatedIndices[1]];

    const state = mergeServerLineageState(local, [
      serverItem(later.opening_key, later.moves, 88),
    ]);
    const pending = new Set(
      state.pendingScoreOccurrences.map(({ index }) => index),
    );

    expect(pending.has(repeatedIndices[0])).toBe(true);
    expect(pending.has(repeatedIndices[1])).toBe(false);
  });
});
