import { memo } from "react";

type DrillStopActionsProps = {
  terminalReason: "off_route" | "accuracy" | "natural_end" | null;
  onAnotherDrill: () => void;
  onAnalyze: () => void;
  /** Hide the Analyze button when there are no moves to review. */
  analyzeEnabled: boolean;
  /** Disable Analyze and show a preparing label while the snapshot is built. */
  isPreparing: boolean;
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
  onAnalyze,
  analyzeEnabled,
  isPreparing,
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
        <button
          className="chess-button primary"
          type="button"
          onClick={onAnotherDrill}
          disabled={isPreparing}
        >
          Again
        </button>
        {analyzeEnabled && (
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
