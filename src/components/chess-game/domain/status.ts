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

export type EndGameAnnouncement = {
  outcome: "win" | "loss" | "draw";
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
