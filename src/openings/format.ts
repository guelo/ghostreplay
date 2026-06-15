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

// Grade/tone boundaries retained at the original A≥85…F<45 scale. The first
// populated v2 calibration (g-m36y) showed a low-skewed distribution that would
// argue for re-centring, but the cohort was ~95% one user — too thin to move a
// product-facing scale. Revisit when more populated pairs exist. See
// docs/openingscore_final.md "Calibration Outcome (v2)".
export function getPriorityTone(score: number | null): OpeningTone {
  if (score === null) {
    return "muted";
  }

  if (score < 45) {
    return "alert";
  }

  if (score < 65) {
    return "watch";
  }

  return "steady";
}

export function getPriorityLabel(score: number | null): string {
  if (score === null) {
    return "No Data";
  }

  if (score >= 85) {
    return "A";
  }

  if (score >= 70) {
    return "B";
  }

  if (score >= 55) {
    return "C";
  }

  if (score >= 45) {
    return "D";
  }

  return "F";
}
