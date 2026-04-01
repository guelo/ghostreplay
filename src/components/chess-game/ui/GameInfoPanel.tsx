import { Chessboard } from "react-chessboard";
import { memo, type RefObject } from "react";
import type { OpeningLookupResult } from "../../../openings/openingBook";
import type { TargetBlunderSrs } from "../../../utils/api";

type BoardOrientation = "white" | "black";

type GameInfoPanelProps = {
  statusText: string;
  gameStatusBadge: { label: string; className: string } | null;
  isRated: boolean;
  isGameActive: boolean;
  playerColorChoice: BoardOrientation | "random";
  playerColor: BoardOrientation;
  playerRating: number;
  isProvisional: boolean;
  opponentMode: "ghost" | "engine";
  opponentName: string;
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
};

const GhostIcon = () => (
  <svg
    className="ghost-icon"
    width="24"
    height="24"
    viewBox="0 0 24 24"
    fill="currentColor"
    aria-hidden="true"
  >
    <path d="M12 2C7.58 2 4 5.58 4 10v10.5c0 .83 1 1.25 1.59.66l1.41-1.41 1.41 1.41a.996.996 0 0 0 1.41 0L11.24 19.75l1.41 1.41a.996.996 0 0 0 1.41 0l1.41-1.41 1.41 1.41c.59.59 1.59.17 1.59-.66V10c0-4.42-3.58-8-8-8Zm-2 11a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3Zm4 0a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3Z" />
  </svg>
);

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

const GameInfoPanel = ({
  statusText,
  gameStatusBadge,
  isRated,
  isGameActive,
  playerColorChoice: _playerColorChoice,
  playerColor: _playerColor,
  playerRating,
  isProvisional,
  opponentMode,
  opponentName,
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
}: GameInfoPanelProps) => {
  return (
    <div className="chess-panel" aria-live="polite">
      <p className="chess-status">{statusText}</p>
      {gameStatusBadge && (
        <span className={`game-status-badge ${gameStatusBadge.className}`}>
          {gameStatusBadge.label}
        </span>
      )}
      {!isRated && isGameActive && (
        <span className="unrated-badge">Unrated</span>
      )}
      <p className="chess-meta">
        Your Elo:{" "}
        <span className="chess-meta-strong">
          {playerRating}
          {isProvisional ? "?" : ""}
        </span>
      </p>
      {!isGameActive && (
        <p className="chess-meta">
          Click New game to start
        </p>
      )}
      {isGameActive && (
        <div
          className={`chess-meta${opponentMode === "ghost" ? " chess-meta--ghost" : ""}`}
        >
          Opponent:{" "}
          {opponentMode === "ghost" ? (
            <>
              <GhostIcon />{" "}
              <span className="chess-meta-strong ghost-mode-label">Ghost</span>
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
                              ? formatLastSeen(blunderReviewSrs.last_reviewed_at)
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
            <span className="chess-meta-strong">{opponentName}</span>
          )}
        </div>
      )}
      {isGameActive && (
        <p className="chess-meta">
          Opening:{" "}
          <span className="chess-meta-strong">
            {displayedOpening
              ? `${displayedOpening.eco} ${displayedOpening.name}`
              : "Unknown"}
          </span>
        </p>
      )}
      {isReviewMomentActive && (
        <div className="review-warning-toast" role="alert">
          <div className="review-warning-toast__header">
            <WarningTriangleIcon />
            <span className="review-warning-toast__label">Review Position</span>
          </div>
          <p className="review-warning-toast__detail">
            Be careful. You've messed this position up before.
          </p>
        </div>
      )}
    </div>
  );
};

export default memo(GameInfoPanel);
