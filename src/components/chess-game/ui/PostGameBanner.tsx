import { memo } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import SettingsGearIcon from "./SettingsGearIcon";
import type {
  DrillSessionState,
  OpeningScoreDeltaItem,
  RatingChange,
  RatingScoreKey,
  RatingScores,
} from "../../../utils/api";
import { getRatingDisplayLabel } from "../../../stores/useGameStore";
import type { OpeningDeltaFreshness } from "../../../stores/useGameStore";
import type { SessionAccuracyStatus } from "../../../hooks/useSessionAccuracy";
import type { GameResult } from "../domain/status";
import { formatOpeningDeltaValue } from "../../../utils/openingDeltaBadge";
import { bannerOpeningRows } from "./PostGameBanner.helpers";

type PostGameBannerProps = {
  isGameActive: boolean;
  isPracticeContinuation: boolean;
  showPostGamePrompt: boolean;
  gameResult: GameResult | null;
  drillOpeningKey?: string | null;
  drillState?: DrillSessionState | null;
  /**
   * Returned from /drill-analysis to the just-played (abandoned) drill (g-65ve).
   * Renders the "Again" + settings presentation instead of the generic inactive
   * "New game" banner, without reviving the abandoned backend session.
   */
  isReviewedDrillReturn?: boolean;
  onNewDrill?: (event: ReactMouseEvent<HTMLButtonElement>) => void;
  /** Open the setup overlay to change drill settings (gear button). */
  onAnotherDrillSettings?: () => void;
  /** Disable the restart actions while a new drill is starting. */
  drillActionsDisabled?: boolean;
  /** Keep only the repeat action event-capable while score reconciliation runs. */
  drillAgainPending?: boolean;
  ratingChange: RatingChange | null;
  scoreChanges?: RatingScores | null;
  ratingDisplayType?: RatingScoreKey;
  /** Player accuracy (0-100) for the ended session, once it settles. */
  accuracy?: number | null;
  /** Lifecycle of `accuracy`; the row is hidden unless this is pending or ready. */
  accuracyStatus?: SessionAccuracyStatus;
  /**
   * This session's opening score changes — the SAME session-stamped items the
   * lineage cards badge. The banner adds rows for them; it does not replace the
   * inline badges, and a delta showing in both places post-game is intended.
   */
  openingScoreChanges?: OpeningScoreDeltaItem[] | null;
  /** Freshness of `openingScoreChanges`; drives the placeholder / omit decision. */
  openingDeltaFreshness?: OpeningDeltaFreshness | null;
  onViewAnalysis: () => void;
  onShowStartOverlay: () => void;
};

const PostGameBanner = ({
  isGameActive,
  isPracticeContinuation,
  showPostGamePrompt,
  gameResult,
  drillOpeningKey,
  drillState,
  isReviewedDrillReturn,
  onNewDrill,
  onAnotherDrillSettings,
  drillActionsDisabled,
  drillAgainPending = false,
  ratingChange,
  scoreChanges,
  ratingDisplayType = "elo",
  accuracy = null,
  accuracyStatus = "idle",
  openingScoreChanges,
  openingDeltaFreshness,
  onViewAnalysis,
  onShowStartOverlay,
}: PostGameBannerProps) => {
  const selectedChange = scoreChanges?.[ratingDisplayType] ?? scoreChanges?.elo;
  const selectedLabel = getRatingDisplayLabel(
    scoreChanges?.[ratingDisplayType] ? ratingDisplayType : "elo",
  );
  const selectedDelta = selectedChange?.rating;
  const isStoppedDrill = Boolean(drillOpeningKey) && drillState === "failed";

  // Reviewed-return presentation (g-65ve): the drill-stopped actions are restored
  // separately (DrillStopActions), so suppress the banner entirely here — in
  // particular the generic inactive "New game" branch below — to avoid a
  // duplicate/misleading "Drill abandoned" message.
  if (isReviewedDrillReturn) {
    return null;
  }

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
            <span className="drill-again-group">
              <button
                className={`chess-button primary${drillAgainPending ? " drill-again-waiting" : ""}`}
                type="button"
                onClick={onNewDrill}
                disabled={drillActionsDisabled}
                aria-disabled={drillAgainPending || undefined}
                aria-busy={drillAgainPending || undefined}
                aria-label={
                  drillAgainPending
                    ? "Updating score before another drill"
                    : undefined
                }
              >
                {drillAgainPending ? "Updating score…" : "Another drill"}
              </button>
              {onAnotherDrillSettings && (
                <button
                  className="drill-settings-gear"
                  type="button"
                  aria-label="Change drill settings"
                  title="Change drill settings"
                  onClick={onAnotherDrillSettings}
                  disabled={drillActionsDisabled}
                >
                  <SettingsGearIcon />
                </button>
              )}
            </span>
          )}
        </div>
      </div>
    );
  }

  if (showPostGamePrompt && gameResult) {
    // Drills get NO stat stack (g-frlfp). This is defence-in-depth, not a live
    // discriminator: every reachable drill terminal shape already returned above
    // (natural end) or renders DrillStopActions instead, and the one state that
    // would land here — a CONVERTED drill — has been unreachable since g-a406
    // removed the "Continue as normal game" action. Deliberately not carved out
    // on drillState !== "converted": that state is being deleted (g-rm-drill-convert).
    const showStats = !drillOpeningKey;

    const eloDelta = ratingChange
      ? selectedDelta ?? ratingChange.rating_after - ratingChange.rating_before
      : 0;

    // The placeholder decision keys on whether the items are KNOWN, not on
    // freshness. A TERMINAL delta arrives carrying its items and stays "pending"
    // only until the reconciliation poll can prove it fresh
    // (useGameStore.setTerminalOpeningDelta), so gating on freshness would leave
    // the banner on a dash for that whole window while the lineage badges beside
    // it already showed the numbers — exactly the divergence these rows must not
    // introduce. A BOUNDARY delta is the genuinely-unknown case: it is pending
    // with items === null, and only then can the changed-only filter not run.
    const openingsUnknown = openingScoreChanges == null;
    const openingsPending =
      openingsUnknown && openingDeltaFreshness === "pending";
    const changedOpenings = openingsUnknown
      ? []
      : bannerOpeningRows(openingScoreChanges);

    const showEloRow = showStats && Boolean(ratingChange) && !isPracticeContinuation;
    const showAccuracyRow =
      showStats &&
      (accuracyStatus === "pending" ||
        (accuracyStatus === "ready" && accuracy != null));
    const showOpeningRows =
      showStats && (openingsPending || changedOpenings.length > 0);
    // The list element itself is conditional so an all-flat game collapses cleanly
    // instead of leaving an empty gap above the action buttons.
    const showStatList = showEloRow || showAccuracyRow || showOpeningRows;

    return (
      <div
        className="game-end-banner game-end-banner--stacked"
        role="region"
        aria-label="Post-game options"
      >
        <p className="game-end-banner-message">{gameResult.message}</p>
        {showStatList && (
          <div className="game-end-stats">
            {showEloRow && ratingChange && (
              <p
                className={`game-end-stat ${eloDelta >= 0 ? "game-end-stat--up" : "game-end-stat--down"}`}
              >
                <span className="game-end-stat__label">
                  {selectedLabel} change:
                </span>
                <span className="game-end-stat__delta">
                  {`${eloDelta >= 0 ? "+" : ""}${eloDelta}`}
                </span>
                {selectedLabel === "Elo" && (
                  <span className="game-end-stat__value">
                    {`(${ratingChange.rating_before} -> ${ratingChange.rating_after}${ratingChange.is_provisional ? "?" : ""})`}
                  </span>
                )}
              </p>
            )}
            {showAccuracyRow && (
              <p
                className={`game-end-stat${accuracyStatus === "pending" ? " game-end-stat--muted" : ""}`}
                aria-busy={accuracyStatus === "pending" || undefined}
              >
                <span className="game-end-stat__label">Accuracy:</span>
                <span className="game-end-stat__delta">
                  {accuracyStatus === "ready" && accuracy != null
                    ? `${accuracy}%`
                    : "—"}
                </span>
              </p>
            )}
            {showOpeningRows && openingsPending && (
              <p className="game-end-stat game-end-stat--muted" aria-busy>
                <span className="game-end-stat__label">Opening scores:</span>
                <span className="game-end-stat__delta">—</span>
              </p>
            )}
            {showOpeningRows &&
              !openingsPending &&
              changedOpenings.map((row) => {
                // `is_new` has no before to subtract from, so it is toneless: it
                // reads as "new -> 41.0", not as a numeric gain.
                const tone = row.isNew
                  ? ""
                  : row.badge.dir === "up"
                    ? " game-end-stat--up"
                    : " game-end-stat--down";
                return (
                  <p key={row.key} className={`game-end-stat${tone}`}>
                    <span className="game-end-stat__label" title={row.name}>
                      {row.name}:
                    </span>
                    <span className="game-end-stat__delta">
                      {row.isNew
                        ? "new"
                        : `${row.badge.diff > 0 ? "+" : ""}${formatOpeningDeltaValue(row.badge.diff)}`}
                    </span>
                    <span className="game-end-stat__value">
                      {`-> ${formatOpeningDeltaValue(row.badge.after)}`}
                    </span>
                  </p>
                );
              })}
          </div>
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
          {drillOpeningKey && onNewDrill && (
            <span className="drill-again-group">
              <button
                className={`chess-button primary${drillAgainPending ? " drill-again-waiting" : ""}`}
                type="button"
                onClick={onNewDrill}
                disabled={drillActionsDisabled}
                aria-disabled={drillAgainPending || undefined}
                aria-busy={drillAgainPending || undefined}
                aria-label={
                  drillAgainPending
                    ? "Updating score before another drill"
                    : undefined
                }
              >
                {drillAgainPending ? "Updating score…" : "New Drill"}
              </button>
              {onAnotherDrillSettings && (
                <button
                  className="drill-settings-gear"
                  type="button"
                  aria-label="Change drill settings"
                  title="Change drill settings"
                  onClick={onAnotherDrillSettings}
                  disabled={drillActionsDisabled}
                >
                  <SettingsGearIcon />
                </button>
              )}
            </span>
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
