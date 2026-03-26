import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "../../../test/utils";
import type { TargetBlunderSrs } from "../../../utils/api";
import GameInfoPanel from "./GameInfoPanel";

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
  return {
    statusText: "White to move",
    gameStatusBadge: { label: "Active", className: "active" },
    isRated: true,
    isGameActive: true,
    playerColorChoice: "white" as const,
    playerColor: "white" as const,
    playerRating: 1234,
    isProvisional: false,
    opponentMode: "engine" as const,
    opponentName: "Ghost Master 2000",
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
  };
};

describe("GameInfoPanel", () => {
  it("renders engine-mode details", () => {
    const props = makeProps();
    render(<GameInfoPanel {...props} />);

    expect(screen.getByText("Ghost Master 2000")).toBeInTheDocument();
    expect(screen.getByText("C20 King's Pawn Game")).toBeInTheDocument();
  });

  it("renders ghost target info and forwards ghost-info callbacks", () => {
    const props = makeProps();
    const srs: TargetBlunderSrs = {
      last_reviewed_at: null,
      pass_count: 3,
      fail_count: 1,
      pass_streak: 2,
    };

    render(
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

});
