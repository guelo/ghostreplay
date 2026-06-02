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

export function formatGames(value: number | null): string {
  if (value === null) {
    return "—";
  }

  return value.toLocaleString();
}

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
