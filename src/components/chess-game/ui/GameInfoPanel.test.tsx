import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "../../../test/utils";
import type { TargetBlunderSrs } from "../../../utils/api";
import GameInfoPanel from "./GameInfoPanel";
import { getOpponentAvatarSrc } from "../config";
import { useGameStore } from "../../../stores/useGameStore";

const makeProps = () => {
  const onToggleGhostInfo = vi.fn();
  const onCloseGhostInfo = vi.fn();
  return {
    statusText: "White to move",
    gameStatusBadge: { label: "Active", className: "active" },
    isRated: true,
    isPracticeContinuation: false,
    isGameActive: true,
    playerColorChoice: "white" as const,
    playerColor: "white" as const,
    playerRating: 1234,
    isProvisional: false,
    opponentMode: "engine" as const,
    opponentName: "Ghost Master 2000",
    engineElo: 2000,
    gameResult: null,
    blunderReviewId: null,
    showGhostInfo: false,
    onToggleGhostInfo,
    onCloseGhostInfo,
    ghostInfoAnchorRef: createRef<HTMLSpanElement>(),
    blunderTargetFen: null,
    boardOrientation: "white" as const,
    blunderReviewSrs: null as TargetBlunderSrs | null,
    openingLineageSlot: (
      <div className="chess-panel__openings">Opening lineage</div>
    ),
    perfectStreak: { current: 0, personalBest: 0 },
  };
};

describe("GameInfoPanel", () => {
  it("renders engine-mode details", () => {
    const props = makeProps();
    const { container } = render(<GameInfoPanel {...props} />);

    expect(screen.getByText("Ghost Master 2000")).toBeInTheDocument();

    const avatar = container.querySelector(
      "img.opponent-avatar",
    ) as HTMLImageElement | null;
    expect(avatar).not.toBeNull();
    expect(avatar?.getAttribute("src")).toBe(getOpponentAvatarSrc(2000));
  });

  it("renders the opening-lineage slot in place of the old opening line", () => {
    const { container } = render(<GameInfoPanel {...makeProps()} />);

    expect(container.querySelector(".chess-panel__openings")).toHaveTextContent(
      "Opening lineage",
    );
    // The old single-line opening element is gone.
    expect(container.querySelector(".chess-panel__opening")).toBeNull();
  });

  it("shows the drilling label instead of the status text in an active drill", () => {
    const props = {
      ...makeProps(),
      isActiveDrill: true,
      drillOpeningName: "Sicilian Defense",
    };
    render(<GameInfoPanel {...props} />);

    expect(screen.getByText("Drilling:")).toBeInTheDocument();
    expect(screen.getByText("Sicilian Defense")).toBeInTheDocument();
    expect(screen.queryByText("White to move")).toBeNull();
  });

  it("falls back to the status text when not in an active drill", () => {
    const props = makeProps();
    render(<GameInfoPanel {...props} />);

    expect(screen.getByText("White to move")).toBeInTheDocument();
    expect(screen.queryByText("Drilling:")).toBeNull();
  });

  it("keeps the opponent visible and shows the result avatar after a game ends", () => {
    const props = {
      ...makeProps(),
      isGameActive: false,
      gameResult: { type: "checkmate_loss" as const, message: "You lost." },
    };
    const { container } = render(<GameInfoPanel {...props} />);

    expect(screen.getByText("Ghost Master 2000")).toBeInTheDocument();
    expect(screen.queryByText("Click New game to start")).toBeNull();

    const avatar = container.querySelector(
      "img.opponent-avatar",
    ) as HTMLImageElement | null;
    expect(avatar?.getAttribute("src")).toBe("/images/victorious/2000.png");
  });

  it("shows the defeated avatar when the player wins", () => {
    const props = {
      ...makeProps(),
      isGameActive: false,
      gameResult: { type: "checkmate_win" as const, message: "You won!" },
    };
    const { container } = render(<GameInfoPanel {...props} />);

    const avatar = container.querySelector(
      "img.opponent-avatar",
    ) as HTMLImageElement | null;
    expect(avatar?.getAttribute("src")).toBe("/images/defeated/2000.png");
  });

  it("leaves the avatar unchanged on a draw", () => {
    const props = {
      ...makeProps(),
      isGameActive: false,
      gameResult: { type: "draw" as const, message: "Draw." },
    };
    const { container } = render(<GameInfoPanel {...props} />);

    const avatar = container.querySelector(
      "img.opponent-avatar",
    ) as HTMLImageElement | null;
    expect(avatar?.getAttribute("src")).toBe(getOpponentAvatarSrc(2000));
  });

  it("groups active matchup data for the compact mobile summary", () => {
    const props = makeProps();
    const { container } = render(<GameInfoPanel {...props} />);

    const matchup = container.querySelector(".chess-panel__active-matchup");
    expect(matchup).not.toBeNull();
    expect(matchup).toHaveTextContent("1234");
    expect(matchup).toHaveTextContent("Ghost Master 2000");
    expect(matchup?.querySelector(".chess-panel__mobile-versus")).toHaveTextContent(
      "vs",
    );
  });

  it("renders the perfect streak badge when a live streak is active", () => {
    const props = makeProps();
    render(
      <GameInfoPanel
        {...props}
        perfectStreak={{ current: 4, personalBest: 7 }}
      />,
    );

    const streak = screen.getByLabelText("Perfect streak 4, best 7");
    expect(streak).toBeInTheDocument();
    expect(streak).toHaveTextContent("Streak⭐4");
    expect(
      streak.querySelector(".perfect-streak-badge"),
    ).toHaveAttribute("data-fire-intensity", "ember");
    expect(screen.queryByText("Best 7")).not.toBeInTheDocument();
  });

  it("hides the perfect streak badge without a live streak or prior best", () => {
    const props = makeProps();
    render(<GameInfoPanel {...props} />);

    expect(screen.queryByLabelText(/Perfect streak/i)).not.toBeInTheDocument();
  });

  it.each([
    [2, "none"],
    [4, "ember"],
    [6, "flame"],
    [9, "hot"],
    [12, "inferno"],
  ] as const)("maps streak %i to %s fire intensity", (current, intensity) => {
    const props = makeProps();
    render(
      <GameInfoPanel
        {...props}
        perfectStreak={{ current, personalBest: 12 }}
      />,
    );

    const streak = screen.getByLabelText(`Perfect streak ${current}, best 12`);
    expect(
      streak.querySelector(".perfect-streak-badge"),
    ).toHaveAttribute("data-fire-intensity", intensity);
  });

  it("renders an on-bin engine avatar", () => {
    const props = makeProps();
    const { container } = render(
      <GameInfoPanel
        {...props}
        engineElo={1200}
        opponentName="Specter Scout 1200"
      />,
    );

    const avatar = container.querySelector(
      "img.opponent-avatar",
    ) as HTMLImageElement | null;
    expect(avatar?.getAttribute("src")).toBe(getOpponentAvatarSrc(1200));
  });

  it("renders ghost target info and forwards ghost-info callbacks", () => {
    const props = makeProps();
    const srs: TargetBlunderSrs = {
      last_reviewed_at: null,
      created_at: "2026-03-01T12:00:00Z",
      pass_count: 3,
      fail_count: 1,
      pass_streak: 2,
    };

    const { container } = render(
      <GameInfoPanel
        {...props}
        opponentMode="ghost"
        opponentName=""
        blunderReviewId={77}
        showGhostInfo
        blunderTargetFen="8/8/8/8/8/8/8/8 w - - 0 1"
        blunderReviewSrs={srs}
      />,
    );

    const avatar = container.querySelector(
      "img.opponent-avatar",
    ) as HTMLImageElement | null;
    expect(avatar?.getAttribute("src")).toBe(
      "/branding/ghost-logo-option-1-buddy.svg",
    );

    fireEvent.click(screen.getByRole("button", { name: /toggle ghost info/i }));
    fireEvent.click(screen.getByRole("button", { name: /close ghost info/i }));

    expect(props.onToggleGhostInfo).toHaveBeenCalledTimes(1);
    expect(props.onCloseGhostInfo).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/pass\/fail: 3\/1/i)).toBeInTheDocument();
    expect(screen.getByText(/streak: 2/i)).toBeInTheDocument();
    expect(screen.getByTestId("ghost-board")).toHaveAttribute(
      "data-position",
      "8/8/8/8/8/8/8/8 w - - 0 1",
    );
  });

  it("renders relocated material from materialFen + materialPerspective", () => {
    const props = makeProps();
    const { container } = render(
      <GameInfoPanel
        {...props}
        materialFen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        materialPerspective="black"
      />,
    );
    const material = container.querySelector(".chess-panel__material");
    expect(material).not.toBeNull();
    expect(material!.querySelector(".material-display")).not.toBeNull();
  });

  it("renders no panel material when the props are absent", () => {
    const { container } = render(<GameInfoPanel {...makeProps()} />);
    expect(container.querySelector(".chess-panel__material")).toBeNull();
  });

  describe("game settings popover", () => {
    beforeEach(() => {
      useGameStore.setState({ soundMuted: false, soundVolume: 1 });
    });

    afterEach(() => {
      vi.restoreAllMocks();
    });

    it("opens settings via the gear and flips aria-expanded", () => {
      render(<GameInfoPanel {...makeProps()} />);
      const gear = screen.getByRole("button", { name: /game settings/i });
      expect(gear).toHaveAttribute("aria-expanded", "false");

      fireEvent.click(gear);
      expect(gear).toHaveAttribute("aria-expanded", "true");
      expect(screen.getByRole("dialog", { name: /game settings/i })).toBeInTheDocument();
    });

    it("toggling mute calls setSoundMuted and disables the slider", () => {
      const setSoundMuted = vi.spyOn(useGameStore.getState(), "setSoundMuted");
      render(<GameInfoPanel {...makeProps()} />);
      fireEvent.click(screen.getByRole("button", { name: /game settings/i }));

      fireEvent.click(screen.getByLabelText("Mute"));
      expect(setSoundMuted).toHaveBeenCalledWith(true);

      useGameStore.setState({ soundMuted: true });
      expect(screen.getByLabelText("Volume")).toBeDisabled();
    });

    it("moving the slider converts percent to a 0-1 value", () => {
      const setSoundVolume = vi.spyOn(useGameStore.getState(), "setSoundVolume");
      render(<GameInfoPanel {...makeProps()} />);
      fireEvent.click(screen.getByRole("button", { name: /game settings/i }));

      fireEvent.change(screen.getByLabelText("Volume"), {
        target: { value: "40" },
      });
      expect(setSoundVolume).toHaveBeenCalledWith(0.4);
    });

    it("closes on Escape and restores focus to the gear", () => {
      render(<GameInfoPanel {...makeProps()} />);
      const gear = screen.getByRole("button", { name: /game settings/i });
      fireEvent.click(gear);
      expect(screen.getByRole("dialog")).toBeInTheDocument();

      fireEvent.keyDown(document, { key: "Escape" });
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(gear).toHaveFocus();
    });

    it("still switches the rating display from the popover", () => {
      const onRatingDisplayTypeChange = vi.fn();
      render(
        <GameInfoPanel
          {...makeProps()}
          onRatingDisplayTypeChange={onRatingDisplayTypeChange}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: /game settings/i }));

      fireEvent.click(screen.getByLabelText("Lichess"));
      expect(onRatingDisplayTypeChange).toHaveBeenCalledWith("lichess");
    });
  });

  it("shows a practice badge during post-revert continuation", () => {
    const props = makeProps();
    render(
      <GameInfoPanel
        {...props}
        isRated={false}
        isPracticeContinuation
      />,
    );

    expect(screen.getByText("Practice")).toBeInTheDocument();
    expect(screen.queryByText("Unrated")).not.toBeInTheDocument();
  });

});
