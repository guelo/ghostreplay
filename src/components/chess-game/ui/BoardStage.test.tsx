import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "../../../test/utils";
import { setMatchMedia } from "../../../test/setup";
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
        data-show-animations={String(options.showAnimations)}
        data-animation-ms={String(options.animationDurationInMs)}
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
    // Mirrors the force-always production default (g-09mu): no tier pre-selected.
    seedStrictnessCp: null,
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

type ResizeCallback = ConstructorParameters<typeof ResizeObserver>[0];

class ControlledResizeObserver {
  static instances: ControlledResizeObserver[] = [];
  readonly observe = vi.fn((element: Element) => {
    this.element = element;
  });
  readonly disconnect = vi.fn();
  readonly unobserve = vi.fn();
  private element: Element | null = null;
  private readonly callback: ResizeCallback;

  constructor(callback: ResizeCallback) {
    this.callback = callback;
    ControlledResizeObserver.instances.push(this);
  }

  resize(width: number) {
    if (!this.element) throw new Error("ResizeObserver has no observed element");
    this.callback(
      [{ contentRect: { width } as DOMRectReadOnly } as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    );
  }
}

const reviewingProps = () => ({
  ...makeProps(),
  showStartOverlay: false,
  isGameActive: true,
  isReviewingPast: true,
  onReturnToLive: vi.fn(),
});

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

  it("washes the board when reviewing a past move", () => {
    const props = makeProps();
    const { container } = render(<BoardStage {...props} isReviewingPast />);
    expect(
      container.querySelector(".chessboard-square-measure--reviewing"),
    ).not.toBeNull();
  });

  it("does not wash the board on the live position", () => {
    const props = makeProps();
    const { container } = render(<BoardStage {...props} />);
    expect(
      container.querySelector(".chessboard-square-measure--reviewing"),
    ).toBeNull();
    expect(
      container.querySelector(".chessboard-square-measure"),
    ).not.toBeNull();
  });

  it("keeps board animations enabled while reviewing a past move (g-kepv)", () => {
    // g-kepv is fixed in CSS (a board-sized backdrop-filter wash that no longer
    // makes each square a stacking context), so the board must KEEP animating
    // while reviewing — showAnimations must not be disabled, animation stays 200ms.
    const props = makeProps();
    render(<BoardStage {...props} isReviewingPast />);
    const board = screen.getByTestId("board");
    expect(board).not.toHaveAttribute("data-show-animations", "false");
    expect(board).toHaveAttribute("data-animation-ms", "200");
  });

  it("shows the return-to-live pill while reviewing and fires onReturnToLive on click", () => {
    const props = makeProps();
    const onReturnToLive = vi.fn();
    render(
      <BoardStage
        {...props}
        showStartOverlay={false}
        isGameActive
        isReviewingPast
        onReturnToLive={onReturnToLive}
      />,
    );

    const pill = screen.getByRole("button", { name: /return to live/i });
    expect(pill).toBeInTheDocument();
    fireEvent.click(pill);
    expect(onReturnToLive).toHaveBeenCalledTimes(1);
  });

  it("does not show the return-to-live pill on the live position", () => {
    const props = makeProps();
    render(
      <BoardStage
        {...props}
        showStartOverlay={false}
        isGameActive
        onReturnToLive={vi.fn()}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /return to live/i }),
    ).not.toBeInTheDocument();
  });

  it("suppresses the return-to-live pill while a modal overlay owns the board", () => {
    const props = makeProps();
    render(
      <BoardStage
        {...props}
        showStartOverlay={false}
        isGameActive
        isReviewingPast
        onReturnToLive={vi.fn()}
        showRevertWarning
      />,
    );
    expect(
      screen.queryByRole("button", { name: /return to live/i }),
    ).not.toBeInTheDocument();
  });

  it("suppresses the return-to-live pill in the stopped-drill end state (g-pagp)", () => {
    // A failed drill keeps isGameActive true and parks the user on the
    // next-to-last move, so isReviewingPast is true — but there's no live game
    // to return to, so the pill must not show.
    const props = makeProps();
    render(
      <BoardStage
        {...props}
        showStartOverlay={false}
        isGameActive
        isReviewingPast
        onReturnToLive={vi.fn()}
        isStoppedDrill
      />,
    );
    expect(
      screen.queryByRole("button", { name: /return to live/i }),
    ).not.toBeInTheDocument();
  });

  it("renders the end-game fanfare when a trigger is provided", () => {
    const props = makeProps();
    const { container } = render(
      <BoardStage
        {...props}
        endGameFanfareTrigger={{
          id: 1,
          result: {
            type: "checkmate_win",
            message: "Checkmate! You won!",
            reason: "checkmate",
          },
        }}
      />,
    );

    const fanfare = container.querySelector(".end-game-fanfare--win");
    expect(fanfare).not.toBeNull();
    expect(
      fanfare?.querySelector(".end-game-fanfare__headline")?.textContent,
    ).toBe("Victory");
    expect(
      fanfare?.querySelector(".end-game-fanfare__reason")?.textContent,
    ).toBe("Checkmate");
  });

  it("does not render the end-game fanfare without a trigger", () => {
    const props = makeProps();
    const { container } = render(<BoardStage {...props} />);
    expect(container.querySelector(".end-game-fanfare")).toBeNull();
  });

  it("shakes the board when the review nudge nonce increments", () => {
    const props = makeProps();
    const { container, rerender } = render(
      <BoardStage
        {...props}
        showStartOverlay={false}
        isGameActive
        isReviewingPast
        reviewNudge={0}
      />,
    );
    expect(
      container.querySelector(".chessboard-square-measure--nudge"),
    ).toBeNull();

    rerender(
      <BoardStage
        {...props}
        showStartOverlay={false}
        isGameActive
        isReviewingPast
        reviewNudge={1}
      />,
    );
    expect(
      container.querySelector(".chessboard-square-measure--nudge"),
    ).not.toBeNull();
  });
});

describe("BoardStage return-to-live control", () => {
  const originalResizeObserver = globalThis.ResizeObserver;

  beforeEach(() => {
    vi.useFakeTimers();
    ControlledResizeObserver.instances = [];
    globalThis.ResizeObserver = ControlledResizeObserver as unknown as typeof ResizeObserver;
  });

  afterEach(() => {
    vi.useRealTimers();
    globalThis.ResizeObserver = originalResizeObserver;
    setMatchMedia("(prefers-reduced-motion: reduce)", false);
  });

  const resizeBoard = (width: number) => {
    const observer = ControlledResizeObserver.instances.at(-1);
    if (!observer) throw new Error("Expected a board ResizeObserver");
    act(() => observer.resize(width));
    return observer;
  };

  it.each([
    { width: 415, tucked: true },
    { width: 416, tucked: false },
    { width: 520, tucked: false },
  ])("classifies a $width px board with one compact breakpoint", ({ width, tucked }) => {
    render(<BoardStage {...reviewingProps()} />);
    resizeBoard(width);
    act(() => vi.advanceTimersByTime(2_000));

    expect(
      screen
        .getByRole("button", { name: "Return to live" })
        .classList.contains("board-return-live--tucked"),
    ).toBe(tucked);
  });

  it.each([
    { reducedMotion: false, advanceMs: 1_999, tucked: false },
    { reducedMotion: false, advanceMs: 2_000, tucked: true },
    { reducedMotion: true, advanceMs: 0, tucked: true },
  ])(
    "tucks with reducedMotion=$reducedMotion after $advanceMs ms",
    ({ reducedMotion, advanceMs, tucked }) => {
      setMatchMedia("(prefers-reduced-motion: reduce)", reducedMotion);
      render(<BoardStage {...reviewingProps()} />);
      resizeBoard(360);
      act(() => vi.advanceTimersByTime(advanceMs));

      expect(
        screen
          .getByRole("button", { name: "Return to live" })
          .classList.contains("board-return-live--tucked"),
      ).toBe(tucked);
    },
  );

  it("preserves its tuck latch through suppression and grow-then-shrink", () => {
    const props = reviewingProps();
    const { rerender } = render(<BoardStage {...props} />);
    resizeBoard(360);
    act(() => vi.advanceTimersByTime(2_000));

    rerender(<BoardStage {...props} showRevertWarning />);
    expect(screen.queryByRole("button", { name: "Return to live" })).toBeNull();

    rerender(<BoardStage {...props} />);
    resizeBoard(520);
    expect(screen.getByRole("button", { name: "Return to live" })).not.toHaveClass(
      "board-return-live--tucked",
    );
    resizeBoard(360);
    expect(screen.getByRole("button", { name: "Return to live" })).toHaveClass(
      "board-return-live--tucked",
    );
  });

  it("clears its timer and disconnects its observer on unmount", () => {
    const clearTimeoutSpy = vi.spyOn(window, "clearTimeout");
    const { unmount } = render(<BoardStage {...reviewingProps()} />);
    const observer = resizeBoard(360);

    unmount();

    expect(clearTimeoutSpy).toHaveBeenCalled();
    expect(observer.disconnect).toHaveBeenCalledTimes(1);
  });

  it("keeps its accessible name and click behavior after tucking", () => {
    const props = reviewingProps();
    render(<BoardStage {...props} />);
    resizeBoard(360);
    act(() => vi.advanceTimersByTime(2_000));

    const button = screen.getByRole("button", { name: "Return to live" });
    expect(button).toHaveClass("board-return-live--tucked");
    fireEvent.click(button);
    expect(props.onReturnToLive).toHaveBeenCalledTimes(1);
  });
});
