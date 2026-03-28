import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "../../../test/utils";
import BoardStage from "./BoardStage";

vi.mock("react-chessboard", () => ({
  Chessboard: ({ options }: { options: Record<string, unknown> }) => (
    <div
      data-testid="board"
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
  ),
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
  const onDismissRehookToast = vi.fn();

  return {
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
    showRehookToast: false,
    onDismissRehookToast,
  };
};

describe("BoardStage", () => {
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

  it("dismisses revert warning and toast overlays through callbacks", () => {
    const props = makeProps();
    render(
      <BoardStage
        {...props}
        showRevertWarning
        showRehookToast
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /revert anyway/i }));
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    fireEvent.click(screen.getByText(/steering to past mistake/i));

    expect(props.onRevertAnyway).toHaveBeenCalledTimes(1);
    expect(props.onCancelRevert).toHaveBeenCalledTimes(1);
    expect(props.onDismissRehookToast).toHaveBeenCalledTimes(1);
  });
});
