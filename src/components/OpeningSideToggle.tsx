import { memo } from "react";
import { defaultPieces } from "react-chessboard";
import type { OpeningPlayerColor } from "../utils/api";

const WhiteKing = defaultPieces.wK;
const BlackKing = defaultPieces.bK;

const COLOR_OPTIONS: Array<{
  label: string;
  value: OpeningPlayerColor;
  King: typeof WhiteKing;
}> = [
  { label: "White", value: "white", King: WhiteKing },
  { label: "Black", value: "black", King: BlackKing },
];

type OpeningSideToggleProps = {
  playerColor: OpeningPlayerColor | null;
  onPlayerColorChange: (color: OpeningPlayerColor) => void;
  disabled?: boolean;
};

/** Shared controlled side selector for opening-tree hosts. */
const OpeningSideToggle = ({
  playerColor,
  onPlayerColorChange,
  disabled = false,
}: OpeningSideToggleProps) => (
  <div
    className="mode-toggle-row segmented-toggle opening-side-toggle"
    role="group"
    aria-label="Playing as"
  >
    {COLOR_OPTIONS.map(({ label, value, King }) => (
      <button
        key={value}
        type="button"
        className={`chess-button toggle${
          playerColor === value ? " active" : ""
        }`}
        aria-pressed={playerColor === value}
        onClick={() => onPlayerColorChange(value)}
        disabled={disabled}
      >
        <span className="opening-side-toggle__piece">
          <King />
        </span>
        {label}
      </button>
    ))}
  </div>
);

export default memo(OpeningSideToggle);
