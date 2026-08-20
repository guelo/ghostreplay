import { badgeFor, type OpeningDeltaBadge } from "../../../utils/openingDeltaBadge";
import type { OpeningScoreDeltaItem } from "../../../utils/api";
import type { GameResult } from "../domain/status";

export type BannerOpeningRow = {
  key: string;
  name: string;
  /** Quantized endpoints; `diff` always equals `after` minus the displayed before. */
  badge: OpeningDeltaBadge;
  isNew: boolean;
};

/**
 * The banner's opening rows, one per opening whose score actually MOVED (g-frlfp).
 *
 * Change and formatting are both delegated to `badgeFor`, the shared authority the
 * inline lineage badges already use. That buys three things at once:
 *  - a row can never disagree with the badge beside it — "a delta that renders
 *    nothing inline must never be surfaced" elsewhere either;
 *  - endpoints are quantized to tenths before subtracting, so raw float scores
 *    cannot print as 0.20000000000000284;
 *  - an unscored opening (`after == null`, including the supported `is_new` with
 *    no resolved score) is dropped rather than rendered as a bare "new".
 *
 * `is_new` is still kept as a flag: it has no `before` to subtract from, so its
 * row reads "new -> 30.4" rather than claiming a numeric gain.
 *
 * ONE row per opening, not one per crossing. The played chain deliberately keeps
 * a non-consecutively repeated root as a separate entry (opening_roots.py:492) so
 * each crossing keeps its own move prefix, and the delta carries an item per
 * entry. But every field of those items is looked up by `opening_key`
 * (opening_score_delta.py:1810), so repeated crossings are field-identical: a
 * second row would restate the same score move AND collide on the React key. The
 * lineage cards key by `${opening_key}:${index}` instead — they show per-crossing
 * move prefixes, so there each crossing is real information.
 *
 * This is a BANNER-LOCAL view. It must not be pushed into the store, the delta
 * poll, or the shared `openingScoreChanges` memo — those carry every played
 * opening, flat ones included, and this filter would silently strip them there.
 */
export const bannerOpeningRows = (
  items: OpeningScoreDeltaItem[],
): BannerOpeningRow[] => {
  const emitted = new Set<string>();
  return items.flatMap((item) => {
    if (emitted.has(item.opening_key)) return [];
    const badge = badgeFor(item);
    if (!badge) return [];
    emitted.add(item.opening_key);
    return [
      {
        key: item.opening_key,
        name: item.opening_name,
        badge,
        isNew: item.is_new,
      },
    ];
  });
};

/**
 * Whether the banner renders anything at all, extracted so the layout can give it
 * a grid row ONLY when it has content — an always-mounted empty row would add a
 * dead grid gap above the move list for the whole game.
 *
 * Mirrors the branch order in PostGameBanner itself; keep the two in step.
 */
export const shouldRenderPostGameBanner = ({
  isGameActive,
  showPostGamePrompt,
  gameResult,
  isReviewedDrillReturn,
}: {
  isGameActive: boolean;
  showPostGamePrompt: boolean;
  gameResult: GameResult | null;
  isReviewedDrillReturn?: boolean;
}): boolean => {
  if (isReviewedDrillReturn) return false;
  if (showPostGamePrompt && gameResult) return true;
  return !isGameActive && !showPostGamePrompt;
};
