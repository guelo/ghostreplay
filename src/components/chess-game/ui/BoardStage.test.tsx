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
    wK: () => <svg data-testid="piece-wK" />,
    wQ: () => <svg data-testid="piece-wQ" />,
    wR: () => <svg data-testid="piece-wR" />,
    wB: () => <svg data-testid="piece-wB" />,
    wN: () => <svg data-testid="piece-wN" />,
    bK: () => <svg data-testid="piece-bK" />,
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
  const onStartPlay = vi.fn();
  const onStartDrill = vi.fn();
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
    seedEngineElo: 1000,
    seedStrictnessCp: 25,
    seedColor: "white" as const,
    seedOpening: null,
    seedLine: null,
    playerRating: 1200,
    isProvisional: false,
    onStartPlay,
    onStartDrill,
    startError: null,
    showRevertWarning: false,
    isRevertPending: false,
    revertError: null,
    onRevertAnyway,
    onCancelRevert,
    showResignWarning: false,
    isPracticeContinuation: false,
    onResignAnyway,
    onCancelResign,
    showEndedScrim: false,
    showFlash: false,
    pendingPromotion: null,
    playerColor: "white" as const,
    onPromotionPick: vi.fn(),
    onPromotionCancel: vi.fn(),
    streakToast: null,
    boardNotice: null,
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

  it("drafts the difficulty locally and commits it on Start", () => {
    const props = makeProps();
    render(<BoardStage {...props} />);

    // Dragging the slider is local to the panel — it must not touch game state.
    fireEvent.change(screen.getByRole("slider"), { target: { value: "2" } });
    expect(props.onStartPlay).not.toHaveBeenCalled();

    // Each side button commits the dragged elo (bins[2] === 1200) on Start.
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));
    fireEvent.click(screen.getByRole("button", { name: /play random/i }));
    fireEvent.click(screen.getByRole("button", { name: /play black/i }));
    expect(props.onStartPlay).toHaveBeenCalledWith("white", 1200);
    expect(props.onStartPlay).toHaveBeenCalledWith("random", 1200);
    expect(props.onStartPlay).toHaveBeenCalledWith("black", 1200);

    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(props.onCloseStartOverlay).toHaveBeenCalledTimes(1);
  });

  it("resyncs the popup opponent avatar when the difficulty seed changes", () => {
    const props = makeProps();
    const { container, rerender } = render(<BoardStage {...props} />);

    const initial = container.querySelector(
      "img.opponent-avatar",
    ) as HTMLImageElement | null;
    expect(initial?.getAttribute("src")).toBe(getOpponentAvatarSrc(1000));

    rerender(<BoardStage {...props} seedEngineElo={1200} />);
    const updated = container.querySelector(
      "img.opponent-avatar",
    ) as HTMLImageElement | null;
    expect(updated?.getAttribute("src")).toBe(getOpponentAvatarSrc(1200));
  });

  it("dismisses revert warning through callbacks", () => {
    const props = makeProps();
    render(<BoardStage {...props} showRevertWarning />);

    expect(
      screen.getByText("Reverting records this game as a resignation"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /revert anyway/i }));
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));

    expect(props.onRevertAnyway).toHaveBeenCalledTimes(1);
    expect(props.onCancelRevert).toHaveBeenCalledTimes(1);
  });

  it("shows inline revert failure and disables actions while revert is pending", () => {
    const props = makeProps();
    render(
      <BoardStage
        {...props}
        showRevertWarning
        isRevertPending
        revertError="Failed to record resignation before revert."
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Failed to record resignation before revert.",
    );
    expect(
      screen.getByRole("button", { name: /recording resignation/i }),
    ).toBeDisabled();
    expect(
      document.querySelector(".revert-warning-dialog__spinner"),
    ).not.toBeNull();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeDisabled();
  });

  it("shows practice-specific resign copy during continuation mode", () => {
    const props = makeProps();
    render(
      <BoardStage
        {...props}
        showResignWarning
        isPracticeContinuation
      />,
    );

    expect(
      screen.getByText("This will end the current practice continuation."),
    ).toBeInTheDocument();
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

  it("renders a streak toast when provided", () => {
    const props = makeProps();
    render(<BoardStage {...props} streakToast={{ type: "record", streak: 8 }} />);

    expect(screen.getByRole("status")).toHaveTextContent("New record: 8");
    expect(screen.getByText("⭐ Perfect streak")).toBeInTheDocument();
  });

  it("renders a review-warning board notice with an alert role", () => {
    const props = makeProps();
    const { container } = render(
      <BoardStage
        {...props}
        boardNotice={{ kind: "review-warning", nonce: 1 }}
      />,
    );

    const notice = container.querySelector(".board-notice--review-warning");
    expect(notice).not.toBeNull();
    expect(notice).toHaveAttribute("role", "alert");
    expect(notice).toHaveTextContent("Review Position");
    expect(notice?.querySelector(".warning-triangle-icon")).not.toBeNull();
  });

  it("renders a pass review-result board notice with a check", () => {
    const props = makeProps();
    const { container } = render(
      <BoardStage
        {...props}
        boardNotice={{ kind: "review-result", result: "pass", nonce: 2 }}
      />,
    );

    const notice = container.querySelector(".board-notice--pass");
    expect(notice).not.toBeNull();
    expect(notice).toHaveAttribute("role", "status");
    expect(notice?.querySelector(".board-notice__result-icon")?.textContent).toBe(
      "✓",
    );
  });

  it("renders a fail review-result board notice with a cross", () => {
    const props = makeProps();
    const { container } = render(
      <BoardStage
        {...props}
        boardNotice={{ kind: "review-result", result: "fail", nonce: 3 }}
      />,
    );

    const notice = container.querySelector(".board-notice--fail");
    expect(notice).not.toBeNull();
    expect(notice?.querySelector(".board-notice__result-icon")?.textContent).toBe(
      "✗",
    );
  });

  it("renders a rehook board notice", () => {
    const props = makeProps();
    const { container } = render(
      <BoardStage {...props} boardNotice={{ kind: "rehook", nonce: 4 }} />,
    );

    const notice = container.querySelector(".board-notice--rehook");
    expect(notice).not.toBeNull();
    expect(notice).toHaveTextContent("The haunting resumes");
  });

  it("calls onPromotionPick when a promotion piece is clicked", () => {
    const props = makeProps();
    render(<BoardStage {...props} pendingPromotion={{ from: "e7", to: "e8" }} playerColor="white" />);
    fireEvent.click(screen.getByRole("button", { name: /promote to q/i }));
    expect(props.onPromotionPick).toHaveBeenCalledWith("q");
  });

  it("hides the start overlay during an active game", () => {
    const props = makeProps();
    render(<BoardStage {...props} isGameActive />);
    expect(screen.queryByRole("button", { name: /close/i })).not.toBeInTheDocument();
  });

  it("opens the start overlay over a stopped drill even while isGameActive is true", () => {
    const props = makeProps();
    render(<BoardStage {...props} isGameActive isStoppedDrill />);
    expect(screen.getByRole("button", { name: /close/i })).toBeInTheDocument();
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
