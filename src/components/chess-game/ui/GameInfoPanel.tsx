import { Chessboard } from "react-chessboard";
import { memo, type RefObject } from "react";
import type { OpeningLookupResult } from "../../../openings/openingBook";
import type { TargetBlunderSrs } from "../../../utils/api";
import type { ResolvedReview } from "../types";
import OpponentAvatar from "./OpponentAvatar";

type BoardOrientation = "white" | "black";
type OpponentMode = "ghost" | "engine";

type GameInfoPanelProps = {
  statusText: string;
  gameStatusBadge: { label: string; className: string } | null;
  isRated: boolean;
  isPracticeContinuation: boolean;
  isGameActive: boolean;
  playerColorChoice: BoardOrientation | "random";
  playerColor: BoardOrientation;
  playerRating: number;
  isProvisional: boolean;
  opponentMode: OpponentMode;
  opponentName: string;
  engineElo: number;
  blunderReviewId: number | null;
  showGhostInfo: boolean;
  onToggleGhostInfo: () => void;
  onCloseGhostInfo: () => void;
  ghostInfoAnchorRef: RefObject<HTMLSpanElement | null>;
  blunderTargetFen: string | null;
  boardOrientation: BoardOrientation;
  blunderReviewSrs: TargetBlunderSrs | null;
  displayedOpening: OpeningLookupResult | null;
  isReviewMomentActive: boolean;
  resolvedReview: ResolvedReview | null;
  isViewingLive: boolean;
  showRehookToast: boolean;
  onDismissRehookToast: () => void;
};

type GameWarningStackProps = {
  className?: string;
  isGameActive: boolean;
  opponentMode: OpponentMode;
  isReviewMomentActive: boolean;
  resolvedReview: ResolvedReview | null;
  isViewingLive: boolean;
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

const formatLastSeen = (isoDate: string): string => {
  const ms = Date.now() - new Date(isoDate).getTime();
  if (ms < 0) return "just now";
  const hours = ms / 3_600_000;
  if (hours < 1) return `${Math.max(1, Math.round(ms / 60_000))}m ago`;
  if (hours < 24) return `${Math.round(hours)}h ago`;
  return new Date(isoDate).toLocaleDateString();
};

export const GameWarningStack = memo(({
  className = "",
  isGameActive,
  opponentMode,
  isReviewMomentActive,
  resolvedReview,
  isViewingLive,
  showRehookToast,
  onDismissRehookToast,
}: GameWarningStackProps) => {
  const reviewWarning =
    resolvedReview && isViewingLive ? (
      <div
        className={`review-warning-toast review-warning-toast--${resolvedReview.result}`}
      >
        <div className="review-warning-toast__header">
          <WarningTriangleIcon />
          <span className="review-warning-toast__label">Review Position</span>
        </div>
        <p className="review-warning-toast__detail">
          Be careful. You've messed this position up before.
        </p>
        {resolvedReview.result !== "pending" && (
          <div className="review-warning-toast__overlay">
            <span className="review-warning-toast__overlay-icon">
              {resolvedReview.result === "pass" ? "✓" : "✗"}
            </span>
          </div>
        )}
      </div>
    ) : isReviewMomentActive ? (
      <div className="review-warning-toast" role="alert">
        <div className="review-warning-toast__header">
          <WarningTriangleIcon />
          <span className="review-warning-toast__label">Review Position</span>
        </div>
        <p className="review-warning-toast__detail">
          Be careful. You've messed this position up before.
        </p>
      </div>
    ) : null;

  const showWarningStack =
    reviewWarning !== null ||
    (isGameActive && opponentMode === "ghost" && showRehookToast);

  if (!showWarningStack) {
    return null;
  }

  return (
    <div className={`chess-warning-stack ${className}`.trim()}>
      {isGameActive && opponentMode === "ghost" && showRehookToast && (
        <button
          className="rehook-toast"
          onClick={onDismissRehookToast}
          type="button"
        >
          <span className="rehook-toast__label">Ghost reactivated</span>
          <span className="rehook-toast__detail">
            Steering to past mistake
          </span>
        </button>
      )}
      {reviewWarning}
    </div>
  );
});

const GameInfoPanel = ({
  statusText,
  gameStatusBadge,
  isRated,
  isPracticeContinuation,
  isGameActive,
  playerColorChoice: _playerColorChoice,
  playerColor: _playerColor,
  playerRating,
  isProvisional,
  opponentMode,
  opponentName,
  engineElo,
  blunderReviewId,
  showGhostInfo,
  onToggleGhostInfo,
  onCloseGhostInfo,
  ghostInfoAnchorRef,
  blunderTargetFen,
  boardOrientation,
  blunderReviewSrs,
  displayedOpening,
  isReviewMomentActive,
  resolvedReview,
  isViewingLive,
  showRehookToast,
  onDismissRehookToast,
}: GameInfoPanelProps) => {
  return (
    <div className="chess-panel" aria-live="polite">
      <p className="chess-status">{statusText}</p>
      {gameStatusBadge && (
        <span className={`game-status-badge ${gameStatusBadge.className}`}>
          {gameStatusBadge.label}
        </span>
      )}
      {isPracticeContinuation && isGameActive && (
        <span className="unrated-badge">Practice</span>
      )}
      {!isPracticeContinuation && !isRated && isGameActive && (
        <span className="unrated-badge">Unrated</span>
      )}
      <div
        className={
          isGameActive
            ? "chess-panel__active-matchup"
            : "chess-panel__inactive-summary"
        }
      >
        <p className="chess-meta chess-panel__player-rating">
          <span className="chess-panel__desktop-label">Your Elo: </span>
          <span className="chess-panel__mobile-label">You </span>
          <span className="chess-meta-strong">
            {playerRating}
            {isProvisional ? "?" : ""}
          </span>
        </p>
        {!isGameActive && <p className="chess-meta">Click New game to start</p>}
        {isGameActive && (
          <div
            className={`chess-meta chess-panel__opponent${
              opponentMode === "ghost"
                ? " chess-meta--ghost"
                : " chess-meta--engine"
            }`}
          >
            <span className="chess-panel__desktop-label">Opponent: </span>
            <span className="chess-panel__mobile-versus">vs</span>
            {opponentMode === "ghost" ? (
              <>
                <OpponentAvatar mode="ghost" engineElo={engineElo} size={70} />{" "}
                <span className="chess-meta-strong ghost-mode-label">
                  Replay Ghost
                </span>
                {blunderReviewId !== null && (
                  <span className="ghost-info-anchor" ref={ghostInfoAnchorRef}>
                    <button
                      className="ghost-info-btn"
                      onClick={onToggleGhostInfo}
                      aria-label="Toggle ghost info"
                      title="Ghost target info"
                    >
                      <svg
                        width="16"
                        height="16"
                        viewBox="0 0 24 24"
                        fill="currentColor"
                        aria-hidden="true"
                      >
                        <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2Zm1 15h-2v-6h2v6Zm0-8h-2V7h2v2Z" />
                      </svg>
                    </button>
                    {showGhostInfo && (
                      <div className="ghost-info-box">
                        <div className="ghost-info-box__header">
                          <span className="ghost-info-box__title">
                            Ghost Target Blunder Position
                          </span>
                          <button
                            className="ghost-info-box__close"
                            onClick={onCloseGhostInfo}
                            aria-label="Close ghost info"
                          >
                            &times;
                          </button>
                        </div>
                        {blunderTargetFen && (
                          <div className="ghost-info-box__board">
                            <Chessboard
                              options={{
                                position: blunderTargetFen,
                                boardOrientation,
                                allowDragging: false,
                                animationDurationInMs: 0,
                                boardStyle: { borderRadius: "4px" },
                              }}
                            />
                          </div>
                        )}
                        {blunderReviewSrs && (
                          <div className="ghost-info-box__srs">
                            <span>
                              Last seen:{" "}
                              {blunderReviewSrs.last_reviewed_at
                                ? formatLastSeen(
                                    blunderReviewSrs.last_reviewed_at,
                                  )
                                : blunderReviewSrs.created_at
                                  ? formatLastSeen(blunderReviewSrs.created_at)
                                  : "never"}
                            </span>
                            <span>
                              Pass/Fail: {blunderReviewSrs.pass_count}/
                              {blunderReviewSrs.fail_count}
                            </span>
                            <span>Streak: {blunderReviewSrs.pass_streak}</span>
                          </div>
                        )}
                      </div>
                    )}
                  </span>
                )}
              </>
            ) : (
              <>
                <OpponentAvatar
                  mode="engine"
                  engineElo={engineElo}
                  size={70}
                />{" "}
                <span className="chess-meta-strong">{opponentName}</span>
              </>
            )}
          </div>
        )}
      </div>
      <GameWarningStack
        className="chess-warning-stack--panel"
        isGameActive={isGameActive}
        opponentMode={opponentMode}
        isReviewMomentActive={isReviewMomentActive}
        resolvedReview={resolvedReview}
        isViewingLive={isViewingLive}
        showRehookToast={showRehookToast}
        onDismissRehookToast={onDismissRehookToast}
      />
      {isGameActive && (
        <p className="chess-meta chess-panel__opening">
          Opening:{" "}
          <span className="chess-meta-strong">
            {displayedOpening
              ? `${displayedOpening.eco} ${displayedOpening.name}`
              : "Unknown"}
          </span>
        </p>
      )}
    </div>
  );
};

export default memo(GameInfoPanel);
