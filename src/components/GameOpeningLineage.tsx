import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import OpeningTreeNodeCard, {
  type OpeningTreeNodeView,
} from "./OpeningTreeNodeCard";
import { buildOpeningsSearchParams } from "../openings/route";
import type {
  OpeningLineageItem,
  OpeningPlayerColor,
  OpeningScoreDeltaItem,
} from "../utils/api";

interface GameOpeningLineageProps {
  playerColor: OpeningPlayerColor;
  lineage: OpeningLineageItem[];
  /** Post-game/drill opening-score changes (g-xanz), keyed by opening_key. When
   *  provided, a changed opening shows an inline diff badge to the right of its
   *  card (g-3gmc). Null during live play -> no badges. */
  scoreChanges?: OpeningScoreDeltaItem[] | null;
  /** When provided, tapping a chip selects that opening's root on the
   *  board/MoveList/graph (history parity). Omit for the live game panel, where
   *  cards are expand-only and must NOT move the live board. */
  onSelectRoot?: (item: OpeningLineageItem) => void;
  /** When provided, the expanded card shows a Start Drill button. Omit to hide
   *  it (live game panel). */
  onStartDrill?: (item: OpeningLineageItem) => void;
}

type LineageBadge = { diff: number; after: number; dir: "up" | "down" };

/**
 * Derive the inline score-diff badge for one opening, or null to render nothing.
 * The badge is computed from the ROUNDED before/after (the cards display rounded
 * scores), so a sub-1.0 float wobble never renders a misleading `+0`/`+1`.
 * Brand-new openings (is_new) show nothing per user choice (g-3gmc).
 */
function badgeFor(change: OpeningScoreDeltaItem | undefined): LineageBadge | null {
  if (!change || change.is_new) return null;
  if (change.before == null || change.after == null) return null;
  const after = Math.round(change.after);
  const diff = after - Math.round(change.before);
  if (diff === 0) return null;
  return { diff, after, dir: diff > 0 ? "up" : "down" };
}

/**
 * Map a lineage item (an opening family identified by a position) onto the
 * tree-node card's view-model. A family has no SAN / ply / eval, so those
 * move-only fields are nulled out and the card is rendered with `kind="family"`
 * (name as header, no move label / Eval tile / move-type chips). `depth` feeds
 * `ply` for completeness only — family mode never reads it.
 */
function toNodeView(item: OpeningLineageItem): OpeningTreeNodeView {
  return {
    ply: item.depth,
    san: null,
    openingName: item.opening_name,
    eco: item.eco,
    inBook: true,
    isUserSelected: false,
    score: item.score,
    evalCp: null,
    evalMate: null,
    coverage: item.coverage,
    gameCount: item.game_count,
    isTerminal: false,
    terminalReason: null,
    drillOpeningKey: item.opening_key,
  };
}

/**
 * Compact vertical stack of opening cards (broadest -> deepest), shared by the
 * /history analysis footer and the live game chess-panel. Each entry is a
 * compact `OpeningTreeNodeCard` (family mode) that expands in place to the
 * expanded variant — the same card /openings uses, minus the mini board. When
 * `onSelectRoot` is provided, tapping a card ALSO selects that opening's root on
 * the board/MoveList/graph (history); when omitted the card is expand-only (live
 * panel). The "View in Openings" link is re-homed as the expanded card's footer,
 * and the optional Start Drill button lives inside the expanded card.
 */
function GameOpeningLineage({
  playerColor,
  lineage,
  scoreChanges,
  onSelectRoot,
  onStartDrill,
}: GameOpeningLineageProps) {
  const [expandedKey, setExpandedKey] = useState<string | null>(null);

  const changeByKey = useMemo(
    () => new Map((scoreChanges ?? []).map((c) => [c.opening_key, c])),
    [scoreChanges],
  );

  if (lineage.length === 0) {
    return null;
  }

  return (
    <section className="game-opening-lineage" aria-label="Openings played">
      <p className="game-opening-lineage__label">Openings</p>
      <ol className="game-opening-lineage__list">
        {lineage.map((item, index) => {
          const isExpanded = expandedKey === item.opening_key;
          const cardId = `opening-card-${index}`;
          const openingsHref = `/openings?${buildOpeningsSearchParams({
            playerColor,
            opening: item.opening_key,
          })}`;
          const badge = badgeFor(changeByKey.get(item.opening_key));
          const badgeSign = badge && badge.diff > 0 ? "+" : "";
          const view = toNodeView(item);

          return (
            <li
              key={item.opening_key}
              className="game-opening-lineage__item"
              style={{ "--lineage-depth": index } as React.CSSProperties}
            >
              {isExpanded ? (
                // Expanded card replaces the collapsed one; its full-surface
                // overlay button collapses it. The re-homed "View in Openings"
                // link is the card's footer (a sibling of the card, not covered
                // by the overlay), so tapping it never collapses the card.
                <div className="opening-lineage-card" id={cardId}>
                  <OpeningTreeNodeCard
                    variant="expanded"
                    kind="family"
                    node={view}
                    onStartDrill={
                      onStartDrill ? () => onStartDrill(item) : undefined
                    }
                    onCollapse={() => setExpandedKey(null)}
                  />
                  <Link
                    className="opening-lineage-card__openings-link"
                    to={openingsHref}
                    onClick={(e) => e.stopPropagation()}
                  >
                    View in Openings
                  </Link>
                </div>
              ) : (
                <OpeningTreeNodeCard
                  variant="compact"
                  kind="family"
                  node={view}
                  // Wording reflects the action: history selects a root + toggles
                  // details; the live panel only expands the card in place.
                  ariaLabel={
                    onSelectRoot
                      ? `Select ${item.opening_name} and toggle details`
                      : `Show ${item.opening_name} details`
                  }
                  onSelect={() => {
                    setExpandedKey(item.opening_key);
                    onSelectRoot?.(item);
                  }}
                  isSelected={isExpanded}
                  isExpanded={isExpanded}
                  controlsId={cardId}
                />
              )}
              {/* Inline score-diff badge (g-3gmc): sibling of the card, to its
                  right, shown in both collapsed and expanded states. */}
              {badge && (
                <span
                  className={`game-opening-lineage__delta game-opening-lineage__delta--${badge.dir}`}
                  aria-label={`${item.opening_name} score ${badgeSign}${badge.diff}, now ${badge.after}`}
                >
                  {badgeSign}
                  {badge.diff} → {badge.after}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export default GameOpeningLineage;
