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

function renderLineage(lineage: OpeningLineageItem[]) {
  return render(
    <MemoryRouter>
      <GameOpeningLineage playerColor="white" lineage={lineage} />
    </MemoryRouter>,
  );
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

  it("links to the opening page with correct color, opening, and repeated path", () => {
    renderLineage([
      makeItem({
        opening_key: "deep-key",
        opening_name: "Berlin Defense",
        path: ["k1", "k2"],
      }),
    ]);

    const link = screen.getByRole("link", { name: /^Berlin Defense/ });
    expect(link).toHaveAttribute(
      "href",
      "/openings?color=white&opening=deep-key&path=k1&path=k2",
    );
  });

  it("toggles the compact card via the expand button (sibling controls)", async () => {
    const user = userEvent.setup();
    renderLineage([makeItem({ opening_key: "k1", opening_name: "Ruy Lopez" })]);

    const expandButton = screen.getByRole("button", { name: /Show Ruy Lopez/ });
    expect(expandButton).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("lineage-board")).not.toBeInTheDocument();

    await user.click(expandButton);

    expect(
      screen.getByRole("button", { name: /Hide Ruy Lopez/ }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("lineage-board")).toHaveAttribute(
      "data-position",
      "k1",
    );

    await user.click(screen.getByRole("button", { name: /Hide Ruy Lopez/ }));
    expect(screen.queryByTestId("lineage-board")).not.toBeInTheDocument();
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
