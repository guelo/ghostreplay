import { Chessboard, defaultPieces } from "react-chessboard";
import type { PieceDropHandlerArgs } from "react-chessboard";
import React, { memo, useRef } from "react";
import type { OpeningRootItem } from "../../../utils/api";
import { PromotionPicker } from "./PromotionPicker";
import OpponentAvatar from "./OpponentAvatar";
import DrillSetupPanel from "./DrillSetupPanel";
import SrsFailSpotlight, { type SrsFailTrigger } from "./SrsFailSpotlight";

type BoardOrientation = "white" | "black";

const WhiteKing = defaultPieces.wK;
const BlackKing = defaultPieces.bK;

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
  // Allows the start overlay to open over a stopped drill (isGameActive stays
  // true so handleNewDrill can abandon the failed session when a new drill starts).
  isStoppedDrill?: boolean;
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
  streakToast: { type: "milestone" | "record"; streak: number } | null;
  // Drill mode props
  isDrillMode?: boolean;
  onSwitchToPlayMode?: () => void;
  onSwitchToDrillMode?: () => void;
  openingFamilies?: Array<{ family_name: string; roots: OpeningRootItem[] }> | null;
  selectedDrillOpening?: OpeningRootItem | null;
  drillPlayerColor?: "white" | "black";
  drillStrictnessCp?: number;
  onSelectDrillOpening?: (opening: OpeningRootItem | null) => void;
  onDrillPlayerColorChange?: (color: "white" | "black") => void;
  onDrillStrictnessChange?: (cp: number) => void;
  onStartDrill?: () => void;
  isLoadingOpenings?: boolean;
  // Repeat-mistake spotlight: nonce trigger + completion callback.
  srsFailTrigger?: SrsFailTrigger | null;
  onSrsFailDone?: (id: number) => void;
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
  isStoppedDrill = false,
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
  streakToast,
  isDrillMode = false,
  onSwitchToPlayMode,
  onSwitchToDrillMode,
  openingFamilies,
  selectedDrillOpening,
  drillPlayerColor = "white",
  drillStrictnessCp = 25,
  onSelectDrillOpening,
  onDrillPlayerColorChange,
  onDrillStrictnessChange,
  onStartDrill,
  isLoadingOpenings = false,
  srsFailTrigger = null,
  onSrsFailDone,
}: BoardStageProps) => {
  const boardSquareRef = useRef<HTMLDivElement | null>(null);
  return (
      <div className="chessboard-board-area">
          {streakToast && (
            <div
              className={`streak-toast streak-toast--${streakToast.type}`}
              role="status"
              aria-live="polite"
            >
              <span className="streak-toast__label">
                {streakToast.type === "record"
                  ? `New record: ${streakToast.streak}`
                  : `${streakToast.streak} best moves`}
              </span>
              <span className="streak-toast__detail">⭐ Perfect streak</span>
            </div>
          )}
          {showStartOverlay && (!isGameActive || isStoppedDrill) && (
            <div className="chessboard-overlay">
              <div className={`chess-start-panel${isDrillMode ? " chess-start-panel--drill" : ""}`}>
                <button
                  className="chess-start-close"
                  type="button"
                  onClick={onCloseStartOverlay}
                  disabled={isStartingGame}
                  aria-label="Close"
                >
                  ×
                </button>

                <div className="mode-toggle-row segmented-toggle">
                  <button
                    className={`chess-button toggle${!isDrillMode ? " active" : ""}`}
                    type="button"
                    onClick={onSwitchToPlayMode}
                    disabled={isStartingGame}
                  >
                    Play
                  </button>
                  <button
                    className={`chess-button toggle${isDrillMode ? " active" : ""}`}
                    type="button"
                    onClick={onSwitchToDrillMode}
                    disabled={isStartingGame}
                  >
                    Drill
                  </button>
                </div>

                <div className={`chess-start-scroll${isDrillMode ? " chess-start-scroll--drill" : ""}`}>
                  {isDrillMode ? (
                    <DrillSetupPanel
                      openingFamilies={openingFamilies ?? null}
                      selectedOpening={selectedDrillOpening ?? null}
                      playerColor={drillPlayerColor}
                      engineElo={engineElo}
                      strictnessCp={drillStrictnessCp}
                      maiaEloBins={maiaEloBins}
                      botLabel={botLabel}
                      isLoadingOpenings={isLoadingOpenings}
                      isStarting={isStartingGame}
                      startError={startError}
                      onSelectOpening={onSelectDrillOpening ?? (() => {})}
                      onPlayerColorChange={onDrillPlayerColorChange ?? (() => {})}
                      onEngineEloChange={onEngineEloChange}
                      onStrictnessChange={onDrillStrictnessChange ?? (() => {})}
                      onStartDrill={onStartDrill ?? (() => {})}
                    />
                  ) : (
                    <>
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
                          className="play-side-button"
                          type="button"
                          aria-label="Play White"
                          onClick={onPlayWhite}
                          disabled={isStartingGame}
                        >
                          <span className="play-side-button__piece">
                            <WhiteKing />
                          </span>
                          <span className="play-side-button__label">White</span>
                        </button>
                        <button
                          className="play-side-button"
                          type="button"
                          aria-label="Play Random"
                          onClick={onPlayRandom}
                          disabled={isStartingGame}
                        >
                          <span className="play-side-button__piece play-side-button__piece--split">
                            <span className="play-side-king play-side-king--left">
                              <WhiteKing />
                            </span>
                            <span className="play-side-king play-side-king--right">
                              <BlackKing />
                            </span>
                          </span>
                          <span className="play-side-button__label">Random</span>
                        </button>
                        <button
                          className="play-side-button"
                          type="button"
                          aria-label="Play Black"
                          onClick={onPlayBlack}
                          disabled={isStartingGame}
                        >
                          <span className="play-side-button__piece">
                            <BlackKing />
                          </span>
                          <span className="play-side-button__label">Black</span>
                        </button>
                      </div>
                      {startError && <p className="chess-start-error">{startError}</p>}
                    </>
                  )}
                </div>
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
          <div ref={boardSquareRef} className="chessboard-square-measure">
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
                // The measuring wrapper sets line-height: 0 to kill the inline gap
                // below the board; without restoring it here the coordinate glyphs
                // collapse to zero height and get clipped by the square edges.
                alphaNotationStyle: { lineHeight: 1 },
                numericNotationStyle: { lineHeight: 1 },
                arrows: arrows.length > 0 ? arrows : undefined,
                boardStyle: {
                  borderRadius: "0",
                  boxShadow: "0 20px 45px rgba(2, 6, 23, 0.5)",
                },
              }}
            />
          </div>
          <SrsFailSpotlight
            trigger={srsFailTrigger}
            targetRef={boardSquareRef}
            onDone={onSrsFailDone ?? (() => {})}
          />
      </div>
  );
};

export default memo(BoardStage);
