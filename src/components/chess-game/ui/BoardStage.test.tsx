import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "../../../test/utils";
import BoardStage from "./BoardStage";
import { getOpponentAvatarSrc } from "../config";

let boardMountCount = 0;
let boardUnmountCount = 0;
let nextBoardInstanceId = 1;

vi.mock("react-chessboard", () => ({
  defaultPieces: {
    wQ: () => <svg data-testid="piece-wQ" />,
    wR: () => <svg data-testid="piece-wR" />,
    wB: () => <svg data-testid="piece-wB" />,
    wN: () => <svg data-testid="piece-wN" />,
    bQ: () => <svg data-testid="piece-bQ" />,
    bR: () => <svg data-testid="piece-bR" />,
    bB: () => <svg data-testid="piece-bB" />,
    bN: () => <svg data-testid="piece-bN" />,
  },
  Chessboard: ({ options }: { options: Record<string, unknown> }) => {
    const instanceIdRef = React.useRef(nextBoardInstanceId++);

    React.useEffect(() => {
      boardMountCount += 1;
      return () => {
        boardUnmountCount += 1;
      };
    }, []);

    return (
      <div
        data-testid="board"
        data-instance-id={String(instanceIdRef.current)}
        data-position={options.position as string}
        data-orientation={options.boardOrientation as string}
      >
        <button
          type="button"
          onClick={() =>
            (
              options.onPieceDrop as ((args: {
                sourceSquare: string;
                targetSquare: string;
              }) => boolean)
            )({ sourceSquare: "e2", targetSquare: "e4" })
          }
        >
          Drop move
        </button>
        <button
          type="button"
          onClick={() =>
            (options.onSquareClick as (args: { square: string }) => void)({
              square: "e2",
            })
          }
        >
          Click square
        </button>
      </div>
    );
  },
}));

const makeProps = () => {
  const onPieceDrop = vi.fn().mockReturnValue(true);
  const onSquareClick = vi.fn();
  const onCloseStartOverlay = vi.fn();
  const onEngineEloChange = vi.fn();
  const onPlayWhite = vi.fn();
  const onPlayRandom = vi.fn();
  const onPlayBlack = vi.fn();
  const onRevertAnyway = vi.fn();
  const onCancelRevert = vi.fn();
  const onResignAnyway = vi.fn();
  const onCancelResign = vi.fn();
  return {
    boardInstanceKey: 0,
    boardOrientation: "black" as const,
    displayedFen: "fen-value",
    onPieceDrop,
    onSquareClick,
    allowDragging: true,
    squareStyles: {},
    arrows: [],
    showStartOverlay: true,
    isGameActive: false,
    isStartingGame: false,
    onCloseStartOverlay,
    maiaEloBins: [800, 1000, 1200] as const,
    engineElo: 1000,
    onEngineEloChange,
    botLabel: "Ghost Master 1000",
    winDelta: 12,
    lossDelta: -8,
    onPlayWhite,
    onPlayRandom,
    onPlayBlack,
    startError: null,
    showRevertWarning: false,
    onRevertAnyway,
    onCancelRevert,
    showResignWarning: false,
    onResignAnyway,
    onCancelResign,
    showEndedScrim: false,
    showFlash: false,
    pendingPromotion: null,
    playerColor: "white" as const,
    onPromotionPick: vi.fn(),
    onPromotionCancel: vi.fn(),
  };
};

describe("BoardStage", () => {
  it("remounts the board when boardInstanceKey changes", () => {
    boardMountCount = 0;
    boardUnmountCount = 0;
    nextBoardInstanceId = 1;

    const props = makeProps();
    const { rerender } = render(<BoardStage {...props} />);
    const firstInstanceId = screen.getByTestId("board").getAttribute("data-instance-id");

    expect(boardMountCount).toBe(1);
    expect(boardUnmountCount).toBe(0);

    rerender(<BoardStage {...props} boardInstanceKey={1} />);
    const secondInstanceId = screen.getByTestId("board").getAttribute("data-instance-id");

    expect(boardMountCount).toBe(2);
    expect(boardUnmountCount).toBe(1);
    expect(secondInstanceId).not.toBe(firstInstanceId);
  });

  it("wires chessboard contract props", () => {
    const props = makeProps();
    render(<BoardStage {...props} />);

    expect(screen.getByTestId("board")).toHaveAttribute(
      "data-orientation",
      "black",
    );

    fireEvent.click(screen.getByRole("button", { name: /drop move/i }));
    fireEvent.click(screen.getByRole("button", { name: /click square/i }));

    expect(props.onPieceDrop).toHaveBeenCalledWith({
      sourceSquare: "e2",
      targetSquare: "e4",
    });
    expect(props.onSquareClick).toHaveBeenCalledWith({ square: "e2" });
  });

  it("handles start-overlay actions and elo selection", () => {
    const props = makeProps();
    render(<BoardStage {...props} />);

    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    fireEvent.change(screen.getByRole("slider"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));
    fireEvent.click(screen.getByRole("button", { name: /play random/i }));
    fireEvent.click(screen.getByRole("button", { name: /play black/i }));

    expect(props.onCloseStartOverlay).toHaveBeenCalledTimes(1);
    expect(props.onEngineEloChange).toHaveBeenCalledWith(1200);
    expect(props.onPlayWhite).toHaveBeenCalledTimes(1);
    expect(props.onPlayRandom).toHaveBeenCalledTimes(1);
    expect(props.onPlayBlack).toHaveBeenCalledTimes(1);
  });

  it("updates the popup opponent avatar when engineElo changes", () => {
    const props = makeProps();
    const { container, rerender } = render(<BoardStage {...props} />);

    const initial = container.querySelector(
      "img.opponent-avatar",
    ) as HTMLImageElement | null;
    expect(initial?.getAttribute("src")).toBe(getOpponentAvatarSrc(1000));

    rerender(<BoardStage {...props} engineElo={1200} />);
    const updated = container.querySelector(
      "img.opponent-avatar",
    ) as HTMLImageElement | null;
    expect(updated?.getAttribute("src")).toBe(getOpponentAvatarSrc(1200));
  });

  it("dismisses revert warning through callbacks", () => {
    const props = makeProps();
    render(<BoardStage {...props} showRevertWarning />);

    fireEvent.click(screen.getByRole("button", { name: /revert anyway/i }));
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));

    expect(props.onRevertAnyway).toHaveBeenCalledTimes(1);
    expect(props.onCancelRevert).toHaveBeenCalledTimes(1);
  });

  it("does not render PromotionPicker when pendingPromotion is null", () => {
    const props = makeProps();
    render(<BoardStage {...props} pendingPromotion={null} />);
    expect(screen.queryByRole("button", { name: /promote to/i })).toBeNull();
  });

  it("renders PromotionPicker when pendingPromotion is non-null", () => {
    const props = makeProps();
    render(<BoardStage {...props} pendingPromotion={{ from: "e7", to: "e8" }} playerColor="white" />);
    expect(screen.getAllByRole("button", { name: /promote to/i })).toHaveLength(4);
  });

  it("calls onPromotionPick when a promotion piece is clicked", () => {
    const props = makeProps();
    render(<BoardStage {...props} pendingPromotion={{ from: "e7", to: "e8" }} playerColor="white" />);
    fireEvent.click(screen.getByRole("button", { name: /promote to q/i }));
    expect(props.onPromotionPick).toHaveBeenCalledWith("q");
  });

  it("calls onPromotionCancel when the backdrop is clicked", () => {
    const props = makeProps();
    const { container } = render(<BoardStage {...props} pendingPromotion={{ from: "e7", to: "e8" }} playerColor="white" />);
    const backdrop = container.querySelector(".promotion-picker-backdrop");
    expect(backdrop).not.toBeNull();
    fireEvent.click(backdrop!);
    expect(props.onPromotionCancel).toHaveBeenCalledTimes(1);
  });
});
