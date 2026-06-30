import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import GameOpeningLineage from "./GameOpeningLineage";
import type { OpeningLineageItem, OpeningScoreDeltaItem } from "../utils/api";

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
    game_count: 2,
    path: [],
    ...overrides,
  };
}

function makeChange(
  overrides: Partial<OpeningScoreDeltaItem>,
): OpeningScoreDeltaItem {
  return {
    opening_key: "key",
    opening_name: "Opening",
    opening_family: "Family",
    eco: null,
    depth: 0,
    before: 41,
    after: 44,
    delta: 3,
    is_new: false,
    ...overrides,
  };
}

function renderLineage(
  lineage: OpeningLineageItem[],
  handlers: {
    onSelectRoot?: (item: OpeningLineageItem) => void;
    onStartDrill?: (item: OpeningLineageItem) => void;
    scoreChanges?: OpeningScoreDeltaItem[] | null;
  } = {},
) {
  const onSelectRoot = handlers.onSelectRoot ?? vi.fn();
  const onStartDrill = handlers.onStartDrill ?? vi.fn();
  const utils = render(
    <MemoryRouter>
      <GameOpeningLineage
        playerColor="white"
        lineage={lineage}
        scoreChanges={handlers.scoreChanges}
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

  it("renders cards broadest -> deepest in order", () => {
    renderLineage([
      makeItem({ opening_key: "k1", opening_name: "Open Game" }),
      makeItem({ opening_key: "k2", opening_name: "Ruy Lopez", depth: 1 }),
      makeItem({ opening_key: "k3", opening_name: "Berlin Defense", depth: 2 }),
    ]);

    const cards = screen.getAllByRole("button");
    expect(cards.map((c) => c.getAttribute("aria-label"))).toEqual([
      "Select Open Game and toggle details",
      "Select Ruy Lopez and toggle details",
      "Select Berlin Defense and toggle details",
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
      "/openings?color=white&opening=deep-key",
    );
  });

  it("expanding replaces the compact card with the expanded card and fires onSelectRoot once", async () => {
    const user = userEvent.setup();
    const item = makeItem({ opening_key: "k1", opening_name: "Ruy Lopez" });
    const onSelectRoot = vi.fn();
    const { onStartDrill } = renderLineage([item], { onSelectRoot });

    const toggle = screen.getByRole("button", { name: /Select Ruy Lopez/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await user.click(toggle);

    expect(onSelectRoot).toHaveBeenCalledTimes(1);
    expect(onSelectRoot).toHaveBeenCalledWith(item);
    // The collapsed card is gone, replaced by the expanded card.
    expect(
      screen.queryByRole("button", { name: /Select Ruy Lopez/ }),
    ).not.toBeInTheDocument();
    // The link + Start Drill only exist in/around the expanded card.
    expect(
      screen.getByRole("link", { name: /View in Openings/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Start Drill/ }),
    ).toBeInTheDocument();

    // Clicking the card surface (not the buttons) collapses it back.
    await user.click(
      screen.getByRole("button", { name: /Collapse Ruy Lopez details/ }),
    );
    expect(
      screen.getByRole("button", { name: /Select Ruy Lopez/ }),
    ).toHaveAttribute("aria-expanded", "false");
    // Collapsing does not re-fire onSelectRoot.
    expect(onSelectRoot).toHaveBeenCalledTimes(1);
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

  it("shows the no-data grade token and em-dash score for a null-score opening", () => {
    renderLineage([
      makeItem({ opening_key: "k1", opening_name: "Unknown", score: null }),
    ]);

    const card = screen.getByRole("button", { name: /Select Unknown/ });
    expect(card.className).toContain("tree-node-card--grade-none");
    // Both the score and the grade tag dash for a null score.
    expect(within(card).getAllByText("—").length).toBeGreaterThanOrEqual(1);
  });

  it("hides Start Drill in the expanded card when onStartDrill is omitted", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <GameOpeningLineage
          playerColor="white"
          lineage={[makeItem({ opening_key: "k1", opening_name: "Ruy Lopez" })]}
          onSelectRoot={vi.fn()}
        />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: /Select Ruy Lopez/ }));

    // The card expanded (link present) but the Start Drill button is absent.
    expect(
      screen.getByRole("link", { name: /View in Openings/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Start Drill/ }),
    ).not.toBeInTheDocument();
  });

  it("is expand-only when onSelectRoot is omitted (live panel)", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <GameOpeningLineage
          playerColor="white"
          lineage={[makeItem({ opening_key: "k1", opening_name: "Ruy Lopez" })]}
        />
      </MemoryRouter>,
    );

    // Wording reflects expand-only (no "Select"); there is no select callback to fire.
    const toggle = screen.getByRole("button", { name: "Show Ruy Lopez details" });
    expect(
      screen.queryByRole("button", { name: /Select Ruy Lopez/ }),
    ).not.toBeInTheDocument();

    await user.click(toggle);

    // Tapping still expands the card in place: the compact toggle is replaced
    // and the expanded card's "View in Openings" link appears.
    expect(
      screen.queryByRole("button", { name: "Show Ruy Lopez details" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /View in Openings/ }),
    ).toBeInTheDocument();
  });

  describe("score-diff badge (g-3gmc)", () => {
    it("renders a positive diff in green to the right of the chip", () => {
      renderLineage(
        [makeItem({ opening_key: "k1", opening_name: "Italian Game" })],
        {
          scoreChanges: [makeChange({ opening_key: "k1", before: 41, after: 44 })],
        },
      );

      const badge = screen.getByText("+3 → 44");
      expect(badge).toHaveClass("game-opening-lineage__delta--up");
    });

    it("renders a negative diff in red", () => {
      renderLineage([makeItem({ opening_key: "k1" })], {
        scoreChanges: [
          makeChange({ opening_key: "k1", before: 64, after: 62, delta: -2 }),
        ],
      });

      const badge = screen.getByText("-2 → 62");
      expect(badge).toHaveClass("game-opening-lineage__delta--down");
    });

    it("hides the badge when the rounded scores don't change (sub-1.0 wobble)", () => {
      // raw delta +0.5, but round(42.1)=42 === round(41.6)=42 -> no visible change.
      renderLineage([makeItem({ opening_key: "k1" })], {
        scoreChanges: [
          makeChange({ opening_key: "k1", before: 41.6, after: 42.1, delta: 0.5 }),
        ],
      });

      expect(screen.queryByText(/→/)).not.toBeInTheDocument();
    });

    it("follows the rounded scores across a boundary cross", () => {
      // round(41.6)=42, round(41.4)=41 -> displayed +1 -> 42.
      renderLineage([makeItem({ opening_key: "k1" })], {
        scoreChanges: [
          makeChange({ opening_key: "k1", before: 41.4, after: 41.6, delta: 0.2 }),
        ],
      });

      const badge = screen.getByText("+1 → 42");
      expect(badge).toHaveClass("game-opening-lineage__delta--up");
    });

    it("renders no badge when no change matches the opening key", () => {
      renderLineage([makeItem({ opening_key: "k1" })], {
        scoreChanges: [
          makeChange({ opening_key: "other", before: 41, after: 44 }),
        ],
      });

      expect(screen.queryByText(/→/)).not.toBeInTheDocument();
    });

    it("renders no badge for a brand-new opening, with or without an after-score", () => {
      const { unmount } = renderLineage([makeItem({ opening_key: "k1" })], {
        scoreChanges: [
          makeChange({
            opening_key: "k1",
            is_new: true,
            before: null,
            delta: null,
            after: 30,
          }),
        ],
      });
      expect(screen.queryByText(/→/)).not.toBeInTheDocument();
      unmount();

      renderLineage([makeItem({ opening_key: "k1" })], {
        scoreChanges: [
          makeChange({
            opening_key: "k1",
            is_new: true,
            before: null,
            delta: null,
            after: null,
          }),
        ],
      });
      expect(screen.queryByText(/→/)).not.toBeInTheDocument();
    });

    it("shows the badge in both collapsed and expanded states, as a sibling of the card", async () => {
      const user = userEvent.setup();
      renderLineage(
        [makeItem({ opening_key: "k1", opening_name: "Ruy Lopez" })],
        {
          scoreChanges: [
            makeChange({
              opening_key: "k1",
              opening_name: "Ruy Lopez",
              before: 41,
              after: 44,
            }),
          ],
        },
      );

      // Collapsed: badge present next to the card.
      expect(screen.getByText("+3 → 44")).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: /Select Ruy Lopez/ }));

      // Still present once expanded, and NOT inside the card body — it is a
      // direct child of the <li>, a sibling of the expanded card.
      const badge = screen.getByText("+3 → 44");
      expect(badge).toBeInTheDocument();
      expect(badge.parentElement?.tagName).toBe("LI");
    });
  });
});
