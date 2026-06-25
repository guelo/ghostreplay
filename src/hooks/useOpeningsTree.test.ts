import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
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

vi.mock("../utils/api", () => ({
  getOpeningTree: (...args: unknown[]) => getOpeningTreeMock(...args),
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
