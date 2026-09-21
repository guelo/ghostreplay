/**
 * Shared warning triangle. Used by the revert/resign dialogs and the board
 * notice (BoardStage) and by the drill-stop panel banner (DrillStopActions).
 *
 * The intrinsic 48px size is deliberate — the dialogs render it at that size.
 * Smaller placements override width/height in CSS (see
 * `.board-notice .warning-triangle-icon`).
 */
const WarningTriangleIcon = () => (
  <svg
    className="warning-triangle-icon"
    width="48"
    height="48"
    viewBox="0 0 24 24"
    fill="currentColor"
    aria-hidden="true"
  >
    <path d="M1 21h22L12 2 1 21Zm12-3h-2v-2h2v2Zm0-4h-2v-4h2v4Z" />
  </svg>
);

export default WarningTriangleIcon;
