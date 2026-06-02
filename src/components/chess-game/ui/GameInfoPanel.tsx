import { Chessboard } from "react-chessboard";
import { memo, useState, type RefObject } from "react";
import type { OpeningLookupResult } from "../../../openings/openingBook";
import type { RatingScoreKey, RatingScores, TargetBlunderSrs } from "../../../utils/api";
import { getRatingDisplayLabel, resolveDisplayScore } from "../../../stores/useGameStore";
import type { ResolvedReview } from "../types";
import { deriveOpponentAvatarMood, type GameResult } from "../domain/status";
import OpponentAvatar from "./OpponentAvatar";

type BoardOrientation = "white" | "black";
type OpponentMode = "ghost" | "engine";

type GameInfoPanelProps = {
  statusText: string;
  gameStatusBadge: { label: string; className: string } | null;
  isRated: boolean;
  isPracticeContinuation: boolean;
  isStoppedDrill?: boolean;
  isGameActive: boolean;
  playerColorChoice: BoardOrientation | "random";
  playerColor: BoardOrientation;
  playerRating: number;
  isProvisional: boolean;
  ratingScores?: RatingScores;
  ratingDisplayType?: RatingScoreKey;
  onRatingDisplayTypeChange?: (type: RatingScoreKey) => void;
  opponentMode: OpponentMode;
  opponentName: string;
  engineElo: number;
  gameResult: GameResult | null;
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
  perfectStreak: {
    current: number;
    personalBest: number;
  };
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

type StreakFireIntensity = "none" | "ember" | "flame" | "hot" | "inferno";

const getStreakFireIntensity = (streak: number): StreakFireIntensity => {
  if (streak >= 12) return "inferno";
  if (streak >= 8) return "hot";
  if (streak >= 5) return "flame";
  if (streak >= 3) return "ember";
  return "none";
};

const PerfectStreakBadge = ({
  current,
  personalBest,
  isGameActive,
}: {
  current: number;
  personalBest: number;
  isGameActive: boolean;
}) => {
  if (current <= 0 && (isGameActive || personalBest <= 0)) {
    return null;
  }

  const hot = current >= 5;
  const pulse = current >= 3;
  const fireIntensity = getStreakFireIntensity(current);
  const classes = [
    "perfect-streak-badge",
    hot ? "perfect-streak-badge--hot" : "",
    pulse ? "perfect-streak-badge--pulse" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      className="perfect-streak"
      aria-label={`Perfect streak ${current}, best ${personalBest}`}
    >
      <span className="perfect-streak__label" aria-hidden="true">
        Streak
      </span>
      <span className={classes} data-fire-intensity={fireIntensity}>
        <span className="perfect-streak-badge__fire" aria-hidden="true">
          <span className="perfect-streak-badge__aura" />
          <span className="perfect-streak-badge__flame perfect-streak-badge__flame--outer" />
          <span className="perfect-streak-badge__flame perfect-streak-badge__flame--middle" />
          <span className="perfect-streak-badge__flame perfect-streak-badge__flame--core" />
          <span className="perfect-streak-badge__ember perfect-streak-badge__ember--one" />
          <span className="perfect-streak-badge__ember perfect-streak-badge__ember--two" />
          <span className="perfect-streak-badge__ember perfect-streak-badge__ember--three" />
        </span>
        <span className="perfect-streak-badge__content">
          <span aria-hidden="true">⭐</span>
          <strong>{current}</strong>
        </span>
      </span>
    </div>
  );
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
  isStoppedDrill = false,
  isGameActive,
  playerColorChoice: _playerColorChoice,
  playerColor: _playerColor,
  playerRating,
  isProvisional,
  ratingScores,
  ratingDisplayType = "elo",
  onRatingDisplayTypeChange,
  opponentMode,
  opponentName,
  engineElo,
  gameResult,
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
  perfectStreak,
}: GameInfoPanelProps) => {
  const [showRatingSettings, setShowRatingSettings] = useState(false);
  const resolvedScores = ratingScores ?? {
    elo: { rating: playerRating, is_provisional: isProvisional },
    chesscom: null,
    lichess: null,
  };
  const displayScore = resolveDisplayScore(resolvedScores, ratingDisplayType);
  const displayLabel = getRatingDisplayLabel(
    resolvedScores[ratingDisplayType] ? ratingDisplayType : "elo",
  );
  return (
    <div className="chess-panel" aria-live="polite">
      <p className="chess-status">{statusText}</p>
      {gameStatusBadge && (
        <span className={`game-status-badge ${gameStatusBadge.className}`}>
          {gameStatusBadge.label}
        </span>
      )}
      {(isPracticeContinuation || isStoppedDrill) && (
        <span className="unrated-badge">Practice</span>
      )}
      {!isPracticeContinuation && !isStoppedDrill && !isRated && isGameActive && (
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
          <span className="chess-panel__desktop-label">Your {displayLabel}: </span>
          <span className="chess-panel__mobile-label">You </span>
          <span className="chess-meta-strong">
            {displayScore.rating}
            {displayScore.is_provisional ? "?" : ""}
          </span>
          <span className="rating-settings-anchor">
            <button
              className="rating-settings-button"
              type="button"
              aria-label="Rating display settings"
              title="Rating display"
              onClick={() => setShowRatingSettings((value) => !value)}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true">
                <path
                  fill="currentColor"
                  d="M19.4 13.5c.1-.5.1-1 .1-1.5s0-1-.1-1.5l2-1.5-2-3.5-2.4 1a8.4 8.4 0 0 0-2.6-1.5L14 2.5h-4l-.4 2.5C8.7 5.3 7.8 5.8 7 6.5l-2.4-1-2 3.5 2 1.5A8.7 8.7 0 0 0 4.5 12c0 .5 0 1 .1 1.5l-2 1.5 2 3.5 2.4-1c.8.7 1.7 1.2 2.6 1.5l.4 2.5h4l.4-2.5c.9-.3 1.8-.8 2.6-1.5l2.4 1 2-3.5-2-1.5ZM12 15.5A3.5 3.5 0 1 1 12 8a3.5 3.5 0 0 1 0 7.5Z"
                />
              </svg>
            </button>
            {showRatingSettings && (
              <div className="rating-settings-popover" role="dialog" aria-label="Rating display">
                {(["elo", "chesscom", "lichess"] as const).map((type) => (
                  <label key={type} className="rating-settings-option">
                    <input
                      type="radio"
                      name="rating-display-type"
                      checked={ratingDisplayType === type}
                      onChange={() => onRatingDisplayTypeChange?.(type)}
                    />
                    {getRatingDisplayLabel(type)}
                  </label>
                ))}
              </div>
            )}
          </span>
        </p>
        {!isGameActive && !gameResult && (
          <p className="chess-meta">Click New game to start</p>
        )}
        {(isGameActive || gameResult !== null) && (
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
                  mood={isGameActive ? null : deriveOpponentAvatarMood(gameResult)}
                />{" "}
                <span className="chess-meta-strong">{opponentName}</span>
              </>
            )}
          </div>
        )}
        <PerfectStreakBadge
          current={perfectStreak.current}
          personalBest={perfectStreak.personalBest}
          isGameActive={isGameActive}
        />
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
