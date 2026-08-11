import { memo } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import SettingsGearIcon from "./SettingsGearIcon";

type DrillStopActionsProps = {
  terminalReason: "off_route" | "accuracy" | "natural_end" | null;
  onAnotherDrill: (event: ReactMouseEvent<HTMLButtonElement>) => void;
  /** Open the setup overlay to change drill settings (gear button). */
  onAnotherDrillSettings: () => void;
  onAnalyze: () => void;
  /** Hide the Analyze button when there are no moves to review. */
  analyzeEnabled: boolean;
  /**
   * Hide Analyze entirely. Defaults to showing it for the live drill-stopped
   * state.
   */
  showAnalyze?: boolean;
  /** Disable Analyze and show a preparing label while the snapshot is built. */
  isPreparing: boolean;
  /** Disable the restart actions while a new drill is starting. */
  disabled?: boolean;
  /** Keep restart event-capable while its opening score is reconciling. */
  drillAgainPending?: boolean;
  /** Non-blocking error (e.g. abandon failed) shown above the actions. */
  errorMessage?: string | null;
};

const subtitleFor = (reason: DrillStopActionsProps["terminalReason"]): string => {
  if (reason === "accuracy") {
    return "Bad move";
  }
  if (reason === "off_route") {
    return "That's not how you get to the opening";
  }
  return "Drill stopped.";
};

const DrillStopActions = ({
  terminalReason,
  onAnotherDrill,
  onAnotherDrillSettings,
  onAnalyze,
  analyzeEnabled,
  showAnalyze = true,
  isPreparing,
  disabled,
  drillAgainPending = false,
  errorMessage,
}: DrillStopActionsProps) => {
  return (
    <div className="chess-start-error" role="region" aria-label="Drill stopped — choose next action">
      <p>{subtitleFor(terminalReason)}</p>
      {errorMessage && (
        <p role="alert" className="drill-stop-error">
          {errorMessage}
        </p>
      )}
      <div className="chess-post-game-actions">
        <span className="drill-again-group">
          <button
            className={`chess-button primary${drillAgainPending ? " drill-again-waiting" : ""}`}
            type="button"
            onClick={onAnotherDrill}
            disabled={isPreparing || disabled}
            aria-disabled={drillAgainPending || undefined}
            aria-busy={drillAgainPending || undefined}
            aria-label={
              drillAgainPending
                ? "Updating score before another drill"
                : undefined
            }
          >
            {drillAgainPending ? "Updating score…" : "Again"}
          </button>
          <button
            className="drill-settings-gear"
            type="button"
            aria-label="Change drill settings"
            title="Change drill settings"
            onClick={onAnotherDrillSettings}
            disabled={isPreparing || disabled}
          >
            <SettingsGearIcon />
          </button>
        </span>
        {showAnalyze && analyzeEnabled && (
          <button
            className="chess-button"
            type="button"
            onClick={onAnalyze}
            disabled={isPreparing}
          >
            {isPreparing ? "Preparing analysis…" : "Analyze"}
          </button>
        )}
      </div>
    </div>
  );
};

export default memo(DrillStopActions);
