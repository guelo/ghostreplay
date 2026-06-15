import { useState } from "react";
import OpeningFamilyCard from "./OpeningFamilyCard";
import { buildOpeningsSearchParams } from "../openings/route";
import {
  formatScore,
  getPriorityLabel,
  getPriorityTone,
} from "../openings/format";
import type { OpeningLineageItem, OpeningPlayerColor } from "../utils/api";

interface GameOpeningLineageProps {
  playerColor: OpeningPlayerColor;
  lineage: OpeningLineageItem[];
  onSelectRoot: (item: OpeningLineageItem) => void;
  onStartDrill: (item: OpeningLineageItem) => void;
}

/**
 * Compact vertical stack of opening chips (broadest -> deepest) for the
 * /history analysis footer. Each chip is a single-action button that toggles an
 * inline OpeningFamilyCard AND selects that opening's root on the
 * board/MoveList/graph. The /openings link and Start Drill button live inside
 * the expanded card.
 */
function GameOpeningLineage({
  playerColor,
  lineage,
  onSelectRoot,
  onStartDrill,
}: GameOpeningLineageProps) {
  const [expandedKey, setExpandedKey] = useState<string | null>(null);

  if (lineage.length === 0) {
    return null;
  }

  return (
    <section className="game-opening-lineage" aria-label="Openings played">
      <p className="game-opening-lineage__label">Your Opening Scores</p>
      <ol className="game-opening-lineage__list">
        {lineage.map((item, index) => {
          const tone = getPriorityTone(item.score);
          const statusLabel = getPriorityLabel(item.score);
          const isExpanded = expandedKey === item.opening_key;
          const isUnscored = item.score === null;
          const cardId = `opening-card-${index}`;
          const openingsHref = `/openings?${buildOpeningsSearchParams({
            playerColor,
            openingKey: item.opening_key,
            path: item.path,
          })}`;

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
                    variant="analysis"
                    openingName={item.opening_name}
                    openingKey={item.opening_key}
                    playerColor={playerColor}
                    score={item.score}
                    coverage={item.coverage}
                    gameCount={item.game_count}
                    confidence={item.confidence}
                    isUnscored={isUnscored}
                    openingsHref={openingsHref}
                    onStartDrill={() => onStartDrill(item)}
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
                    aria-label={`Select ${item.opening_name} and toggle details`}
                    onClick={() => {
                      setExpandedKey(item.opening_key);
                      onSelectRoot(item);
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
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export default GameOpeningLineage;
