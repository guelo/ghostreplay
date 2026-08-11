import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import GameOpeningLineage from "./GameOpeningLineage";
import type {
  OpeningLineageItem,
  OpeningScoreDeltaItem,
  OpeningScoreStatus,
} from "../utils/api";

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
    moves: [],
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
    startPly?: number;
    scoreStatus?: OpeningScoreStatus;
    pendingScoreIndices?: ReadonlySet<number>;
    activeMoveIndex?: number | null;
  } = {},
) {
  const onSelectRoot = handlers.onSelectRoot ?? vi.fn();
  const onStartDrill = handlers.onStartDrill ?? vi.fn();
  const utils = render(
    <MemoryRouter>
      <GameOpeningLineage
        playerColor="white"
        lineage={lineage}
        startPly={handlers.startPly ?? 1}
        scoreChanges={handlers.scoreChanges}
        scoreStatus={handlers.scoreStatus}
        pendingScoreIndices={handlers.pendingScoreIndices}
        activeMoveIndex={handlers.activeMoveIndex}
        onSelectRoot={onSelectRoot}
        onStartDrill={onStartDrill}
      />
    </MemoryRouter>,
  );
  const rerenderWith = (activeMoveIndex: number | null) =>
    utils.rerender(
      <MemoryRouter>
        <GameOpeningLineage
          playerColor="white"
          lineage={lineage}
          startPly={handlers.startPly ?? 1}
          scoreChanges={handlers.scoreChanges}
          scoreStatus={handlers.scoreStatus}
          pendingScoreIndices={handlers.pendingScoreIndices}
          activeMoveIndex={activeMoveIndex}
          onSelectRoot={onSelectRoot}
          onStartDrill={onStartDrill}
        />
      </MemoryRouter>,
    );
  return { ...utils, onSelectRoot, onStartDrill, rerenderWith };
}

/** Names of every card currently rendered in its expanded variant (the expanded
 *  card is the only one carrying a "Collapse … details" overlay). */
function expandedNames(): string[] {
  return screen
    .getAllByRole("button")
    .map((b) => b.getAttribute("aria-label") ?? "")
    .map((label) => /^Collapse (.*) details$/.exec(label)?.[1])
    .filter((name): name is string => Boolean(name));
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

  it("links to the opening page from inside the expanded card without collapsing it", async () => {
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
    // The link is rendered inside the expanded card (as its footer action).
    expect(
      link.closest(".tree-node-card--expanded"),
    ).toBeInTheDocument();

    // Tapping the link does not collapse the card (its click is stopped).
    await user.click(link);
    expect(screen.queryByRole("button", { name: /Select Berlin Defense/ })).not
      .toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /View in Openings/ }),
    ).toBeInTheDocument();
  });

  it("expands each crossing independently when the same opening root repeats", async () => {
    const user = userEvent.setup();
    // A lineage can (defensively) cross the same opening_key as two separate
    // crossings; keying by opening_key alone would expand both at once.
    renderLineage([
      makeItem({
        opening_key: "dup",
        opening_name: "First Crossing",
        moves: ["e4"],
      }),
      makeItem({
        opening_key: "dup",
        opening_name: "Second Crossing",
        moves: ["e4", "e5", "d4"],
      }),
    ]);

    await user.click(
      screen.getByRole("button", { name: /Select First Crossing/ }),
    );

    // Only the first crossing expanded; the second stays a compact toggle.
    expect(
      screen.queryByRole("button", { name: /Select First Crossing/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Select Second Crossing/ }),
    ).toBeInTheDocument();
    // Exactly one card expanded → exactly one "View in Openings" link.
    expect(screen.getAllByRole("link", { name: /View in Openings/ })).toHaveLength(
      1,
    );
  });

  it("renders each card's played move list with the last move bold", () => {
    renderLineage(
      [
        makeItem({
          opening_key: "k1",
          opening_name: "Caro-Kann Defense: Hillbilly Attack",
          moves: ["e4", "c6", "Bc4"],
        }),
      ],
      { startPly: 1 },
    );

    // "1.e4 c6 2.Bc4" with the crossing move (2.Bc4) bold.
    expect(screen.getByText("1.e4")).toBeInTheDocument();
    expect(screen.getByText("c6")).toBeInTheDocument();
    expect(screen.getByText("2.Bc4").tagName).toBe("STRONG");
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
          startPly={1}
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
          startPly={1}
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

  describe("score-change reveal (g-ptea)", () => {
    it("announces the resolved outcome and does not replay it after a compact-card remount", () => {
      vi.useFakeTimers();
      try {
        renderLineage(
          [makeItem({ opening_key: "k1", opening_name: "Italian Game" })],
          {
            scoreChanges: [makeChange({ opening_key: "k1", before: 41, after: 44 })],
          },
        );

        const card = screen.getByRole("button", { name: /Italian Game/ });
        expect(within(card).getByText("41")).toBeInTheDocument();
        expect(card).toHaveAccessibleName(/Score increased by 3, now 44/);

        act(() => vi.advanceTimersByTime(150));

        expect(within(card).queryByText("41")).not.toBeInTheDocument();
        expect(within(card).getByText("44")).toBeInTheDocument();
        expect(
          within(card).getByRole("img", { name: "Score increased by 3, now 44" }),
        ).toBeInTheDocument();

        // Finish the outcome animation, then replace compact -> expanded -> compact.
        act(() => vi.advanceTimersByTime(600));
        fireEvent.click(card);
        fireEvent.click(
          screen.getByRole("button", { name: "Collapse Italian Game details" }),
        );

        const remountedCard = screen.getByRole("button", { name: /Italian Game/ });
        expect(within(remountedCard).getByText("44")).toBeInTheDocument();
        expect(within(remountedCard).queryByText("41")).not.toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
    });

    it("announces a downward outcome", () => {
      vi.useFakeTimers();
      try {
        renderLineage([makeItem({ opening_key: "k1" })], {
          scoreChanges: [
            makeChange({ opening_key: "k1", before: 64, after: 62, delta: -2 }),
          ],
        });

        act(() => vi.advanceTimersByTime(150));
        const card = screen.getByRole("button", { name: /Opening/ });
        expect(
          within(card).getByRole("img", { name: "Score decreased by 2, now 62" }),
        ).toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
    });

    it("suppresses score-change treatment when rounded scores do not change", () => {
      // raw delta +0.5, but round(42.1)=42 === round(41.6)=42 -> no visible change.
      renderLineage([makeItem({ opening_key: "k1" })], {
        scoreChanges: [
          makeChange({ opening_key: "k1", before: 41.6, after: 42.1, delta: 0.5 }),
        ],
      });

      expect(
        screen.queryByRole("img", { name: /^Score (increased|decreased)/ }),
      ).not.toBeInTheDocument();
    });

    it("uses the rounded score change for the capsule", () => {
      // round(41.6)=42, round(41.4)=41 -> displayed +1 -> 42.
      vi.useFakeTimers();
      try {
        renderLineage([makeItem({ opening_key: "k1" })], {
          scoreChanges: [
            makeChange({ opening_key: "k1", before: 41.4, after: 41.6, delta: 0.2 }),
          ],
        });

        act(() => vi.advanceTimersByTime(150));
        expect(screen.getByText("42")).toBeInTheDocument();
        expect(
          screen.getByRole("img", { name: "Score increased by 1, now 42" }),
        ).toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
    });

    it("renders no score-change treatment when no change matches the opening key", () => {
      renderLineage([makeItem({ opening_key: "k1" })], {
        scoreChanges: [
          makeChange({ opening_key: "other", before: 41, after: 44 }),
        ],
      });

      expect(
        screen.queryByRole("img", { name: /^Score (increased|decreased)/ }),
      ).not.toBeInTheDocument();
    });

    it("reveals a brand-new opening from zero to its real score", () => {
      vi.useFakeTimers();
      try {
        renderLineage(
          [makeItem({ opening_key: "k1", opening_name: "New Opening", score: 60 })],
          {
            scoreChanges: [
              makeChange({
                opening_key: "k1",
                is_new: true,
                before: null,
                delta: null,
                after: 30,
              }),
            ],
          },
        );

        const card = screen.getByRole("button", { name: /New Opening/ });
        expect(within(card).getByText("0")).toBeInTheDocument();
        expect(card).not.toHaveTextContent("60");
        act(() => vi.advanceTimersByTime(150));
        expect(within(card).getByText("30")).toBeInTheDocument();
        expect(
          within(card).getByRole("img", { name: "Score increased by 30, now 30" }),
        ).toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
    });

    it("keeps a new opening unscored when its after-score is unavailable", () => {
      renderLineage(
        [makeItem({ opening_key: "k1", opening_name: "New Opening", score: 60 })],
        {
          scoreChanges: [
            makeChange({
              opening_key: "k1",
              is_new: true,
              before: null,
              delta: null,
              after: null,
            }),
          ],
        },
      );
      expect(
        screen.queryByRole("img", { name: /^Score (increased|decreased)/ }),
      ).not.toBeInTheDocument();
      const card = screen.getByRole("button", { name: /New Opening/ });
      expect(card).not.toHaveTextContent("60");
    });

    it("keeps a new opening unscored when its after-score rounds to zero", () => {
      renderLineage(
        [makeItem({ opening_key: "k1", opening_name: "New Opening", score: 60 })],
        {
          scoreChanges: [
            makeChange({
              opening_key: "k1",
              is_new: true,
              before: null,
              delta: null,
              after: 0.4,
            }),
          ],
        },
      );

      const card = screen.getByRole("button", { name: /New Opening/ });
      expect(
        screen.queryByRole("img", { name: /^Score (increased|decreased)/ }),
      ).not.toBeInTheDocument();
      expect(card).not.toHaveTextContent("60");
      expect(card).not.toHaveTextContent("F");
      expect(card).toHaveTextContent("—");
    });

    it("keeps the score outcome available when details are expanded", async () => {
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

      await screen.findByRole("img", { name: "Score increased by 3, now 44" });
      const compact = screen.getByRole("button", { name: /Select Ruy Lopez/ });

      await user.click(compact);

      expect(
        screen.getByRole("img", { name: "Score increased by 3, now 44" }),
      ).toBeInTheDocument();
    });
  });

  describe("score-change data resolution", () => {
    it("starts the card from the delta's pre-game `before`, not the refetched post-game score", () => {
      // At game end the lineage refetch loads the POST-game item.score (44), but
      // the score reveal must begin at the delta's pre-game value (41).
      renderLineage(
        [makeItem({ opening_key: "k1", opening_name: "Italian Game", score: 44 })],
        {
          scoreChanges: [makeChange({ opening_key: "k1", before: 41, after: 44 })],
        },
      );

      const card = screen.getByRole("button", { name: /Italian Game/ });
      expect(within(card).getByText("41")).toBeInTheDocument();
      expect(within(card).queryByText("44")).not.toBeInTheDocument();
      expect(
        within(card).queryByRole("img", { name: /^Score (increased|decreased)/ }),
      ).not.toBeInTheDocument();
    });

    it("shows the available after-score without a baseline and suppresses the badge", () => {
      renderLineage(
        [makeItem({ opening_key: "k1", opening_name: "Italian Game", score: 60 })],
        {
          scoreChanges: [
            makeChange({
              opening_key: "k1",
              before: null,
              after: 72,
              delta: null,
              is_new: false,
            }),
          ],
        },
      );

      const card = screen.getByRole("button", { name: /Italian Game/ });
      expect(within(card).getByText("72")).toBeInTheDocument();
      expect(within(card).queryByText("60")).not.toBeInTheDocument();
      expect(
        screen.queryByRole("img", { name: /^Score (increased|decreased)/ }),
      ).not.toBeInTheDocument();
    });

    it("resign/empty-lineage first paint: changed cards begin at `before`, unchanged cards keep item.score", () => {
      // Resign loads the lineage for the first time with POST-game item.scores.
      // A card with a matching delta begins at its pre-game `before`; a card with
      // no delta entry renders item.score exactly as today.
      renderLineage(
        [
          makeItem({ opening_key: "k1", opening_name: "Open Game", score: 50 }),
          makeItem({
            opening_key: "k2",
            opening_name: "Ruy Lopez",
            depth: 1,
            score: 44,
          }),
        ],
        {
          scoreChanges: [makeChange({ opening_key: "k2", before: 41, after: 44 })],
        },
      );

      // k1 has no delta → shows its item.score (50) unchanged.
      const openGame = screen.getByRole("button", { name: /Open Game/ });
      expect(within(openGame).getByText("50")).toBeInTheDocument();

      // k2 has a delta → starts on pre-game before (41), not post-game 44.
      const ruyLopez = screen.getByRole("button", { name: /Ruy Lopez/ });
      expect(within(ruyLopez).getByText("41")).toBeInTheDocument();
      expect(within(ruyLopez).queryByText("44")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Score-loading spinner for a cold score cache (g-a5v3, g-nclr)
  // -------------------------------------------------------------------------

  describe("pending scores", () => {
    it("shows a loading spinner instead of a score when pending", () => {
      renderLineage([makeItem({ opening_key: "k1", opening_name: "Ruy Lopez", score: null })], {
        scoreStatus: "pending",
      });

      const card = screen.getByRole("button", { name: /Ruy Lopez/ });
      expect(within(card).getByText(/score loading/i)).toBeInTheDocument();
      // The "no score" dash must NOT be showing — that state means something
      // different (genuinely unscored) and would be a lie while loading.
      expect(within(card).queryByText("\u2014")).not.toBeInTheDocument();
    });

    it("still renders a dash for a null score once scores are ready", () => {
      renderLineage([makeItem({ opening_key: "k1", opening_name: "Ruy Lopez", score: null })], {
        scoreStatus: "ready",
      });

      const card = screen.getByRole("button", { name: /Ruy Lopez/ });
      expect(within(card).queryByText(/score loading/i)).not.toBeInTheDocument();
      // A null score renders the dash twice: the score slot and the grade tag.
      expect(within(card).getAllByText("\u2014")).toHaveLength(2);
    });

    it("shows loading only for the unmatched live card occurrence", () => {
      renderLineage(
        [
          makeItem({ opening_key: "k1", opening_name: "Ruy Lopez", score: null }),
          makeItem({ opening_key: "k2", opening_name: "Italian Game", score: null }),
        ],
        { scoreStatus: "ready", pendingScoreIndices: new Set([0]) },
      );

      const loadingCard = screen.getByRole("button", { name: /Ruy Lopez/ });
      expect(within(loadingCard).getByText(/score loading/i)).toBeInTheDocument();
      expect(within(loadingCard).queryByText("\u2014")).not.toBeInTheDocument();

      const resolvedCard = screen.getByRole("button", { name: /Italian Game/ });
      expect(within(resolvedCard).queryByText(/score loading/i)).not.toBeInTheDocument();
      expect(within(resolvedCard).getAllByText("\u2014")).toHaveLength(2);
    });

    it("defaults to ready when scoreStatus is omitted", () => {
      renderLineage([makeItem({ opening_key: "k1", opening_name: "Ruy Lopez", score: 72 })]);

      const card = screen.getByRole("button", { name: /Ruy Lopez/ });
      expect(within(card).queryByText(/score loading/i)).not.toBeInTheDocument();
      expect(within(card).getByText("72")).toBeInTheDocument();
    });

    it("keeps the score-reveal baseline when cache status is pending", () => {
      // The terminal outcome wins over the spinner: the reveal begins from this
      // number, so replacing it with a placeholder would hide its starting point.
      renderLineage(
        [makeItem({ opening_key: "k1", opening_name: "Ruy Lopez", score: 44 })],
        {
          scoreStatus: "pending",
          scoreChanges: [makeChange({ opening_key: "k1", before: 41, after: 44 })],
        },
      );

      const card = screen.getByRole("button", { name: /Ruy Lopez/ });
      expect(within(card).getByText("41")).toBeInTheDocument();
      expect(within(card).queryByText(/score loading/i)).not.toBeInTheDocument();
    });

    it("keeps the score-reveal baseline when the live occurrence is pending", () => {
      renderLineage(
        [makeItem({ opening_key: "k1", opening_name: "Ruy Lopez", score: 44 })],
        {
          scoreStatus: "ready",
          pendingScoreIndices: new Set([0]),
          scoreChanges: [makeChange({ opening_key: "k1", before: 41, after: 44 })],
        },
      );

      const card = screen.getByRole("button", { name: /Ruy Lopez/ });
      expect(within(card).getByText("41")).toBeInTheDocument();
      expect(within(card).queryByText(/score loading/i)).not.toBeInTheDocument();
    });

    it("shows the loading spinner in the expanded card too", async () => {
      const user = userEvent.setup();
      renderLineage([makeItem({ opening_key: "k1", opening_name: "Ruy Lopez", score: null })], {
        scoreStatus: "pending",
      });

      await user.click(screen.getByRole("button", { name: /Ruy Lopez/ }));

      expect(screen.getByText(/score loading/i)).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Board synchronization (g-m1xc)
  // -------------------------------------------------------------------------

  describe("board synchronization", () => {
    // Crossing index of each card is `moves.length - 1`: 1, 4 and 5 here.
    const OPEN_GAME = makeItem({
      opening_key: "k-open",
      opening_name: "Open Game",
      moves: ["e4", "e5"],
    });
    const RUY = makeItem({
      opening_key: "k-ruy",
      opening_name: "Ruy Lopez",
      depth: 1,
      moves: ["e4", "e5", "Nf3", "Nc6", "Bb5"],
    });
    const BERLIN = makeItem({
      opening_key: "k-berlin",
      opening_name: "Berlin Defense",
      depth: 2,
      moves: ["e4", "e5", "Nf3", "Nc6", "Bb5", "Nf6"],
    });
    const THREE = [OPEN_GAME, RUY, BERLIN];

    it("expands the last crossing at or before the displayed move", () => {
      // Move 3 sits BETWEEN the Open Game crossing (1) and the Ruy crossing (4):
      // the most recently crossed opening stays expanded.
      renderLineage(THREE, { activeMoveIndex: 3 });

      expect(expandedNames()).toEqual(["Open Game"]);
    });

    it("follows the board forward and back, keeping exactly one card expanded", () => {
      const { rerenderWith } = renderLineage(THREE, { activeMoveIndex: -1 });

      // Starting position: nothing has been crossed yet.
      expect(expandedNames()).toEqual([]);

      rerenderWith(1);
      expect(expandedNames()).toEqual(["Open Game"]);

      rerenderWith(4);
      expect(expandedNames()).toEqual(["Ruy Lopez"]);

      rerenderWith(5);
      expect(expandedNames()).toEqual(["Berlin Defense"]);

      // Rewinding across a root switches back to the preceding opening...
      rerenderWith(4);
      expect(expandedNames()).toEqual(["Ruy Lopez"]);
      rerenderWith(2);
      expect(expandedNames()).toEqual(["Open Game"]);

      // ...and rewinding before the first crossing collapses everything.
      rerenderWith(0);
      expect(expandedNames()).toEqual([]);
    });

    it("collapses the deepest card once the board moves past its crossing", () => {
      // The deepest crossing has no successor to hand the expansion to: past its
      // crossing move the game has left the opening, so no card describes the
      // position on the board.
      const { rerenderWith } = renderLineage(THREE, { activeMoveIndex: 5 });
      expect(expandedNames()).toEqual(["Berlin Defense"]);

      rerenderWith(6);
      expect(expandedNames()).toEqual([]);
      rerenderWith(30);
      expect(expandedNames()).toEqual([]);

      // Rewinding back onto the crossing re-opens it.
      rerenderWith(5);
      expect(expandedNames()).toEqual(["Berlin Defense"]);
    });

    it("collapses a lone card once the board moves past it", () => {
      // Same rule with nothing to fall back to — the live panel's common case
      // while a game is still in its first crossing.
      const { rerenderWith } = renderLineage([OPEN_GAME], { activeMoveIndex: 1 });
      expect(expandedNames()).toEqual(["Open Game"]);

      rerenderWith(2);
      expect(expandedNames()).toEqual([]);
    });

    it("collapses every card when the board leaves the played main line (null)", () => {
      const { rerenderWith } = renderLineage(THREE, { activeMoveIndex: 5 });
      expect(expandedNames()).toEqual(["Berlin Defense"]);

      // A hypothetical variation is not part of the played lineage.
      rerenderWith(null);
      expect(expandedNames()).toEqual([]);

      // Returning to the main line restores that move's opening.
      rerenderWith(4);
      expect(expandedNames()).toEqual(["Ruy Lopez"]);
    });

    it("never auto-expands a card with no played moves", () => {
      const { rerenderWith } = renderLineage(
        [
          OPEN_GAME,
          makeItem({ opening_key: "k1", opening_name: "No Moves", moves: [] }),
        ],
        { activeMoveIndex: 10 },
      );

      // Neither card: the empty one has no resolvable crossing index, and — not
      // being a crossing — it is no successor for Open Game to hand off to, so
      // Open Game still closes once the board is past its crossing (1).
      expect(expandedNames()).toEqual([]);

      rerenderWith(1);
      expect(expandedNames()).toEqual(["Open Game"]);
    });

    it("expands the matching occurrence when the same opening repeats", () => {
      // Same opening_key crossed twice (indices 0 and 2); only the occurrence
      // the board is inside may expand.
      const { rerenderWith } = renderLineage(
        [
          makeItem({
            opening_key: "dup",
            opening_name: "First Crossing",
            moves: ["e4"],
          }),
          makeItem({
            opening_key: "dup",
            opening_name: "Second Crossing",
            moves: ["e4", "e5", "d4"],
          }),
        ],
        { activeMoveIndex: 1 },
      );

      expect(expandedNames()).toEqual(["First Crossing"]);

      rerenderWith(2);
      expect(expandedNames()).toEqual(["Second Crossing"]);
    });

    it("respects a manual choice until the board moves, then resynchronizes", async () => {
      const user = userEvent.setup();
      const { rerenderWith } = renderLineage(THREE, { activeMoveIndex: 4 });
      expect(expandedNames()).toEqual(["Ruy Lopez"]);

      // Manual collapse of the synchronized card sticks while the board is
      // stationary (a re-render at the same index must not re-open it).
      await user.click(
        screen.getByRole("button", { name: "Collapse Ruy Lopez details" }),
      );
      expect(expandedNames()).toEqual([]);
      rerenderWith(4);
      expect(expandedNames()).toEqual([]);

      // Manually expanding a different card also survives a stationary board.
      await user.click(
        screen.getByRole("button", { name: /Select Open Game/ }),
      );
      expect(expandedNames()).toEqual(["Open Game"]);
      rerenderWith(4);
      expect(expandedNames()).toEqual(["Open Game"]);

      // The next board change re-applies synchronization.
      rerenderWith(5);
      expect(expandedNames()).toEqual(["Berlin Defense"]);
    });

    it("does not resurrect an expired manual choice on a return visit", async () => {
      // A manual choice lasts until the board moves — moving back onto the move
      // it was made at must NOT bring it back, or every position the player ever
      // collapsed would stay collapsed forever.
      const user = userEvent.setup();
      const { rerenderWith } = renderLineage(THREE, { activeMoveIndex: 4 });

      await user.click(
        screen.getByRole("button", { name: "Collapse Ruy Lopez details" }),
      );
      expect(expandedNames()).toEqual([]);

      rerenderWith(5);
      expect(expandedNames()).toEqual(["Berlin Defense"]);
      rerenderWith(4);
      expect(expandedNames()).toEqual(["Ruy Lopez"]);

      // Same for a manual choice of a card other than the synchronized one.
      await user.click(screen.getByRole("button", { name: /Select Open Game/ }));
      expect(expandedNames()).toEqual(["Open Game"]);

      rerenderWith(5);
      expect(expandedNames()).toEqual(["Berlin Defense"]);
      rerenderWith(4);
      expect(expandedNames()).toEqual(["Ruy Lopez"]);
    });

    it("opens the right card when the lineage arrives after the board index", () => {
      // The lineage is derived/fetched asynchronously, so the board index can be
      // settled before any card exists. The matched-key dependency covers this
      // without waiting for another board move.
      const { rerender } = render(
        <MemoryRouter>
          <GameOpeningLineage
            playerColor="white"
            lineage={[]}
            startPly={1}
            activeMoveIndex={4}
          />
        </MemoryRouter>,
      );
      expect(screen.queryByRole("region")).not.toBeInTheDocument();

      rerender(
        <MemoryRouter>
          <GameOpeningLineage
            playerColor="white"
            lineage={THREE}
            startPly={1}
            activeMoveIndex={4}
          />
        </MemoryRouter>,
      );

      expect(expandedNames()).toEqual(["Ruy Lopez"]);
    });

    it("stays fully manual when activeMoveIndex is omitted", async () => {
      const user = userEvent.setup();
      renderLineage(THREE);

      // No card opens on its own...
      expect(expandedNames()).toEqual([]);

      // ...and clicking still expands exactly the clicked card.
      await user.click(screen.getByRole("button", { name: /Select Ruy Lopez/ }));
      expect(expandedNames()).toEqual(["Ruy Lopez"]);
    });
  });
});
