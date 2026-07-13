import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "../../../test/utils";
import type { RatingChange } from "../../../utils/api";
import type { GameResult } from "../domain/status";
import PostGameBanner from "./PostGameBanner";

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
});
