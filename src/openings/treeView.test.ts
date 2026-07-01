import { describe, it, expect } from "vitest";
import {
  buildTreeView,
  connectorStyle,
  nodeToView,
  replayLine,
  resolveDrop,
  synthesizeRootView,
  type DisplayNode,
} from "./treeView";
import type { TreeColumn, TreeNode, TreeResponse } from "../utils/api";

const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

function makeNode(overrides: Partial<TreeNode> & { uci: string }): TreeNode {
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

function makeColumn(
  ply: number,
  nodes: TreeNode[],
  selectedUci: string | null = null,
): TreeColumn {
  return { position_fen: `pos-${ply}`, ply, selected_uci: selectedUci, nodes };
}

function makeResponse(overrides: Partial<TreeResponse> = {}): TreeResponse {
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
    batch_computed_at: "2026-06-01T00:00:00Z",
    model_version: "v2",
    ...overrides,
  };
}

// A two-ply Sicilian-ish response: columns[0]=moves out of start,
// columns[1]=moves out of pos1, columns[2]=moves out of pos2.
function sicilianResponse(): TreeResponse {
  return makeResponse({
    canonical_line: ["e2e4", "c7c5"],
    columns: [
      makeColumn(
        0,
        [
          makeNode({ uci: "e2e4", ply: 1, eval_cp: 30 }),
          makeNode({ uci: "d2d4", ply: 1, eval_cp: 20 }),
        ],
        "e2e4",
      ),
      makeColumn(
        1,
        [
          makeNode({ uci: "c7c5", ply: 2, eval_cp: 25 }),
          makeNode({ uci: "e7e5", ply: 2, eval_cp: 18 }),
        ],
        "c7c5",
      ),
      makeColumn(2, [makeNode({ uci: "g1f3", ply: 3, eval_cp: 22 })]),
    ],
  });
}

describe("nodeToView", () => {
  it("maps fields and keeps eval white-relative for both perspectives", () => {
    const node = makeNode({
      uci: "e2e4",
      san: "e4",
      ply: 1,
      opening_name: "King's Pawn",
      eco: "B00",
      opening_score: 61,
      coverage: 0.4,
      game_count: 12,
      eval_cp: 40,
      eval_mate: 3,
    });

    // The eval is white-relative regardless of which side's repertoire is shown
    // (the +white / −black convention; the column SORT applies the perspective).
    const view = nodeToView(node);
    expect(view.evalCp).toBe(40);
    expect(view.evalMate).toBe(3);
    expect(view.score).toBe(61);
    expect(view.openingName).toBe("King's Pawn");
    expect(view.isTerminal).toBe(false);
  });

  it("marks terminal nodes from terminal_reason", () => {
    const node = makeNode({ uci: "d8h4", terminal_reason: "checkmate" });
    const view = nodeToView(node);
    expect(view.isTerminal).toBe(true);
    expect(view.terminalReason).toBe("checkmate");
  });

  it("maps is_user_selected (the third move type) onto the view", () => {
    expect(nodeToView(makeNode({ uci: "d7d5" })).isUserSelected).toBe(false);
    expect(
      nodeToView(makeNode({ uci: "d7d5", is_user_selected: true }))
        .isUserSelected,
    ).toBe(true);
  });
});

describe("synthesizeRootView", () => {
  it("uses the white-relative root eval regardless of clipping", () => {
    const response = makeResponse({ root_eval_cp: 30, root_eval_mate: null });
    expect(synthesizeRootView(response, false, 2).evalCp).toBe(30);
  });

  it("propagates root metrics regardless of depth and clipping", () => {
    const response = makeResponse({
      root_opening_score: 72,
      root_coverage: 0.6,
      root_game_count: 15,
    });
    const view = synthesizeRootView(response, false, 2);
    expect(view.score).toBe(72);
    expect(view.coverage).toBe(0.6);
    expect(view.gameCount).toBe(15);
  });

  it("root metrics are null when response has no batch data", () => {
    const response = makeResponse({});
    const view = synthesizeRootView(response, true, 0);
    expect(view.score).toBeNull();
    expect(view.coverage).toBeNull();
    expect(view.gameCount).toBeNull();
  });

  it("propagates terminal/drill only when fetched for the root", () => {
    const response = makeResponse({
      root_eval_cp: 10,
      selected_is_terminal: true,
      selected_terminal_reason: "stalemate",
      drill_opening_key: "kp-root",
    });

    const fetchedForRoot = synthesizeRootView(response, true, 0);
    expect(fetchedForRoot.isTerminal).toBe(true);
    expect(fetchedForRoot.terminalReason).toBe("stalemate");
    expect(fetchedForRoot.drillOpeningKey).toBe("kp-root");

    // Exact line but deeper (k>0): the response's selected fields describe the
    // deeper line, so they must not leak onto the root card.
    const deeper = synthesizeRootView(response, true, 2);
    expect(deeper.isTerminal).toBe(false);
    expect(deeper.terminalReason).toBeNull();
    expect(deeper.drillOpeningKey).toBeNull();

    // Clipped view (isExactResponseLine false) at the root: also suppressed.
    const clipped = synthesizeRootView(response, false, 0);
    expect(clipped.drillOpeningKey).toBeNull();
    expect(clipped.isTerminal).toBe(false);
  });
});

describe("buildTreeView", () => {
  it("prepends a root column and expands the deepest selected node", () => {
    const view = buildTreeView(
      sicilianResponse(),
      {
        selectionLine: ["e2e4", "c7c5"],
        loadedThroughPly: 2,
        isExactResponseLine: true,
      },
    );

    expect(view.columns.map((c) => c.kind)).toEqual([
      "root",
      "moves",
      "moves",
      "moves",
    ]);
    expect(view.columns.map((c) => c.lineIndex)).toEqual([-1, 0, 1, 2]);

    const [root, col0, col1] = view.columns;
    // Root is on-path but not expanded once we are deeper than k=0.
    expect(root.nodes[0].isSelected).toBe(true);
    expect(root.nodes[0].isExpanded).toBe(false);

    // e2e4 selected (on path) but NOT expanded (it is not the deepest).
    const e2e4 = col0.nodes.find((n) => n.uci === "e2e4")!;
    expect(e2e4.isSelected).toBe(true);
    expect(e2e4.isExpanded).toBe(false);

    // c7c5 is the deepest selected node → expanded.
    const c7c5 = col1.nodes.find((n) => n.uci === "c7c5")!;
    expect(c7c5.isSelected).toBe(true);
    expect(c7c5.isExpanded).toBe(true);
    expect(col1.nodes.find((n) => n.uci === "e7e5")!.isSelected).toBe(false);
  });

  it("expands the synthesized root at k === 0", () => {
    const view = buildTreeView(
      sicilianResponse(),
      { selectionLine: [], loadedThroughPly: 0, isExactResponseLine: true },
    );
    expect(view.columns[0].nodes[0].isExpanded).toBe(true);
    // Only the children-of-root column renders at loadedThroughPly 0.
    expect(view.columns.map((c) => c.lineIndex)).toEqual([-1, 0]);
  });

  it("drops columns deeper than loadedThroughPly (clip a superset)", () => {
    const view = buildTreeView(
      sicilianResponse(),
      {
        selectionLine: ["e2e4"],
        loadedThroughPly: 1,
        isExactResponseLine: false,
      },
    );
    // columns[2] (ply 2) is dropped; column ply 1 renders with nothing selected.
    expect(view.columns.map((c) => c.lineIndex)).toEqual([-1, 0, 1]);
    const col0 = view.columns[1];
    expect(col0.nodes.find((n) => n.uci === "e2e4")!.isExpanded).toBe(true);
    const col1 = view.columns[2];
    expect(col1.nodes.every((n) => !n.isSelected)).toBe(true);
  });

  it("computes selectLine with truncation (sibling in a mid column)", () => {
    const view = buildTreeView(
      sicilianResponse(),
      {
        selectionLine: ["e2e4", "c7c5"],
        loadedThroughPly: 2,
        isExactResponseLine: true,
      },
    );
    const col1 = view.columns.find((c) => c.lineIndex === 1)!;
    // Selecting the sibling e7e5 truncates c7c5 off the line.
    expect(col1.nodes.find((n) => n.uci === "e7e5")!.selectLine).toEqual([
      "e2e4",
      "e7e5",
    ]);
    // Selecting the root column resets to [].
    expect(view.columns[0].nodes[0].selectLine).toEqual([]);
    // A first-column move replaces the whole line.
    const col0 = view.columns.find((c) => c.lineIndex === 0)!;
    expect(col0.nodes.find((n) => n.uci === "d2d4")!.selectLine).toEqual([
      "d2d4",
    ]);
  });

  it("marks non-navigable boundary nodes as not selectable; root stays selectable", () => {
    const view = buildTreeView(
      makeResponse({
        canonical_line: [],
        columns: [
          makeColumn(0, [
            makeNode({ uci: "e2e4", ply: 1, is_navigable: true }),
            makeNode({ uci: "h2h4", ply: 1, is_navigable: false }),
          ]),
        ],
      }),
      { selectionLine: [], loadedThroughPly: 0, isExactResponseLine: true },
    );

    // The synthesized root is always selectable even though it is not a drop
    // target (`isNavigable` false).
    expect(view.columns[0].nodes[0].isSelectable).toBe(true);
    expect(view.columns[0].nodes[0].isNavigable).toBe(false);

    const col0 = view.columns[1];
    expect(col0.nodes.find((n) => n.uci === "e2e4")!.isSelectable).toBe(true);
    expect(col0.nodes.find((n) => n.uci === "h2h4")!.isSelectable).toBe(false);
  });

  it("threads in_book/is_observed/encounter_count/childFen onto api nodes; defaults the root", () => {
    const view = buildTreeView(
      makeResponse({
        columns: [
          makeColumn(0, [
            makeNode({
              uci: "e2e4",
              ply: 1,
              child_fen: "fen-after-e4",
              in_book: true,
              is_observed: true,
              encounter_count: 9,
            }),
            makeNode({
              uci: "d2d4",
              ply: 1,
              child_fen: "fen-after-d4",
              in_book: true,
              is_observed: false,
              encounter_count: 0,
            }),
          ]),
        ],
      }),
      { selectionLine: [], loadedThroughPly: 0, isExactResponseLine: true },
    );

    // The synthesized root is only ever a connector parent — never styled, and
    // its childFen is null so the page never wires it as a drill target.
    const root = view.columns[0].nodes[0];
    expect(root.inBook).toBe(false);
    expect(root.isObserved).toBe(false);
    expect(root.encounterCount).toBe(0);
    expect(root.childFen).toBeNull();

    const col0 = view.columns[1];
    const e2e4 = col0.nodes.find((n) => n.uci === "e2e4")!;
    expect(e2e4.inBook).toBe(true);
    expect(e2e4.isObserved).toBe(true);
    expect(e2e4.encounterCount).toBe(9);
    // childFen is the move's resulting FEN — the drill target for a card drill.
    expect(e2e4.childFen).toBe("fen-after-e4");

    const d2d4 = col0.nodes.find((n) => n.uci === "d2d4")!;
    expect(d2d4.isObserved).toBe(false);
    expect(d2d4.encounterCount).toBe(0);
    expect(d2d4.childFen).toBe("fen-after-d4");
  });

  it("maps is_user_selected onto api nodes and defaults the root to false", () => {
    const view = buildTreeView(
      makeResponse({
        canonical_line: ["e2e4", "a7a6"],
        columns: [
          makeColumn(0, [makeNode({ uci: "e2e4", ply: 1 })], "e2e4"),
          makeColumn(
            1,
            [
              makeNode({
                uci: "a7a6",
                ply: 2,
                is_user_selected: true,
                is_navigable: true,
              }),
            ],
            "a7a6",
          ),
        ],
      }),
      {
        selectionLine: ["e2e4", "a7a6"],
        loadedThroughPly: 2,
        isExactResponseLine: true,
      },
    );
    // The third-type node IS the selected move of its column → kept + flagged.
    const a7a6 = view.columns[2].nodes.find((n) => n.uci === "a7a6")!;
    expect(a7a6.isUserSelected).toBe(true);
    expect(a7a6.isSelected).toBe(true);
    // The synthesized root is never user-selected.
    expect(view.columns[0].nodes[0].isUserSelected).toBe(false);
  });

  it("omits a user-selected node that is not the column's selected move (line-scope)", () => {
    // A deeper cached/provisional response can carry a third-type sibling that
    // has left the line; it must not render as a navigable child of a shorter
    // prefix (g-obh5 line-scope invariant).
    const view = buildTreeView(
      makeResponse({
        canonical_line: ["e2e4"],
        columns: [
          makeColumn(
            0,
            [
              makeNode({ uci: "e2e4", ply: 1 }),
              makeNode({
                uci: "a2a3",
                ply: 1,
                is_user_selected: true,
                is_navigable: true,
              }),
            ],
            "e2e4",
          ),
        ],
      }),
      { selectionLine: ["e2e4"], loadedThroughPly: 1, isExactResponseLine: true },
    );
    // a2a3 is stale here (selectedUci is e2e4) → dropped; e2e4 stays.
    expect(view.columns[1].nodes.map((n) => n.uci)).toEqual(["e2e4"]);
  });

  it("derives the board from the effective line, not selected_fen", () => {
    const view = buildTreeView(
      makeResponse({
        canonical_line: ["e2e4"],
        selected_fen: "should-not-be-used",
        columns: [makeColumn(0, [makeNode({ uci: "e2e4", ply: 1 })], "e2e4")],
      }),
      {
        selectionLine: ["e2e4"],
        loadedThroughPly: 1,
        isExactResponseLine: true,
      },
    );
    expect(view.board.fen).toMatch(
      /^rnbqkbnr\/pppppppp\/8\/8\/4P3\/8\/PPPP1PPP\/RNBQKBNR b/,
    );
    expect(view.board.lastMove).toEqual({ from: "e2", to: "e4" });
    expect(view.selectionLine).toEqual(["e2e4"]);
  });
});

describe("connectorStyle", () => {
  // connectorStyle reads encounterCount (width) and the move-type flags
  // (variant); dashing is a render-time, measured concern (an endpoint scrolled
  // off-screen), not a model property.
  const edge = (
    encounterCount: number,
    flags: Partial<DisplayNode> = {},
  ): DisplayNode =>
    ({ encounterCount, ...flags }) as unknown as DisplayNode;

  it("returns a base-width default pointer for a null (frontier) child", () => {
    expect(connectorStyle(null)).toEqual({ width: 2, variant: "default" });
  });

  it("grows width with encounter count", () => {
    // 2 + log2(7 + 1) = 2 + 3 = 5.
    expect(connectorStyle(edge(7)).width).toBe(5);
  });

  it("clamps width into [2, 6] across encounter counts", () => {
    expect(connectorStyle(edge(0)).width).toBe(2);
    expect(connectorStyle(edge(100_000)).width).toBe(6);
  });

  it("recolours only the third (selected) type; book/observed share default", () => {
    expect(connectorStyle(edge(0)).variant).toBe("default");
    // Observed is conveyed by width, not colour — it stays the default variant.
    expect(connectorStyle(edge(0, { isObserved: true })).variant).toBe(
      "default",
    );
    expect(connectorStyle(edge(0, { isUserSelected: true })).variant).toBe(
      "selected",
    );
  });
});

describe("replayLine", () => {
  it("returns the start position with no last move for an empty line", () => {
    const board = replayLine([]);
    expect(board.fen.startsWith(START_FEN.split(" ").slice(0, 1).join(""))).toBe(
      true,
    );
    expect(board.fen).toMatch(/^rnbqkbnr\/pppppppp\/8\/8\/8\/8\/PPPPPPPP/);
    expect(board.lastMove).toBeNull();
  });

  it("replays a line and reports the final last-move squares", () => {
    const board = replayLine(["e2e4", "e7e5"]);
    expect(board.fen).toMatch(/^rnbqkbnr\/pppp1ppp\/8\/4p3\/4P3\/8\/PPPP1PPP/);
    expect(board.lastMove).toEqual({ from: "e7", to: "e5" });
  });

  it("stops at the first illegal move", () => {
    const board = replayLine(["e2e4", "e2e4"]);
    // Second e2e4 is illegal; board halts after the first move.
    expect(board.fen).toMatch(/4P3.*b/);
    expect(board.lastMove).toEqual({ from: "e2", to: "e4" });
  });
});

describe("resolveDrop", () => {
  it("returns the uci for a legal move", () => {
    expect(resolveDrop(START_FEN, "e2", "e4")).toBe("e2e4");
  });

  it("returns null for an illegal move", () => {
    expect(resolveDrop(START_FEN, "e2", "e5")).toBeNull();
  });

  it("appends the queen promotion suffix", () => {
    const promoFen = "7k/P7/8/8/8/8/8/7K w - - 0 1";
    expect(resolveDrop(promoFen, "a7", "a8")).toBe("a7a8q");
  });
});
