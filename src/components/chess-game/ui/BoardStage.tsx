import { Chessboard } from "react-chessboard";
import type { PieceDropHandlerArgs } from "react-chessboard";
import React, { memo } from "react";
import { PromotionPicker } from "./PromotionPicker";
import OpponentAvatar from "./OpponentAvatar";

type BoardOrientation = "white" | "black";

type BoardStageProps = {
  boardInstanceKey: number;
  boardOrientation: BoardOrientation;
  displayedFen: string;
  onPieceDrop: (args: PieceDropHandlerArgs) => boolean;
  onSquareClick: ({ square }: { square: string }) => void;
  allowDragging: boolean;
  squareStyles: Record<string, React.CSSProperties>;
  arrows: { startSquare: string; endSquare: string; color: string }[];
  showStartOverlay: boolean;
  isGameActive: boolean;
  isStartingGame: boolean;
  onCloseStartOverlay: () => void;
  maiaEloBins: readonly number[];
  engineElo: number;
  onEngineEloChange: (elo: number) => void;
  botLabel: string;
  winDelta: number;
  lossDelta: number;
  onPlayWhite: () => void;
  onPlayRandom: () => void;
  onPlayBlack: () => void;
  startError: string | null;
  showRevertWarning: boolean;
  isRevertPending: boolean;
  revertError: string | null;
  onRevertAnyway: () => void;
  onCancelRevert: () => void;
  showResignWarning: boolean;
  isPracticeContinuation: boolean;
  onResignAnyway: () => void;
  onCancelResign: () => void;
  showEndedScrim: boolean;
  showFlash: boolean;
  pendingPromotion: { from: string; to: string } | null;
  playerColor: 'white' | 'black';
  onPromotionPick: (piece: 'q' | 'r' | 'b' | 'n') => void;
  onPromotionCancel: () => void;
};

const WarningTriangleIcon = () => (
  <svg
    className="review-warning-toast__icon"
    width="48"
    height="48"
    viewBox="0 0 24 24"
    fill="currentColor"
    aria-hidden="true"
  >
    <path d="M1 21h22L12 2 1 21Zm12-3h-2v-2h2v2Zm0-4h-2v-4h2v4Z" />
  </svg>
);

const InlineSpinner = () => (
  <span
    className="revert-warning-dialog__spinner"
    aria-hidden="true"
  />
);

const BoardStage = ({
  boardInstanceKey,
  boardOrientation,
  displayedFen,
  onPieceDrop,
  onSquareClick,
  allowDragging,
  squareStyles,
  arrows,
  showStartOverlay,
  isGameActive,
  isStartingGame,
  onCloseStartOverlay,
  maiaEloBins,
  engineElo,
  onEngineEloChange,
  botLabel,
  winDelta,
  lossDelta,
  onPlayWhite,
  onPlayRandom,
  onPlayBlack,
  startError,
  showRevertWarning,
  isRevertPending,
  revertError,
  onRevertAnyway,
  onCancelRevert,
  showResignWarning,
  isPracticeContinuation,
  onResignAnyway,
  onCancelResign,
  showEndedScrim,
  showFlash,
  pendingPromotion,
  playerColor,
  onPromotionPick,
  onPromotionCancel,
}: BoardStageProps) => {
  return (
      <div className="chessboard-board-area">
          {showStartOverlay && !isGameActive && (
            <div className="chessboard-overlay">
              <div className="chess-start-panel">
                <button
                  className="chess-start-close"
                  type="button"
                  onClick={onCloseStartOverlay}
                  disabled={isStartingGame}
                  aria-label="Close"
                >
                  ×
                </button>
                <p className="chess-start-title">Difficulty</p>
                <div className="chess-elo-selector">
                  <div className="chess-elo-slider-row">
                    <input
                      type="range"
                      min={0}
                      max={maiaEloBins.length - 1}
                      step={1}
                      value={maiaEloBins.indexOf(engineElo)}
                      onChange={(e) => {
                        const nextElo = maiaEloBins[Number(e.target.value)];
                        if (nextElo !== undefined) {
                          onEngineEloChange(nextElo);
                        }
                      }}
                      disabled={isStartingGame}
                      className="chess-elo-slider"
                    />
                  </div>
                  <div className="chess-elo-bot-row">
                    <OpponentAvatar
                      mode="engine"
                      engineElo={engineElo}
                      size={70}
                    />
                    <span className="chess-elo-label">{botLabel}</span>
                  </div>
                </div>
                <p className="elo-stakes">
                  <span className="elo-stakes__win">Win +{winDelta}</span>
                  {" / "}
                  <span className="elo-stakes__loss">Loss {lossDelta}</span>
                </p>
                <p className="chess-start-title">Side</p>
                <div className="chess-start-options">
                  <button
                    className="chess-button primary"
                    type="button"
                    onClick={onPlayWhite}
                    disabled={isStartingGame}
                  >
                    Play White
                  </button>
                  <button
                    className="chess-button primary"
                    type="button"
                    onClick={onPlayRandom}
                    disabled={isStartingGame}
                  >
                    Play Random
                  </button>
                  <button
                    className="chess-button primary"
                    type="button"
                    onClick={onPlayBlack}
                    disabled={isStartingGame}
                  >
                    Play Black
                  </button>
                </div>
                {startError && <p className="chess-start-error">{startError}</p>}
              </div>
            </div>
          )}
          {showRevertWarning && (
            <div className="chessboard-overlay">
              <div
                className="revert-warning-dialog"
                role="alertdialog"
                aria-labelledby="revert-warning-title"
              >
                <WarningTriangleIcon />
                <p
                  id="revert-warning-title"
                  className="revert-warning-dialog__title"
                >
                  Reverting records this game as a resignation
                </p>
                <p className="revert-warning-dialog__body">
                  The rated result is locked as a loss before the board rewinds.
                  After that, you can keep playing in practice mode.
                </p>
                {revertError && (
                  <p className="chess-start-error" role="alert">
                    {revertError}
                  </p>
                )}
                <div className="revert-warning-dialog__actions">
                  <button
                    className="chess-button danger"
                    type="button"
                    onClick={onRevertAnyway}
                    disabled={isRevertPending}
                  >
                    {isRevertPending ? (
                      <span className="revert-warning-dialog__pending-label">
                        <InlineSpinner />
                        <span>Recording resignation...</span>
                      </span>
                    ) : (
                      "Revert anyway"
                    )}
                  </button>
                  <button
                    className="chess-button"
                    type="button"
                    onClick={onCancelRevert}
                    disabled={isRevertPending}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          )}
          {showResignWarning && (
            <div className="chessboard-overlay">
              <div
                className="revert-warning-dialog"
                role="alertdialog"
                aria-labelledby="resign-warning-title"
              >
                <WarningTriangleIcon />
                <p
                  id="resign-warning-title"
                  className="revert-warning-dialog__title"
                >
                  Are you sure?
                </p>
                <p className="revert-warning-dialog__body">
                  {isPracticeContinuation
                    ? "This will end the current practice continuation."
                    : "Resigning will end the current game and count as a loss."}
                </p>
                <div className="revert-warning-dialog__actions">
                  <button
                    className="chess-button danger"
                    type="button"
                    onClick={onResignAnyway}
                  >
                    Resign
                  </button>
                  <button
                    className="chess-button"
                    type="button"
                    onClick={onCancelResign}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          )}
          {showEndedScrim && <div className="chessboard-ended-scrim" />}
          {showFlash && <div className="blunder-flash" />}
          {pendingPromotion && !showRevertWarning && !showResignWarning && (
            <PromotionPicker
              targetSquare={pendingPromotion.to}
              playerColor={playerColor}
              boardOrientation={boardOrientation}
              onPick={onPromotionPick}
              onCancel={onPromotionCancel}
            />
          )}
          <Chessboard
            key={boardInstanceKey}
            options={{
              position: displayedFen,
              onPieceDrop,
              onSquareClick,
              boardOrientation,
              animationDurationInMs: 200,
              allowDragging,
              squareStyles,
              arrows: arrows.length > 0 ? arrows : undefined,
              boardStyle: {
                borderRadius: "0",
                boxShadow: "0 20px 45px rgba(2, 6, 23, 0.5)",
              },
            }}
          />
      </div>
  );
};

export default memo(BoardStage);
