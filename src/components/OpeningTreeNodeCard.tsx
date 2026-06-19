import {
  formatGames,
  formatMoveLabel,
  formatOpeningName,
  formatPercent,
  formatScore,
  formatTerminalReason,
  getGradeText,
  getGradeToken,
  getPriorityLabel,
} from "../openings/format";
import { formatWhiteEval } from "./MoveRow.helpers";

/**
 * Presentational view-model for a single opening-tree node. Built by
 * `g-tree-page-state` from a `TreeNode` (or synthesized for the root); the card
 * is a pure render layer that already handles every null via the format
 * helpers. Eval fields are PERSPECTIVE-RELATIVE — the page flips white-relative
 * API values before constructing this view.
 */
export interface OpeningTreeNodeView {
  /** 0 = root (start position); 1 = after White's first move. */
  ply: number;
  /** SAN that produced this node; null at the root. */
  san: string | null;
  openingName: string | null;
  eco: string | null;
  /** 0–100 opening score; null = no evidence. */
  score: number | null;
  /** Perspective-relative centipawns; null when no best-move row. */
  evalCp: number | null;
  /** Perspective-relative mate-in-N; null when not a forced mate. */
  evalMate: number | null;
  coverage: number | null;
  /** Distinct sessions reaching this node (never raw sample_size). */
  gameCount: number | null;
  confidence: number | null;
  isTerminal: boolean;
  /** checkmate | stalemate | opening_boundary | no_children; null when not terminal. */
  terminalReason: string | null;
  /** Drillable-root key from the API; non-null => Start Drill is offered. */
  drillOpeningKey: string | null;
}

interface OpeningTreeNodeCardProps {
  variant: "compact" | "expanded";
  node: OpeningTreeNodeView;

  /** Compact: when provided, the card renders as a selection `<button>`. */
  onSelect?: () => void;
  /** Compact: selected/on-path highlight + `aria-pressed`. */
  isSelected?: boolean;
  /** Compact: when defined, sets `aria-expanded` (disclosure reuse). */
  isExpanded?: boolean;
  /** Compact: when set, sets `aria-controls` (disclosure reuse). */
  controlsId?: string;

  /** Expanded: handler for the owned "Start Drill" button. */
  onStartDrill?: () => void;
}

/**
 * Grade tag shared by both variants. Shows the grade letter (A–F) or "—" for a
 * null score; the accent colour is decorative (grade is also encoded in text),
 * and the accessible name spells out the grade.
 */
function GradeTag({ score }: { score: number | null }) {
  const visible = score === null ? "—" : getPriorityLabel(score);
  return (
    <span className="tree-node-card__grade" aria-label={getGradeText(score)}>
      {visible}
    </span>
  );
}

/** Compact body: two lines (SAN + score/grade, then opening name + eval). */
function CompactBody({ node }: { node: OpeningTreeNodeView }) {
  const isRoot = node.san === null;
  const name = formatOpeningName(node.openingName);
  const evalText = formatWhiteEval(node.evalCp, node.evalMate) || "—";

  return (
    <>
      <span className="tree-node-card__line tree-node-card__line--primary">
        <span className="tree-node-card__move">{node.san ?? "Start"}</span>
        <span className="tree-node-card__primary-right">
          <span className="tree-node-card__score">{formatScore(node.score)}</span>
          <GradeTag score={node.score} />
        </span>
      </span>
      <span className="tree-node-card__line tree-node-card__line--secondary">
        {!isRoot && (
          <span className="tree-node-card__name" title={name}>
            {name}
          </span>
        )}
        <span className="tree-node-card__eval">{evalText}</span>
      </span>
    </>
  );
}

/** Expanded body: header, score/eval panel, metrics, terminal note, drill. */
function ExpandedBody({
  node,
  onStartDrill,
}: {
  node: OpeningTreeNodeView;
  onStartDrill?: () => void;
}) {
  const isRoot = node.san === null;
  const name = formatOpeningName(node.openingName);
  const evalText = formatWhiteEval(node.evalCp, node.evalMate) || "—";
  // Every expanded move card is drillable; the page decides drillability by
  // passing onStartDrill (wired for move cards, omitted for the synthesized
  // root). drillOpeningKey no longer gates this — it denotes named-root identity
  // only, and cards inherit names far more broadly than roots are registered.
  const showDrill = onStartDrill != null;

  return (
    <>
      <div className="tree-node-card__header">
        <span className="tree-node-card__move-label">
          {formatMoveLabel(node.ply, node.san)}
        </span>
        {!isRoot && (
          <span className="tree-node-card__name" title={name}>
            {name}
          </span>
        )}
      </div>

      <dl className="tree-node-card__score-panel">
        <div className="tree-node-card__score-metric">
          <dt>Score</dt>
          <dd className="tree-node-card__score-value">
            {formatScore(node.score)}
            <GradeTag score={node.score} />
          </dd>
        </div>
        <div className="tree-node-card__score-metric">
          <dt>Eval</dt>
          <dd>{evalText}</dd>
        </div>
      </dl>

      <dl className="tree-node-card__metrics">
        <div className="tree-node-card__metric">
          <dt>Coverage</dt>
          <dd>{formatPercent(node.coverage)}</dd>
        </div>
        <div className="tree-node-card__metric">
          <dt>Games</dt>
          <dd>{formatGames(node.gameCount)}</dd>
        </div>
        <div className="tree-node-card__metric">
          <dt>Confidence</dt>
          <dd>{formatPercent(node.confidence)}</dd>
        </div>
      </dl>

      {node.isTerminal && (
        <p className="tree-node-card__terminal">
          {formatTerminalReason(node.terminalReason)}
        </p>
      )}

      {showDrill && (
        <button
          type="button"
          className="tree-node-card__drill-button"
          onClick={onStartDrill}
        >
          Start Drill
        </button>
      )}
    </>
  );
}

/**
 * Reusable presentational card for an opening-tree node. The compact variant is
 * the primary rendering for every non-expanded node (and reused by
 * GameOpeningLineage); the expanded variant renders the single deepest selected
 * node. Self-contained: the card owns its selection button and Start Drill
 * button, but the business logic behind them arrives as injected callbacks.
 */
function OpeningTreeNodeCard({
  variant,
  node,
  onSelect,
  isSelected,
  isExpanded,
  controlsId,
  onStartDrill,
}: OpeningTreeNodeCardProps) {
  const className = [
    "tree-node-card",
    `tree-node-card--${variant}`,
    `tree-node-card--grade-${getGradeToken(node.score)}`,
    isSelected ? "tree-node-card--selected" : "",
  ]
    .filter(Boolean)
    .join(" ");

  if (variant === "compact") {
    if (onSelect) {
      return (
        <button
          type="button"
          className={className}
          onClick={onSelect}
          aria-pressed={!!isSelected}
          aria-expanded={isExpanded === undefined ? undefined : isExpanded}
          aria-controls={controlsId}
        >
          <CompactBody node={node} />
        </button>
      );
    }

    return (
      <div className={className}>
        <CompactBody node={node} />
      </div>
    );
  }

  return (
    <div className={className}>
      <ExpandedBody node={node} onStartDrill={onStartDrill} />
    </div>
  );
}

export default OpeningTreeNodeCard;
