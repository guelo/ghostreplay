import type { OpeningScoreDeltaItem } from "./api";

export type OpeningDeltaBadge = {
  before: number;
  diff: number;
  after: number;
  dir: "up" | "down";
};

/** A concise, non-visual description shared by the card and its compact action. */
export function describeOpeningDeltaBadge(badge: OpeningDeltaBadge): string {
  const direction = badge.dir === "up" ? "increased" : "decreased";
  return `Score ${direction} by ${Math.abs(badge.diff)}, now ${badge.after}`;
}

/**
 * Derive the score-diff badge for one opening, or null to render nothing.
 * The badge is computed from the ROUNDED before/after (the cards display rounded
 * scores), so a sub-1.0 float wobble never renders a misleading `+0`/`+1`.
 *
 * Brand-new openings (is_new) have no baseline, so a visible diff is quantified
 * against 0. The card reveal then starts at 0 and promotes the resolved score,
 * while the capsule communicates the full gain (g-ptea). A new score that rounds
 * to 0 has no visible diff and remains unscored.
 *
 * Extracted from GameOpeningLineage (g-f3m4) so the inline lineage badges and the
 * last-drill toast agree on exactly what counts as a change — a delta that renders
 * nothing inline must never be surfaced as a toast either.
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
 * Whether a delta payload would render at least one badge. Null / empty / fully
 * badge-suppressed payloads are unrenderable: they must not be queued as late
 * notifications, or an invisible head would block the drills behind it.
 */
export function hasRenderableBadge(
  items: OpeningScoreDeltaItem[] | null | undefined,
): boolean {
  return (items ?? []).some((item) => badgeFor(item) !== null);
}
