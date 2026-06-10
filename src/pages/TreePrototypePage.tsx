import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Chess } from "chess.js";
import { Chessboard } from "react-chessboard";
import type { PieceDropHandlerArgs } from "react-chessboard";
import { getPriorityLabel } from "../openings/format";

/**
 * g-nge9 — Standalone prototype exploring a chesstree.net-style horizontal
 * move-tree graph synced with a chessboard. NOT hooked up to any game data:
 * the tree is generated lazily from legal moves.
 *
 * Model (chesstree.net-style, only the selected branch is shown expanded):
 *   - Column 0 is the root (starting position).
 *   - Each subsequent column shows ALL legal moves from the node selected in
 *     the previous column. Clicking a move selects it and reveals the next
 *     column of its legal replies, truncating any deeper selection.
 *
 * Pruning legal moves down to the "interesting" ones is g-d5cu's job and is
 * deliberately out of scope here — every legal move is rendered.
 */

const STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

type TreeNode = {
  /** Move (SAN) that produced this position; null for the root. */
  san: string | null;
  /** Resulting FEN. */
  fen: string;
  /** 0 = root, 1 = after White's first move, etc. */
  ply: number;
};

const ROOT: TreeNode = { san: null, fen: STARTING_FEN, ply: 0 };

/**
 * A drawn elbow from the selected cell in one column to the selected cell in
 * the next column (or, when the next column has no selection yet, its vertical
 * midpoint). Coordinates are in tree-local space (relative to the `tree`
 * element, so they survive horizontal scrolling). `off`/`off2` are 0 when the
 * origin/target cell is visible, or ±1 when it has scrolled above (-1) / below
 * (+1) its column's viewport and the endpoint has been clamped to the edge.
 */
type Connector = {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  off: -1 | 0 | 1;
  off2: -1 | 0 | 1;
};

/** Below this width the board and tree stack vertically. */
const NARROW_QUERY = "(max-width: 720px)";

/** Track a CSS media query, SSR-safe. */
function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () =>
      typeof window !== "undefined" && window.matchMedia(query).matches,
  );
  useEffect(() => {
    if (typeof window === "undefined") return;
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

/** Generate every legal move from a position as child tree nodes. */
function legalChildren(fen: string, parentPly: number): TreeNode[] {
  const chess = new Chess(fen);
  return chess.moves({ verbose: true }).map((move) => {
    const next = new Chess(fen);
    next.move(move);
    return { san: move.san, fen: next.fen(), ply: parentPly + 1 };
  });
}

/** Highlight the squares of the last move played to reach `node`. */
function lastMoveSquares(
  parent: TreeNode | undefined,
  node: TreeNode,
): Record<string, React.CSSProperties> {
  if (!parent || !node.san) return {};
  const chess = new Chess(parent.fen);
  const move = chess.move(node.san);
  if (!move) return {};
  const style: React.CSSProperties = { background: "rgba(56, 189, 248, 0.35)" };
  return { [move.from]: style, [move.to]: style };
}

function moveLabel(node: TreeNode): string {
  if (!node.san) return "Start";
  const moveNumber = Math.ceil(node.ply / 2);
  const isWhite = node.ply % 2 === 1;
  return isWhite ? `${moveNumber}. ${node.san}` : `${moveNumber}… ${node.san}`;
}

/** First four FEN fields — the key format used by eco.byPosition.json. */
function canonicalFen(fen: string): string {
  return fen.split(" ").slice(0, 4).join(" ");
}

/**
 * Deterministic mock "opening score" (10–99) derived from the FEN, standing in
 * for the real score shown in /openings. Stable per position so it doesn't
 * flicker between renders.
 */
function fenHash(fen: string, salt = 0): number {
  let hash = salt;
  for (let i = 0; i < fen.length; i++) {
    hash = (hash * 31 + fen.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

function mockScore(fen: string): number {
  return 10 + (fenHash(fen) % 90);
}

/** Format a centipawn eval the way engines do: +1.23 / -0.40 / 0.00. */
function formatEval(centipawns: number): string {
  const pawns = centipawns / 100;
  return `${pawns >= 0 ? "+" : ""}${pawns.toFixed(2)}`;
}

/** Mock /openings-card metrics, deterministic per position. */
function mockStats(fen: string) {
  const score = mockScore(fen);
  return {
    score,
    grade: getPriorityLabel(score),
    coverage: fenHash(fen, 7) % 101, // %
    games: 5 + (fenHash(fen, 13) % 400),
    confidence: 40 + (fenHash(fen, 23) % 60), // %
    // Mock Stockfish eval in centipawns, roughly -3.00 … +3.00.
    evalCp: (fenHash(fen, 31) % 601) - 300,
  };
}

/**
 * Compact, in-cell version of the /openings family card, rendered for the
 * deepest-selected node. Uses mock metrics (this prototype has no game data).
 */
function OpeningCard({
  node,
  name,
}: {
  node: TreeNode;
  name: string | undefined;
}) {
  const stats = mockStats(node.fen);
  return (
    <div style={styles.card}>
      <div style={styles.cardTitle}>
        {name ?? (node.san ? moveLabel(node) : "Starting position")}
      </div>

      <div style={styles.cardStatRow}>
        <div style={styles.cardStat}>
          <div style={styles.cardStatLabel}>Your Score</div>
          <div style={styles.cardScorePanel}>
            <span style={styles.cardScoreValue}>{stats.score}</span>
            <span style={styles.cardGrade}>{stats.grade}</span>
          </div>
        </div>
        <div style={styles.cardStat}>
          <div style={styles.cardStatLabel}>Engine Eval</div>
          <span style={styles.cardEvalValue}>{formatEval(stats.evalCp)}</span>
        </div>
      </div>

      <dl style={styles.cardMetrics}>
        <div style={styles.cardMetric}>
          <dt style={styles.cardMetricLabel}>Coverage</dt>
          <dd style={styles.cardMetricValue}>{stats.coverage}%</dd>
        </div>
        <div style={styles.cardMetric}>
          <dt style={styles.cardMetricLabel}>Games</dt>
          <dd style={styles.cardMetricValue}>
            {stats.games.toLocaleString()}
          </dd>
        </div>
        <div style={styles.cardMetric}>
          <dt style={styles.cardMetricLabel}>Confidence</dt>
          <dd style={styles.cardMetricValue}>{stats.confidence}%</dd>
        </div>
      </dl>

      <button
        type="button"
        style={styles.cardDrillButton}
        onClick={() => {
          // Mock action — no drill flow wired up in the prototype.
          window.alert(`Start Drill: ${name ?? moveLabel(node)}`);
        }}
      >
        Start Drill
      </button>
    </div>
  );
}

function TreePrototypePage() {
  // The selected path from root to the currently-focused node. The board shows
  // the last element's position.
  const [path, setPath] = useState<TreeNode[]>([ROOT]);
  const isNarrow = useMediaQuery(NARROW_QUERY);

  // Opening-name lookup by canonical FEN, loaded once from eco.byPosition.json
  // (same data /openings uses). Null until loaded.
  const [ecoNames, setEcoNames] = useState<Map<string, string> | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetch("/data/openings/eco.byPosition.json")
      .then((res) => res.json())
      .then((data: { by_position: Record<string, { name: string }> }) => {
        if (cancelled) return;
        const map = new Map<string, string>();
        for (const [fen, entry] of Object.entries(data.by_position)) {
          map.set(fen, entry.name);
        }
        setEcoNames(map);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);
  const openingName = (fen: string): string | undefined =>
    ecoNames?.get(canonicalFen(fen));

  // Build the columns to render: column 0 is the root; each later column holds
  // the legal moves from the selected node in the previous column.
  const columns = useMemo(() => {
    const cols: { nodes: TreeNode[]; selectedSan: string | null }[] = [
      { nodes: [ROOT], selectedSan: ROOT.san },
    ];
    for (let i = 0; i < path.length; i++) {
      const node = path[i];
      const children = legalChildren(node.fen, node.ply);
      if (children.length === 0) break; // checkmate / stalemate
      const selectedChild = path[i + 1] ?? null;
      cols.push({ nodes: children, selectedSan: selectedChild?.san ?? null });
    }
    return cols;
  }, [path]);

  const selected = path[path.length - 1];

  // Autoscroll the grid so a newly-revealed right-most column is visible.
  const treeScrollRef = useRef<HTMLDivElement>(null);
  const columnCount = columns.length;
  useEffect(() => {
    const el = treeScrollRef.current;
    if (!el) return;
    el.scrollTo({ left: el.scrollWidth, behavior: "smooth" });
  }, [columnCount]);

  // --- Cell → child-column-header connectors -------------------------------
  // The tree is the positioned/measured frame. Everything (cells, headers, the
  // SVG overlay) lives inside it, so a measurement of `elementRect - treeRect`
  // is stable under both horizontal tree scroll and per-column vertical scroll.
  const treeRef = useRef<HTMLDivElement>(null);
  // Stable keyed element stores. Plain Maps (not refs) so the ref-callback
  // factory and render code never touch a ref's `.current` during render.
  const columnRefs = useMemo(() => new Map<number, HTMLDivElement>(), []);
  const nodesScrollRefs = useMemo(() => new Map<number, HTMLDivElement>(), []);
  const headerRefs = useMemo(() => new Map<number, HTMLButtonElement>(), []);
  const pathNodeRefs = useMemo(() => new Map<number, HTMLElement>(), []);
  const setMapRef =
    <T extends HTMLElement>(map: Map<number, T>, key: number) =>
    (el: T | null) => {
      if (el) map.set(key, el);
      else map.delete(key);
    };

  const [connectors, setConnectors] = useState<Connector[]>([]);
  useLayoutEffect(() => {
    let frame = 0;
    const measure = () => {
      const treeEl = treeRef.current;
      if (!treeEl) return;
      const t = treeEl.getBoundingClientRect();
      const next: Connector[] = [];
      // path[c] is the selected node in column c and the parent of column c+1.
      for (let c = 0; c < path.length; c++) {
        const childNodes = nodesScrollRefs.get(c + 1);
        const cell = pathNodeRefs.get(c);
        const col = columnRefs.get(c);
        const nodes = nodesScrollRefs.get(c);
        if (!childNodes || !cell || !col || !nodes) continue;

        const colR = col.getBoundingClientRect();
        const cellR = cell.getBoundingClientRect();
        const bandR = nodes.getBoundingClientRect();
        const childR = childNodes.getBoundingClientRect();

        // Origin x is the parent column's right edge (stable even when the
        // cell itself is scrolled out of view). Origin y is the cell's center,
        // clamped to the column's visible scroll band.
        const x1 = colR.right - t.left;
        const cellCenter = cellR.top + cellR.height / 2 - t.top;
        const bandTop = bandR.top - t.top;
        const bandBottom = bandR.bottom - t.top;
        let y1 = cellCenter;
        let off: Connector["off"] = 0;
        if (cellCenter < bandTop) {
          y1 = bandTop;
          off = -1;
        } else if (cellCenter > bandBottom) {
          y1 = bandBottom;
          off = 1;
        }

        // Target x is the child column's left edge. Aim y at the selected
        // cell's center in the child column (clamped to that column's scroll
        // band, same technique as the origin). When the child column has no
        // selection yet (the freshly-revealed replies column), fall back to
        // the column's vertical midpoint.
        const x2 = childR.left - t.left;
        const childCell = pathNodeRefs.get(c + 1);
        let y2: number;
        let off2: Connector["off2"] = 0;
        if (childCell) {
          const childCellR = childCell.getBoundingClientRect();
          const childCellCenter =
            childCellR.top + childCellR.height / 2 - t.top;
          const childBandTop = childR.top - t.top;
          const childBandBottom = childR.bottom - t.top;
          y2 = childCellCenter;
          if (childCellCenter < childBandTop) {
            y2 = childBandTop;
            off2 = -1;
          } else if (childCellCenter > childBandBottom) {
            y2 = childBandBottom;
            off2 = 1;
          }
        } else {
          y2 = childR.top + childR.height / 2 - t.top;
        }
        next.push({ x1, y1, x2, y2, off, off2 });
      }
      setConnectors(next);
    };

    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    };

    measure();
    const treeScroll = treeScrollRef.current;
    window.addEventListener("resize", schedule);
    // capture: true so vertical scrolls inside individual columns also fire.
    treeScroll?.addEventListener("scroll", schedule, true);
    // The active column's card embeds a board that can resize after mount.
    const ro = new ResizeObserver(schedule);
    if (treeRef.current) ro.observe(treeRef.current);

    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", schedule);
      treeScroll?.removeEventListener("scroll", schedule, true);
      ro.disconnect();
    };
    // columnRefs/nodesScrollRefs/pathNodeRefs are stable memoized Maps.
  }, [path, columnCount, ecoNames, columnRefs, nodesScrollRefs, pathNodeRefs]);

  // Select a node in a given column (columnIndex 0 = root column).
  const handleSelect = (columnIndex: number, node: TreeNode) => {
    if (columnIndex === 0) {
      setPath([ROOT]);
      return;
    }
    // Keep the path up to the previous column, then append the clicked node.
    setPath((prev) => [...prev.slice(0, columnIndex), node]);
  };

  // Board → tree: playing a move on the board extends the selected path,
  // which reveals the move as the selection in the next tree column.
  const handlePieceDrop = ({
    sourceSquare,
    targetSquare,
  }: PieceDropHandlerArgs): boolean => {
    if (!targetSquare) return false;
    const chess = new Chess(selected.fen);
    const move = chess.move({
      from: sourceSquare,
      to: targetSquare,
      promotion: "q",
    });
    if (!move) return false;
    setPath((prev) => [
      ...prev,
      { san: move.san, fen: chess.fen(), ply: selected.ply + 1 },
    ]);
    return true;
  };

  return (
    <div style={styles.page}>
      <h1 style={styles.heading}>Move Tree Prototype</h1>
      <p style={styles.subhead}>
        chesstree.net-style horizontal tree of legal moves, synced with the
        board. Click any move to set the board and reveal its replies.
      </p>

      <div
        style={{
          ...styles.layout,
          ...(isNarrow ? styles.layoutNarrow : {}),
        }}
      >
        <div
          style={{
            ...styles.boardCol,
            ...(isNarrow ? styles.boardColNarrow : {}),
          }}
        >
          <Chessboard
            options={{
              id: "tree-prototype-board",
              position: selected.fen,
              allowDragging: true,
              onPieceDrop: handlePieceDrop,
              animationDurationInMs: 150,
              squareStyles: lastMoveSquares(path[path.length - 2], selected),
            }}
          />
        </div>

        <div
          ref={treeScrollRef}
          style={{
            ...styles.treeScroll,
            ...(isNarrow ? styles.treeScrollNarrow : {}),
          }}
        >
          <div ref={treeRef} style={styles.tree}>
            <svg style={styles.connectorLayer} aria-hidden="true">
              <defs>
                <marker
                  id="tree-arrowhead"
                  markerUnits="userSpaceOnUse"
                  markerWidth="10"
                  markerHeight="10"
                  refX="0"
                  refY="5"
                  orient="auto"
                >
                  <path d="M0,0 L9,5 L0,10 Z" fill="#0ea5e9" />
                </marker>
              </defs>
              {connectors.map((c, i) => {
                // End the stroke short of the column by the arrowhead length;
                // the marker (refX=0, base at the line end) fills the gap so
                // its tip lands on the column. The bezier's end tangent is
                // horizontal, so shortening x keeps the tip on target.
                const ARROW = 9;
                const x2 = c.x2 - ARROW;
                const dx = Math.max(16, (x2 - c.x1) / 2);
                const d = `M ${c.x1} ${c.y1} C ${c.x1 + dx} ${c.y1}, ${
                  x2 - dx
                } ${c.y2}, ${x2} ${c.y2}`;
                // When an endpoint's cell is scrolled out of its column, mark
                // the clamped edge with a small triangle pointing the way to
                // the selection. `cx` lets us draw the same glyph at either end
                // (the origin sits at x1; the target a touch left of the arrow
                // tip so it doesn't collide with the arrowhead).
                const clampTip = (cx: number, cy: number, off: -1 | 0 | 1) =>
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
                      d={d}
                      fill="none"
                      stroke="#0ea5e9"
                      strokeWidth={2}
                      markerEnd="url(#tree-arrowhead)"
                      opacity={clamped ? 0.5 : 0.9}
                      strokeDasharray={clamped ? "4 3" : undefined}
                    />
                    {tip && <path d={tip} fill="#0ea5e9" opacity={0.8} />}
                    {tip2 && <path d={tip2} fill="#0ea5e9" opacity={0.8} />}
                  </g>
                );
              })}
            </svg>
            {columns.map((col, colIndex) => {
              // The deepest selected node lives in column path.length - 1; its
              // cell expands into a compact /openings-style card.
              const isActiveColumn = colIndex === path.length - 1;
              return (
                <div
                  key={colIndex}
                  ref={setMapRef(columnRefs, colIndex)}
                  style={{
                    ...styles.column,
                    ...(isActiveColumn ? styles.columnActive : {}),
                  }}
                >
                  <button
                    type="button"
                    ref={setMapRef(headerRefs, colIndex)}
                    style={{
                      ...styles.columnHeader,
                      ...(path[colIndex] ? styles.columnHeaderActive : {}),
                    }}
                    onClick={() => setPath(path.slice(0, colIndex + 1))}
                  >
                    {colIndex === 0
                      ? "Start"
                      : path[colIndex]
                      ? moveLabel(path[colIndex])
                      : "—"}
                  </button>
                  <div
                    ref={setMapRef(nodesScrollRefs, colIndex)}
                    style={{
                      ...styles.columnNodes,
                      ...(isNarrow ? styles.columnNodesNarrow : {}),
                    }}
                  >
                    {col.nodes.map((node) => {
                      const isSelected =
                        colIndex === 0
                          ? path.length === 1
                          : node.san === col.selectedSan;
                      // The node on the selected path (parent of column c+1) is
                      // the connector's origin — distinct from `isSelected`,
                      // which de-highlights the root once you move past it.
                      const isPathNode =
                        colIndex === 0 || node.san === col.selectedSan;
                      const name = openingName(node.fen);

                      if (isSelected && isActiveColumn) {
                        return (
                          <div
                            key={node.san ?? "root"}
                            ref={setMapRef(pathNodeRefs, colIndex)}
                          >
                            <OpeningCard node={node} name={name} />
                          </div>
                        );
                      }

                      return (
                        <button
                          key={node.san ?? "root"}
                          type="button"
                          ref={
                            isPathNode
                              ? setMapRef(pathNodeRefs, colIndex)
                              : undefined
                          }
                          style={{
                            ...styles.node,
                            ...(isSelected ? styles.nodeSelected : {}),
                          }}
                          onClick={() => handleSelect(colIndex, node)}
                        >
                          <span style={styles.nodeTopRow}>
                            <span style={styles.nodeMove}>
                              {node.san ?? "Start"}
                            </span>
                            <span
                              style={{
                                ...styles.nodeScore,
                                ...(isSelected ? styles.nodeScoreSelected : {}),
                              }}
                            >
                              {mockScore(node.fen)}
                            </span>
                          </span>
                          {name && (
                            <span style={styles.nodeName} title={name}>
                              {name}
                            </span>
                          )}
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  page: {
    padding: "24px",
    color: "#e2e8f0",
    fontFamily: "system-ui, sans-serif",
    minHeight: "100vh",
    background: "#0f172a",
  },
  heading: { margin: "0 0 4px", fontSize: 24 },
  subhead: { margin: "0 0 20px", color: "#94a3b8", maxWidth: 560 },
  layout: { display: "flex", gap: 24, alignItems: "flex-start" },
  layoutNarrow: { flexDirection: "column" },
  boardCol: { width: 360, flexShrink: 0 },
  boardColNarrow: { width: "100%", maxWidth: 360 },
  treeScroll: { flex: 1, overflowX: "auto", paddingBottom: 12 },
  treeScrollNarrow: { width: "100%" },
  tree: { position: "relative", display: "flex", gap: 36, alignItems: "flex-start" },
  // SVG sits over the columns but ignores pointer events so cells stay
  // clickable. overflow visible keeps arrowheads from being clipped.
  connectorLayer: {
    position: "absolute",
    top: 0,
    left: 0,
    width: "100%",
    height: "100%",
    overflow: "visible",
    pointerEvents: "none",
    zIndex: 5,
  },
  column: {
    minWidth: 100,
    display: "flex",
    flexDirection: "column",
    gap: 6,
  },
  // Active column hosts the expanded card — about half a portrait phone wide
  // so the next column still peeks in beside it.
  columnActive: { minWidth: 200, width: 200 },
  columnHeader: {
    background: "transparent",
    border: "none",
    borderBottom: "1px solid #1e293b",
    borderRadius: 0,
    color: "#64748b",
    cursor: "pointer",
    fontSize: 13,
    fontWeight: 600,
    padding: "2px 0 4px",
    textAlign: "left",
    whiteSpace: "nowrap",
  },
  columnHeaderActive: { color: "#38bdf8", borderBottomColor: "#38bdf8" },
  columnNodes: {
    display: "flex",
    flexDirection: "column",
    gap: 4,
    maxHeight: "70vh",
    overflowY: "auto",
    // Pin overflowX so the browser doesn't auto-promote it to "auto" (which it
    // does when only overflowY is non-visible), giving every column a spurious
    // horizontal scrollbar.
    overflowX: "hidden",
  },
  // Narrow: shorter per-column scroll areas so the board above stays visible.
  columnNodesNarrow: { maxHeight: "45vh" },
  columnNodesActive: { overflow: "visible", maxHeight: "none" },
  node: {
    background: "#1e293b",
    border: "1px solid #334155",
    borderRadius: 6,
    color: "#e2e8f0",
    cursor: "pointer",
    display: "flex",
    flexDirection: "column",
    gap: 2,
    fontSize: 13,
    padding: "6px 10px",
    textAlign: "left",
  },
  nodeSelected: {
    background: "#0ea5e9",
    borderColor: "#0ea5e9",
    color: "#0f172a",
    fontWeight: 600,
  },
  nodeTopRow: {
    display: "flex",
    alignItems: "baseline",
    justifyContent: "space-between",
    gap: 10,
    whiteSpace: "nowrap",
  },
  nodeMove: { fontWeight: 600 },
  nodeScore: {
    fontSize: 12,
    fontVariantNumeric: "tabular-nums",
    color: "#94a3b8",
  },
  nodeScoreSelected: { color: "#0c4a6e" },
  nodeName: {
    maxWidth: 160,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    fontSize: 11,
    fontWeight: 400,
    opacity: 0.85,
  },

  // --- Expanded /openings-style card ---------------------------------------
  card: {
    position: "relative",
    background: "#0b3a52",
    border: "1px solid #0ea5e9",
    borderRadius: 8,
    // Tight horizontal padding so the board can grow nearly card-wide.
    padding: "10px 4px",
    display: "flex",
    flexDirection: "column",
    gap: 10,
  },
  cardTitle: {
    fontSize: 13,
    fontWeight: 700,
    color: "#e0f2fe",
    lineHeight: 1.25,
    minWidth: 0,
  },
  // Your Score and Engine Eval sit side by side below the title.
  cardStatRow: {
    display: "flex",
    gap: 10,
    alignItems: "flex-start",
  },
  cardStat: {
    display: "flex",
    flexDirection: "column",
    gap: 2,
    flex: 1,
  },
  cardStatLabel: {
    fontSize: 10,
    textTransform: "uppercase",
    letterSpacing: 0.5,
    color: "#7dd3fc",
  },
  cardScorePanel: {
    display: "flex",
    alignItems: "center",
    gap: 6,
  },
  cardEvalValue: {
    fontSize: 22,
    fontWeight: 700,
    fontVariantNumeric: "tabular-nums",
    color: "#f0f9ff",
    lineHeight: 1,
  },
  cardScoreValue: {
    fontSize: 22,
    fontWeight: 700,
    fontVariantNumeric: "tabular-nums",
    color: "#f0f9ff",
    lineHeight: 1,
  },
  cardGrade: {
    fontSize: 12,
    fontWeight: 700,
    color: "#0c4a6e",
    background: "#7dd3fc",
    borderRadius: 4,
    padding: "1px 8px",
  },
  cardMetrics: {
    display: "flex",
    justifyContent: "space-between",
    gap: 6,
    margin: 0,
  },
  cardMetric: {
    display: "flex",
    flexDirection: "column",
    gap: 1,
    flex: 1,
  },
  cardMetricLabel: {
    fontSize: 9,
    textTransform: "uppercase",
    letterSpacing: 0.4,
    color: "#7dd3fc",
  },
  cardMetricValue: {
    margin: 0,
    fontSize: 13,
    fontWeight: 600,
    fontVariantNumeric: "tabular-nums",
    color: "#e0f2fe",
  },
  cardDrillButton: {
    background: "#0ea5e9",
    border: "none",
    borderRadius: 6,
    color: "#04283a",
    cursor: "pointer",
    fontSize: 13,
    fontWeight: 700,
    padding: "8px 10px",
  },
};

export default TreePrototypePage;
