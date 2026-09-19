import type { OpeningScoreDeltaItem } from "./api";

export type OpeningDeltaBadge = {
  /** Display values rounded to whole numbers. */
  before: number;
  diff: number;
  after: number;
  dir: "up" | "down";
};

/** Score-change values always render as whole numbers. */
export function formatOpeningDeltaValue(value: number): string {
  return String(Math.round(value));
}

/** A concise, non-visual description shared by the card and its compact action. */
export function describeOpeningDeltaBadge(badge: OpeningDeltaBadge): string {
  const direction = badge.dir === "up" ? "increased" : "decreased";
  return `Score ${direction} by ${formatOpeningDeltaValue(Math.abs(badge.diff))}, now ${formatOpeningDeltaValue(badge.after)}`;
}

/**
 * Derive the score-diff badge for one opening, or null to render nothing.
 * Round both endpoints to whole numbers before subtracting. That makes the
 * displayed delta exactly equal the displayed after minus displayed before and
 * suppresses changes whose endpoints resolve to the same visible whole number.
 *
 * Brand-new openings (is_new) have no baseline, so a visible diff is quantified
 * against 0. The card reveal then starts at 0 and promotes the resolved score,
 * while the capsule communicates the full gain (g-ptea). A new score that rounds
 * to 0 has no visible diff and remains unscored.
 *
 * Shared by lineage cards and the post-game banner so both surfaces agree on
 * exactly what counts as a visible change.
 */
export function badgeFor(
  change: OpeningScoreDeltaItem | undefined | null,
): OpeningDeltaBadge | null {
  if (!change || change.after == null) return null;
  const after = Math.round(change.after);
  const before = change.is_new
    ? 0
    : change.before == null
      ? null
      : Math.round(change.before);
  if (before == null) return null;
  const diff = after - before;
  if (diff === 0) return null;
  return { before, diff, after, dir: diff > 0 ? "up" : "down" };
}

/**
 * Whether a response would render at least one badge. Poll completion telemetry
 * uses this even when session replacement prevents presentation.
 */
export function hasRenderableBadge(
  items: OpeningScoreDeltaItem[] | null | undefined,
): boolean {
  return (items ?? []).some((item) => badgeFor(item) !== null);
}
