import { memo, type ReactNode, type RefObject } from "react";
import StaticMiniBoard from "./StaticMiniBoard";
import SoundToggleButton from "./SoundToggleButton";
import type { RatingScores } from "../../../utils/api";
import { deriveOpponentAvatarMood, type GameResult } from "../domain/status";
import type { OpponentPresentation } from "../domain/opponentPresentation";
import OpponentAvatar from "./OpponentAvatar";
import MaterialDisplay from "../../MaterialDisplay";

type BoardOrientation = "white" | "black";

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
  opponentPresentation: OpponentPresentation;
  opponentName: string;
  engineElo: number;
  gameResult: GameResult | null;
  showGhostInfo: boolean;
  onToggleGhostInfo: () => void;
  onCloseGhostInfo: () => void;
  ghostInfoAnchorRef: RefObject<HTMLSpanElement | null>;
  boardOrientation: BoardOrientation;
  /** Live opening-lineage hierarchy (broadest -> deepest), rendered in place of
   *  the old single-line "Opening: …". Owned by ChessGame; null/undefined when
   *  there is nothing to show (e.g. before the first boundary, or inactive). */
  openingLineageSlot?: ReactNode;
  perfectStreak: {
    current: number;
    personalBest: number;
  };
  /** Mobile-portrait only: opponent-capture material relocated into the panel.
   *  Hidden by default CSS; shown inside the 659px block. */
  materialFen?: string;
  materialPerspective?: BoardOrientation;
  /** The narrow game layout relocates this control into the move actions row. */
  showSoundToggle?: boolean;
};

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
  opponentPresentation,
  opponentName,
  engineElo,
  gameResult,
  showGhostInfo,
  onToggleGhostInfo,
  onCloseGhostInfo,
  ghostInfoAnchorRef,
  boardOrientation,
  openingLineageSlot,
  perfectStreak,
  materialFen,
  materialPerspective,
  showSoundToggle = true,
}: GameInfoPanelProps) => {
  const resolvedScores = ratingScores ?? {
    elo: { rating: playerRating, is_provisional: isProvisional },
    chesscom: null,
    lichess: null,
  };
  const displayScore = resolvedScores.elo;
  const displayLabel = "Elo";
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
        {showSoundToggle && (
          <span className="panel-settings">
            <SoundToggleButton className="panel-sound-toggle" />
          </span>
        )}
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
              opponentPresentation.kind !== "engine"
                ? " chess-meta--ghost"
                : " chess-meta--engine"
            }`}
          >
            <span className="chess-panel__desktop-label">Opponent: </span>
            <span className="chess-panel__mobile-versus">vs</span>
            {opponentPresentation.kind !== "engine" ? (
              <>
                <OpponentAvatar mode="ghost" engineElo={engineElo} size={70} />{" "}
                <span className="chess-meta-strong ghost-mode-label">
                  {opponentPresentation.kind === "targeted_ghost"
                    ? "Replay Ghost"
                    : "Opening Guide"}
                </span>
                {opponentPresentation.kind === "targeted_ghost" && (
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
                        {opponentPresentation.targetFen ? (
                          <div className="ghost-info-box__board">
                            <StaticMiniBoard
                              fen={opponentPresentation.targetFen}
                              orientation={boardOrientation}
                            />
                          </div>
                        ) : (
                          <div className="ghost-info-box__unavailable">
                            Target position preview unavailable.
                          </div>
                        )}
                        {opponentPresentation.targetBlunderSrs ? (
                          <div className="ghost-info-box__srs">
                            <span>
                              Last seen:{" "}
                              {opponentPresentation.targetBlunderSrs.last_reviewed_at
                                ? formatLastSeen(
                                    opponentPresentation.targetBlunderSrs
                                      .last_reviewed_at,
                                  )
                                : opponentPresentation.targetBlunderSrs.created_at
                                  ? formatLastSeen(
                                      opponentPresentation.targetBlunderSrs.created_at,
                                    )
                                  : "never"}
                            </span>
                            <span>
                              Pass/Fail: {opponentPresentation.targetBlunderSrs.pass_count}/
                              {opponentPresentation.targetBlunderSrs.fail_count}
                            </span>
                            <span>
                              Streak: {opponentPresentation.targetBlunderSrs.pass_streak}
                            </span>
                          </div>
                        ) : (
                          <div className="ghost-info-box__unavailable">
                            Review history unavailable.
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
