import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
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
 * helpers. Eval fields are WHITE-RELATIVE (+white / −black) — passed through
 * unchanged from the API; the column SORT (backend) applies the column's
 * side-to-move favorability, while the displayed number stays in the standard
 * convention.
 */
export interface OpeningTreeNodeView {
  /** 0 = root (start position); 1 = after White's first move. */
  ply: number;
  /** SAN that produced this node; null at the root. */
  san: string | null;
  openingName: string | null;
  eco: string | null;
  /** True when this move's edge exists in the eco.json opening book. False =>
   *  an off-book branch from the player's games; the name is inherited from the
   *  last book leaf, so an "Off book" chip flags it. Always true for the root. */
  inBook: boolean;
  /** A legal move chosen on the board, not from the book or the player's games —
   *  the third move type. Flagged with a "Your move" chip (wins over off-book). */
  isUserSelected: boolean;
  /** 0–100 opening score; null = no evidence. */
  score: number | null;
  /** White-relative centipawns (+white / −black); null when no best-move row. */
  evalCp: number | null;
  /** White-relative mate-in-N (+white / −black); null when not a forced mate. */
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

  /** Rendering mode. "move" (default) is the /openings tree-node card (move
   *  label + Eval tile + move-type chips). "family" is the /history & /play
   *  opening-lineage card: a position-identified opening family with no SAN /
   *  ply / eval — the name becomes the header, the Eval tile and move-type chips
   *  are dropped, and the ECO (when present) is the compact secondary line. */
  kind?: "move" | "family";

  /** Compact: when provided, the card renders as a selection `<button>`. */
  onSelect?: () => void;
  /** Compact: selected/on-path highlight + `aria-pressed`. */
  isSelected?: boolean;
  /** Compact: when defined, sets `aria-expanded` (disclosure reuse). */
  isExpanded?: boolean;
  /** Compact: when set, sets `aria-controls` (disclosure reuse). */
  controlsId?: string;
  /** Compact: overrides the selection button's accessible name. Without it the
   *  name collapses to the visible text; the lineage passes its action name. */
  ariaLabel?: string;

  /** Expanded: handler for the owned "Start Drill" button. */
  onStartDrill?: () => void;
  /** Expanded: when set, a full-surface overlay button collapses the card (used
   *  by the in-place lineage expansion). Requires `--expanded` to be a
   *  positioning context; the Start Drill button is raised above the overlay. */
  onCollapse?: () => void;
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

/**
 * A small chip that is itself a clickable trigger toggling an info popover —
 * shared plumbing for the move-type chips (Off book / Your move).
 *
 * It renders as a `role="button"` span (not a `<button>`) because the compact
 * card is itself a selection `<button>` and a real nested button is invalid; the
 * click is stopped from propagating so tapping the chip never selects the card.
 * The popover is `position: fixed` and anchored to the trigger rect so it
 * escapes the tree columns' `overflow` scrollers (which would otherwise clip it).
 */
function PopoverChip({
  label,
  ariaLabel,
  triggerClassName,
  title,
  children,
}: {
  label: string;
  ariaLabel: string;
  triggerClassName: string;
  title: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number; visibility: "hidden" | "visible" }>({
    top: 0,
    left: 0,
    visibility: "hidden",
  });
  const wrapRef = useRef<HTMLSpanElement>(null);
  const triggerRef = useRef<HTMLSpanElement>(null);
  const popupRef = useRef<HTMLSpanElement>(null);

  const toggle = (e: { stopPropagation: () => void; preventDefault: () => void }) => {
    e.stopPropagation();
    e.preventDefault();
    setOpen((v) => !v);
  };

  // Anchor the fixed popover to the trigger after it mounts (so we can measure
  // its size and flip/clamp it into the viewport). Rendered hidden for one frame
  // to avoid a flash at the unpositioned origin.
  useLayoutEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current;
    const popup = popupRef.current;
    if (!trigger || !popup) return;
    const t = trigger.getBoundingClientRect();
    const p = popup.getBoundingClientRect();
    const margin = 8;
    let left = t.left;
    if (left + p.width > window.innerWidth - margin) {
      left = window.innerWidth - p.width - margin;
    }
    if (left < margin) left = margin;
    let top = t.bottom + 6;
    if (top + p.height > window.innerHeight - margin) {
      top = t.top - 6 - p.height; // flip above when there is no room below
    }
    if (top < margin) top = margin;
    setPos({ top, left, visibility: "visible" });
  }, [open]);

  // Dismiss on outside click, Escape, or any scroll/resize (the fixed popover
  // would otherwise detach from the chip as the tree scrolls).
  useEffect(() => {
    if (!open) return;
    const onPointer = (e: MouseEvent) => {
      const target = e.target as Node;
      if (wrapRef.current?.contains(target) || popupRef.current?.contains(target)) {
        return;
      }
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const dismiss = () => setOpen(false);
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", dismiss, true);
    window.addEventListener("resize", dismiss);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", dismiss, true);
      window.removeEventListener("resize", dismiss);
    };
  }, [open]);

  return (
    <span className="tree-node-card__off-book-wrap" ref={wrapRef}>
      <span
        ref={triggerRef}
        role="button"
        tabIndex={0}
        className={triggerClassName}
        aria-label={ariaLabel}
        aria-expanded={open}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") toggle(e);
        }}
      >
        {label}
      </span>
      {open && (
        <span
          ref={popupRef}
          className="tree-node-card__off-book-popup"
          role="tooltip"
          style={pos}
          onClick={(e) => e.stopPropagation()}
        >
          <strong>{title}</strong>
          <span>{children}</span>
        </span>
      )}
    </span>
  );
}

/**
 * Marks a move whose edge is NOT in the eco.json book — an off-book branch from
 * the player's own games. Its opening name is inherited from the last book leaf,
 * so this chip flags that the name is approximate and the line is the player's.
 */
function OffBookChip() {
  return (
    <PopoverChip
      label="Off book"
      ariaLabel="Off book — what does this mean?"
      triggerClassName="tree-node-card__off-book"
      title="Off book"
    >
      This move isn't in the official opening book — it is an opening move you played in a game.
    </PopoverChip>
  );
}

/**
 * Marks the third move type: a legal move chosen on the board to explore.
 * Line-scoped — it exists only while it is the selected move of its column — and
 * may be on or off book (a crossed book boundary still gets this chip), so the
 * copy avoids claiming it is not in the book.
 */
function SelectedMoveChip() {
  return (
    <PopoverChip
      label="Your move"
      ariaLabel="Your move — what does this mean?"
      triggerClassName="tree-node-card__selected-move"
      title="Your move"
    >
      A move you explored on the board; shown while it's part of the current line.
    </PopoverChip>
  );
}

/** Compact body: two lines (SAN + score/grade, then opening name + eval). In
 *  family mode the primary line is the opening name (with score/grade) and the
 *  secondary line is the ECO when present — no SAN/eval/move-type chips. */
function CompactBody({
  node,
  kind,
}: {
  node: OpeningTreeNodeView;
  kind: "move" | "family";
}) {
  if (kind === "family") {
    const familyName = formatOpeningName(node.openingName);
    return (
      <>
        <span className="tree-node-card__line tree-node-card__line--primary">
          <span
            className="tree-node-card__move tree-node-card__move--family"
            title={familyName}
          >
            {familyName}
          </span>
          <span className="tree-node-card__primary-right">
            <span className="tree-node-card__score">{formatScore(node.score)}</span>
            <GradeTag score={node.score} />
          </span>
        </span>
        {node.eco && (
          <span className="tree-node-card__line tree-node-card__line--secondary">
            <span className="tree-node-card__name">{node.eco}</span>
          </span>
        )}
      </>
    );
  }

  const isRoot = node.san === null;
  const isUserSelected = !isRoot && node.isUserSelected;
  const isOffBook = !isRoot && !node.inBook && !node.isUserSelected;
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
        {isUserSelected && <SelectedMoveChip />}
        {isOffBook && <OffBookChip />}
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

/** Expanded body: header, score/eval panel, metrics, terminal note, drill. In
 *  family mode the header is the opening name (no move label), the score panel
 *  drops the Eval tile, and there is no name-line/move-type chip/terminal note —
 *  a family has no SAN/eval. Metrics and Start Drill render as in move mode. */
function ExpandedBody({
  node,
  kind,
  onStartDrill,
}: {
  node: OpeningTreeNodeView;
  kind: "move" | "family";
  onStartDrill?: () => void;
}) {
  // Every expanded card is drillable; the page decides drillability by passing
  // onStartDrill (wired for move/family cards, omitted for the synthesized root
  // and the live-panel lineage). drillOpeningKey no longer gates this.
  const showDrill = onStartDrill != null;

  if (kind === "family") {
    const familyName = formatOpeningName(node.openingName);
    return (
      <>
        <div className="tree-node-card__header">
          <span className="tree-node-card__move-label" title={familyName}>
            {familyName}
          </span>
        </div>

        <dl className="tree-node-card__score-panel">
          <div className="tree-node-card__score-metric">
            <dt>Score</dt>
            <dd className="tree-node-card__score-value">
              {formatScore(node.score)}
              <GradeTag score={node.score} />
            </dd>
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

  const isRoot = node.san === null;
  const isUserSelected = !isRoot && node.isUserSelected;
  const isOffBook = !isRoot && !node.inBook && !node.isUserSelected;
  const name = formatOpeningName(node.openingName);
  const evalText = formatWhiteEval(node.evalCp, node.evalMate) || "—";

  return (
    <>
      <div className="tree-node-card__header">
        <span className="tree-node-card__move-label">
          {formatMoveLabel(node.ply, node.san)}
        </span>
        {!isRoot && (
          <span className="tree-node-card__name-line">
            <span className="tree-node-card__name" title={name}>
              {name}
            </span>
            {isUserSelected && <SelectedMoveChip />}
            {isOffBook && <OffBookChip />}
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
  kind = "move",
  onSelect,
  isSelected,
  isExpanded,
  controlsId,
  ariaLabel,
  onStartDrill,
  onCollapse,
}: OpeningTreeNodeCardProps) {
  const className = [
    "tree-node-card",
    `tree-node-card--${variant}`,
    `tree-node-card--grade-${getGradeToken(node.score)}`,
    kind === "family" ? "tree-node-card--family" : "",
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
          aria-label={ariaLabel}
          aria-pressed={!!isSelected}
          aria-expanded={isExpanded === undefined ? undefined : isExpanded}
          aria-controls={controlsId}
        >
          <CompactBody node={node} kind={kind} />
        </button>
      );
    }

    return (
      <div className={className}>
        <CompactBody node={node} kind={kind} />
      </div>
    );
  }

  return (
    <div className={className}>
      {onCollapse && (
        // Full-surface overlay that collapses the card. Sits behind the Start
        // Drill button (raised via z-index) so that control stays clickable.
        <button
          type="button"
          className="tree-node-card__collapse-nav"
          aria-label={`Collapse ${formatOpeningName(node.openingName)} details`}
          onClick={onCollapse}
        />
      )}
      <ExpandedBody node={node} kind={kind} onStartDrill={onStartDrill} />
    </div>
  );
}

export default OpeningTreeNodeCard;
