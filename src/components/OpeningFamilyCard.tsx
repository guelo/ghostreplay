import { Chessboard } from "react-chessboard";
import { Link } from "react-router-dom";
import type { OpeningPlayerColor } from "../utils/api";
import {
  formatGames,
  formatPercent,
  formatScore,
  getPriorityLabel,
} from "../openings/format";

export interface OpeningFamilyCardProps {
  openingName: string;
  openingKey: string;
  playerColor: OpeningPlayerColor;
  score: number | null;
  coverage: number | null;
  gameCount: number | null;
  confidence: number | null;
  isUnscored: boolean;
  /** Handler for the "Start Drill" button. */
  onStartDrill?: () => void;
  /** href to this opening's /openings page. */
  openingsHref?: string;
  /** Collapses the card when its surface is clicked. */
  onCollapse?: () => void;
}

/**
 * Compact opening card for the /history analysis lineage and the live game
 * chess-panel. Rendered inside a tone-classed wrapper (`opening-family-card
 * opening-family-card--{tone}`) by the caller: a small board with the
 * score/grade beside it, a metrics row, and a footer linking to /openings with a
 * Start Drill button. When `onCollapse` is set, the card surface (everything but
 * the footer controls) collapses the card.
 */
function OpeningFamilyCard({
  openingName,
  openingKey,
  playerColor,
  score,
  coverage,
  gameCount,
  confidence,
  isUnscored,
  onStartDrill,
  openingsHref,
  onCollapse,
}: OpeningFamilyCardProps) {
  const statusLabel = getPriorityLabel(score);

  return (
    <>
      {onCollapse && (
        <button
          type="button"
          className="opening-family-card__collapse-nav"
          aria-label={`Collapse ${openingName} details`}
          onClick={onCollapse}
        />
      )}
      <div className="opening-family-card__headline">
        <h2 className="opening-family-card__title">{openingName}</h2>
        {isUnscored && (
          <p className="opening-family-card__subhint">
            No scored roots in this subtree yet.
          </p>
        )}
      </div>

      <div className="opening-family-card__overview">
        <div className="opening-family-card__board" aria-hidden="true">
          <Chessboard
            options={{
              position: openingKey,
              boardOrientation: playerColor,
              allowDragging: false,
              animationDurationInMs: 0,
              boardStyle: {
                borderRadius: "10px",
                pointerEvents: "none",
              },
            }}
          />
        </div>
        <dl className="opening-family-card__score-panel">
          <div className="opening-family-card__score-metric">
            <dt>Score</dt>
            <dd>{formatScore(score)}</dd>
          </div>
          <div
            aria-label={`Status ${statusLabel}`}
            className="opening-family-card__grade"
          >
            {statusLabel}
          </div>
        </dl>
      </div>

      <dl className="opening-family-card__metrics">
        <div className="opening-family-card__metric">
          <dt>Coverage</dt>
          <dd>{formatPercent(coverage)}</dd>
        </div>
        <div className="opening-family-card__metric">
          <dt>Games</dt>
          <dd>{formatGames(gameCount)}</dd>
        </div>
        <div className="opening-family-card__metric">
          <dt>Confidence</dt>
          <dd>{formatPercent(confidence)}</dd>
        </div>
      </dl>

      <div className="opening-family-card__analysis-footer">
        {openingsHref && (
          <Link className="opening-family-card__analysis-link" to={openingsHref}>
            View in Openings
          </Link>
        )}
        {onStartDrill && (
          <button
            type="button"
            className="opening-family-card__drill-button"
            onClick={(e) => {
              e.stopPropagation();
              onStartDrill();
            }}
          >
            Start Drill
          </button>
        )}
      </div>
    </>
  );
}

export default OpeningFamilyCard;
