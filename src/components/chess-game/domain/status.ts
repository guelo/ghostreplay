// How a finished game reached its terminal state. Optional on GameResult so
// existing/synthetic constructions (and any untagged pseudo-end) stay valid and
// fall back to a type-derived default (see deriveEndGameAnnouncement).
export type GameEndReason =
  | "checkmate"
  | "stalemate"
  | "threefold"
  | "insufficient"
  | "fifty_move"
  | "draw"
  | "resignation";

export type GameResult = {
  type: "checkmate_win" | "checkmate_loss" | "draw" | "resign";
  message: string;
  reason?: GameEndReason;
};

// Human-facing termination labels for the end-game fanfare subtitle (g-8079).
export const REASON_LABELS: Record<GameEndReason, string> = {
  checkmate: "Checkmate",
  stalemate: "Stalemate",
  threefold: "Threefold repetition",
  insufficient: "Insufficient material",
  fifty_move: "Fifty-move rule",
  draw: "Draw",
  resignation: "Resignation",
};

// Fallback reason when a GameResult carries no explicit `reason` (older/synthetic
// constructions). Keyed off the coarse result type.
const defaultReasonFor = (type: GameResult["type"]): GameEndReason => {
  switch (type) {
    case "checkmate_win":
    case "checkmate_loss":
      return "checkmate";
    case "resign":
      return "resignation";
    case "draw":
    default:
      return "draw";
  }
};

// Card tone for the over-the-board fanfare. "drill-stop" joins the three
// genuine-game outcomes so a stopped drill can reuse the same card (g-kfc6w).
export type FanfareTone = "win" | "loss" | "draw" | "drill-stop";

export type EndGameAnnouncement = {
  // Drives the `end-game-fanfare--${outcome}` variant class.
  outcome: FanfareTone;
  headline: string;
  reason: string;
};

/**
 * Pure mapping from a finished GameResult to the dramatic over-the-board fanfare
 * copy (g-8079): the big outcome word + the termination-type subtitle. A missing
 * `reason` falls back to the type-derived default so no path renders blank.
 */
export const deriveEndGameAnnouncement = (
  result: GameResult,
): EndGameAnnouncement => {
  const outcome =
    result.type === "checkmate_win"
      ? "win"
      : result.type === "draw"
        ? "draw"
        : "loss";
  const headline =
    outcome === "win" ? "Victory" : outcome === "draw" ? "Draw" : "Defeat";
  const reason = REASON_LABELS[result.reason ?? defaultReasonFor(result.type)];
  return { outcome, headline, reason };
};

export type DrillTerminalReason =
  | "off_route"
  | "accuracy"
  | "natural_end"
  | null;

/**
 * Single source of truth for drill-stop wording (g-kfc6w). Three surfaces read
 * it: the over-the-board fanfare (`headline` + `reason`), the persistent
 * moves-column banner (`headline` + `detail`), and `engineMessage` (`detail`).
 *
 * `reason` and `detail` are deliberately different strings for the same stop:
 * the fanfare and the banner are on screen at the same time, so sharing a
 * string would make every getByText for it ambiguous.
 */
const DRILL_STOP_COPY = {
  accuracy: {
    headline: "Bad move",
    reason: "Too inaccurate",
    detail: "That move exceeded this drill's centipawn limit",
  },
  off_route: {
    headline: "Off route",
    reason: "Left the route",
    detail: "That's not how you get to the opening",
  },
} as const;

/** Neutral banner headline for a drill that stopped without failing. */
const DRILL_STOP_NEUTRAL_HEADLINE = "Drill stopped.";

/**
 * engineMessage text for a stop. Same copy as the panel banner, so the two can
 * never drift; a stop with no recorded reason falls back to the neutral
 * headline rather than claiming a failure mode it does not know about.
 */
export const drillStopEngineMessage = (reason: DrillTerminalReason): string =>
  reason === "accuracy" || reason === "off_route"
    ? DRILL_STOP_COPY[reason].detail
    : DRILL_STOP_NEUTRAL_HEADLINE;

/**
 * Fanfare copy for a drill stop, or null when there is nothing to announce.
 * `natural_end` already ends the game for real and gets the genuine game-end
 * fanfare; a null reason has no story to tell.
 */
export const deriveDrillStopAnnouncement = (
  reason: DrillTerminalReason,
): EndGameAnnouncement | null => {
  if (reason !== "accuracy" && reason !== "off_route") return null;
  const copy = DRILL_STOP_COPY[reason];
  return {
    outcome: "drill-stop",
    headline: copy.headline,
    reason: copy.reason,
  };
};

/**
 * Persistent stop-panel banner copy. Always returns something — the panel is
 * the durable record of the stop once the fanfare has cleared.
 */
export const deriveDrillStopBanner = (
  reason: DrillTerminalReason,
): { variant: "fail" | "neutral"; headline: string; detail: string | null } => {
  if (reason !== "accuracy" && reason !== "off_route") {
    return {
      variant: "neutral",
      headline: DRILL_STOP_NEUTRAL_HEADLINE,
      detail: null,
    };
  }
  const copy = DRILL_STOP_COPY[reason];
  return { variant: "fail", headline: copy.headline, detail: copy.detail };
};

/**
 * Opponent avatar mood for a finished game. The opponent is "victorious" when
 * the player lost (checkmate loss or resignation) and "defeated" when the
 * player won. Draws return null — the avatar image is left unchanged.
 */
export const deriveOpponentAvatarMood = (
  gameResult: GameResult | null,
): "victorious" | "defeated" | null => {
  if (!gameResult) return null;
  switch (gameResult.type) {
    case "checkmate_win":
      return "defeated";
    case "checkmate_loss":
    case "resign":
      return "victorious";
    case "draw":
    default:
      return null;
  }
};

type ChessStatusSource = {
  isCheckmate: () => boolean;
  isDraw: () => boolean;
  isGameOver: () => boolean;
  inCheck: () => boolean;
  turn: () => "w" | "b";
};

export const deriveStatusText = (chess: ChessStatusSource): string => {
  if (chess.isCheckmate()) {
    const winningColor = chess.turn() === "w" ? "Black" : "White";
    return `${winningColor} wins by checkmate`;
  }

  if (chess.isDraw()) {
    return "Drawn position";
  }

  if (chess.isGameOver()) {
    return "Game over";
  }

  const active = chess.turn() === "w" ? "White" : "Black";
  const suffix = chess.inCheck() ? " (check)" : "";
  return `${active} to move${suffix}`;
};

export const deriveGameStatusBadge = (
  isGameActive: boolean,
  gameResult: GameResult | null,
): { label: string; className: string } | null => {
  if (isGameActive) {
    return { label: "Live", className: "game-status-badge--live" };
  }

  if (!gameResult) return null;

  switch (gameResult.type) {
    case "checkmate_win":
      return {
        label: "Win — Checkmate",
        className: "game-status-badge--win",
      };
    case "checkmate_loss":
      return {
        label: "Loss — Checkmate",
        className: "game-status-badge--loss",
      };
    case "draw":
      return { label: "Draw", className: "game-status-badge--other" };
    case "resign":
      return { label: "Resigned", className: "game-status-badge--other" };
    default:
      return null;
  }
};
