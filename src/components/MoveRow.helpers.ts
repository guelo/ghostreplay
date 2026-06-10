import type { MoveClassification } from "../workers/analysisUtils";

export const CLASSIFICATION_ICON: Partial<
  Record<MoveClassification, { icon: string; title: string }>
> = {
  best: { icon: "⭐", title: "Best move" },
  excellent: { icon: "!", title: "Excellent move" },
  good: { icon: "✓", title: "Good move" },
  inaccuracy: { icon: "?!", title: "Inaccuracy" },
  mistake: { icon: "?", title: "Mistake" },
  blunder: { icon: "??", title: "Blunder" },
};

/** Format centipawns as compact string: "+1.2", "−3", "0" */
export const formatEval = (cp: number): string => {
  const value = cp / 100;
  const abs = Math.abs(value);
  const num = abs % 1 === 0 ? abs.toFixed(0) : abs.toFixed(1);
  if (value === 0) return "0";
  return `${value > 0 ? "+" : "−"}${num}`;
};

/** Format a white-perspective eval as a compact string, preferring mate codes.
 *  '#' for checkmate on the board (mate === 0), 'M{n}'/'-M{n}' for mate-in-N,
 *  otherwise the centipawn string. Returns "" when neither is available. */
export const formatWhiteEval = (
  cp: number | null | undefined,
  mate: number | null | undefined,
): string => {
  if (mate != null) {
    if (mate === 0) return "#";
    return mate > 0 ? `M${mate}` : `−M${Math.abs(mate)}`;
  }
  if (cp == null) return "";
  return formatEval(cp);
};

export const classificationClass = (c?: MoveClassification | null): string => {
  if (!c) return "";
  return `move-${c}`;
};
