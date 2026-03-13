import { Chessboard } from "react-chessboard";
import type { PieceDropHandlerArgs } from "react-chessboard";
import React, { memo } from "react";
import EvalBar from "../../EvalBar";

type BoardOrientation = "white" | "black";

type BoardStageProps = {
  selectedEvalCp: number | null;
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
  onRevertAnyway: () => void;
  onCancelRevert: () => void;
  showEndedScrim: boolean;
  showFlash: boolean;
  showRehookToast: boolean;
  onDismissRehookToast: () => void;
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

const BoardStage = ({
  selectedEvalCp,
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
  onRevertAnyway,
  onCancelRevert,
  showEndedScrim,
  showFlash,
  showRehookToast,
  onDismissRehookToast,
}: BoardStageProps) => {
  return (
    <div className="chessboard-board-with-eval">
      <EvalBar
        whitePerspectiveCp={selectedEvalCp}
        whiteOnBottom={boardOrientation === "white"}
      />
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
                  <span className="chess-elo-label">{botLabel}</span>
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
                  This game will not be rated
                </p>
                <p className="revert-warning-dialog__body">
                  Reverting a move removes this game from your rating history.
                  This cannot be undone.
                </p>
                <div className="revert-warning-dialog__actions">
                  <button
                    className="chess-button danger"
                    type="button"
                    onClick={onRevertAnyway}
                  >
                    Revert anyway
                  </button>
                  <button
                    className="chess-button"
                    type="button"
                    onClick={onCancelRevert}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          )}
          {showEndedScrim && <div className="chessboard-ended-scrim" />}
          {showFlash && <div className="blunder-flash" />}
          <Chessboard
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
          {showRehookToast && (
            <div
              className="rehook-toast"
              onClick={onDismissRehookToast}
              role="status"
            >
              <span className="rehook-toast__label">Ghost reactivated</span>
              <p className="rehook-toast__detail">
                Ghost reactivated: steering to past mistake
              </p>
            </div>
          )}
      </div>
    </div>
  );
};

export default memo(BoardStage);
