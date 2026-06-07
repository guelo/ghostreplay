import React from "react";

export type ControlsRowProps = {
  onResign?: () => void;
  isResignDisabled?: boolean;
  onRevert?: () => void;
  isRevertDisabled?: boolean;
  onFlipBoard?: () => void;
  onReset?: () => void;
  isGameActive?: boolean;
  isInteractionDisabled?: boolean;
  canAddSelectedMove?: boolean;
  isAddingSelectedMove?: boolean;
  onAddSelectedMove?: (index: number) => void;
  /** Index used when the add button is clicked (effective selected move). */
  effectiveIndex?: number;
};

/** Row of game-control icon buttons (resign / flip / add / revert / reset).
 *  Extracted from MoveList's `.move-list-actions` block so the horizontal move
 *  list can render the same controls above the strip. */
const ControlsRow = ({
  onResign,
  isResignDisabled = false,
  onRevert,
  isRevertDisabled = false,
  onFlipBoard,
  onReset,
  isGameActive = false,
  isInteractionDisabled = false,
  canAddSelectedMove = false,
  isAddingSelectedMove = false,
  onAddSelectedMove,
  effectiveIndex = -1,
}: ControlsRowProps) => {
  const hasAddButton = Boolean(onAddSelectedMove);
  const isAddEnabled = hasAddButton && effectiveIndex >= 0 && canAddSelectedMove;

  if (!(onResign || onFlipBoard || onReset || onRevert || hasAddButton)) {
    return null;
  }

  return (
    <div className="controls-row">
      {onResign && (
        <button
          className="move-action-button danger"
          type="button"
          onClick={onResign}
          disabled={isInteractionDisabled || isResignDisabled}
          title="Resign"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M5 3v18h2v-7h4.5l.5 1h5V5h-5l-.5-1H7V3H5Zm4 2h3.5l.5 1h3v6h-3l-.5-1H7V5h2Z" />
          </svg>
        </button>
      )}
      {onFlipBoard && (
        <button
          className="move-action-button"
          type="button"
          onClick={onFlipBoard}
          disabled={isInteractionDisabled}
          title="Flip board"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M17 1l4 4-4 4" />
            <path d="M3 11V9a4 4 0 0 1 4-4h14" />
            <path d="M7 23l-4-4 4-4" />
            <path d="M21 13v2a4 4 0 0 1-4 4H3" />
          </svg>
        </button>
      )}
      {hasAddButton && (
        <button
          className="move-action-button"
          type="button"
          onClick={() => {
            if (onAddSelectedMove && effectiveIndex >= 0) {
              onAddSelectedMove(effectiveIndex);
            }
          }}
          disabled={isInteractionDisabled || !isAddEnabled || isAddingSelectedMove}
          title="Add selected move to ghost library"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
            <line x1="12" y1="5" x2="12" y2="19" />
            <line x1="5" y1="12" x2="19" y2="12" />
          </svg>
        </button>
      )}
      {isGameActive && onRevert && (
        <button
          className="move-action-button"
          type="button"
          onClick={onRevert}
          disabled={isInteractionDisabled || isRevertDisabled}
          title="Revert last move"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M3 10h10a5 5 0 0 1 0 10H11" />
            <polyline points="7 6 3 10 7 14" />
          </svg>
        </button>
      )}
      {onReset && (
        <button
          className="move-action-button"
          type="button"
          onClick={onReset}
          disabled={isInteractionDisabled}
          title="Reset game"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <polyline points="1 4 1 10 7 10" />
            <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
          </svg>
        </button>
      )}
    </div>
  );
};

export default React.memo(ControlsRow);
