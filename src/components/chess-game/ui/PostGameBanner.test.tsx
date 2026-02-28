import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "../../../test/utils";
import type { RatingChange } from "../../../utils/api";
import type { GameResult } from "../domain/status";
import PostGameBanner from "./PostGameBanner";

const makeProps = () => {
  const onViewAnalysis = vi.fn();
  const onShowStartOverlay = vi.fn();
  const onViewHistory = vi.fn();

  return {
    isGameActive: false,
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
    onViewHistory,
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
    fireEvent.click(screen.getByRole("button", { name: /history/i }));

    expect(props.onViewAnalysis).toHaveBeenCalledTimes(1);
    expect(props.onShowStartOverlay).toHaveBeenCalledTimes(1);
    expect(props.onViewHistory).toHaveBeenCalledTimes(1);
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
});
