import { memo } from "react";

type DrillStopActionsProps = {
  terminalReason: "off_route" | "accuracy" | "natural_end" | null;
  onAnotherDrill: () => void;
  onContinueAsNormal: () => void;
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
  onContinueAsNormal,
}: DrillStopActionsProps) => {
  return (
    <div className="chess-start-error" role="region" aria-label="Drill stopped — choose next action">
      <p>{subtitleFor(terminalReason)}</p>
      <div className="chess-post-game-actions">
        <button
          className="chess-button primary"
          type="button"
          onClick={onAnotherDrill}
        >
          Again
        </button>
        <button
          className="chess-button"
          type="button"
          onClick={onContinueAsNormal}
        >
          Continue as normal game
        </button>
      </div>
    </div>
  );
};

export default memo(DrillStopActions);
