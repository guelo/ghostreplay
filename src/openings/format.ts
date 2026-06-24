export type OpeningTone = "alert" | "watch" | "steady" | "muted";

export function formatScore(value: number | null): string {
  if (value === null) {
    return "—";
  }

  return String(Math.round(value));
}

function normalizePercentValue(value: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }

  const normalizedValue = value <= 1 ? value * 100 : value;
  return Math.min(100, Math.max(0, normalizedValue));
}

export function formatPercent(value: number | null): string {
  if (value === null) {
    return "—";
  }

  return `${Math.round(normalizePercentValue(value))}%`;
}

export function formatGames(value: number | null | undefined): string {
  if (value == null) {
    return "—";
  }

  return value.toLocaleString();
}

// Grade/tone boundaries re-centred onto the observed v2 score distribution
// (g-g5sg, 2026-06-24 calibration: pooled p50≈33, p95≈55, and six included
// pairs all median 31–34). The original A≥85…F<45 scale graded ~95% of cards
// F/alert and conveyed no signal, so the bands now track pooled percentiles:
// A≥50 (~p95) · B≥38 (~p82) · C≥28 (~p40) · D≥22 (~p12) · F<22; tone alert<25
// · watch<38. The raw score is still displayed unchanged (formatScore), so a
// strong card reads e.g. "A · 50" — grade is the relative signal, the number
// is the absolute quality. See docs/openingscore_final.md "Calibration Outcome
// (v2)".
export function getPriorityTone(score: number | null): OpeningTone {
  if (score === null) {
    return "muted";
  }

  if (score < 25) {
    return "alert";
  }

  if (score < 38) {
    return "watch";
  }

  return "steady";
}

export function getPriorityLabel(score: number | null): string {
  if (score === null) {
    return "No Data";
  }

  if (score >= 50) {
    return "A";
  }

  if (score >= 38) {
    return "B";
  }

  if (score >= 28) {
    return "C";
  }

  if (score >= 22) {
    return "D";
  }

  return "F";
}

export type GradeToken = "a" | "b" | "c" | "d" | "f" | "none";

const GRADE_TOKENS: Record<string, GradeToken> = {
  A: "a",
  B: "b",
  C: "c",
  D: "d",
  F: "f",
};

/**
 * Single-letter grade as a CSS-friendly token for accent styling. Derived from
 * getPriorityLabel so the A≥85…F<45 thresholds stay single-sourced; null score
 * (no evidence) maps to "none".
 */
export function getGradeToken(score: number | null): GradeToken {
  if (score === null) {
    return "none";
  }

  return GRADE_TOKENS[getPriorityLabel(score)] ?? "none";
}

/**
 * Accessible-name string for a grade tag, e.g. "Grade A". Deliberately returns
 * sentence-case "No data" for a null score so it reads naturally as an aria
 * label — distinct from getPriorityLabel(null)'s visible "No Data".
 */
export function getGradeText(score: number | null): string {
  if (score === null) {
    return "No data";
  }

  return `Grade ${getPriorityLabel(score)}`;
}

/**
 * Move-number label for a tree node: "Starting position" at the root (ply ≤ 0
 * or no SAN), "1. e4" for White's moves, "1… e5" for Black's.
 */
export function formatMoveLabel(ply: number | null, san: string | null): string {
  if (ply == null || ply <= 0 || san === null) {
    return "Starting position";
  }

  const moveNumber = Math.ceil(ply / 2);
  const isWhite = ply % 2 === 1;
  return isWhite ? `${moveNumber}. ${san}` : `${moveNumber}… ${san}`;
}

/** Human-readable reason a tree line terminates. */
export function formatTerminalReason(reason: string | null): string {
  switch (reason) {
    case "checkmate":
      return "Checkmate";
    case "stalemate":
      return "Stalemate";
    case "opening_boundary":
      return "Opening boundary reached";
    default:
      return "End of line";
  }
}

/** Opening name with a fallback for unclassified positions. */
export function formatOpeningName(name: string | null): string {
  return name ?? "Unclassified";
}
