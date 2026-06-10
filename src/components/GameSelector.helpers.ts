/** Locale-aware short date: en-US -> 06/05/26, en-GB -> 05/06/26. */
export function formatShortDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, {
    month: "2-digit",
    day: "2-digit",
    year: "2-digit",
  });
}

export function resultLabel(result: string | null): string {
  switch (result) {
    case "checkmate_win":
      return "Win";
    case "checkmate_loss":
      return "Loss";
    case "resign":
      return "Resigned";
    case "draw":
      return "Draw";
    default:
      return result ?? "Unknown";
  }
}

/** Map a game result to a win/loss/draw outcome class for coloring. */
export function resultClass(result: string | null): "win" | "loss" | "draw" {
  switch (result) {
    case "checkmate_win":
      return "win";
    case "checkmate_loss":
    case "resign":
      return "loss";
    default:
      return "draw";
  }
}
