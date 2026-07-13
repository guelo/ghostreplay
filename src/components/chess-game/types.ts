export type BoardOrientation = "white" | "black";

export type ResolvedReview = {
  analysisId: string;
  moveIndex: number;
  result: 'pending' | 'pass' | 'fail';
};

/**
 * A single board-anchored notice rendered top-left over the chessboard. Exactly
 * one is shown at a time (see useBoardNotice). `nonce` increments every time a
 * notice is (re)triggered so React re-keys the entrance animation and the
 * auto-dismiss timer restarts.
 */
export type BoardNotice =
  | { kind: "review-warning"; nonce: number }
  | { kind: "review-result"; result: "pass" | "fail"; nonce: number }
  | { kind: "rehook"; nonce: number };

export type OpenHistoryOptions = {
  select: "latest";
  source: "post_game_view_analysis";
  sessionId?: string;
};
