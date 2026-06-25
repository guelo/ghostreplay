import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { useOpeningsTree } from "./useOpeningsTree";
import type { TreeColumn, TreeNode, TreeResponse } from "../utils/api";

/**
 * Focused tests for the user-selected (third type) cache scope (g-obh5). A
 * user-selected node is line-scoped — the backend only emits it as the selected
 * move of its column — so the hook's prefix no-fetch path must NOT reuse a deeper
 * response that carries one when rendering a shorter prefix, or the selected
 * sibling would leak as a navigable child of a position that no longer selects
 * it. The broader state machine is covered by OpeningsPage.test.tsx.
 */

const getOpeningTreeMock = vi.fn();
const getOpeningTreeStatusMock = vi.fn();

vi.mock("../utils/api", () => ({
  getOpeningTree: (...args: unknown[]) => getOpeningTreeMock(...args),
  getOpeningTreeStatus: (...args: unknown[]) => getOpeningTreeStatusMock(...args),
}));

function tn(overrides: Partial<TreeNode> & { uci: string }): TreeNode {
  return {
    parent_fen: "parent",
    child_fen: "child",
    san: overrides.uci,
    ply: 1,
    opening_name: null,
    eco: null,
    in_book: true,
    is_navigable: true,
    is_observed: false,
    is_user_selected: false,
    is_prepared: false,
    user_choice_count: 0,
    encounter_count: 0,
    opening_score: null,
    confidence: null,
    coverage: null,
    sample_size: null,
    game_count: null,
    last_practiced_at: null,
    eval_cp: null,
    eval_mate: null,
    terminal_reason: null,
    drill_opening_key: null,
    is_selected: false,
    ...overrides,
  };
}

function tc(
  ply: number,
  nodes: TreeNode[],
  selectedUci: string | null = null,
): TreeColumn {
  return { position_fen: `pos-${ply}`, ply, selected_uci: selectedUci, nodes };
}

function tr(overrides: Partial<TreeResponse> = {}): TreeResponse {
  return {
    player_color: "white",
    canonical_line: [],
    selected_fen: "sel",
    selected_ply: 0,
    selected_is_terminal: false,
    selected_terminal_reason: null,
    drill_opening_key: null,
    root_eval_cp: null,
    root_eval_mate: null,
    root_opening_score: null,
    root_coverage: null,
    root_game_count: null,
    root_confidence: null,
    columns: [],
    batch_computed_at: null,
    model_version: "v2",
    ...overrides,
  };
}

const baseRoute = { playerColor: "white" as const, opening: null };

describe("useOpeningsTree — user-selected cache scope (g-obh5)", () => {
  beforeEach(() => {
    getOpeningTreeMock.mockReset();
    // Default: cache is warm, so the fetch path is unchanged for these tests.
    getOpeningTreeStatusMock.mockReset();
    getOpeningTreeStatusMock.mockResolvedValue({
      player_color: "white",
      state: "warm",
    });
  });

  it("refetches the exact prefix when the displayed response carries a user-selected node", async () => {
    // Deep response a2a3,a7a6: a7a6 is a user-selected (third type) child of the
    // a2a3 position (and a2a3 itself is user-selected at the root column).
    getOpeningTreeMock.mockResolvedValue(
      tr({
        canonical_line: ["a2a3", "a7a6"],
        columns: [
          tc(
            0,
            [tn({ uci: "a2a3", san: "a3", ply: 1, is_user_selected: true, in_book: false })],
            "a2a3",
          ),
          tc(
            1,
            [tn({ uci: "a7a6", san: "a6", ply: 2, is_user_selected: true, in_book: false })],
            "a7a6",
          ),
        ],
      }),
    );

    const { result, rerender } = renderHook(
      (props: { playerColor: "white"; opening: null; moves: string[] }) =>
        useOpeningsTree(props),
      { initialProps: { ...baseRoute, moves: ["a2a3", "a7a6"] } },
    );
    await waitFor(() => expect(result.current.isSettled).toBe(true));
    expect(getOpeningTreeMock).toHaveBeenCalledTimes(1);

    // Back to the a2a3 prefix: the displayed deep response carries a user-selected
    // node, so the no-fetch prefix path must bail → an exact ["a2a3"] refetch.
    getOpeningTreeMock.mockResolvedValue(
      tr({
        canonical_line: ["a2a3"],
        columns: [
          tc(
            0,
            [tn({ uci: "a2a3", san: "a3", ply: 1, is_user_selected: true, in_book: false })],
            "a2a3",
          ),
        ],
      }),
    );
    rerender({ ...baseRoute, moves: ["a2a3"] });

    await waitFor(() => expect(getOpeningTreeMock).toHaveBeenCalledTimes(2));
    expect(getOpeningTreeMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ moves: ["a2a3"] }),
      expect.anything(),
    );
  });

  it("reuses the displayed response without refetching for a plain (book) prefix", async () => {
    // No user-selected node anywhere → the prefix no-fetch path stays in effect.
    getOpeningTreeMock.mockResolvedValue(
      tr({
        canonical_line: ["e2e4", "e7e5"],
        columns: [
          tc(0, [tn({ uci: "e2e4", san: "e4", ply: 1 })], "e2e4"),
          tc(1, [tn({ uci: "e7e5", san: "e5", ply: 2 })], "e7e5"),
        ],
      }),
    );

    const { result, rerender } = renderHook(
      (props: { playerColor: "white"; opening: null; moves: string[] }) =>
        useOpeningsTree(props),
      { initialProps: { ...baseRoute, moves: ["e2e4", "e7e5"] } },
    );
    await waitFor(() => expect(result.current.isSettled).toBe(true));
    expect(getOpeningTreeMock).toHaveBeenCalledTimes(1);

    // Back to the e2e4 prefix: a superset response with no third-type node is
    // reused with no network.
    rerender({ ...baseRoute, moves: ["e2e4"] });
    await waitFor(() =>
      expect(result.current.canonicalLine).toEqual(["e2e4"]),
    );
    expect(getOpeningTreeMock).toHaveBeenCalledTimes(1);
  });
});

describe("useOpeningsTree — cold-cache bootstrap gate (g-k4z2)", () => {
  // Poll cadence mirrors STATUS_POLL_INTERVAL_MS in useOpeningsTree.ts.
  const POLL_MS = 2000;

  beforeEach(() => {
    getOpeningTreeMock.mockReset();
    getOpeningTreeStatusMock.mockReset();
  });

  it("shows the initializing state while building, then loads the tree once warm", async () => {
    vi.useFakeTimers();
    try {
      // First probe is still building (the one-time bootstrap), second is warm.
      getOpeningTreeStatusMock
        .mockResolvedValueOnce({ player_color: "white", state: "building" })
        .mockResolvedValueOnce({ player_color: "white", state: "warm" });
      getOpeningTreeMock.mockResolvedValue(tr({ canonical_line: [] }));

      const { result } = renderHook(() =>
        useOpeningsTree({ ...baseRoute, moves: [] }),
      );

      // First /tree/status probe resolves "building" → explicit initializing
      // state, and crucially NO /tree fetch is issued (no silent server block).
      await act(async () => {
        await Promise.resolve();
      });
      expect(result.current.pageStatus).toBe("initializing");
      expect(getOpeningTreeMock).not.toHaveBeenCalled();

      // After one poll interval the next probe is warm → the tree loads.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_MS);
      });
      expect(getOpeningTreeMock).toHaveBeenCalledTimes(1);
      expect(result.current.pageStatus).toBe("ready");
    } finally {
      vi.useRealTimers();
    }
  });

  it("probes status at most once per color (warm stays warm for the session)", async () => {
    getOpeningTreeStatusMock.mockResolvedValue({
      player_color: "white",
      state: "warm",
    });
    getOpeningTreeMock.mockResolvedValue(
      tr({
        canonical_line: ["e2e4"],
        columns: [tc(0, [tn({ uci: "e2e4", san: "e4", ply: 1 })], "e2e4")],
      }),
    );

    const { result, rerender } = renderHook(
      (props: { playerColor: "white"; opening: null; moves: string[] }) =>
        useOpeningsTree(props),
      { initialProps: { ...baseRoute, moves: [] as string[] } },
    );
    await waitFor(() => expect(result.current.isSettled).toBe(true));
    expect(getOpeningTreeStatusMock).toHaveBeenCalledTimes(1);

    // A divergent line for the same (now known-warm) color forces a fresh fetch
    // but skips the status probe — warm is remembered per color.
    rerender({ ...baseRoute, moves: ["d2d4"] });
    await waitFor(() => expect(getOpeningTreeMock).toHaveBeenCalledTimes(2));
    expect(getOpeningTreeStatusMock).toHaveBeenCalledTimes(1);
  });

  it("surfaces a retry-able PAGE error (not a pinned setup screen) when the poll times out after a tree was shown", async () => {
    vi.useFakeTimers();
    try {
      // White loads warm (so a previous tree is displayed + displayedRef is set).
      getOpeningTreeStatusMock.mockResolvedValue({
        player_color: "white",
        state: "warm",
      });
      getOpeningTreeMock.mockResolvedValue(tr({ canonical_line: [] }));

      const { result, rerender } = renderHook(
        (props: {
          playerColor: "white" | "black";
          opening: null;
          moves: string[];
        }) => useOpeningsTree(props),
        {
          initialProps: {
            playerColor: "white" as "white" | "black",
            opening: null,
            moves: [] as string[],
          },
        },
      );
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(result.current.isSettled).toBe(true);

      // Switch to a cold color whose one-time bootstrap never finishes.
      getOpeningTreeStatusMock.mockReset();
      getOpeningTreeStatusMock.mockResolvedValue({
        player_color: "black",
        state: "building",
      });
      rerender({ playerColor: "black", opening: null, moves: [] });
      await act(async () => {
        await Promise.resolve();
      });
      expect(result.current.pageStatus).toBe("initializing");

      // Exhaust the ~25×2s poll cap. Despite a previous (white) tree still in
      // displayedRef, this must become a retry-able PAGE error — not an append
      // error that would leave the setup screen pinned with no Retry.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      expect(result.current.pageStatus).toBe("error");
      expect(result.current.view).toBeNull();
      expect(result.current.error).toMatch(/still being set up/i);
    } finally {
      vi.useRealTimers();
    }
  });

  it("treats a /tree bootstrap_timeout as a retry-able setup state, not ready", async () => {
    // Warm status gate passes, but the /tree fetch races onto a degraded,
    // still-building book-only tree (the batch was pruned/invalidated): it must
    // NOT render as ready.
    getOpeningTreeStatusMock.mockResolvedValue({
      player_color: "white",
      state: "warm",
    });
    getOpeningTreeMock.mockResolvedValue(
      tr({ canonical_line: [], cache_state: "bootstrap_timeout" }),
    );

    const { result } = renderHook(() =>
      useOpeningsTree({ ...baseRoute, moves: [] }),
    );
    await waitFor(() => expect(result.current.pageStatus).toBe("error"));
    expect(result.current.isSettled).toBe(false);
    expect(result.current.error).toMatch(/finishing setup/i);
  });
});
