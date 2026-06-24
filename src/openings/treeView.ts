import { Chess } from "chess.js";
import type { OpeningTreeNodeView } from "../components/OpeningTreeNodeCard";
import type { TreeNode, TreeResponse } from "../utils/api";
import { formatMoveLabel } from "./format";

/**
 * Pure (no-React) transform layer for the `/openings` move tree. Turns a
 * white-relative {@link TreeResponse} plus a render instruction into the display
 * column model the page renders, and provides the board replay + drop resolver.
 *
 * The render instruction separates two depths that must never be conflated:
 *   - `loadedThroughPly` caps which response columns are *rendered* (deeper ones
 *     are dropped; the page draws a frontier placeholder beyond it).
 *   - `selectionLine` drives which node is *selected/expanded* per column.
 * The caller guarantees `selectionLine.length - 1 <= loadedThroughPly`, so the
 * expanded node always sits in a rendered column.
 *
 * Selection/expansion derive from `selectionLine`, NOT the response's baked
 * `is_selected`/`selected_uci`, so a longer cached/displayed response can be
 * clipped and re-rendered (back-nav with no refetch; provisional views).
 */

/** A move applied on the board, used for last-move highlight squares. */
export interface LastMove {
  from: string;
  to: string;
}

export interface BoardState {
  /** Full FEN of the deepest selected position. */
  fen: string;
  /** Squares of the move that reached it, or null at the root. */
  lastMove: LastMove | null;
}

export interface DisplayNode {
  /** Stable React key within its column. */
  key: string;
  view: OpeningTreeNodeView;
  /** UCI that produces this node; null for the synthesized root. */
  uci: string | null;
  /** Normalized FEN this node's move reaches — the drill target for a card
   *  drill. null for the synthesized root (which is never drillable). */
  childFen: string | null;
  /** On the selected path → compact highlight (or expanded when deepest). */
  isSelected: boolean;
  /** The single deepest selected node → renders the expanded card. */
  isExpanded: boolean;
  /** Drops only land on navigable nodes; the root is never a drop target. */
  isNavigable: boolean;
  /** Whether clicking the card selects it. The synthesized root is always
   *  selectable; an API node is selectable only when `is_navigable` — the
   *  backend includes display-only boundary moves you cannot navigate into,
   *  and selecting one would push a URL the backend immediately truncates. */
  isSelectable: boolean;
  /** The new selection line produced by selecting this node. */
  selectLine: string[];
  /** Reference opening-book edge (the edge identity is this child) → drives a
   *  dashed connector when in book but not yet played. */
  inBook: boolean;
  /** Edge traversed in the user's real sessions → solid connector. */
  isObserved: boolean;
  /** Times the user reached this edge → connector thickness. */
  encounterCount: number;
}

export interface ConnectorStyle {
  /** Stroke width; clamps log2(encounters) into [2, 6]. */
  width: number;
}

/**
 * Style for the connector aimed at the selected child of a column — the edge
 * identity is that child, so style derives from one node, not the whole column
 * (no measuring of siblings). `null` → neutral frontier pointer (base width).
 * Width grows with encounter count, clamped to [2, 6]. Dashing is NOT decided
 * here: it's a measured concern (an endpoint scrolled off-screen) applied at
 * render time, not a model property.
 */
export function connectorStyle(node: DisplayNode | null): ConnectorStyle {
  if (!node) {
    return { width: 2 };
  }
  const width = Math.max(2, Math.min(6, 2 + Math.log2(node.encounterCount + 1)));
  return { width };
}

export interface DisplayColumn {
  /** 'root' = synthesized start column; 'moves' = an API position column. */
  kind: "root" | "moves";
  /** For 'moves': the column ply (index into the line where its nodes are
   *  chosen). -1 for the root column. */
  lineIndex: number;
  nodes: DisplayNode[];
}

export interface TreeView {
  columns: DisplayColumn[];
  board: BoardState;
  /** The effective line this view was built for. */
  selectionLine: string[];
}

export interface BuildTreeViewOptions {
  selectionLine: string[];
  loadedThroughPly: number;
  isExactResponseLine: boolean;
}

/** Map a raw API node to a card view. Evals stay white-relative (the standard
 *  +white / −black convention) so the column sort and the displayed number read
 *  consistently regardless of which side's repertoire is shown. */
export function nodeToView(node: TreeNode): OpeningTreeNodeView {
  return {
    ply: node.ply,
    san: node.san,
    openingName: node.opening_name,
    eco: node.eco,
    inBook: node.in_book,
    score: node.opening_score,
    evalCp: node.eval_cp,
    evalMate: node.eval_mate,
    coverage: node.coverage,
    gameCount: node.game_count,
    confidence: node.confidence,
    isTerminal: node.terminal_reason != null,
    terminalReason: node.terminal_reason,
    drillOpeningKey: node.drill_opening_key,
  };
}

/**
 * Synthesize the start-position ("whole repertoire") card. Terminal/drill
 * fields are read from the response ONLY when it was actually fetched for the
 * root (`isExactResponseLine && k === 0`); a deeper response's selected fields
 * describe the deeper line and must not leak onto a clipped root. The root eval
 * is the line-independent start-position eval and is always usable.
 */
export function synthesizeRootView(
  response: TreeResponse,
  isExactResponseLine: boolean,
  k: number,
): OpeningTreeNodeView {
  const fetchedForRoot = isExactResponseLine && k === 0;
  return {
    ply: 0,
    san: null,
    openingName: null,
    eco: null,
    // The start position is conceptually always "in book"; also gated by the
    // null SAN, so the chip never shows on the root regardless.
    inBook: true,
    score: response.root_opening_score ?? null,
    evalCp: response.root_eval_cp,
    evalMate: response.root_eval_mate,
    coverage: response.root_coverage ?? null,
    gameCount: response.root_game_count ?? null,
    confidence: response.root_confidence ?? null,
    isTerminal: fetchedForRoot ? response.selected_is_terminal : false,
    terminalReason: fetchedForRoot ? response.selected_terminal_reason : null,
    drillOpeningKey: fetchedForRoot ? response.drill_opening_key : null,
  };
}

/**
 * Apply a UCI move to a board, returning the {from,to} actually played or null
 * if it is illegal. chess.js throws on illegal moves, hence the try/catch.
 */
function applyUci(chess: Chess, uci: string): LastMove | null {
  try {
    const move = chess.move({
      from: uci.slice(0, 2),
      to: uci.slice(2, 4),
      promotion: uci.slice(4) || undefined,
    });
    return move ? { from: move.from, to: move.to } : null;
  } catch {
    return null;
  }
}

/**
 * Replay a UCI line from the start position. Returns the resulting full FEN and
 * the last move's squares. The board always follows the EFFECTIVE line, never a
 * response's `selected_fen` (which may describe a deeper fetched line).
 */
export function replayLine(line: string[]): BoardState {
  const chess = new Chess();
  let lastMove: LastMove | null = null;
  for (const uci of line) {
    const played = applyUci(chess, uci);
    if (!played) {
      break;
    }
    lastMove = played;
  }
  return { fen: chess.fen(), lastMove };
}

/**
 * Header label ("1. e4" / "2… Nf6") for the move at `index` in a UCI line, or
 * null when the index is out of range or a move is illegal. Used to head the
 * loading placeholder column with the move it is about to render — the move the
 * settled column will be selected on — by replaying the line for that move's
 * SAN (the column model isn't built yet while the fetch is in flight).
 */
export function moveLabelAt(line: string[], index: number): string | null {
  if (index < 0 || index >= line.length) {
    return null;
  }
  const chess = new Chess();
  let san: string | null = null;
  for (let i = 0; i <= index; i++) {
    const played = applyUci(chess, line[i]);
    if (!played) {
      return null;
    }
    san = chess.history().at(-1) ?? null;
  }
  // ply is 1-based: line index 0 is ply 1 (White's first move).
  return formatMoveLabel(index + 1, san);
}

/**
 * Resolve a board drag (from→to) on the given position to its UCI, or null when
 * the move is illegal. Promotions are assumed to be queen (matching the board's
 * drag default); navigable underpromotions remain selectable via the tree node.
 */
export function resolveDrop(
  fen: string,
  from: string,
  to: string,
): string | null {
  const chess = new Chess(fen);
  try {
    const move = chess.move({ from, to, promotion: "q" });
    if (!move) {
      return null;
    }
    return from + to + (move.promotion ?? "");
  } catch {
    return null;
  }
}

/**
 * Build the display-column model for one render instruction.
 *
 * Display columns = `[rootColumn, ...apiColumns]`. The selected path is
 * highlighted across every column; the single deepest selected node renders
 * expanded (the synthesized root when `k === 0`).
 */
export function buildTreeView(
  response: TreeResponse,
  options: BuildTreeViewOptions,
): TreeView {
  const { selectionLine, loadedThroughPly, isExactResponseLine } = options;
  const k = selectionLine.length;

  const rootColumn: DisplayColumn = {
    kind: "root",
    lineIndex: -1,
    nodes: [
      {
        key: "root",
        view: synthesizeRootView(response, isExactResponseLine, k),
        uci: null,
        childFen: null,
        // The root is always on the selected path; expanded only at k === 0.
        isSelected: true,
        isExpanded: k === 0,
        isNavigable: false,
        isSelectable: true,
        selectLine: [],
        // The root is only ever a connector *parent*, never a styled child.
        inBook: false,
        isObserved: false,
        encounterCount: 0,
      },
    ],
  };

  const apiColumns: DisplayColumn[] = [];
  for (const column of response.columns) {
    if (column.ply > loadedThroughPly) {
      continue; // deeper than what is rendered — dropped
    }
    const lineIndex = column.ply;
    const selectedUci = selectionLine[lineIndex] ?? null;
    const nodes: DisplayNode[] = column.nodes.map((node) => {
      const isSelected = node.uci === selectedUci;
      return {
        key: node.uci,
        view: nodeToView(node),
        uci: node.uci,
        childFen: node.child_fen,
        isSelected,
        // Only the deepest selected node (in column k-1) expands.
        isExpanded: isSelected && lineIndex === k - 1,
        isNavigable: node.is_navigable,
        isSelectable: node.is_navigable,
        selectLine: selectionLine.slice(0, lineIndex).concat(node.uci),
        inBook: node.in_book,
        isObserved: node.is_observed,
        encounterCount: node.encounter_count,
      };
    });
    apiColumns.push({ kind: "moves", lineIndex, nodes });
  }
  // Backend returns columns in ply order; sort defensively so lineIndex math
  // (and the frontier-column lookup for drops) never depends on response order.
  apiColumns.sort((left, right) => left.lineIndex - right.lineIndex);

  return {
    columns: [rootColumn, ...apiColumns],
    board: replayLine(selectionLine),
    selectionLine,
  };
}
