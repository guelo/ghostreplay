import { memo } from "react";
import type {
  DrillSessionState,
  RatingChange,
  RatingScoreKey,
  RatingScores,
} from "../../../utils/api";
import { getRatingDisplayLabel } from "../../../stores/useGameStore";
import type { GameResult } from "../domain/status";

type PostGameBannerProps = {
  isGameActive: boolean;
  isPracticeContinuation: boolean;
  showPostGamePrompt: boolean;
  gameResult: GameResult | null;
  drillOpeningKey?: string | null;
  drillState?: DrillSessionState | null;
  onNewDrill?: () => void;
  ratingChange: RatingChange | null;
  scoreChanges?: RatingScores | null;
  ratingDisplayType?: RatingScoreKey;
  onViewAnalysis: () => void;
  onShowStartOverlay: () => void;
  onViewHistory: () => void;
};

const PostGameBanner = ({
  isGameActive,
  isPracticeContinuation,
  showPostGamePrompt,
  gameResult,
  drillOpeningKey,
  drillState,
  onNewDrill,
  ratingChange,
  scoreChanges,
  ratingDisplayType = "elo",
  onViewAnalysis,
  onShowStartOverlay,
  onViewHistory,
}: PostGameBannerProps) => {
  const selectedChange = scoreChanges?.[ratingDisplayType] ?? scoreChanges?.elo;
  const selectedLabel = getRatingDisplayLabel(
    scoreChanges?.[ratingDisplayType] ? ratingDisplayType : "elo",
  );
  const selectedDelta = selectedChange?.rating;
  const isStoppedDrill = Boolean(drillOpeningKey) && drillState === "failed";

  if (showPostGamePrompt && gameResult && isStoppedDrill) {
    return (
      <div
        className="game-end-banner"
        role="region"
        aria-label="Post-game options"
      >
        <p className="game-end-banner-message">{gameResult.message}</p>
        <div className="chess-post-game-actions">
          {onNewDrill && (
            <button
              className="chess-button primary"
              type="button"
              onClick={onNewDrill}
            >
              Another drill
            </button>
          )}
        </div>
      </div>
    );
  }

  if (showPostGamePrompt && gameResult) {
    return (
      <div
        className="game-end-banner"
        role="region"
        aria-label="Post-game options"
      >
        <p className="game-end-banner-message">{gameResult.message}</p>
        {ratingChange && !isPracticeContinuation && (
          <p
            className={`rating-delta ${(selectedDelta ?? ratingChange.rating_after - ratingChange.rating_before) >= 0 ? "rating-delta--up" : "rating-delta--down"}`}
          >
            {(selectedDelta ?? ratingChange.rating_after - ratingChange.rating_before) >= 0 ? "+" : ""}
            {selectedDelta ?? ratingChange.rating_after - ratingChange.rating_before}{" "}
            <span className="rating-delta__value">
              {selectedLabel === "Elo"
                ? `(${ratingChange.rating_before} -> ${ratingChange.rating_after}${ratingChange.is_provisional ? "?" : ""})`
                : `(${selectedLabel})`}
            </span>
          </p>
        )}
        <div className="chess-post-game-actions">
          <button
            className="chess-button primary"
            type="button"
            onClick={onViewAnalysis}
          >
            View Analysis
          </button>
          <button
            className="chess-button"
            type="button"
            onClick={onShowStartOverlay}
          >
            New Game
          </button>
          <button className="chess-button" type="button" onClick={onViewHistory}>
            History
          </button>
          {drillOpeningKey && onNewDrill && (
            <button
              className="chess-button primary"
              type="button"
              onClick={onNewDrill}
            >
              New Drill
            </button>
          )}
        </div>
      </div>
    );
  }

  if (!isGameActive && !showPostGamePrompt) {
    return (
      <div className="game-end-banner">
        <p className="game-end-banner-message">
          {gameResult ? gameResult.message : "Ready for a new game?"}
        </p>
        <button
          className="chess-button primary"
          type="button"
          onClick={onShowStartOverlay}
        >
          New game
        </button>
      </div>
    );
  }

  return null;
};

export default memo(PostGameBanner);
