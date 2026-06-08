import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import GameOpeningLineage from "./GameOpeningLineage";
import type { OpeningLineageItem } from "../utils/api";

vi.mock("react-chessboard", () => ({
  Chessboard: ({ options }: { options: Record<string, unknown> }) => (
    <div data-testid="lineage-board" data-position={options.position as string} />
  ),
}));

function makeItem(overrides: Partial<OpeningLineageItem>): OpeningLineageItem {
  return {
    opening_key: "key",
    opening_name: "Opening",
    opening_family: "Family",
    eco: null,
    depth: 0,
    score: 60,
    confidence: 0.5,
    coverage: 0.5,
    sample_size: 5,
    path: [],
    ...overrides,
  };
}

function renderLineage(
  lineage: OpeningLineageItem[],
  handlers: {
    onSelectRoot?: (item: OpeningLineageItem) => void;
    onStartDrill?: (item: OpeningLineageItem) => void;
  } = {},
) {
  const onSelectRoot = handlers.onSelectRoot ?? vi.fn();
  const onStartDrill = handlers.onStartDrill ?? vi.fn();
  const utils = render(
    <MemoryRouter>
      <GameOpeningLineage
        playerColor="white"
        lineage={lineage}
        onSelectRoot={onSelectRoot}
        onStartDrill={onStartDrill}
      />
    </MemoryRouter>,
  );
  return { ...utils, onSelectRoot, onStartDrill };
}

describe("GameOpeningLineage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders nothing for an empty lineage", () => {
    const { container } = renderLineage([]);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders chips broadest -> deepest in order", () => {
    renderLineage([
      makeItem({ opening_key: "k1", opening_name: "Open Game" }),
      makeItem({ opening_key: "k2", opening_name: "Ruy Lopez", depth: 1 }),
      makeItem({ opening_key: "k3", opening_name: "Berlin Defense", depth: 2 }),
    ]);

    const groups = screen.getAllByRole("group");
    expect(groups.map((g) => g.getAttribute("aria-label"))).toEqual([
      "Open Game",
      "Ruy Lopez",
      "Berlin Defense",
    ]);
  });

  it("links to the opening page from inside the expanded card", async () => {
    const user = userEvent.setup();
    renderLineage([
      makeItem({
        opening_key: "deep-key",
        opening_name: "Berlin Defense",
        path: ["k1", "k2"],
      }),
    ]);

    // No link is visible until the card is expanded.
    expect(screen.queryByRole("link")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /Select Berlin Defense/ }),
    );

    const link = screen.getByRole("link", { name: /View in Openings/ });
    expect(link).toHaveAttribute(
      "href",
      "/openings?color=white&opening=deep-key&path=k1&path=k2",
    );
  });

  it("single click expands the card AND fires onSelectRoot; 2nd click collapses", async () => {
    const user = userEvent.setup();
    const item = makeItem({ opening_key: "k1", opening_name: "Ruy Lopez" });
    const onSelectRoot = vi.fn();
    const { onStartDrill } = renderLineage([item], { onSelectRoot });

    const toggle = screen.getByRole("button", { name: /Select Ruy Lopez/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("lineage-board")).not.toBeInTheDocument();

    await user.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(onSelectRoot).toHaveBeenCalledTimes(1);
    expect(onSelectRoot).toHaveBeenCalledWith(item);
    expect(screen.getByTestId("lineage-board")).toHaveAttribute(
      "data-position",
      "k1",
    );
    // The link + Start Drill only exist inside the expanded card.
    expect(
      screen.getByRole("link", { name: /View in Openings/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Start Drill/ }),
    ).toBeInTheDocument();

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("lineage-board")).not.toBeInTheDocument();
    // onSelectRoot fires on every click, including the collapsing one.
    expect(onSelectRoot).toHaveBeenCalledTimes(2);
    expect(onStartDrill).not.toHaveBeenCalled();
  });

  it("fires onStartDrill from the Start Drill button inside the card", async () => {
    const user = userEvent.setup();
    const item = makeItem({ opening_key: "k1", opening_name: "Ruy Lopez" });
    const onStartDrill = vi.fn();
    renderLineage([item], { onStartDrill });

    await user.click(screen.getByRole("button", { name: /Select Ruy Lopez/ }));
    await user.click(screen.getByRole("button", { name: /Start Drill/ }));

    expect(onStartDrill).toHaveBeenCalledTimes(1);
    expect(onStartDrill).toHaveBeenCalledWith(item);
  });

  it("shows muted tone and em-dash score for a null-score opening", () => {
    renderLineage([
      makeItem({ opening_key: "k1", opening_name: "Unknown", score: null }),
    ]);

    const group = screen.getByRole("group", { name: "Unknown" });
    expect(group.className).toContain("game-opening-chip--muted");
    expect(within(group).getByText("—")).toBeInTheDocument();
  });
});
