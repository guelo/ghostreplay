import { act, useState } from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "../../../test/utils";
import type { OpeningsTreeExplorerProps } from "../../OpeningsTreeExplorer";
import type { OpeningRootItem } from "../../../utils/api";

const captureEventMock = vi.fn();
let explorerProps: OpeningsTreeExplorerProps | null = null;

const treeTarget = {
  targetFen: "target-fen",
  line: ["e2e4", "c7c5"],
  displayName: "Sicilian Defense",
  eco: "B20",
};

vi.mock("../../../analytics/posthog", () => ({
  captureEvent: (...args: unknown[]) => captureEventMock(...args),
}));

vi.mock("../../OpeningsTreeExplorer", () => ({
  default: (props: OpeningsTreeExplorerProps) => {
    explorerProps = props;
    return (
      <div data-testid="tree-explorer">
        <output data-testid="tree-route">
          {JSON.stringify(props.route)}
        </output>
        <button
          type="button"
          onClick={() => props.onSelectLine(["e2e4"])}
        >
          Explore e4
        </button>
        <button
          type="button"
          onClick={() => props.onCanonicalLine?.(["e2e4", "c7c5"])}
        >
          Adopt canonical line
        </button>
        <button
          type="button"
          onClick={() => props.expandedAction?.onSelect(treeTarget)}
        >
          {props.expandedAction?.label}
        </button>
        <select aria-label="Tree order" defaultValue="score">
          <option value="score">Score</option>
        </select>
        <button type="button" hidden>
          Hidden tree action
        </button>
      </div>
    );
  },
}));

import OpeningPicker, { type OpeningPickerSelection } from "./OpeningPicker";

const KINGS_PAWN: OpeningRootItem = {
  opening_key: "kings-pawn-fen",
  opening_name: "King's Pawn Game",
  opening_family: "King's Pawn",
  eco: "B00",
  depth: 1,
};

const CARO_KANN: OpeningRootItem = {
  opening_key: "caro-kann-fen",
  opening_name: "Caro-Kann Defense",
  opening_family: "King's Pawn",
  eco: "B10",
  depth: 2,
};

const QUEENS_PAWN: OpeningRootItem = {
  opening_key: "queens-pawn-fen",
  opening_name: "Queen's Pawn Game",
  opening_family: "Queen's Pawn",
  eco: "A40",
  depth: 1,
};

const makeFamilies = () => [
  { family_name: "King's Pawn", roots: [KINGS_PAWN, CARO_KANN] },
  { family_name: "Queen's Pawn", roots: [QUEENS_PAWN] },
];

function renderPicker(
  overrides: Partial<React.ComponentProps<typeof OpeningPicker>> = {},
) {
  const onSelect = vi.fn();
  const onPlayerColorChange = vi.fn();
  const props: React.ComponentProps<typeof OpeningPicker> = {
    openingFamilies: makeFamilies(),
    selectedOpening: null,
    selectedLine: null,
    playerColor: "white",
    onSelect,
    onPlayerColorChange,
    ...overrides,
  };
  const result = render(<OpeningPicker {...props} />);
  return { ...result, onSelect, onPlayerColorChange, props };
}

function ControlledPicker({
  selectedOpening = null,
  selectedLine = null,
  onSelect,
  onPlayerColorChange,
}: {
  selectedOpening?: OpeningRootItem | null;
  selectedLine?: string[] | null;
  onSelect: (selection: OpeningPickerSelection) => void;
  onPlayerColorChange: (color: "white" | "black") => void;
}) {
  const [playerColor, setPlayerColor] = useState<"white" | "black">("white");

  return (
    <OpeningPicker
      openingFamilies={makeFamilies()}
      selectedOpening={selectedOpening}
      selectedLine={selectedLine}
      playerColor={playerColor}
      onSelect={onSelect}
      onPlayerColorChange={(color) => {
        onPlayerColorChange(color);
        setPlayerColor(color);
      }}
    />
  );
}

function openList() {
  fireEvent.click(screen.getByRole("combobox"));
}

function openTree() {
  openList();
  fireEvent.click(screen.getByRole("tab", { name: "Tree" }));
}

describe("OpeningPicker", () => {
  beforeEach(() => {
    captureEventMock.mockReset();
    explorerProps = null;
    document.body.style.overflow = "";
  });

  it("keeps List compact, searchable, keyboard-selectable, and returns line null", () => {
    const { onSelect } = renderPicker();
    openList();
    fireEvent.change(screen.getByPlaceholderText(/search openings/i), {
      target: { value: "caro" },
    });

    expect(
      screen.getByRole("option", { name: /Caro-Kann/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("option", { name: /Queen's Pawn/ }),
    ).not.toBeInTheDocument();

    fireEvent.keyDown(screen.getByPlaceholderText(/search openings/i), {
      key: "Enter",
    });
    expect(onSelect).toHaveBeenCalledWith({
      opening: CARO_KANN,
      line: null,
    });
    expect(captureEventMock).toHaveBeenCalledWith(
      "drill_opening_selected",
      {
        source: "list",
        opening_key: CARO_KANN.opening_key,
        depth: CARO_KANN.depth,
        player_color: "white",
      },
    );
  });

  it("moves the active List row with arrow keys", () => {
    const { onSelect } = renderPicker();
    openList();
    const search = screen.getByPlaceholderText(/search openings/i);
    fireEvent.keyDown(search, { key: "ArrowDown" });
    fireEvent.keyDown(search, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith({
      opening: CARO_KANN,
      line: null,
    });
  });

  it("renders the List in a body portal and closes on outside click", () => {
    const { container } = renderPicker();
    openList();
    const listbox = screen.getByRole("listbox");
    expect(container.contains(listbox)).toBe(false);

    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("opens Tree as a modal dialog, locks scroll, and restores trigger focus", () => {
    renderPicker();
    const trigger = screen.getByRole("combobox");
    openTree();

    expect(
      screen.getByRole("dialog", {
        name: "Choose an opening from the tree",
      }),
    ).toHaveAttribute("aria-modal", "true");
    expect(document.body.style.overflow).toBe("hidden");
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.body.style.overflow).toBe("");
    expect(trigger).toHaveFocus();
  });

  it("reopens in the compact List after Tree is dismissed", () => {
    renderPicker();
    openTree();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));

    fireEvent.click(screen.getByRole("combobox"));
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "List" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("capture-phase Escape closes only the picker layer", () => {
    const outerKeyDown = vi.fn();
    render(
      <div onKeyDown={outerKeyDown}>
        <OpeningPicker
          openingFamilies={makeFamilies()}
          selectedOpening={null}
          selectedLine={null}
          playerColor="white"
          onSelect={vi.fn()}
          onPlayerColorChange={vi.fn()}
        />
      </div>,
    );
    openTree();
    fireEvent.keyDown(document.activeElement ?? document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(outerKeyDown).not.toHaveBeenCalled();
    expect(screen.getByRole("combobox")).toHaveFocus();
  });

  it("closes Tree from the backdrop without committing", () => {
    const { onSelect } = renderPicker();
    openTree();
    fireEvent.mouseDown(
      document.querySelector(".opening-picker__tree-backdrop")!,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it.each([
    {
      name: "an exact ad-hoc line",
      selectedOpening: CARO_KANN,
      selectedLine: ["d2d4", "d7d5"],
      expected: {
        playerColor: "black",
        moves: ["d2d4", "d7d5"],
        opening: null,
      },
    },
    {
      name: "a registered FEN",
      selectedOpening: CARO_KANN,
      selectedLine: null,
      expected: {
        playerColor: "black",
        moves: [],
        opening: CARO_KANN.opening_key,
      },
    },
    {
      name: "the repertoire root",
      selectedOpening: null,
      selectedLine: null,
      expected: { playerColor: "black", moves: [], opening: null },
    },
  ])("seeds Tree from $name", ({ selectedOpening, selectedLine, expected }) => {
    renderPicker({
      selectedOpening,
      selectedLine,
      playerColor: "black",
    });
    openTree();
    expect(explorerProps?.route).toEqual(expected);
  });

  it("adopts a registered FEN's canonical line only inside the modal", () => {
    const { onSelect } = renderPicker({
      selectedOpening: CARO_KANN,
      selectedLine: null,
    });
    openTree();
    expect(explorerProps?.route.opening).toBe(CARO_KANN.opening_key);

    fireEvent.click(
      screen.getByRole("button", { name: "Adopt canonical line" }),
    );
    expect(explorerProps?.route).toEqual({
      playerColor: "white",
      moves: ["e2e4", "c7c5"],
      opening: null,
    });
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("keeps exploration tentative and discards it when switching to List", () => {
    const { onSelect } = renderPicker();
    openTree();
    fireEvent.click(screen.getByRole("button", { name: "Explore e4" }));
    expect(explorerProps?.route.moves).toEqual(["e2e4"]);
    expect(onSelect).not.toHaveBeenCalled();
    expect(captureEventMock).toHaveBeenCalledWith("opening_explored", {
      source: "drill_picker",
      from_key: "",
      to_key: "e2e4",
      depth: 1,
      player_color: "white",
    });

    fireEvent.click(screen.getByRole("tab", { name: "List" }));
    fireEvent.click(screen.getByRole("tab", { name: "Tree" }));
    expect(explorerProps?.route).toEqual({
      playerColor: "white",
      moves: [],
      opening: null,
    });
  });

  it("preserves a tentative line and uses the synchronized color for Tree events", () => {
    const onSelect = vi.fn();
    const onPlayerColorChange = vi.fn();
    render(
      <ControlledPicker
        onSelect={onSelect}
        onPlayerColorChange={onPlayerColorChange}
      />,
    );
    openTree();
    fireEvent.click(screen.getByRole("button", { name: "Explore e4" }));

    const sideToggle = screen.getByRole("group", { name: "Playing as" });
    fireEvent.click(within(sideToggle).getByRole("button", { name: "Black" }));

    expect(onPlayerColorChange).toHaveBeenCalledWith("black");
    expect(explorerProps?.route).toEqual({
      playerColor: "black",
      moves: ["e2e4"],
      opening: null,
    });
    expect(
      within(sideToggle).getByRole("button", { name: "Black" }),
    ).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: "Explore e4" }));
    expect(captureEventMock).toHaveBeenLastCalledWith("opening_explored", {
      source: "drill_picker",
      from_key: "e2e4",
      to_key: "e2e4",
      depth: 1,
      player_color: "black",
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Use this opening" }),
    );
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ line: treeTarget.line }),
    );
    expect(captureEventMock).toHaveBeenLastCalledWith(
      "drill_opening_selected",
      expect.objectContaining({ player_color: "black" }),
    );
  });

  it("preserves an unresolved registered opening across a Tree color switch", () => {
    const onPlayerColorChange = vi.fn();
    render(
      <ControlledPicker
        selectedOpening={CARO_KANN}
        selectedLine={null}
        onSelect={vi.fn()}
        onPlayerColorChange={onPlayerColorChange}
      />,
    );
    openTree();
    expect(explorerProps?.route.opening).toBe(CARO_KANN.opening_key);

    fireEvent.click(screen.getByRole("button", { name: "Black" }));

    expect(onPlayerColorChange).toHaveBeenCalledWith("black");
    expect(explorerProps?.route).toEqual({
      playerColor: "black",
      moves: [],
      opening: CARO_KANN.opening_key,
    });
  });

  it("confirms Tree with a synthetic opening and copied exact line", () => {
    const { onSelect } = renderPicker({ playerColor: "black" });
    openTree();
    fireEvent.click(
      screen.getByRole("button", { name: "Use this opening" }),
    );

    expect(onSelect).toHaveBeenCalledWith({
      opening: {
        opening_key: treeTarget.targetFen,
        opening_name: treeTarget.displayName,
        opening_family: "",
        eco: treeTarget.eco,
        depth: treeTarget.line.length,
      },
      line: treeTarget.line,
    });
    expect(onSelect.mock.calls[0]?.[0].line).not.toBe(treeTarget.line);
    expect(captureEventMock).toHaveBeenCalledWith(
      "drill_opening_selected",
      {
        source: "tree",
        opening_key: treeTarget.targetFen,
        depth: treeTarget.line.length,
        player_color: "black",
      },
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("combobox")).toHaveFocus();
  });

  it("traps focus within the Tree dialog", () => {
    renderPicker();
    openTree();
    const first = screen.getByRole("tab", { name: "List" });
    first.focus();

    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(screen.getByRole("combobox", { name: "Tree order" })).toHaveFocus();
    expect(screen.getByText("Hidden tree action")).not.toHaveFocus();
  });

  it("shows failed/loading trigger states and preserves a selected label", () => {
    const { rerender } = renderPicker({
      openingFamilies: null,
      isLoading: true,
    });
    expect(screen.getByRole("combobox")).toBeDisabled();
    expect(screen.getByText(/loading openings/i)).toBeInTheDocument();

    rerender(
      <OpeningPicker
        openingFamilies={null}
        selectedOpening={CARO_KANN}
        selectedLine={null}
        playerColor="white"
        isLoading={false}
        onSelect={vi.fn()}
        onPlayerColorChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("combobox")).toBeDisabled();
    expect(screen.getByText(/Caro-Kann Defense/i)).toBeInTheDocument();
    expect(screen.queryByText(/failed to load openings/i)).not.toBeInTheDocument();
  });

  it("closes List on Escape and restores focus", () => {
    renderPicker();
    const trigger = screen.getByRole("combobox");
    openList();
    act(() => fireEvent.keyDown(document, { key: "Escape" }));
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});
