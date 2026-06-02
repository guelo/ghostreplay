import { Chessboard } from "react-chessboard";
import type { OpeningPlayerColor } from "../utils/api";
import {
  formatGames,
  formatPercent,
  formatScore,
  getPriorityLabel,
} from "../openings/format";

export type OpeningFamilyCardVariant = "full" | "analysis";

export interface OpeningFamilyCardProps {
  variant?: OpeningFamilyCardVariant;
  openingName: string;
  openingKey: string;
  playerColor: OpeningPlayerColor;
  score: number | null;
  coverage: number | null;
  sampleSize: number | null;
  confidence: number | null;
  isUnscored: boolean;
  /** Full variant only: the move line for this opening. */
  moveLine?: string | null;
  /** Full variant only: footer note (e.g. child count). */
  footerNote?: string;
  /** Full variant only: drill-down affordance label. */
  drillDownLabel?: string;
  /** Full variant only: navigates into this opening's children (stretched link). */
  onDrillDown?: () => void;
  /** Full variant only: handler for the "Start Drill" button. */
  onStartDrill?: () => void;
}

/**
 * Shared inner body for an opening family card. Rendered inside a tone-classed
 * wrapper (`opening-family-card opening-family-card--{tone}`) by the caller.
 *
 * - `variant="full"` (default): the /openings scoreboard card with move line,
 *   metrics, and drill footer.
 * - `variant="analysis"`: a compact card for the /history analysis lineage —
 *   smaller board, no move line, no drill footer.
 */
function OpeningFamilyCard({
  variant = "full",
  openingName,
  openingKey,
  playerColor,
  score,
  coverage,
  sampleSize,
  confidence,
  isUnscored,
  moveLine,
  footerNote,
  drillDownLabel,
  onDrillDown,
  onStartDrill,
}: OpeningFamilyCardProps) {
  const statusLabel = getPriorityLabel(score);
  const isFull = variant === "full";

  return (
    <>
      {onDrillDown && (
        <button
          type="button"
          className="opening-family-card__drill-nav"
          aria-label={`Open ${openingName}`}
          onClick={onDrillDown}
        />
      )}
      <div className="opening-family-card__headline">
        <h2 className="opening-family-card__title">{openingName}</h2>
        {isFull && (
          <p className="opening-family-card__hint">
            Moves: <strong>{moveLine ?? "Line unavailable."}</strong>
          </p>
        )}
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
          <dd>{formatGames(sampleSize)}</dd>
        </div>
        <div className="opening-family-card__metric">
          <dt>Confidence</dt>
          <dd>{formatPercent(confidence)}</dd>
        </div>
      </dl>

      {isFull && (
        <div className="opening-family-card__footer">
          <span className="opening-family-card__footer-note">{footerNote}</span>
          <div className="opening-family-card__footer-actions">
            {drillDownLabel && (
              <span className="opening-family-card__drilldown">
                {drillDownLabel}
              </span>
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
        </div>
      )}
    </>
  );
}

export default OpeningFamilyCard;
