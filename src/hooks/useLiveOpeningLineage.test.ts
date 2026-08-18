import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import {
  useLiveOpeningLineage,
  deriveStartPly,
  LOCAL_SCORE_PENDING_TIMEOUT_MS,
} from "./useLiveOpeningLineage";
import { resetOpeningRootsCacheForTests } from "../openings/openingRootsLoader";
import type { OpeningLineageItem, OpeningRootItem } from "../utils/api";
import fixture from "../openings/__fixtures__/openingChainParity.json";

const getOpeningRootsMock = vi.fn();

vi.mock("../utils/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../utils/api")>();
  return {
    ...actual,
    getOpeningRoots: (...args: unknown[]) => getOpeningRootsMock(...args),
  };
});

const ROOTS_RESPONSE = {
  families: [{ family_name: "all", roots: fixture.roots as OpeningRootItem[] }],
  total_roots: fixture.roots.length,
  total_families: 1,
};

const linearCase = fixture.cases.find(
  (c) => c.name === "linear walk through nested roots",
)!;
const moveHistory = linearCase.sans.map((san, i) => ({
  san,
  fen: linearCase.fens[i],
}));

function serverItem(
  openingKey: string,
  moves: string[],
  score: number | null,
): OpeningLineageItem {
  return {
    opening_key: openingKey,
    opening_name: "server",
    opening_family: "server",
    eco: null,
    depth: 0,
    score,
    confidence: null,
    coverage: null,
    sample_size: null,
    game_count: null,
    path: [],
    moves,
  };
}

describe("useLiveOpeningLineage", () => {
  beforeEach(() => {
    resetOpeningRootsCacheForTests();
    getOpeningRootsMock.mockReset();
    getOpeningRootsMock.mockResolvedValue(ROOTS_RESPONSE);
  });

  it("renders cards from LOCAL move history with no server lineage at all", async () => {
    // The core immediacy contract: the cards exist while the session-openings
    // request (and the move upload behind it) are still unresolved.
    const { result } = renderHook(() => useLiveOpeningLineage(moveHistory, []));

    await waitFor(() => {
      expect(result.current.lineage.length).toBeGreaterThan(0);
    });
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual(
      linearCase.expected.map((e) => e.opening_key),
    );
    // ...and they are unscored until the server answers.
    expect(result.current.lineage.every((i) => i.score === null)).toBe(true);
    expect([...result.current.pendingScoreIndices]).toEqual(
      result.current.lineage.map((_, index) => index),
    );
  });

  it("hydrates scores from the server without changing the cards", async () => {
    const { result, rerender } = renderHook(
      ({ server }: { server: OpeningLineageItem[] }) =>
        useLiveOpeningLineage(moveHistory, server),
      { initialProps: { server: [] as OpeningLineageItem[] } },
    );
    await waitFor(() => expect(result.current.lineage.length).toBeGreaterThan(0));
    const before = result.current.lineage;
    const pendingBefore = result.current.pendingScoreIndices;

    // An equivalent response array does not change membership, so consumers
    // keep the same Set reference instead of rerendering for allocation alone.
    rerender({ server: [] });
    expect(result.current.pendingScoreIndices).toBe(pendingBefore);

    const target = before[1];
    rerender({ server: [serverItem(target.opening_key, target.moves, 64)] });

    expect(result.current.lineage[1].score).toBe(64);
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual(
      before.map((i) => i.opening_key),
    );
    expect(result.current.lineage.map((i) => i.moves)).toEqual(
      before.map((i) => i.moves),
    );
    expect(result.current.pendingScoreIndices.has(1)).toBe(false);
    expect(result.current.pendingScoreIndices.has(0)).toBe(true);
  });

  it("treats a matching null score as loaded rather than pending", async () => {
    const first = linearCase.expected[0];
    const server = [serverItem(first.opening_key, first.moves, null)];
    const { result } = renderHook(() =>
      useLiveOpeningLineage(moveHistory, server),
    );

    await waitFor(() =>
      expect(result.current.lineage.length).toBeGreaterThan(server.length),
    );
    expect(result.current.lineage[0].score).toBeNull();
    expect(result.current.pendingScoreIndices.has(0)).toBe(false);
    expect(result.current.pendingScoreIndices.has(1)).toBe(true);
  });

  it("expires each unmatched occurrence on its own bounded clock", async () => {
    vi.useFakeTimers();
    const firstCrossingMoves = moveHistory.slice(
      0,
      linearCase.expected[0].moves.length,
    );
    const hook = renderHook(
      ({ moves, scope }: { moves: typeof moveHistory; scope: string }) =>
        useLiveOpeningLineage(moves, [], scope),
      { initialProps: { moves: firstCrossingMoves, scope: "session-a" } },
    );

    try {
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect([...hook.result.current.pendingScoreIndices]).toEqual([0]);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      hook.rerender({ moves: moveHistory, scope: "session-a" });
      expect(hook.result.current.pendingScoreIndices.has(1)).toBe(true);

      // The first card has waited 30s and falls back to the unscored state;
      // later cards still own the remainder of their independent budgets.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(
          LOCAL_SCORE_PENDING_TIMEOUT_MS - 10_000,
        );
      });
      expect(hook.result.current.pendingScoreIndices.has(0)).toBe(false);
      expect(hook.result.current.pendingScoreIndices.has(1)).toBe(true);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(hook.result.current.pendingScoreIndices.size).toBe(0);

      // A new session scopes the same occurrence to a fresh loading window.
      hook.rerender({ moves: firstCrossingMoves, scope: "session-b" });
      expect([...hook.result.current.pendingScoreIndices]).toEqual([0]);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(LOCAL_SCORE_PENDING_TIMEOUT_MS);
      });
      expect(hook.result.current.pendingScoreIndices.size).toBe(0);

      // Reverting removes the old appearance's expiry marker, so reaching the
      // same occurrence again in this session also gets a fresh window.
      hook.rerender({ moves: [], scope: "session-b" });
      hook.rerender({ moves: firstCrossingMoves, scope: "session-b" });
      expect([...hook.result.current.pendingScoreIndices]).toEqual([0]);
    } finally {
      hook.unmount();
      vi.useRealTimers();
    }
  });

  it("still hydrates a score after its local loading timeout expires", async () => {
    vi.useFakeTimers();
    const first = linearCase.expected[0];
    const firstCrossingMoves = moveHistory.slice(0, first.moves.length);
    const hook = renderHook(
      ({ server }: { server: OpeningLineageItem[] }) =>
        useLiveOpeningLineage(firstCrossingMoves, server, "session-a"),
      { initialProps: { server: [] as OpeningLineageItem[] } },
    );

    try {
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(LOCAL_SCORE_PENDING_TIMEOUT_MS);
      });
      expect(hook.result.current.pendingScoreIndices.size).toBe(0);

      hook.rerender({
        server: [serverItem(first.opening_key, first.moves, 73)],
      });
      expect(hook.result.current.lineage[0].score).toBe(73);
      expect(hook.result.current.pendingScoreIndices.size).toBe(0);
    } finally {
      hook.unmount();
      vi.useRealTimers();
    }
  });

  it("a shorter server lineage cannot remove a locally visible card", async () => {
    const { result } = renderHook(() =>
      useLiveOpeningLineage(moveHistory, [
        serverItem(fixture.roots[0].opening_key, ["e4"], 50),
      ]),
    );
    await waitFor(() => expect(result.current.lineage.length).toBeGreaterThan(1));
    expect(result.current.lineage).toHaveLength(linearCase.expected.length);
  });

  it("falls back to the server lineage when the registry fails to load", async () => {
    getOpeningRootsMock.mockRejectedValue(new Error("boom"));
    const server = [serverItem(fixture.roots[0].opening_key, ["e4"], 50)];

    const { result } = renderHook(() => useLiveOpeningLineage(moveHistory, server));
    await act(async () => {});

    // Degrades to the pre-g-a5v3 behavior rather than showing nothing.
    expect(result.current.lineage).toEqual(server);
    expect(result.current.pendingScoreIndices.size).toBe(0);
  });

  it("fetches the root registry once and shares it across mounts", async () => {
    const first = renderHook(() => useLiveOpeningLineage(moveHistory, []));
    await waitFor(() => expect(first.result.current.lineage.length).toBeGreaterThan(0));
    first.unmount();

    const second = renderHook(() => useLiveOpeningLineage(moveHistory, []));
    await waitFor(() => expect(second.result.current.lineage.length).toBeGreaterThan(0));

    expect(getOpeningRootsMock).toHaveBeenCalledTimes(1);
  });

  it("retries the registry after a failure rather than caching it", async () => {
    getOpeningRootsMock.mockRejectedValueOnce(new Error("boom"));
    const first = renderHook(() => useLiveOpeningLineage(moveHistory, []));
    await act(async () => {});
    expect(first.result.current.lineage).toEqual([]);

    const second = renderHook(() => useLiveOpeningLineage(moveHistory, []));
    await waitFor(() => expect(second.result.current.lineage.length).toBeGreaterThan(0));
    expect(getOpeningRootsMock).toHaveBeenCalledTimes(2);
  });
});

describe("deriveStartPly", () => {
  it("returns 1 for a game starting from the initial position", () => {
    expect(deriveStartPly(moveHistory)).toBe(1);
  });

  it("returns 1 for an empty history", () => {
    expect(deriveStartPly([])).toBe(1);
  });

  it("numbers a mid-game (drill) start from the resulting position", () => {
    // After White's move 5 the FEN reads (fullmove 5, black to move) -> ply 9.
    expect(
      deriveStartPly([
        { san: "Nf3", fen: "8/8/8/8/8/8/8/8 b - - 0 5" },
      ]),
    ).toBe(9);
    // After Black's move 5 it reads (fullmove 6, white to move) -> ply 10.
    expect(
      deriveStartPly([
        { san: "Nf6", fen: "8/8/8/8/8/8/8/8 w - - 0 6" },
      ]),
    ).toBe(10);
  });

  it("falls back to 1 on a malformed FEN rather than producing a bad ply", () => {
    expect(deriveStartPly([{ san: "e4", fen: "garbage" }])).toBe(1);
  });
});
