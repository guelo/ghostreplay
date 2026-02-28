export type GameResult = {
  type: "checkmate_win" | "checkmate_loss" | "draw" | "resign";
  message: string;
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
