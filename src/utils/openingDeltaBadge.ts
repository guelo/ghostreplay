import type { OpeningScoreDeltaItem } from "./api";

export type OpeningDeltaBadge = {
  /** Display values quantized to one decimal place. */
  before: number;
  diff: number;
  after: number;
  dir: "up" | "down";
};

/** Terminal score-change values always render at exactly one decimal place. */
export function formatOpeningDeltaValue(value: number): string {
  return value.toFixed(1);
}

/** A concise, non-visual description shared by the card and its compact action. */
export function describeOpeningDeltaBadge(badge: OpeningDeltaBadge): string {
  const direction = badge.dir === "up" ? "increased" : "decreased";
  return `Score ${direction} by ${formatOpeningDeltaValue(Math.abs(badge.diff))}, now ${formatOpeningDeltaValue(badge.after)}`;
}

/**
 * Derive the score-diff badge for one opening, or null to render nothing.
 * Quantize both endpoints to integer tenths before subtracting. That makes the
 * displayed delta exactly equal the displayed after minus displayed before and
 * suppresses changes whose endpoints resolve to the same visible tenth.
 *
 * Brand-new openings (is_new) have no baseline, so a visible diff is quantified
 * against 0. The card reveal then starts at 0.0 and promotes the resolved score,
 * while the capsule communicates the full gain (g-ptea). A new score that rounds
 * to 0.0 has no visible diff and remains unscored.
 *
 * Extracted from GameOpeningLineage (g-f3m4) so the inline lineage badges and the
 * last-drill toast agree on exactly what counts as a change — a delta that renders
 * nothing inline must never be surfaced as a toast either.
 */
export function badgeFor(
  change: OpeningScoreDeltaItem | undefined | null,
): OpeningDeltaBadge | null {
  if (!change || change.after == null) return null;
  const afterTenths = Math.round(change.after * 10);
  const beforeTenths = change.is_new
    ? 0
    : change.before == null
      ? null
      : Math.round(change.before * 10);
  if (beforeTenths == null) return null;
  const diffTenths = afterTenths - beforeTenths;
  if (diffTenths === 0) return null;
  const before = beforeTenths / 10;
  const after = afterTenths / 10;
  const diff = diffTenths / 10;
  return { before, diff, after, dir: diff > 0 ? "up" : "down" };
}

/**
 * Whether a delta payload would render at least one badge. Null / empty / fully
 * badge-suppressed payloads are unrenderable: they must not be queued as late
 * notifications, or an invisible head would block the drills behind it.
 */
export function hasRenderableBadge(
  items: OpeningScoreDeltaItem[] | null | undefined,
): boolean {
  return (items ?? []).some((item) => badgeFor(item) !== null);
}
