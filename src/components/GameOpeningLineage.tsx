import { useMemo, useState } from "react";
import OpeningFamilyCard from "./OpeningFamilyCard";
import { buildOpeningsSearchParams } from "../openings/route";
import {
  formatScore,
  getPriorityLabel,
  getPriorityTone,
} from "../openings/format";
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
 * Compact vertical stack of opening chips (broadest -> deepest), shared by the
 * /history analysis footer and the live game chess-panel. Each chip is a button
 * that toggles an inline OpeningFamilyCard. When `onSelectRoot` is provided it
 * ALSO selects that opening's root on the board/MoveList/graph (history); when
 * omitted the chip is expand-only (live panel). The /openings link and the
 * optional Start Drill button live inside the expanded card.
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
          const tone = getPriorityTone(item.score);
          const statusLabel = getPriorityLabel(item.score);
          const isExpanded = expandedKey === item.opening_key;
          const isUnscored = item.score === null;
          const cardId = `opening-card-${index}`;
          const openingsHref = `/openings?${buildOpeningsSearchParams({
            playerColor,
            opening: item.opening_key,
          })}`;
          const badge = badgeFor(changeByKey.get(item.opening_key));
          const badgeSign = badge && badge.diff > 0 ? "+" : "";

          return (
            <li
              key={item.opening_key}
              className="game-opening-lineage__item"
              style={{ "--lineage-depth": index } as React.CSSProperties}
            >
              {isExpanded ? (
                // Expanded card replaces the collapsed chip. Clicking its surface
                // (outside the link / Start Drill buttons) collapses it again.
                <div
                  id={cardId}
                  className={`opening-family-card opening-family-card--analysis opening-family-card--${tone}`}
                >
                  <OpeningFamilyCard
                    openingName={item.opening_name}
                    openingKey={item.opening_key}
                    playerColor={playerColor}
                    score={item.score}
                    coverage={item.coverage}
                    gameCount={item.game_count}
                    confidence={item.confidence}
                    isUnscored={isUnscored}
                    openingsHref={openingsHref}
                    onStartDrill={
                      onStartDrill ? () => onStartDrill(item) : undefined
                    }
                    onCollapse={() => setExpandedKey(null)}
                  />
                </div>
              ) : (
                <div
                  className={`game-opening-chip game-opening-chip--${tone}`}
                  role="group"
                  aria-label={item.opening_name}
                >
                  {index > 0 && (
                    <span
                      className="game-opening-chip__connector"
                      aria-hidden="true"
                    >
                      {"└"}
                    </span>
                  )}
                  <button
                    type="button"
                    className="game-opening-chip__toggle"
                    aria-expanded={isExpanded}
                    aria-controls={cardId}
                    // Wording reflects the action: history selects a root + toggles
                    // details; the live panel only expands the card in place.
                    aria-label={
                      onSelectRoot
                        ? `Select ${item.opening_name} and toggle details`
                        : `Show ${item.opening_name} details`
                    }
                    onClick={() => {
                      setExpandedKey(item.opening_key);
                      onSelectRoot?.(item);
                    }}
                  >
                    <span
                      className={`game-opening-chip__tone game-opening-chip__tone--${tone}`}
                      aria-hidden="true"
                    />
                    <span className="game-opening-chip__name">
                      {item.opening_name}
                    </span>
                    <span className="game-opening-chip__score">
                      {formatScore(item.score)}
                    </span>
                    <span
                      className="game-opening-chip__grade"
                      aria-label={`Status ${statusLabel}`}
                    >
                      {statusLabel}
                    </span>
                  </button>
                </div>
              )}
              {/* Inline score-diff badge (g-3gmc): sibling of the card, to its
                  right, shown in both collapsed-chip and expanded-card states. */}
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
