import { Chessboard } from "react-chessboard";
import type { PieceDropHandlerArgs } from "react-chessboard";
import React, { memo, useRef } from "react";
import type { OpeningRootItem } from "../../../utils/api";
import { PromotionPicker } from "./PromotionPicker";
import StartPanel, { type StartDrillDraft } from "./StartPanel";
import SrsFailSpotlight, { type SrsFailTrigger } from "./SrsFailSpotlight";
import type { BoardNotice } from "../types";

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
  // Live game + not on the latest move: wash the squares so the board reads as
  // history, not the live position (a lighter cousin of the what-if wash).
  isReviewingPast?: boolean;
  // Allows the start overlay to open over a stopped drill (isGameActive stays
  // true so handleNewDrill can abandon the failed session when a new drill starts).
  isStoppedDrill?: boolean;
  isStartingGame: boolean;
  onCloseStartOverlay: () => void;
  maiaEloBins: readonly number[];
  // Seeds for the start panel — non-committed. StartPanel drafts from these and
  // commits to game state only on Start, so opening/cancelling never mutates it.
  seedEngineElo: number;
  seedStrictnessCp: number;
  seedColor: "white" | "black";
  seedOpening: OpeningRootItem | null;
  seedLine: string[] | null;
  playerRating: number;
  isProvisional: boolean;
  onStartPlay: (side: "white" | "random" | "black", engineElo: number) => void;
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
  // Single board-anchored notice (review warning / result / rehook), top-left.
  boardNotice: BoardNotice | null;
  // Drill mode props
  isDrillMode?: boolean;
  onSwitchToPlayMode?: () => void;
  onSwitchToDrillMode?: () => void;
  openingFamilies?: Array<{ family_name: string; roots: OpeningRootItem[] }> | null;
  onStartDrill: (draft: StartDrillDraft) => void;
  isLoadingOpenings?: boolean;
  // Repeat-mistake spotlight: nonce trigger + completion callback.
  srsFailTrigger?: SrsFailTrigger | null;
  onSrsFailDone?: (id: number) => void;
};

const WarningTriangleIcon = () => (
  <svg
    className="warning-triangle-icon"
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
  isReviewingPast = false,
  isStoppedDrill = false,
  isStartingGame,
  onCloseStartOverlay,
  maiaEloBins,
  seedEngineElo,
  seedStrictnessCp,
  seedColor,
  seedOpening,
  seedLine,
  playerRating,
  isProvisional,
  onStartPlay,
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
  boardNotice,
  isDrillMode = false,
  onSwitchToPlayMode,
  onSwitchToDrillMode,
  openingFamilies,
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
          {boardNotice && (
            <div
              key={boardNotice.nonce}
              className={
                boardNotice.kind === "review-result"
                  ? `board-notice board-notice--review-result board-notice--${boardNotice.result}`
                  : `board-notice board-notice--${boardNotice.kind}`
              }
              role={boardNotice.kind === "review-warning" ? "alert" : "status"}
              aria-live={
                boardNotice.kind === "review-warning" ? "assertive" : "polite"
              }
            >
              {boardNotice.kind === "review-warning" && (
                <>
                  <div className="board-notice__header">
                    <WarningTriangleIcon />
                    <span className="board-notice__label">Review Position</span>
                  </div>
                  <p className="board-notice__detail">
                    You've blundered here before.
                  </p>
                </>
              )}
              {boardNotice.kind === "review-result" && (
                <div className="board-notice__result">
                  <span className="board-notice__result-icon" aria-hidden="true">
                    {boardNotice.result === "pass" ? "✓" : "✗"}
                  </span>
                  <span className="board-notice__result-label">
                    {boardNotice.result === "pass"
                      ? "Blunder Avoided!"
                      : "Blundered again"}
                  </span>
                </div>
              )}
              {boardNotice.kind === "rehook" && (
                <>
                  <span className="board-notice__label">
                    The haunting resumes
                  </span>
                  <span className="board-notice__detail">
                    Ghost is replaying moves to your old blunder.
                  </span>
                </>
              )}
            </div>
          )}
          {showStartOverlay && (!isGameActive || isStoppedDrill) && (
            <div className="chessboard-overlay">
              <StartPanel
                isDrillMode={isDrillMode}
                isStartingGame={isStartingGame}
                startError={startError}
                onClose={onCloseStartOverlay}
                onSwitchToPlayMode={onSwitchToPlayMode ?? (() => {})}
                onSwitchToDrillMode={onSwitchToDrillMode ?? (() => {})}
                maiaEloBins={maiaEloBins}
                seedEngineElo={seedEngineElo}
                seedStrictnessCp={seedStrictnessCp}
                seedColor={seedColor}
                seedOpening={seedOpening}
                seedLine={seedLine}
                playerRating={playerRating}
                isProvisional={isProvisional}
                openingFamilies={openingFamilies ?? null}
                isLoadingOpenings={isLoadingOpenings}
                onStartPlay={onStartPlay}
                onStartDrill={onStartDrill}
              />
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
          <div
            ref={boardSquareRef}
            className={`chessboard-square-measure${isReviewingPast ? " chessboard-square-measure--reviewing" : ""}`}
          >
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
