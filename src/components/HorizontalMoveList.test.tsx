import { describe, expect, it, vi } from "vitest";
import { render, fireEvent, within } from "@testing-library/react";
import HorizontalMoveList from "./HorizontalMoveList";
import type { Move, MoveListProps } from "./MoveList";
import type { MoveMessage } from "./MoveRow";
import type { VariationTree, VarNode } from "../types/variationTree";

const MOVES: Move[] = [
  { san: "e4" },
  { san: "c6" },
  { san: "Bc4" },
  { san: "d5" },
  { san: "exd5" },
  { san: "cxd5" },
];

function renderList(overrides: Partial<MoveListProps> = {}) {
  const onNavigate = vi.fn();
  const utils = render(
    <HorizontalMoveList moves={MOVES} currentIndex={null} onNavigate={onNavigate} {...overrides} />,
  );
  return { ...utils, onNavigate };
}

describe("HorizontalMoveList", () => {
  it("renders SAN tokens with move numbers and no periods", () => {
    const { container } = renderList();
    const nums = Array.from(container.querySelectorAll(".h-move-num")).map((n) => n.textContent);
    expect(nums).toEqual(["1", "2", "3"]);
    const sans = Array.from(container.querySelectorAll(".h-move-san")).map((n) => n.textContent);
    expect(sans).toEqual(["e4", "c6", "Bc4", "d5", "exd5", "cxd5"]);
  });

  it("highlights the selected move", () => {
    const { container } = renderList({ currentIndex: 2 });
    const selected = container.querySelectorAll(".h-move.selected");
    expect(selected).toHaveLength(1);
    expect(selected[0].textContent).toContain("Bc4");
  });

  it("navigates on move click (last move → null)", () => {
    const { container, onNavigate } = renderList({ currentIndex: 0 });
    const buttons = container.querySelectorAll(".h-move");
    fireEvent.click(buttons[2]); // Bc4 (index 2)
    expect(onNavigate).toHaveBeenCalledWith(2);
    fireEvent.click(buttons[5]); // last move → null
    expect(onNavigate).toHaveBeenCalledWith(null);
  });

  it("disables prev arrow at the start and next at the end", () => {
    const atStart = renderList({ currentIndex: -1 });
    const startQ = within(atStart.container);
    expect((startQ.getByLabelText("Previous move") as HTMLButtonElement).disabled).toBe(true);
    expect((startQ.getByLabelText("Next move") as HTMLButtonElement).disabled).toBe(false);
    atStart.unmount();

    const atEnd = renderList({ currentIndex: null });
    const endQ = within(atEnd.container);
    expect((endQ.getByLabelText("Next move") as HTMLButtonElement).disabled).toBe(true);
    expect((endQ.getByLabelText("Previous move") as HTMLButtonElement).disabled).toBe(false);
  });

  it("arrows call navigate", () => {
    const { getByLabelText, onNavigate } = renderList({ currentIndex: 2 });
    fireEvent.click(getByLabelText("Previous move"));
    expect(onNavigate).toHaveBeenCalledWith(1);
    fireEvent.click(getByLabelText("Next move"));
    expect(onNavigate).toHaveBeenCalledWith(3);
  });

  it("renders badge icons and analysis spinners", () => {
    const classified: Move[] = [{ san: "e4", classification: "best" }, { san: "c6" }];
    const { container } = render(
      <HorizontalMoveList
        moves={classified}
        currentIndex={null}
        onNavigate={vi.fn()}
        analyzingIndices={new Set([1])}
      />,
    );
    expect(container.querySelector(".h-move-badge")).not.toBeNull();
    expect(container.querySelector(".h-move-spinner")).not.toBeNull();
  });

  it("fires onFreshAnimationDone for a fresh best move", () => {
    const onFresh = vi.fn();
    const classified: Move[] = [{ san: "e4", classification: "best" }];
    const { container } = render(
      <HorizontalMoveList
        moves={classified}
        currentIndex={null}
        onNavigate={vi.fn()}
        freshlyResolvedIndices={new Set([0])}
        onFreshAnimationDone={onFresh}
      />,
    );
    const icon = container.querySelector(".h-move-badge")!;
    fireEvent.animationEnd(icon);
    expect(onFresh).toHaveBeenCalledWith(0);
  });

  it("autoscrolls to the latest move when a new move is appended", () => {
    const onNavigate = vi.fn();
    const { container, rerender } = render(
      <HorizontalMoveList moves={MOVES.slice(0, 4)} currentIndex={null} onNavigate={onNavigate} />,
    );
    const strip = container.querySelector(".h-move-list__strip") as HTMLDivElement;
    Object.defineProperty(strip, "scrollWidth", { value: 999, configurable: true });
    strip.scrollLeft = 0;
    rerender(
      <HorizontalMoveList moves={MOVES} currentIndex={null} onNavigate={onNavigate} />,
    );
    expect(strip.scrollLeft).toBe(999);
  });

  it("does not autoscroll on navigation (no new move)", () => {
    const onNavigate = vi.fn();
    const { container, rerender } = render(
      <HorizontalMoveList moves={MOVES} currentIndex={null} onNavigate={onNavigate} />,
    );
    const strip = container.querySelector(".h-move-list__strip") as HTMLDivElement;
    Object.defineProperty(strip, "scrollWidth", { value: 999, configurable: true });
    strip.scrollLeft = 42;
    rerender(<HorizontalMoveList moves={MOVES} currentIndex={1} onNavigate={onNavigate} />);
    expect(strip.scrollLeft).toBe(42);
  });

  it("renders relocated material in the controls row when supplied", () => {
    const { container } = renderList({
      materialFen: "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
      materialPerspective: "white",
    });
    const material = container.querySelector(".controls-row__material");
    expect(material).not.toBeNull();
    expect(material!.querySelector(".material-display")).not.toBeNull();
  });

  it("opens a popup on the message badge and dismisses on navigation", () => {
    const msgs: MoveMessage[] = [{ key: "m1", variant: "srs-pass", text: "Nice!" }];
    const messages = new Map<number, MoveMessage[]>([[0, msgs]]);
    const { container, getByLabelText } = render(
      <HorizontalMoveList
        moves={MOVES}
        currentIndex={1}
        onNavigate={vi.fn()}
        messages={messages}
      />,
    );
    fireEvent.click(container.querySelector(".h-move-msg")!);
    const popup = document.querySelector(".h-move-popup");
    expect(popup).not.toBeNull();
    expect(within(popup as HTMLElement).getByText("Nice!")).toBeTruthy();
    // Navigate → dismiss
    fireEvent.click(getByLabelText("Previous move"));
    expect(document.querySelector(".h-move-popup")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Variation mode
// ---------------------------------------------------------------------------

function makeTree(parentGameIndex: number): {
  tree: VariationTree;
  nodeId: string;
} {
  const node: VarNode = {
    id: "v1",
    san: "Nf3",
    fen: "",
    fenBefore: "",
    uci: "g1f3",
    parentId: null,
    parentGameIndex,
    branchPlyOffset: 0,
    children: [],
    nestingLevel: 0,
  };
  const tree: VariationTree = {
    nodes: new Map([["v1", node]]),
    rootBranches: new Map([[parentGameIndex, ["v1"]]]),
  };
  return { tree, nodeId: "v1" };
}

describe("HorizontalMoveList — variations", () => {
  it("highlights the branch-point and does not render variation lines", () => {
    const { tree, nodeId } = makeTree(2);
    const { container } = render(
      <HorizontalMoveList
        moves={MOVES}
        currentIndex={2}
        onNavigate={vi.fn()}
        variationTree={tree}
        selectedVarNodeId={nodeId}
        onVarSelect={vi.fn()}
        navigateUp={vi.fn()}
        navigateDown={vi.fn(() => null)}
      />,
    );
    const branch = container.querySelectorAll(".h-move.branch-point");
    expect(branch).toHaveLength(1);
    expect(branch[0].textContent).toContain("Bc4"); // index 2
    // No variation SAN ("Nf3") rendered in the strip
    expect(container.textContent).not.toContain("Nf3");
  });

  it("does not apply branch-point when parentGameIndex is -1", () => {
    const { tree, nodeId } = makeTree(-1);
    const { container } = render(
      <HorizontalMoveList
        moves={MOVES}
        currentIndex={-1}
        onNavigate={vi.fn()}
        variationTree={tree}
        selectedVarNodeId={nodeId}
        onVarSelect={vi.fn()}
        navigateUp={vi.fn()}
        navigateDown={vi.fn(() => null)}
      />,
    );
    expect(container.querySelectorAll(".h-move.branch-point")).toHaveLength(0);
  });

  it("uses navigateUp / navigateDown in variation mode", () => {
    const { tree, nodeId } = makeTree(2);
    const navigateUp = vi.fn(() => null);
    const navigateDown = vi.fn(() => "v2");
    const { getByLabelText } = render(
      <HorizontalMoveList
        moves={MOVES}
        currentIndex={2}
        onNavigate={vi.fn()}
        variationTree={tree}
        selectedVarNodeId={nodeId}
        onVarSelect={vi.fn()}
        navigateUp={navigateUp}
        navigateDown={navigateDown}
      />,
    );
    fireEvent.click(getByLabelText("Previous move"));
    expect(navigateUp).toHaveBeenCalledWith("v1");
    fireEvent.click(getByLabelText("Next move"));
    expect(navigateDown).toHaveBeenCalledWith("v1");
  });

  describe("return-to-live emphasis (g-1y68 A2, mobile)", () => {
    it("shows a return-to-live button with a LIVE label while reviewing a live game", () => {
      const { getByLabelText, getByText } = renderList({
        currentIndex: 0,
        isGameActive: true,
      });
      const button = getByLabelText("Return to live") as HTMLButtonElement;
      expect(button).toBeTruthy();
      expect(button.className).toContain("h-move-arrow--return-live");
      expect(getByText("LIVE")).toBeTruthy();
    });

    it("does not show the button at the latest move or when the game is over", () => {
      const atLatest = renderList({ currentIndex: null, isGameActive: true });
      expect(within(atLatest.container).queryByLabelText("Return to live")).toBeNull();
      atLatest.unmount();

      const gameOver = renderList({ currentIndex: 0, isGameActive: false });
      expect(within(gameOver.container).queryByLabelText("Return to live")).toBeNull();
    });

    it("returns to the latest move when the button is clicked", () => {
      const { getByLabelText, onNavigate } = renderList({
        currentIndex: 0,
        isGameActive: true,
      });
      fireEvent.click(getByLabelText("Return to live"));
      expect(onNavigate).toHaveBeenCalledWith(null);
    });
  });
});
