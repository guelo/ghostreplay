import { useState } from "react";
import { Link } from "react-router-dom";
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
}

/**
 * Compact vertical stack of opening chips (broadest -> deepest) for the
 * /history analysis footer. Each chip is a non-interactive container with two
 * sibling controls: a nav link to the opening's /openings page and an expand
 * button that reveals a compact OpeningFamilyCard.
 */
function GameOpeningLineage({ playerColor, lineage }: GameOpeningLineageProps) {
  const [expandedKey, setExpandedKey] = useState<string | null>(null);

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

          return (
            <li
              key={item.opening_key}
              className="game-opening-lineage__item"
              style={{ "--lineage-depth": index } as React.CSSProperties}
            >
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
                <Link
                  className="game-opening-chip__nav"
                  to={`/openings?${buildOpeningsSearchParams({
                    playerColor,
                    openingKey: item.opening_key,
                    path: item.path,
                  })}`}
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
                </Link>
                <button
                  type="button"
                  className="game-opening-chip__expand"
                  aria-expanded={isExpanded}
                  aria-label={
                    isExpanded
                      ? `Hide ${item.opening_name} details`
                      : `Show ${item.opening_name} details`
                  }
                  onClick={() => {
                    setExpandedKey((current) =>
                      current === item.opening_key ? null : item.opening_key,
                    );
                  }}
                >
                  {isExpanded ? "▾" : "▸"}
                </button>
              </div>

              {isExpanded && (
                <div
                  className={`opening-family-card opening-family-card--analysis opening-family-card--${tone}`}
                >
                  <OpeningFamilyCard
                    variant="analysis"
                    openingName={item.opening_name}
                    openingKey={item.opening_key}
                    playerColor={playerColor}
                    score={item.score}
                    coverage={item.coverage}
                    sampleSize={item.sample_size}
                    confidence={item.confidence}
                    isUnscored={isUnscored}
                  />
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
