import { useEffect, useMemo, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Chessboard, defaultPieces } from "react-chessboard";
import type { PieceDropHandlerArgs } from "react-chessboard";
import AppNav from "../components/AppNav";
import OpeningTreeNodeCard from "../components/OpeningTreeNodeCard";
import { formatMoveLabel } from "../openings/format";
import {
  buildCanonicalReplacement,
  buildOpeningsSearchParams,
  parseOpeningsSearchParams,
} from "../openings/route";
import {
  connectorStyle,
  resolveDrop,
  type DisplayColumn,
} from "../openings/treeView";
import { useTreeConnectors } from "../openings/useTreeConnectors";
import { useOpeningsTree } from "../hooks/useOpeningsTree";
import { captureEvent } from "../analytics/posthog";
import type { OpeningPlayerColor } from "../utils/api";
import "../App.css";

const WhiteKing = defaultPieces.wK;
const BlackKing = defaultPieces.bK;

const COLOR_OPTIONS: Array<{
  label: string;
  value: OpeningPlayerColor;
  King: typeof WhiteKing;
}> = [
  { label: "White", value: "white", King: WhiteKing },
  { label: "Black", value: "black", King: BlackKing },
];

const LAST_MOVE_HIGHLIGHT: React.CSSProperties = {
  background: "rgba(56, 189, 248, 0.35)",
};

/** One vertical column of tree node cards; the deepest selected node expands. */
function TreeColumnView({
  column,
  columnIndex,
  registerColumn,
  registerSelectedNode,
  onSelect,
  onStartDrill,
}: {
  column: DisplayColumn;
  columnIndex: number;
  registerColumn: (idx: number, el: HTMLElement | null) => void;
  registerSelectedNode: (idx: number, el: HTMLElement | null) => void;
  onSelect: (line: string[]) => void;
  onStartDrill: (openingKey: string) => void;
}) {
  // The column hosting the expanded (deepest selected) card is the active one.
  const isActive = column.nodes.some((node) => node.isExpanded);

  // Header (copied from TreePrototype): the move chosen in this column, shown
  // with its move number. "Start" at the root, the selected move label for a
  // chosen column, "—" for the frontier column that has no selection yet.
  const selectedNode = column.nodes.find((node) => node.isSelected) ?? null;
  const headerLabel =
    column.kind === "root"
      ? "Start"
      : selectedNode
        ? formatMoveLabel(selectedNode.view.ply, selectedNode.view.san)
        : "—";
  // Where clicking the header navigates: the root jumps to the start; a chosen
  // column truncates the line to its selection; the frontier header is inert.
  const headerLine: string[] | null =
    column.kind === "root" ? [] : (selectedNode?.selectLine ?? null);

  return (
    <div
      className={`openings-tree-column${
        isActive ? " openings-tree-column--active" : ""
      }`}
      data-testid="tree-column"
      data-line-index={column.lineIndex}
    >
      {/* Header sits OUTSIDE the scroller (like TreePrototype) so the per-column
          scrollbar spans only the cards — the header's underline stays full
          column width. */}
      <button
        type="button"
        className={`openings-tree-column__header${
          selectedNode ? " openings-tree-column__header--active" : ""
        }`}
        data-testid="tree-column-header"
        disabled={headerLine === null}
        onClick={headerLine !== null ? () => onSelect(headerLine) : undefined}
      >
        {headerLabel}
      </button>
      {/* The nodes wrapper is the per-column vertical scroller, and the element
          the connector hook measures (its rect is both the connector x-edge and
          the y-clamp band). */}
      <div
        className="openings-tree-column__nodes"
        ref={(el) => registerColumn(columnIndex, el)}
      >
        {column.nodes.map((node) => {
          const card = node.isExpanded ? (
            <OpeningTreeNodeCard
              variant="expanded"
              node={node.view}
              onStartDrill={
                node.view.drillOpeningKey != null
                  ? () => onStartDrill(node.view.drillOpeningKey as string)
                  : undefined
              }
            />
          ) : (
            <OpeningTreeNodeCard
              variant="compact"
              node={node.view}
              // A non-navigable boundary node renders as a plain (non-button)
              // card; clicking it would only push a URL the backend truncates.
              onSelect={
                node.isSelectable ? () => onSelect(node.selectLine) : undefined
              }
              isSelected={node.isSelected}
            />
          );

          // Wrap ONLY the selected node so the connector hook can measure its
          // center (never siblings). The card is width:100%, so the wrapper box
          // equals the card box and layout is unchanged.
          if (node.isSelected) {
            return (
              <div
                key={node.key}
                ref={(el) => registerSelectedNode(columnIndex, el)}
              >
                {card}
              </div>
            );
          }
          return <div key={node.key}>{card}</div>;
          // NB: every node renders inside a 1:1 wrapper div so the column's flex
          // children are uniform; only the selected one carries a measure ref.
        })}
      </div>
    </div>
  );
}

/**
 * `/openings` — chesstree.net-style horizontal move tree synced with a board.
 * The page parses the URL line, drives {@link useOpeningsTree}, renders the
 * board + a horizontally-scrolling canvas of {@link OpeningTreeNodeCard}
 * columns with measured selected-path SVG connectors, and owns selection /
 * board drops / perspective / canonicalization / Start Drill / the five states.
 */
function OpeningsPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { playerColor, moves, opening } =
    parseOpeningsSearchParams(searchParams);

  const {
    view,
    pageStatus,
    appendStatus,
    error,
    canonicalLine,
    isSettled,
    batchComputedAt,
    retry,
  } = useOpeningsTree({ playerColor, moves, opening });

  // --- Connector measurement plumbing --------------------------------------
  // The canvas is the positioned/measured frame; the scroller is the overflow-x
  // wrapper. Stable Maps (held behind refs so the page can mutate them without
  // tripping react-hooks/immutability) key the column + selected-node elements
  // by display-column index, populated via ref-setter callbacks during commit.
  const scrollRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const columnElsRef = useRef<Map<number, HTMLElement>>(new Map());
  const selectedNodeElsRef = useRef<Map<number, HTMLElement>>(new Map());

  const registerColumn = (idx: number, el: HTMLElement | null) => {
    if (el) columnElsRef.current.set(idx, el);
    else columnElsRef.current.delete(idx);
  };
  const registerSelectedNode = (idx: number, el: HTMLElement | null) => {
    if (el) selectedNodeElsRef.current.set(idx, el);
    else selectedNodeElsRef.current.delete(idx);
  };

  const columns = view?.columns ?? null;
  const selectionLine = view?.selectionLine ?? [];
  const columnCount = columns?.length ?? 0;
  // Full line (not a depth count): a same-depth sibling switch changes which
  // node is selected without changing columnCount and must re-aim the lines.
  const selectionKey = `${playerColor}\u0000${selectionLine.join("\u0000")}`;

  // Geometry only. Style is applied at render (below) so live in_book /
  // is_observed / encounter_count metadata always reaches the DOM without a
  // re-measure.
  const connectors = useTreeConnectors({
    canvasRef,
    scrollRef,
    columnElsRef,
    selectedNodeElsRef,
    columnCount,
    selectionKey,
  });

  // Connector styles, derived purely from the model every render (NOT measured):
  // the style for the pair (i, i+1) comes from the selected child of column
  // i+1 — the edge identity is the child. Index-aligned with `connectors`
  // because both walk the same `columns` adjacency.
  const connectorStyles = useMemo(
    () =>
      (columns ?? [])
        .slice(0, -1)
        .map((_, i) =>
          connectorStyle(
            columns?.[i + 1]?.nodes.find((node) => node.isSelected) ?? null,
          ),
        ),
    [columns],
  );

  // Autoscroll the active display column near the left edge so the expanded
  // column sits left and the next compact column peeks. No-op when refs are
  // missing (covers jsdom and the pre-settle frames).
  useEffect(() => {
    const scroller = scrollRef.current;
    const activeEl = columnElsRef.current.get(selectionLine.length);
    if (!scroller || !activeEl) return;
    const inset = 24;
    scroller.scrollLeft = activeEl.offsetLeft - inset;
    // selectionKey/columnCount move the active column; the ref Map is stable.
  }, [selectionKey, columnCount, selectionLine.length]);

  // Canonicalize the URL only when the rendered view is settled for the current
  // route, so a stale response kept on screen during a refetch can never rewrite
  // a freshly-selected line backward. Rewrites legacy opening=<fen> → move=,
  // truncates invalid/stale lines, and normalizes color; null => already
  // canonical (no history loop).
  useEffect(() => {
    if (!isSettled || !canonicalLine) {
      return;
    }
    const replacement = buildCanonicalReplacement(
      searchParams,
      playerColor,
      canonicalLine,
    );
    if (replacement) {
      setSearchParams(replacement, { replace: true });
    }
  }, [isSettled, canonicalLine, playerColor, searchParams, setSearchParams]);

  // Selection pushes a new history entry (so Back works); truncation falls out
  // of the shorter line each node computes. Both entry points (node click and
  // board drop) flow through here, so one capture covers all tree navigation.
  const selectLine = (newLine: string[]) => {
    captureEvent("opening_explored", {
      from_key: moves.join(","),
      to_key: newLine.join(","),
      depth: newLine.length,
      player_color: playerColor,
    });
    setSearchParams(buildOpeningsSearchParams({ playerColor, moves: newLine }));
  };

  // Perspective switch keeps the shared line; the hook refetches (color in key)
  // and the board orientation flips immediately, before the refetch resolves.
  const switchColor = (color: OpeningPlayerColor) => {
    if (color === playerColor) {
      return;
    }
    setSearchParams(buildOpeningsSearchParams({ playerColor: color, moves }));
  };

  const startDrill = (openingKey: string) => {
    navigate("/play", { state: { drillSetup: { openingKey, playerColor } } });
  };

  // Board → tree: accept a drag only when the children column of the deepest
  // position is actually rendered (settled) and the move lands on a navigable
  // node there. Otherwise reject (board snaps back) so board and tree can never
  // diverge.
  const handlePieceDrop = ({
    sourceSquare,
    targetSquare,
  }: PieceDropHandlerArgs): boolean => {
    if (!view || !isSettled || !targetSquare) {
      return false;
    }
    const uci = resolveDrop(view.board.fen, sourceSquare, targetSquare);
    if (!uci) {
      return false;
    }
    const frontier = view.columns.find(
      (column) =>
        column.kind === "moves" &&
        column.lineIndex === view.selectionLine.length,
    );
    const target = frontier?.nodes.find(
      (node) => node.uci === uci && node.isNavigable,
    );
    if (!target) {
      return false;
    }
    selectLine(target.selectLine);
    return true;
  };

  const colorLabel = playerColor === "white" ? "White" : "Black";
  const lastMove = view?.board.lastMove ?? null;
  const squareStyles = lastMove
    ? {
        [lastMove.from]: LAST_MOVE_HIGHLIGHT,
        [lastMove.to]: LAST_MOVE_HIGHLIGHT,
      }
    : {};

  return (
    <main className="app-shell openings-page">
      <AppNav />

      <div className="constrained-content">
        <section className="openings-tree">
          <header className="openings-tree__header">
            <h1 className="openings-tree__title">Openings Tree</h1>
            <div
              className="openings-color-picker"
              role="group"
              aria-label="Playing as"
            >
              <span className="openings-tree__color-label">Playing as:</span>
              <div className="mode-toggle-row segmented-toggle openings-color-picker__toggle">
                {COLOR_OPTIONS.map(({ label, value, King }) => (
                  <button
                    key={value}
                    type="button"
                    className={`chess-button toggle${
                      playerColor === value ? " active" : ""
                    }`}
                    aria-pressed={playerColor === value}
                    onClick={() => switchColor(value)}
                  >
                    <span className="openings-color-picker__piece">
                      <King />
                    </span>
                    {label}
                  </button>
                ))}
              </div>
            </div>
          </header>

          {pageStatus === "error" && !view ? (
            <section
              className="openings-state openings-state--error"
              role="alert"
            >
              <p className="openings-state__title">
                {error ?? "Failed to load openings"}
              </p>
              <p className="openings-state__body">
                The {colorLabel.toLowerCase()} opening tree did not load. Retry
                to fetch the latest cached scores.
              </p>
              <button
                className="chess-button primary"
                type="button"
                onClick={retry}
              >
                Retry
              </button>
            </section>
          ) : !view ? (
            <section
              className="openings-state openings-state--loading"
              aria-live="polite"
              aria-label="Loading openings"
            >
              <p className="openings-state__title">Loading openings…</p>
              <div className="openings-tree-workspace openings-tree-workspace--loading">
                <div
                  className="openings-tree-board openings-tree-board--skeleton"
                  aria-hidden="true"
                />
                <div className="openings-tree-scroll">
                  <div className="openings-tree-canvas">
                    {Array.from({ length: 3 }).map((_, index) => (
                      <div
                        key={index}
                        className="openings-tree-column openings-tree-column--skeleton"
                        aria-hidden="true"
                      />
                    ))}
                  </div>
                </div>
              </div>
            </section>
          ) : (
            <>
              {/* Only label the banner when the view is SETTLED for the current
                  route: a provisional/append-error render keeps the previous
                  response on screen, whose batch (and color) may not match the
                  current playerColor. A settled response is always the current
                  color (step-1/4 fetch or the same-color step-2 prefix). */}
              {isSettled && batchComputedAt === null && (
                <p className="openings-tree__nodata-banner" role="note">
                  No games for {colorLabel} yet — showing the reference opening
                  book.
                </p>
              )}
              <div className="openings-tree-workspace">
                <div className="openings-tree-board">
                  <Chessboard
                    options={{
                      id: "openings-tree-board",
                      position: view.board.fen,
                      boardOrientation: playerColor,
                      allowDragging: true,
                      onPieceDrop: handlePieceDrop,
                      animationDurationInMs: 150,
                      squareStyles,
                    }}
                  />
                </div>

                <div className="openings-tree-scroll" ref={scrollRef}>
                  <div
                    className="openings-tree-canvas"
                    ref={canvasRef}
                    aria-label={`${colorLabel} opening tree`}
                  >
                    <svg
                      className="openings-tree-connectors"
                      aria-hidden="true"
                    >
                      <defs>
                        <marker
                          id="openings-tree-arrowhead"
                          markerUnits="userSpaceOnUse"
                          markerWidth="10"
                          markerHeight="10"
                          refX="0"
                          refY="5"
                          orient="auto"
                        >
                          <path d="M0,0 L9,5 L0,10 Z" fill="currentColor" />
                        </marker>
                      </defs>
                      {connectors.map((c, i) => {
                        const style = connectorStyles[i] ?? { width: 2 };
                        // End the stroke short of the column by the arrowhead
                        // length; the marker (refX=0) fills the gap so its tip
                        // lands on the column. The bezier's end tangent is
                        // horizontal, so shortening x keeps the tip on target.
                        const ARROW = 9;
                        const x2 = c.x2 - ARROW;
                        const dx = Math.max(16, (x2 - c.x1) / 2);
                        const d = `M ${c.x1} ${c.y1} C ${c.x1 + dx} ${c.y1}, ${
                          x2 - dx
                        } ${c.y2}, ${x2} ${c.y2}`;
                        // When an endpoint's cell is scrolled out of its column,
                        // mark the clamped edge with a small triangle pointing
                        // toward the selection, and dash the line (matching
                        // TreePrototype) so an off-screen endpoint reads as
                        // "continues past the visible band."
                        const clampTip = (
                          cx: number,
                          cy: number,
                          off: -1 | 0 | 1,
                        ) =>
                          off === -1
                            ? `M ${cx - 5} ${cy + 5} L ${cx} ${cy - 2} L ${
                                cx + 5
                              } ${cy + 5} Z`
                            : off === 1
                              ? `M ${cx - 5} ${cy - 5} L ${cx} ${cy + 2} L ${
                                  cx + 5
                                } ${cy - 5} Z`
                              : null;
                        const tip = clampTip(c.x1, c.y1, c.off);
                        const tip2 = clampTip(x2 - 7, c.y2, c.off2);
                        const clamped = c.off || c.off2;
                        return (
                          <g key={i}>
                            <path
                              className="openings-tree-connector"
                              d={d}
                              fill="none"
                              stroke="currentColor"
                              strokeWidth={style.width}
                              strokeDasharray={clamped ? "5 4" : undefined}
                              markerEnd="url(#openings-tree-arrowhead)"
                              opacity={clamped ? 0.5 : 0.9}
                            />
                            {tip && (
                              <path d={tip} fill="currentColor" opacity={0.8} />
                            )}
                            {tip2 && (
                              <path
                                d={tip2}
                                fill="currentColor"
                                opacity={0.8}
                              />
                            )}
                          </g>
                        );
                      })}
                    </svg>

                    {view.columns.map((column, index) => (
                      <TreeColumnView
                        key={`${column.kind}-${column.lineIndex}`}
                        column={column}
                        columnIndex={index}
                        registerColumn={registerColumn}
                        registerSelectedNode={registerSelectedNode}
                        onSelect={selectLine}
                        onStartDrill={startDrill}
                      />
                    ))}

                    {appendStatus === "loading" && (
                      <div
                        className="openings-tree-column openings-tree-append openings-tree-append--loading"
                        aria-live="polite"
                      >
                        <p className="openings-tree-append__label">Loading…</p>
                      </div>
                    )}

                    {appendStatus === "error" && (
                      <div
                        className="openings-tree-column openings-tree-append openings-tree-append--error"
                        role="alert"
                      >
                        <p className="openings-tree-append__label">
                          {error ?? "Failed to load moves"}
                        </p>
                        <button
                          className="chess-button"
                          type="button"
                          onClick={retry}
                        >
                          Retry
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </>
          )}
        </section>
      </div>
    </main>
  );
}

export default OpeningsPage;
