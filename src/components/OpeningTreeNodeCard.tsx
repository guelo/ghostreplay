import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import {
  buildMoveListTokens,
  formatGames,
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
  isTerminal: boolean;
  /** checkmate | stalemate | opening_boundary | no_children; null when not terminal. */
  terminalReason: string | null;
  /** Drillable-root key from the API; non-null => Start Drill is offered. */
  drillOpeningKey: string | null;
  /** SAN moves that produced this node/opening, oldest-first, with this node's
   *  own move as the LAST (bold) token. Empty for the synthesized root; the card
   *  then renders no secondary move-list line. */
  moveListSan: string[];
  /** Ply of `moveListSan[0]` (1 = White's move 1); anchors move numbering. */
  moveListStartPly: number;
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
  /** Expanded: optional footer node (e.g. a "View in Openings" link) rendered
   *  inside the card, raised above the `onCollapse` overlay so it stays
   *  clickable. Its clicks are stopped from bubbling so a tap never collapses the
   *  card. Kept as an injected node so the card stays router-free. */
  footerAction?: ReactNode;

  /** When true, this card's score is still being computed server-side, so the
   *  score + grade slot renders a loading placeholder instead of "—" (g-a5v3).
   *
   *  Carried explicitly rather than inferred from `score == null`, because a
   *  null score already has a distinct, legitimate meaning ("no score for this
   *  opening") that must stay visually different from "still loading". */
  scorePending?: boolean;
}

/**
 * Placeholder occupying the score + grade slot while scores load (g-a5v3).
 * Reserves the slot's width so hydrating a score does not reflow the card, and
 * exposes an accessible loading label — a bare visual shimmer would leave the
 * state unannounced to assistive tech.
 */
function ScorePlaceholder() {
  return (
    <span className="tree-node-card__score-loading" aria-busy="true">
      <span className="tree-node-card__score-shimmer" aria-hidden="true" />
      <span className="tree-node-card__score-loading-label">Score loading</span>
    </span>
  );
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
    popup.style.top = `${top}px`;
    popup.style.left = `${left}px`;
    popup.style.visibility = "visible";
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
          style={{ top: 0, left: 0, visibility: "hidden" }}
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

/** The synthesized `/openings` start card — never a family card (whose `san` is
 *  also null and whose `moves` may defensively be `[]`). Only this renders as
 *  "Starting position" with no move list. */
function isSynthesizedRoot(node: OpeningTreeNodeView, kind: "move" | "family") {
  return (
    kind === "move" &&
    node.ply === 0 &&
    node.san === null &&
    node.openingName == null
  );
}

/**
 * Inline move-list line ("1.e4 c6 2.Bc4") shared by both variants, with the last
 * (crossing) move bold to disambiguate sibling cards that share an inherited
 * name. Compact truncates to one line (with a full-text `title`); expanded wraps.
 * Renders nothing when there are no moves (the root / a defensive empty family).
 */
function MoveListLine({
  sanMoves,
  startPly,
  variant,
}: {
  sanMoves: string[];
  startPly: number;
  variant: "compact" | "expanded";
}) {
  const tokens = buildMoveListTokens(sanMoves, startPly);
  if (tokens.length === 0) {
    return null;
  }
  const fullText = tokens.map((token) => token.text).join(" ");
  return (
    <span
      className={`tree-node-card__move-list tree-node-card__move-list--${variant}`}
      title={variant === "compact" ? fullText : undefined}
    >
      {tokens.map((token, index) =>
        token.isLast ? (
          <strong key={index} className="tree-node-card__move-list-last">
            {token.text}
          </strong>
        ) : (
          <span key={index}>{token.text} </span>
        ),
      )}
    </span>
  );
}

/** Compact body: primary line = opening name + score/grade; secondary line = the
 *  played move list (last move bold). The `move` kind keeps the move-type chips
 *  (before the list) and the eval (right); the `family` kind shows just the move
 *  list. The synthesized `/openings` root shows "Starting position", no list. */
function CompactBody({
  node,
  kind,
  scorePending,
}: {
  node: OpeningTreeNodeView;
  kind: "move" | "family";
  scorePending?: boolean;
}) {
  const isRoot = isSynthesizedRoot(node, kind);
  const isMove = kind === "move";
  const isUserSelected = isMove && !isRoot && node.isUserSelected;
  const isOffBook = isMove && !isRoot && !node.inBook && !node.isUserSelected;
  const name = isRoot ? "Starting position" : formatOpeningName(node.openingName);
  const evalText = formatWhiteEval(node.evalCp, node.evalMate) || "—";

  return (
    <>
      <span className="tree-node-card__line tree-node-card__line--primary">
        <span
          className="tree-node-card__move tree-node-card__move--name"
          title={name}
        >
          {name}
        </span>
        <span className="tree-node-card__primary-right">
          {scorePending ? (
            <ScorePlaceholder />
          ) : (
            <>
              <span className="tree-node-card__score">{formatScore(node.score)}</span>
              <GradeTag score={node.score} />
            </>
          )}
        </span>
      </span>
      {!isRoot && (isMove || node.moveListSan.length > 0) && (
        <span className="tree-node-card__line tree-node-card__line--secondary">
          {isUserSelected && <SelectedMoveChip />}
          {isOffBook && <OffBookChip />}
          <MoveListLine
            sanMoves={node.moveListSan}
            startPly={node.moveListStartPly}
            variant="compact"
          />
          {isMove && <span className="tree-node-card__eval">{evalText}</span>}
        </span>
      )}
    </>
  );
}

/** Expanded body: header (opening name), the played move list under it, the
 *  score/eval panel, metrics, terminal note, and Start Drill. The `family` kind
 *  drops the Eval tile, the move-type chips and the terminal note (a family has
 *  no SAN/eval); the synthesized `/openings` root shows "Starting position" with
 *  no move list. */
function ExpandedBody({
  node,
  kind,
  onStartDrill,
  scorePending,
}: {
  node: OpeningTreeNodeView;
  kind: "move" | "family";
  onStartDrill?: () => void;
  scorePending?: boolean;
}) {
  // Every expanded card is drillable; the page decides drillability by passing
  // onStartDrill (wired for move/family cards, omitted for the synthesized root
  // and the live-panel lineage). drillOpeningKey no longer gates this.
  const showDrill = onStartDrill != null;

  const isRoot = isSynthesizedRoot(node, kind);
  const isMove = kind === "move";
  const isUserSelected = isMove && !isRoot && node.isUserSelected;
  const isOffBook = isMove && !isRoot && !node.inBook && !node.isUserSelected;
  const headerLabel = isRoot
    ? "Starting position"
    : formatOpeningName(node.openingName);
  const evalText = formatWhiteEval(node.evalCp, node.evalMate) || "—";

  return (
    <>
      <div className="tree-node-card__header">
        <span className="tree-node-card__move-label" title={headerLabel}>
          {headerLabel}
        </span>
        {!isRoot && (
          <span className="tree-node-card__name-line">
            <MoveListLine
              sanMoves={node.moveListSan}
              startPly={node.moveListStartPly}
              variant="expanded"
            />
            {isUserSelected && <SelectedMoveChip />}
            {isOffBook && <OffBookChip />}
          </span>
        )}
      </div>

      <dl className="tree-node-card__score-panel">
        <div className="tree-node-card__score-metric">
          <dt>Score</dt>
          <dd className="tree-node-card__score-value">
            {scorePending ? (
              <ScorePlaceholder />
            ) : (
              <>
                {formatScore(node.score)}
                <GradeTag score={node.score} />
              </>
            )}
          </dd>
        </div>
        {isMove && (
          <div className="tree-node-card__score-metric">
            <dt>Eval</dt>
            <dd>{evalText}</dd>
          </div>
        )}
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
      </dl>

      {isMove && node.isTerminal && (
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
  footerAction,
  scorePending,
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
          <CompactBody node={node} kind={kind} scorePending={scorePending} />
        </button>
      );
    }

    return (
      <div className={className}>
        <CompactBody node={node} kind={kind} scorePending={scorePending} />
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
      <ExpandedBody
        node={node}
        kind={kind}
        onStartDrill={onStartDrill}
        scorePending={scorePending}
      />
      {footerAction && (
        // Raised above the collapse overlay (z-index) so it stays clickable; its
        // clicks are stopped so tapping it never collapses the card.
        <div
          className="tree-node-card__footer-action"
          onClick={(e) => e.stopPropagation()}
        >
          {footerAction}
        </div>
      )}
    </div>
  );
}

export default OpeningTreeNodeCard;
