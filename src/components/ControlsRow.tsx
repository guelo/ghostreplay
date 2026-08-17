import React from "react";
import MaterialDisplay from "./MaterialDisplay";
import "./ControlsRow.css";

export type ControlsRowProps = {
  className?: string;
  onResign?: () => void;
  isResignDisabled?: boolean;
  onRevert?: () => void;
  isRevertDisabled?: boolean;
  onFlipBoard?: () => void;
  onCopyPosition?: () => void;
  onReset?: () => void;
  isGameActive?: boolean;
  isInteractionDisabled?: boolean;
  canAddSelectedMove?: boolean;
  isAddingSelectedMove?: boolean;
  onAddSelectedMove?: (index: number) => void;
  /** Index used when the add button is clicked (effective selected move). */
  effectiveIndex?: number;
  /** When supplied, render a MaterialDisplay at the trailing edge of the row.
   *  The responsive reveal CSS assumes the default `.controls-row` class;
   *  custom-class callers must provide equivalent styling. */
  materialFen?: string;
  materialPerspective?: "white" | "black";
};

/** Shared row of game-control icon buttons.
 *  MoveList supplies its desktop class; the horizontal list uses the default
 *  mobile controls-row class. */
const ControlsRow = ({
  className = "controls-row",
  onResign,
  isResignDisabled = false,
  onRevert,
  isRevertDisabled = false,
  onFlipBoard,
  onCopyPosition,
  onReset,
  isGameActive = false,
  isInteractionDisabled = false,
  canAddSelectedMove = false,
  isAddingSelectedMove = false,
  onAddSelectedMove,
  effectiveIndex = -1,
  materialFen,
  materialPerspective,
}: ControlsRowProps) => {
  const hasAddButton = Boolean(onAddSelectedMove);
  // Both list callers derive -1 for empty history, so this is also the shared
  // non-empty-history guard that the former desktop action block expressed.
  const isAddEnabled = hasAddButton && effectiveIndex >= 0 && canAddSelectedMove;
  const hasMaterial = Boolean(materialFen && materialPerspective);

  if (
    !(
      onResign ||
      onFlipBoard ||
      onCopyPosition ||
      onReset ||
      onRevert ||
      hasAddButton
    ) &&
    !hasMaterial
  ) {
    return null;
  }

  return (
    <div className={className}>
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
      {onCopyPosition && (
        <button
          className="move-action-button"
          type="button"
          onClick={onCopyPosition}
          title="Copy position FEN"
        >
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <rect x="9" y="9" width="13" height="13" rx="2" />
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
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
      {hasMaterial && (
        <div className="controls-row__material">
          <MaterialDisplay fen={materialFen!} perspective={materialPerspective!} />
        </div>
      )}
    </div>
  );
};

export default React.memo(ControlsRow);
