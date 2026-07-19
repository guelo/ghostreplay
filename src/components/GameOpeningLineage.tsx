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
  OpeningScoreStatus,
} from "../utils/api";

interface GameOpeningLineageProps {
  playerColor: OpeningPlayerColor;
  lineage: OpeningLineageItem[];
  /** Ply of each item's `moves[0]` (1 = White's move 1); anchors move-list
   *  numbering on the cards. From the session-openings response. */
  startPly: number;
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
  /** Whether the server's opening scores are still being computed (g-a5v3).
   *  "pending" makes each card render a loading placeholder in place of its
   *  score, so a cold cache reads as "loading" rather than "unscored".
   *  Defaults to "ready" — callers that never see a cold cache can omit it. */
  scoreStatus?: OpeningScoreStatus;
}

type LineageBadge = { diff: number; after: number; dir: "up" | "down" };

/**
 * Derive the inline score-diff badge for one opening, or null to render nothing.
 * The badge is computed from the ROUNDED before/after (the cards display rounded
 * scores), so a sub-1.0 float wobble never renders a misleading `+0`/`+1`.
 *
 * Brand-new openings (is_new) have no baseline, so the diff is quantified against
 * 0 — a new opening ending at 37 reads `+37 → 37` (g-gkkn). Its card itself stays
 * "—" (there was no prior score); the badge is the sole signal of the new value.
 */
function badgeFor(change: OpeningScoreDeltaItem | undefined): LineageBadge | null {
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
  return { diff, after, dir: diff > 0 ? "up" : "down" };
}

/**
 * Map a lineage item (an opening family identified by a position) onto the
 * tree-node card's view-model. A family has no SAN / ply / eval, so those
 * move-only fields are nulled out and the card is rendered with `kind="family"`
 * (name as header, played move list as the secondary line, no Eval tile /
 * move-type chips). `depth` feeds `ply` for completeness only — family mode
 * never reads it. `moveListSan` is the player's actual SAN prefix, numbered from
 * `startPly`. `score` is the resolved card score — usually `item.score`, but
 * pinned to the delta's pre-game `before` at game end (g-gkkn).
 */
function toNodeView(
  item: OpeningLineageItem,
  startPly: number,
  score: number | null,
): OpeningTreeNodeView {
  return {
    ply: item.depth,
    san: null,
    openingName: item.opening_name,
    eco: item.eco,
    inBook: true,
    isUserSelected: false,
    score,
    evalCp: null,
    evalMate: null,
    coverage: item.coverage,
    gameCount: item.game_count,
    isTerminal: false,
    terminalReason: null,
    drillOpeningKey: item.opening_key,
    moveListSan: item.moves,
    moveListStartPly: startPly,
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
  startPly,
  scoreChanges,
  onSelectRoot,
  onStartDrill,
  scoreStatus = "ready",
}: GameOpeningLineageProps) {
  // Track expansion by a per-occurrence key (opening_key + index), not the bare
  // opening_key: a lineage can (defensively) repeat the same root as separate
  // crossings, and keying by opening_key alone would collide React keys and
  // expand every matching card at once.
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
          const cardKey = `${item.opening_key}:${index}`;
          const isExpanded = expandedKey === cardKey;
          const cardId = `opening-card-${index}`;
          const openingsHref = `/openings?${buildOpeningsSearchParams({
            playerColor,
            opening: item.opening_key,
          })}`;
          const change = changeByKey.get(item.opening_key);
          const badge = badgeFor(change);
          const badgeSign = badge && badge.diff > 0 ? "+" : "";
          // Pre-game score pin (g-gkkn): during play `change` is undefined and
          // item.score is already pre-game-fresh (g-dmd1); at a terminal event the
          // delta's `before` overrides the refetched post-game item.score so the
          // card number never changes. is_new keeps "—" (no baseline); a non-new
          // entry missing `before` (data anomaly) falls back to item.score.
          const cardScore = !change
            ? item.score
            : change.is_new
              ? null
              : change.before ?? item.score;
          // The terminal pin wins over the pending shimmer: once `change` is
          // present the card shows the pinned pre-game number beside the diff
          // badge, and swapping that number for a shimmer mid-pin would make
          // the badge reference a value that is no longer on screen.
          const scorePending = scoreStatus === "pending" && !change;
          const view = toNodeView(item, startPly, cardScore);

          return (
            <li
              key={cardKey}
              className="game-opening-lineage__item"
              style={{ "--lineage-depth": index } as React.CSSProperties}
            >
              {isExpanded ? (
                // Expanded card replaces the collapsed one; its full-surface
                // overlay button collapses it. The "View in Openings" link is
                // passed as the card's footerAction — rendered inside the card,
                // raised above the collapse overlay, with its clicks stopped so
                // tapping it never collapses the card. This component owns the
                // router Link so the card stays router-free.
                <div className="opening-lineage-card" id={cardId}>
                  <OpeningTreeNodeCard
                    variant="expanded"
                    kind="family"
                    node={view}
                    scorePending={scorePending}
                    onStartDrill={
                      onStartDrill ? () => onStartDrill(item) : undefined
                    }
                    onCollapse={() => setExpandedKey(null)}
                    footerAction={
                      <Link
                        className="opening-lineage-card__openings-link"
                        to={openingsHref}
                      >
                        View in Openings
                      </Link>
                    }
                  />
                </div>
              ) : (
                <OpeningTreeNodeCard
                  variant="compact"
                  kind="family"
                  node={view}
                  scorePending={scorePending}
                  // Wording reflects the action: history selects a root + toggles
                  // details; the live panel only expands the card in place.
                  ariaLabel={
                    onSelectRoot
                      ? `Select ${item.opening_name} and toggle details`
                      : `Show ${item.opening_name} details`
                  }
                  onSelect={() => {
                    setExpandedKey(cardKey);
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
