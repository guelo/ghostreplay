import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "../test/utils";
import type { UseOpeningsTreeResult } from "../hooks/useOpeningsTree";
import type {
  DisplayNode,
  TreeView,
} from "../openings/treeView";
import type { OpeningTreeNodeView } from "./OpeningTreeNodeCard";

const mocks = vi.hoisted(() => ({
  useOpeningsTree: vi.fn(),
  retry: vi.fn(),
}));

vi.mock("../hooks/useOpeningsTree", () => ({
  useOpeningsTree: (...args: unknown[]) => mocks.useOpeningsTree(...args),
}));

vi.mock("../openings/useTreeConnectors", () => ({
  useTreeConnectors: () => [],
}));

vi.mock("react-chessboard", () => ({
  Chessboard: ({ options }: { options: Record<string, unknown> }) => (
    <div
      data-testid="explorer-board"
      data-board-id={options.id as string}
      data-position={options.position as string}
      data-orientation={options.boardOrientation as string}
    />
  ),
}));

import OpeningsTreeExplorer from "./OpeningsTreeExplorer";

const rootView: OpeningTreeNodeView = {
  ply: 0,
  san: null,
  openingName: null,
  eco: null,
  inBook: true,
  isUserSelected: false,
  isTransposition: false,
  score: null,
  evalCp: 15,
  evalMate: null,
  coverage: null,
  gameCount: null,
  isTerminal: false,
  terminalReason: null,
  drillOpeningKey: null,
  moveListSan: [],
  moveListStartPly: 1,
};

const moveView: OpeningTreeNodeView = {
  ...rootView,
  ply: 1,
  san: "e4",
  openingName: "King's Pawn Game",
  eco: "B00",
  score: 61,
  coverage: 0.5,
  gameCount: 12,
  moveListSan: ["e4"],
};

function displayNode(
  overrides: Partial<DisplayNode> & Pick<DisplayNode, "key" | "view">,
): DisplayNode {
  return {
    uci: null,
    childFen: null,
    isSelected: false,
    isExpanded: false,
    isNavigable: true,
    isSelectable: true,
    selectLine: [],
    inBook: true,
    isObserved: true,
    isUserSelected: false,
    encounterCount: 1,
    ...overrides,
  };
}

const deepView: TreeView = {
  selectionLine: ["e2e4"],
  board: {
    fen: "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
    lastMove: { from: "e2", to: "e4" },
  },
  columns: [
    {
      kind: "root",
      lineIndex: -1,
      nodes: [
        displayNode({
          key: "root",
          view: rootView,
          isSelected: true,
          selectLine: [],
        }),
      ],
    },
    {
      kind: "moves",
      lineIndex: 0,
      nodes: [
        displayNode({
          key: "e2e4",
          view: moveView,
          uci: "e2e4",
          childFen: "target-fen",
          isSelected: true,
          isExpanded: true,
          selectLine: ["e2e4"],
        }),
      ],
    },
  ],
};

const rootOnlyView: TreeView = {
  selectionLine: [],
  board: {
    fen: "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    lastMove: null,
  },
  columns: [
    {
      kind: "root",
      lineIndex: -1,
      nodes: [
        displayNode({
          key: "root",
          view: rootView,
          isSelected: true,
          isExpanded: true,
          selectLine: [],
        }),
      ],
    },
  ],
};

function hookResult(
  overrides: Partial<UseOpeningsTreeResult> = {},
): UseOpeningsTreeResult {
  return {
    view: deepView,
    pageStatus: "ready",
    appendStatus: "idle",
    error: null,
    canonicalLine: ["e2e4"],
    isSettled: true,
    batchComputedAt: "2026-07-30T00:00:00Z",
    retry: mocks.retry,
    ...overrides,
  };
}

describe("OpeningsTreeExplorer", () => {
  beforeEach(() => {
    mocks.retry.mockReset();
    mocks.useOpeningsTree.mockReset();
    mocks.useOpeningsTree.mockReturnValue(hookResult());
  });

  it("reports a settled canonical line and delegates controlled navigation", async () => {
    const onSelectLine = vi.fn();
    const onCanonicalLine = vi.fn();
    render(
      <OpeningsTreeExplorer
        route={{
          playerColor: "white",
          moves: ["e2e4"],
          opening: null,
        }}
        onSelectLine={onSelectLine}
        onCanonicalLine={onCanonicalLine}
      />,
    );

    await waitFor(() =>
      expect(onCanonicalLine).toHaveBeenCalledWith(["e2e4"]),
    );
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(onSelectLine).toHaveBeenCalledWith([]);
  });

  it("renders the configured footer action with the exact target payload", () => {
    const onAction = vi.fn();
    render(
      <OpeningsTreeExplorer
        route={{
          playerColor: "white",
          moves: ["e2e4"],
          opening: null,
        }}
        onSelectLine={vi.fn()}
        expandedAction={{ label: "Use this opening", onSelect: onAction }}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Use this opening" }),
    );
    expect(onAction).toHaveBeenCalledWith({
      targetFen: "target-fen",
      line: ["e2e4"],
      displayName: "King's Pawn Game",
      eco: "B00",
    });
  });

  it("does not render an expanded action for the synthesized root", () => {
    mocks.useOpeningsTree.mockReturnValue(
      hookResult({
        view: rootOnlyView,
        canonicalLine: [],
      }),
    );
    render(
      <OpeningsTreeExplorer
        route={{ playerColor: "white", moves: [], opening: null }}
        onSelectLine={vi.fn()}
        expandedAction={{ label: "Start Drill", onSelect: vi.fn() }}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "Start Drill" }),
    ).not.toBeInTheDocument();
  });

  it("propagates full-load retry", () => {
    mocks.useOpeningsTree.mockReturnValue(
      hookResult({
        view: null,
        pageStatus: "error",
        error: "Tree unavailable",
        canonicalLine: null,
        isSettled: false,
      }),
    );
    render(
      <OpeningsTreeExplorer
        route={{ playerColor: "black", moves: [], opening: null }}
        onSelectLine={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(mocks.retry).toHaveBeenCalledTimes(1);
  });

  it("uses instance-local SVG marker ids", () => {
    render(
      <>
        <OpeningsTreeExplorer
          route={{ playerColor: "white", moves: ["e2e4"], opening: null }}
          onSelectLine={vi.fn()}
        />
        <OpeningsTreeExplorer
          route={{ playerColor: "white", moves: ["e2e4"], opening: null }}
          onSelectLine={vi.fn()}
        />
      </>,
    );
    const markerIds = Array.from(document.querySelectorAll("marker")).map(
      (marker) => marker.id,
    );
    expect(new Set(markerIds).size).toBe(markerIds.length);
    const boardIds = screen
      .getAllByTestId("explorer-board")
      .map((board) => board.dataset.boardId);
    expect(new Set(boardIds).size).toBe(boardIds.length);
  });
});
