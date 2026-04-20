import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "../../../test/utils";
import type { TargetBlunderSrs } from "../../../utils/api";
import GameInfoPanel from "./GameInfoPanel";
import { getOpponentAvatarSrc } from "../config";

vi.mock("react-chessboard", () => ({
  Chessboard: ({ options }: { options: Record<string, unknown> }) => (
    <div
      data-testid="ghost-board"
      data-position={options.position as string}
      data-orientation={options.boardOrientation as string}
    />
  ),
}));

const makeProps = () => {
  const onToggleGhostInfo = vi.fn();
  const onCloseGhostInfo = vi.fn();
  const onDismissRehookToast = vi.fn();
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
    blunderReviewId: null,
    showGhostInfo: false,
    onToggleGhostInfo,
    onCloseGhostInfo,
    ghostInfoAnchorRef: createRef<HTMLSpanElement>(),
    blunderTargetFen: null,
    boardOrientation: "white" as const,
    blunderReviewSrs: null as TargetBlunderSrs | null,
    displayedOpening: { eco: "C20", name: "King's Pawn Game", source: "eco" },
    isReviewMomentActive: false,
    resolvedReview: null,
    isViewingLive: true,
    showRehookToast: false,
    onDismissRehookToast,
  };
};

describe("GameInfoPanel", () => {
  it("renders engine-mode details", () => {
    const props = makeProps();
    const { container } = render(<GameInfoPanel {...props} />);

    expect(screen.getByText("Ghost Master 2000")).toBeInTheDocument();
    expect(screen.getByText("C20 King's Pawn Game")).toBeInTheDocument();

    const avatar = container.querySelector(
      "img.opponent-avatar",
    ) as HTMLImageElement | null;
    expect(avatar).not.toBeNull();
    expect(avatar?.getAttribute("src")).toBe(getOpponentAvatarSrc(2000));
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

  it("shows rehook toast below opponent label and calls dismiss on click", () => {
    const props = makeProps();
    render(
      <GameInfoPanel
        {...props}
        opponentMode="ghost"
        opponentName=""
        showRehookToast
      />,
    );

    expect(screen.getByText("Ghost reactivated")).toBeInTheDocument();
    expect(screen.getByText("Steering to past mistake")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Ghost reactivated"));
    expect(props.onDismissRehookToast).toHaveBeenCalledTimes(1);
  });

  it("stacks ghost warnings in a shared warning container", () => {
    const props = makeProps();
    const { container } = render(
      <GameInfoPanel
        {...props}
        opponentMode="ghost"
        opponentName=""
        showRehookToast
        isReviewMomentActive
      />,
    );

    const stack = container.querySelector(".chess-warning-stack");
    expect(stack).not.toBeNull();
    expect(stack?.querySelector(".rehook-toast")).not.toBeNull();
    expect(stack?.querySelector(".review-warning-toast")).not.toBeNull();
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
