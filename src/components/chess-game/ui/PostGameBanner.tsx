import type { RatingChange } from "../../../utils/api";
import type { GameResult } from "../domain/status";

type PostGameBannerProps = {
  isGameActive: boolean;
  showPostGamePrompt: boolean;
  gameResult: GameResult | null;
  ratingChange: RatingChange | null;
  onViewAnalysis: () => void;
  onShowStartOverlay: () => void;
  onViewHistory: () => void;
};

const PostGameBanner = ({
  isGameActive,
  showPostGamePrompt,
  gameResult,
  ratingChange,
  onViewAnalysis,
  onShowStartOverlay,
  onViewHistory,
}: PostGameBannerProps) => {
  if (showPostGamePrompt && gameResult) {
    return (
      <div
        className="game-end-banner"
        role="region"
        aria-label="Post-game options"
      >
        <p className="game-end-banner-message">{gameResult.message}</p>
        {ratingChange && (
          <p
            className={`rating-delta ${ratingChange.rating_after >= ratingChange.rating_before ? "rating-delta--up" : "rating-delta--down"}`}
          >
            {ratingChange.rating_after >= ratingChange.rating_before ? "+" : ""}
            {ratingChange.rating_after - ratingChange.rating_before}{" "}
            <span className="rating-delta__value">
              ({ratingChange.rating_before} → {ratingChange.rating_after}
              {ratingChange.is_provisional ? "?" : ""})
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

export default PostGameBanner;
