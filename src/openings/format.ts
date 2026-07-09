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

// Grade/tone boundaries re-centred onto the readiness-fold score distribution
// (g-xnv7, 2026-07-09 final grid: lcb_z=1, coverage_fold=gate,
// coverage_live_threshold=1; pooled p50≈10, p75≈21, p95≈44 across five
// included pairs). Bands keep the prior percentile intent after the score now
// folds sample sufficiency and opponent breadth: A≥44 (~p95), B≥29 (~p82),
// C≥8 (~p40), D≥2 (~p12), F<2; tone alert<5 (~p25), watch<29. The raw score is
// still displayed unchanged (formatScore), so grade is the relative signal and
// the number is the absolute readiness score. See docs/openingscore_final.md
// "Calibration Outcome (v2)".
export function getPriorityTone(score: number | null): OpeningTone {
  if (score === null) {
    return "muted";
  }

  if (score < 5) {
    return "alert";
  }

  if (score < 29) {
    return "watch";
  }

  return "steady";
}

export function getPriorityLabel(score: number | null): string {
  if (score === null) {
    return "No Data";
  }

  if (score >= 44) {
    return "A";
  }

  if (score >= 29) {
    return "B";
  }

  if (score >= 8) {
    return "C";
  }

  if (score >= 2) {
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
 * getPriorityLabel so thresholds stay single-sourced; null score (no evidence)
 * maps to "none".
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

/** One rendered move in an opening card's move-list line. `isLast` marks the
 *  move that crossed into the opening — the card bolds it to disambiguate
 *  sibling cards that share an inherited name. */
export interface MoveListToken {
  text: string;
  isLast: boolean;
}

/**
 * Turn a SAN move list into display tokens ("1.e4", "c6", "2.Bc4"), numbered
 * from `startPly` (the ply of `sanMoves[0]`; 1 = White's move 1). White plies
 * (odd) get an "{n}." prefix; Black plies print the SAN alone. The card joins
 * tokens with spaces and bolds the `isLast` token.
 *
 * Empty input short-circuits to `[]` regardless of `startPly`, so the defensive
 * empty case (a family card with `moves: []`) renders no secondary line.
 */
export function buildMoveListTokens(
  sanMoves: string[],
  startPly?: number | null,
): MoveListToken[] {
  // Nullish guard is defensive: a stale/partial API response (or a test fixture)
  // may omit the list; the type says string[] but real data can lag the schema.
  if (!sanMoves || sanMoves.length === 0) {
    return [];
  }
  const base = startPly == null || startPly <= 0 ? 1 : startPly;
  return sanMoves.map((san, index) => {
    const ply = base + index;
    const isWhite = ply % 2 === 1;
    const moveNumber = Math.ceil(ply / 2);
    return {
      text: isWhite ? `${moveNumber}.${san}` : san,
      isLast: index === sanMoves.length - 1,
    };
  });
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
