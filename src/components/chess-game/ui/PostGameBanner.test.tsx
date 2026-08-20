import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "../../../test/utils";
import type { OpeningScoreDeltaItem, RatingChange } from "../../../utils/api";
import type { GameResult } from "../domain/status";
import PostGameBanner from "./PostGameBanner";
import { shouldRenderPostGameBanner } from "./PostGameBanner.helpers";

const makeOpening = (
  overrides: Partial<OpeningScoreDeltaItem> & { opening_key: string },
): OpeningScoreDeltaItem => ({
  opening_name: overrides.opening_key,
  opening_family: "Open Game",
  eco: "C60",
  depth: 3,
  before: 38,
  after: 41,
  delta: 3,
  is_new: false,
  ...overrides,
});

/** The stat row owning `label`, so a row's whole contents can be asserted at once. */
const rowFor = (label: string) => screen.getByText(label).closest("p");

const makeProps = () => {
  const onViewAnalysis = vi.fn();
  const onShowStartOverlay = vi.fn();

  return {
    isGameActive: false,
    isPracticeContinuation: false,
    showPostGamePrompt: true,
    gameResult: {
      type: "checkmate_win",
      message: "Checkmate! You won!",
    } as GameResult,
    ratingChange: {
      rating_before: 1200,
      rating_after: 1216,
      is_provisional: false,
    } as RatingChange,
    onViewAnalysis,
    onShowStartOverlay,
  };
};

describe("PostGameBanner", () => {
  it("renders post-game actions and forwards button callbacks", () => {
    const props = makeProps();
    render(<PostGameBanner {...props} />);

    expect(screen.getByText("Checkmate! You won!")).toBeInTheDocument();
    expect(screen.getByText("+16")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /view analysis/i }));
    fireEvent.click(screen.getByRole("button", { name: /new game/i }));

    expect(props.onViewAnalysis).toHaveBeenCalledTimes(1);
    expect(props.onShowStartOverlay).toHaveBeenCalledTimes(1);
  });

  // g-e01b: History was redundant with View Analysis (both open the latest game).
  it("does not render a History button alongside View Analysis", () => {
    const props = makeProps();
    render(<PostGameBanner {...props} />);

    expect(
      screen.getByRole("button", { name: /view analysis/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^history$/i }),
    ).not.toBeInTheDocument();
  });

  it("renders idle new-game prompt when not active and no post-game prompt", () => {
    const props = makeProps();
    render(
      <PostGameBanner
        {...props}
        showPostGamePrompt={false}
        gameResult={null}
        ratingChange={null}
      />,
    );

    expect(screen.getByText(/ready for a new game/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    expect(props.onShowStartOverlay).toHaveBeenCalledTimes(1);
  });

  it("renders nothing during active game when no post-game prompt is visible", () => {
    const props = makeProps();
    const { container } = render(
      <PostGameBanner
        {...props}
        isGameActive
        showPostGamePrompt={false}
        gameResult={null}
        ratingChange={null}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("suppresses rating delta after a practice continuation ends", () => {
    const props = makeProps();
    render(
      <PostGameBanner
        {...props}
        isPracticeContinuation
      />,
    );

    expect(screen.getByText("Checkmate! You won!")).toBeInTheDocument();
    expect(screen.queryByText("+16")).not.toBeInTheDocument();
  });

  it("renders 'New Drill' button when drillOpeningKey is present", () => {
    const props = makeProps();
    const onNewDrill = vi.fn();
    render(
      <PostGameBanner
        {...props}
        drillOpeningKey="some-opening"
        onNewDrill={onNewDrill}
      />,
    );

    expect(screen.getByRole("button", { name: /new drill/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /new drill/i }));
    expect(onNewDrill).toHaveBeenCalledTimes(1);
  });

  it("does not render 'New Drill' button when drillOpeningKey is null", () => {
    const props = makeProps();
    render(<PostGameBanner {...props} drillOpeningKey={null} />);

    expect(screen.queryByRole("button", { name: /new drill/i })).not.toBeInTheDocument();
  });

  it("natural-end during drill renders only 'Another drill' (no rating delta, no analysis)", () => {
    const props = makeProps();
    const onNewDrill = vi.fn();
    render(
      <PostGameBanner
        {...props}
        drillOpeningKey="some-opening"
        drillState="failed"
        onNewDrill={onNewDrill}
      />,
    );

    expect(screen.getByText("Checkmate! You won!")).toBeInTheDocument();
    // No rating delta even though ratingChange is set
    expect(screen.queryByText("+16")).not.toBeInTheDocument();
    // No View Analysis / New Game buttons in stopped-drill branch
    expect(screen.queryByRole("button", { name: /view analysis/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /new game/i })).not.toBeInTheDocument();

    const btn = screen.getByRole("button", { name: /another drill/i });
    fireEvent.click(btn);
    expect(onNewDrill).toHaveBeenCalledTimes(1);
  });

  it("gates only the natural-end repeat action while score freshness is pending", () => {
    const props = makeProps();
    const onNewDrill = vi.fn();
    const onAnotherDrillSettings = vi.fn();
    render(
      <PostGameBanner
        {...props}
        drillOpeningKey="some-opening"
        drillState="failed"
        onNewDrill={onNewDrill}
        onAnotherDrillSettings={onAnotherDrillSettings}
        drillAgainPending
      />,
    );

    const waiting = screen.getByRole("button", {
      name: "Updating score before another drill",
    });
    expect(waiting).not.toBeDisabled();
    expect(waiting).toHaveAttribute("aria-disabled", "true");
    expect(waiting).toHaveAttribute("aria-busy", "true");
    fireEvent.click(waiting, { detail: 1 });
    fireEvent.click(screen.getByRole("button", { name: /change drill settings/i }));

    expect(onNewDrill).toHaveBeenCalledTimes(1);
    expect(onAnotherDrillSettings).toHaveBeenCalledTimes(1);
  });

  it("gates generic New Drill without gating View Analysis or New Game", () => {
    const props = makeProps();
    const onNewDrill = vi.fn();
    const onAnotherDrillSettings = vi.fn();
    render(
      <PostGameBanner
        {...props}
        drillOpeningKey="some-opening"
        onNewDrill={onNewDrill}
        onAnotherDrillSettings={onAnotherDrillSettings}
        drillAgainPending
      />,
    );

    const waiting = screen.getByRole("button", {
      name: "Updating score before another drill",
    });
    expect(waiting).not.toBeDisabled();
    fireEvent.click(waiting, { detail: 1 });
    fireEvent.click(screen.getByRole("button", { name: /view analysis/i }));
    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /change drill settings/i }));

    expect(onNewDrill).toHaveBeenCalledTimes(1);
    expect(props.onViewAnalysis).toHaveBeenCalledTimes(1);
    expect(props.onShowStartOverlay).toHaveBeenCalledTimes(1);
    expect(onAnotherDrillSettings).toHaveBeenCalledTimes(1);
  });

  it("renders a gear button in the natural-end drill branch and fires onAnotherDrillSettings", () => {
    const props = makeProps();
    const onAnotherDrillSettings = vi.fn();
    render(
      <PostGameBanner
        {...props}
        drillOpeningKey="some-opening"
        onNewDrill={vi.fn()}
        onAnotherDrillSettings={onAnotherDrillSettings}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /change drill settings/i }));
    expect(onAnotherDrillSettings).toHaveBeenCalledTimes(1);
  });

  it("renders a gear button in the stopped-drill branch and fires onAnotherDrillSettings", () => {
    const props = makeProps();
    const onAnotherDrillSettings = vi.fn();
    render(
      <PostGameBanner
        {...props}
        drillOpeningKey="some-opening"
        drillState="failed"
        onNewDrill={vi.fn()}
        onAnotherDrillSettings={onAnotherDrillSettings}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /change drill settings/i }));
    expect(onAnotherDrillSettings).toHaveBeenCalledTimes(1);
  });

  it("renders nothing in reviewed-return mode, suppressing the generic New game", () => {
    const props = makeProps();
    const { container } = render(
      <PostGameBanner
        {...props}
        showPostGamePrompt={false}
        gameResult={null}
        ratingChange={null}
        drillOpeningKey="some-opening"
        drillState="abandoned"
        isReviewedDrillReturn
      />,
    );

    // DrillStopActions restores the drill-stopped controls separately; the banner
    // must not render its generic inactive "New game" prompt here.
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("button", { name: /new game/i })).toBeNull();
  });

  it("disables the drill restart actions when drillActionsDisabled is set", () => {
    const props = makeProps();
    render(
      <PostGameBanner
        {...props}
        drillOpeningKey="some-opening"
        drillState="failed"
        onNewDrill={vi.fn()}
        onAnotherDrillSettings={vi.fn()}
        drillActionsDisabled
      />,
    );

    expect(screen.getByRole("button", { name: /another drill/i })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /change drill settings/i }),
    ).toBeDisabled();
  });

  describe("stat stack", () => {
    it("labels the Elo row and keeps the before -> after value", () => {
      const props = makeProps();
      render(<PostGameBanner {...props} />);

      expect(rowFor("Elo change:")).toHaveTextContent("+16");
      expect(rowFor("Elo change:")).toHaveTextContent("(1200 -> 1216)");
    });

    it("marks a provisional rating with the ? suffix", () => {
      const props = makeProps();
      render(
        <PostGameBanner
          {...props}
          ratingChange={{
            rating_before: 1200,
            rating_after: 1216,
            is_provisional: true,
          } as RatingChange}
        />,
      );

      expect(rowFor("Elo change:")).toHaveTextContent("(1200 -> 1216?)");
    });

    it("shows a placeholder accuracy while the analysis is still processing", () => {
      const props = makeProps();
      render(<PostGameBanner {...props} accuracyStatus="pending" />);

      const row = rowFor("Accuracy:");
      expect(row).toHaveTextContent("—");
      expect(row).toHaveAttribute("aria-busy", "true");
    });

    it("shows the accuracy percentage once it resolves", () => {
      const props = makeProps();
      render(
        <PostGameBanner {...props} accuracy={87} accuracyStatus="ready" />,
      );

      expect(rowFor("Accuracy:")).toHaveTextContent("87%");
    });

    // accuracy fails CLOSED to null (backend/app/accuracy.py:96), so a settled
    // null means "not measurable" — rendering it as 0% would be a lie.
    it("hides the accuracy row when it settles with no value", () => {
      const props = makeProps();
      render(
        <PostGameBanner
          {...props}
          accuracy={null}
          accuracyStatus="unavailable"
        />,
      );

      expect(screen.queryByText("Accuracy:")).not.toBeInTheDocument();
    });

    it("hides the accuracy row when nothing was ever requested", () => {
      const props = makeProps();
      render(<PostGameBanner {...props} accuracyStatus="idle" />);

      expect(screen.queryByText("Accuracy:")).not.toBeInTheDocument();
    });

    it("lists only the openings whose score actually changed", () => {
      const props = makeProps();
      render(
        <PostGameBanner
          {...props}
          openingDeltaFreshness="fresh"
          openingScoreChanges={[
            makeOpening({ opening_key: "Ruy Lopez", before: 38, after: 41 }),
            makeOpening({ opening_key: "Italian Game", before: 32, after: 30 }),
            // Played but unmoved, and nothing measurable: both are noise rows.
            makeOpening({ opening_key: "Flat Line", before: 25, after: 25 }),
            makeOpening({
              opening_key: "Unmeasured Line",
              delta: null,
              before: null,
              after: null,
            }),
          ]}
        />,
      );

      expect(rowFor("Ruy Lopez:")).toHaveTextContent("+3.0");
      expect(rowFor("Ruy Lopez:")).toHaveTextContent("-> 41.0");
      expect(rowFor("Italian Game:")).toHaveTextContent("-2.0");
      expect(screen.queryByText("Flat Line:")).not.toBeInTheDocument();
      expect(screen.queryByText("Unmeasured Line:")).not.toBeInTheDocument();
    });

    // A first-ever score IS the change and has no `before` to subtract from, so
    // it survives a filter keyed on delta.
    it("keeps a brand-new opening even though it carries no delta", () => {
      const props = makeProps();
      render(
        <PostGameBanner
          {...props}
          openingDeltaFreshness="fresh"
          openingScoreChanges={[
            makeOpening({
              opening_key: "Scotch Game",
              is_new: true,
              before: null,
              delta: null,
              after: 41,
            }),
          ]}
        />,
      );

      expect(rowFor("Scotch Game:")).toHaveTextContent("new");
      expect(rowFor("Scotch Game:")).toHaveTextContent("-> 41.0");
    });

    // `is_new` with no resolved score is a supported shape. The lineage card
    // treats it as unscored; a bare "new" with no target would be worse than
    // silence, so the banner drops the row too.
    it("drops a brand-new opening that has no score yet", () => {
      const props = makeProps();
      render(
        <PostGameBanner
          {...props}
          openingDeltaFreshness="fresh"
          openingScoreChanges={[
            makeOpening({
              opening_key: "Scotch Game",
              is_new: true,
              before: null,
              delta: null,
              after: null,
            }),
          ]}
        />,
      );

      expect(screen.queryByText("Scotch Game:")).not.toBeInTheDocument();
    });

    // The played chain keeps a non-consecutively repeated root as its own entry,
    // so the delta carries one field-identical item per crossing. The banner has
    // no per-crossing information to show, so a second row would be a duplicate
    // AND a duplicate React key.
    it("renders one row per opening even when a root is crossed twice", () => {
      const props = makeProps();
      const crossing = {
        opening_key: "k1",
        opening_name: "King's Pawn Game",
        before: 41,
        after: 44,
      };
      render(
        <PostGameBanner
          {...props}
          openingDeltaFreshness="fresh"
          openingScoreChanges={[
            makeOpening(crossing),
            makeOpening({ opening_key: "k2", opening_name: "Sicilian Defence", before: 20, after: 22 }),
            makeOpening(crossing),
          ]}
        />,
      );

      expect(screen.getAllByText("King's Pawn Game:")).toHaveLength(1);
      expect(rowFor("King's Pawn Game:")).toHaveTextContent("+3.0");
      expect(screen.getAllByText("Sicilian Defence:")).toHaveLength(1);
    });

    // Scores are floats. Subtracting them raw prints 0.20000000000000284, so the
    // rows quantize through the same badge helper the lineage cards use.
    it("renders fractional score moves at one decimal place", () => {
      const props = makeProps();
      render(
        <PostGameBanner
          {...props}
          openingDeltaFreshness="fresh"
          openingScoreChanges={[
            makeOpening({
              opening_key: "Ruy Lopez",
              before: 41.4,
              after: 41.6,
              delta: 41.6 - 41.4,
            }),
            // Endpoints that resolve to the same visible tenth are not a change.
            makeOpening({
              opening_key: "Rounding Noise",
              before: 30.02,
              after: 30.04,
              delta: 0.02,
            }),
          ]}
        />,
      );

      expect(rowFor("Ruy Lopez:")).toHaveTextContent("+0.2");
      expect(rowFor("Ruy Lopez:")).toHaveTextContent("-> 41.6");
      expect(screen.queryByText("Rounding Noise:")).not.toBeInTheDocument();
    });

    it("renders no opening rows when every played opening scored flat", () => {
      const props = makeProps();
      render(
        <PostGameBanner
          {...props}
          isPracticeContinuation
          accuracyStatus="idle"
          openingDeltaFreshness="fresh"
          openingScoreChanges={[
            makeOpening({ opening_key: "Flat One", before: 25, after: 25 }),
            makeOpening({ opening_key: "Flat Two", before: 25, after: 25 }),
          ]}
        />,
      );

      expect(screen.queryByText("Flat One:")).not.toBeInTheDocument();
      expect(screen.queryByText("Flat Two:")).not.toBeInTheDocument();
      // With Elo suppressed and accuracy idle too, the whole section collapses
      // rather than leaving an empty gap above the buttons.
      expect(document.querySelector(".game-end-stats")).toBeNull();
      expect(
        screen.getByRole("button", { name: /view analysis/i }),
      ).toBeInTheDocument();
    });

    // A boundary delta is pending with no items at all: the filter cannot run, so
    // a placeholder is the honest state rather than a filtered-empty section.
    it("shows a placeholder while the item list is not yet known", () => {
      const props = makeProps();
      render(
        <PostGameBanner
          {...props}
          openingDeltaFreshness="pending"
          openingScoreChanges={null}
        />,
      );

      expect(rowFor("Opening scores:")).toHaveTextContent("—");
    });

    // A TERMINAL delta arrives carrying its items and is "pending" only until
    // reconciliation proves it fresh. The lineage badges show those numbers
    // straight away; the banner must not diverge by sitting on a dash.
    it("renders known items during reconciliation instead of a placeholder", () => {
      const props = makeProps();
      render(
        <PostGameBanner
          {...props}
          openingDeltaFreshness="pending"
          openingScoreChanges={[
            makeOpening({ opening_key: "Ruy Lopez", before: 38, after: 41 }),
          ]}
        />,
      );

      expect(screen.queryByText("Opening scores:")).not.toBeInTheDocument();
      expect(rowFor("Ruy Lopez:")).toHaveTextContent("+3.0");
    });

    it("omits the opening section when nothing measurable ever arrived", () => {
      const props = makeProps();
      render(
        <PostGameBanner
          {...props}
          openingDeltaFreshness="unavailable"
          openingScoreChanges={null}
        />,
      );

      expect(screen.queryByText("Opening scores:")).not.toBeInTheDocument();
    });

    // A delta stamped for another session reaches the banner as no items and no
    // freshness — the same shape as "never arrived".
    it("omits the opening section for a delta the session did not earn", () => {
      const props = makeProps();
      render(
        <PostGameBanner
          {...props}
          openingDeltaFreshness={null}
          openingScoreChanges={null}
        />,
      );

      expect(screen.queryByText("Opening scores:")).not.toBeInTheDocument();
    });
  });

  // Drills are out of scope for the stat stack (g-frlfp). One case per reachable
  // drill terminal shape.
  describe("drill regressions", () => {
    it("gives a stopped drill its message and repeat actions with no stat stack", () => {
      const props = makeProps();
      render(
        <PostGameBanner
          {...props}
          drillOpeningKey="some-opening"
          drillState="failed"
          onNewDrill={vi.fn()}
          onAnotherDrillSettings={vi.fn()}
          accuracy={87}
          accuracyStatus="ready"
          openingDeltaFreshness="fresh"
          openingScoreChanges={[
            makeOpening({ opening_key: "Ruy Lopez", delta: 3 }),
          ]}
        />,
      );

      expect(screen.getByText("Checkmate! You won!")).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: /another drill/i }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: /change drill settings/i }),
      ).toBeInTheDocument();
      expect(screen.queryByText("Accuracy:")).not.toBeInTheDocument();
      expect(screen.queryByText("Elo change:")).not.toBeInTheDocument();
      expect(screen.queryByText("Ruy Lopez:")).not.toBeInTheDocument();
    });

    it("renders no stat stack for any drill session reaching the shared branch", () => {
      const props = makeProps();
      render(
        <PostGameBanner
          {...props}
          drillOpeningKey="some-opening"
          onNewDrill={vi.fn()}
          accuracy={87}
          accuracyStatus="ready"
          openingDeltaFreshness="fresh"
          openingScoreChanges={[
            makeOpening({ opening_key: "Ruy Lopez", delta: 3 }),
          ]}
        />,
      );

      expect(document.querySelector(".game-end-stats")).toBeNull();
      expect(screen.queryByText("Accuracy:")).not.toBeInTheDocument();
      expect(screen.queryByText("Elo change:")).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: /new drill/i }),
      ).toBeInTheDocument();
    });

    it("still renders nothing on a reviewed drill return", () => {
      const props = makeProps();
      const { container } = render(
        <PostGameBanner
          {...props}
          showPostGamePrompt={false}
          gameResult={null}
          ratingChange={null}
          drillOpeningKey="some-opening"
          drillState="abandoned"
          isReviewedDrillReturn
          accuracy={87}
          accuracyStatus="ready"
        />,
      );

      expect(container).toBeEmptyDOMElement();
    });
  });

  // The layout gives the banner a grid row only when it has content, so this
  // predicate must agree with the component's own branches.
  describe("shouldRenderPostGameBanner", () => {
    it("agrees with the component about when the banner has content", () => {
      const cases = [
        { isGameActive: false, showPostGamePrompt: true, hasResult: true },
        { isGameActive: false, showPostGamePrompt: false, hasResult: false },
        { isGameActive: true, showPostGamePrompt: false, hasResult: false },
        { isGameActive: true, showPostGamePrompt: true, hasResult: false },
      ];

      for (const { isGameActive, showPostGamePrompt, hasResult } of cases) {
        const props = makeProps();
        const gameResult = hasResult ? props.gameResult : null;
        const { container, unmount } = render(
          <PostGameBanner
            {...props}
            isGameActive={isGameActive}
            showPostGamePrompt={showPostGamePrompt}
            gameResult={gameResult}
          />,
        );

        expect(
          shouldRenderPostGameBanner({
            isGameActive,
            showPostGamePrompt,
            gameResult,
          }),
        ).toBe(container.innerHTML !== "");
        unmount();
      }
    });

    it("reports no banner on a reviewed drill return", () => {
      expect(
        shouldRenderPostGameBanner({
          isGameActive: false,
          showPostGamePrompt: false,
          gameResult: null,
          isReviewedDrillReturn: true,
        }),
      ).toBe(false);
    });
  });
});
