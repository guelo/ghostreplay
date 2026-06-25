import { memo, useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import StaticMiniBoard from "./StaticMiniBoard";
import SettingsGearIcon from "./SettingsGearIcon";
import type { RatingScoreKey, RatingScores, TargetBlunderSrs } from "../../../utils/api";
import { getRatingDisplayLabel, resolveDisplayScore, useGameStore } from "../../../stores/useGameStore";
import type { ResolvedReview } from "../types";
import { deriveOpponentAvatarMood, type GameResult } from "../domain/status";
import OpponentAvatar from "./OpponentAvatar";
import MaterialDisplay from "../../MaterialDisplay";

type BoardOrientation = "white" | "black";
type OpponentMode = "ghost" | "engine";

type GameInfoPanelProps = {
  statusText: string;
  gameStatusBadge: { label: string; className: string } | null;
  isRated: boolean;
  isPracticeContinuation: boolean;
  isStoppedDrill?: boolean;
  isGameActive: boolean;
  isActiveDrill?: boolean;
  drillOpeningName?: string | null;
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
  /** Live opening-lineage hierarchy (broadest -> deepest), rendered in place of
   *  the old single-line "Opening: …". Owned by ChessGame; null/undefined when
   *  there is nothing to show (e.g. before the first boundary, or inactive). */
  openingLineageSlot?: ReactNode;
  isReviewMomentActive: boolean;
  resolvedReview: ResolvedReview | null;
  isViewingLive: boolean;
  showRehookToast: boolean;
  onDismissRehookToast: () => void;
  perfectStreak: {
    current: number;
    personalBest: number;
  };
  /** Mobile-portrait only: opponent-capture material relocated into the panel.
   *  Hidden by default CSS; shown inside the 659px block. */
  materialFen?: string;
  materialPerspective?: BoardOrientation;
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
          This position has come back to haunt you. You've messed this up before.
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
          This position has come back to haunt you. You've messed this up before.
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
          <span className="rehook-toast__label">The haunting resumes</span>
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
  isActiveDrill = false,
  drillOpeningName,
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
  openingLineageSlot,
  isReviewMomentActive,
  resolvedReview,
  isViewingLive,
  showRehookToast,
  onDismissRehookToast,
  perfectStreak,
  materialFen,
  materialPerspective,
}: GameInfoPanelProps) => {
  const [showSettings, setShowSettings] = useState(false);
  const gearButtonRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const soundMuted = useGameStore((s) => s.soundMuted);
  const soundVolume = useGameStore((s) => s.soundVolume);
  const setSoundMuted = useGameStore((s) => s.setSoundMuted);
  const setSoundVolume = useGameStore((s) => s.setSoundVolume);
  const volumePercent = Math.round(soundVolume * 100);

  useEffect(() => {
    if (!showSettings) return;

    const closeAndRestore = () => {
      setShowSettings(false);
      gearButtonRef.current?.focus();
    };

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        closeAndRestore();
      }
    };

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        dialogRef.current?.contains(target) ||
        gearButtonRef.current?.contains(target)
      ) {
        return;
      }
      setShowSettings(false);
    };

    dialogRef.current?.focus();
    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("pointerdown", handlePointerDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("pointerdown", handlePointerDown);
    };
  }, [showSettings]);

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
      <div className="panel-header">
        {isActiveDrill ? (
          <p className="chess-status chess-status--drill">
            <span className="chess-status__drill-label">Drilling:</span>{" "}
            {drillOpeningName ?? "Opening"}
          </p>
        ) : (
          <p className="chess-status">{statusText}</p>
        )}
        <span className="panel-settings">
          <button
            ref={gearButtonRef}
            className="rating-settings-button"
            type="button"
            aria-label="Game settings"
            title="Game settings"
            aria-haspopup="dialog"
            aria-expanded={showSettings}
            onClick={() => setShowSettings((value) => !value)}
          >
            <SettingsGearIcon />
          </button>
          {showSettings && (
            <div
              ref={dialogRef}
              className="panel-settings__popover rating-settings-popover"
              role="dialog"
              aria-label="Game settings"
              tabIndex={-1}
            >
              <div className="panel-settings__section">
                <span className="panel-settings__heading">Sound</span>
                <label className="rating-settings-option">
                  <input
                    type="checkbox"
                    checked={soundMuted}
                    onChange={(e) => setSoundMuted(e.target.checked)}
                  />
                  Mute
                </label>
                <label className="rating-settings-option panel-settings__volume">
                  <input
                    type="range"
                    min={0}
                    max={100}
                    step={1}
                    value={volumePercent}
                    disabled={soundMuted}
                    aria-label="Volume"
                    aria-valuetext={`${volumePercent}%`}
                    onChange={(e) =>
                      setSoundVolume(Number(e.target.value) / 100)
                    }
                  />
                  <span className="panel-settings__volume-value">
                    {volumePercent}%
                  </span>
                </label>
              </div>
              <div className="panel-settings__section">
                <span className="panel-settings__heading">Rating display</span>
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
            </div>
          )}
        </span>
      </div>
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
                            <StaticMiniBoard
                              fen={blunderTargetFen}
                              orientation={boardOrientation}
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
      {openingLineageSlot}
      {materialFen && materialPerspective && (
        <div className="chess-panel__material">
          <MaterialDisplay fen={materialFen} perspective={materialPerspective} />
        </div>
      )}
    </div>
  );
};

export default memo(GameInfoPanel);
