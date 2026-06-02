export type GameResult = {
  type: "checkmate_win" | "checkmate_loss" | "draw" | "resign";
  message: string;
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
