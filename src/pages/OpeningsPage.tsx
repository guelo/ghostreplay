import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Chess } from "chess.js";
import type { Square } from "chess.js";
import { Chessboard, defaultPieces } from "react-chessboard";
import type { PieceDropHandlerArgs, SquareHandlerArgs } from "react-chessboard";
import AppNav from "../components/AppNav";
import OpeningTreeNodeCard from "../components/OpeningTreeNodeCard";
import OpeningsMetricsLegend from "../components/OpeningsMetricsLegend";
import { formatMoveLabel } from "../openings/format";
import {
  buildCanonicalReplacement,
  buildOpeningsSearchParams,
  parseOpeningsSearchParams,
} from "../openings/route";
import {
  connectorStyle,
  moveLabelAt,
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

/** Narrow a raw board coordinate to a chess.js Square (`a1`–`h8`). */
const isSquare = (value: string): value is Square => /^[a-h][1-8]$/.test(value);

/** One vertical column of tree node cards; the deepest selected node expands. */
function TreeColumnView({
  column,
  columnIndex,
  isNextAfterActive,
  showLoadingFooter,
  registerColumn,
  registerSelectedNode,
  onSelect,
  onStartDrill,
}: {
  column: DisplayColumn;
  columnIndex: number;
  // The column immediately right of the active one; widened (like the active
  // column) so its collapsed opening names are easier to read.
  isNextAfterActive: boolean;
  // An off-tree board move is loading: its card will settle INTO this (the
  // deepest/frontier) column, so the spinner lives here, below the cards —
  // never as a standalone appended column (g-42md).
  showLoadingFooter: boolean;
  registerColumn: (idx: number, el: HTMLElement | null) => void;
  registerSelectedNode: (idx: number, el: HTMLElement | null) => void;
  onSelect: (line: string[]) => void;
  onStartDrill: (p: {
    targetFen: string;
    line: string[];
    displayName: string | null;
    eco: string | null;
  }) => void;
}) {
  // The column hosting the expanded (deepest selected) card is the active one.
  const isActive = column.nodes.some((node) => node.isExpanded);

  // Header: the move chosen in this column, shown with its move number. "Start"
  // at the root, the selected move label for a chosen column, "—" for the
  // frontier column that has no selection yet.
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
      }${isNextAfterActive ? " openings-tree-column--next" : ""}`}
      data-testid="tree-column"
      data-line-index={column.lineIndex}
    >
      {/* Header sits OUTSIDE the scroller so the per-column scrollbar spans only
          the cards — the header's underline stays full column width. */}
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
              // Wire drill only for move cards (childFen present); the
              // synthesized root has childFen === null → no Start Drill button.
              onStartDrill={
                node.childFen != null
                  ? () =>
                      onStartDrill({
                        targetFen: node.childFen as string,
                        line: node.selectLine,
                        displayName: node.view.openingName,
                        eco: node.view.eco,
                      })
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
        {/* Off-tree move loading: the new card will appear in THIS column once the
            refetch settles, so the spinner sits below the existing cards rather
            than spawning a standalone column (g-42md). */}
        {showLoadingFooter && (
          <div
            className="openings-tree-column__loading-footer"
            aria-live="polite"
          >
            <span
              className="openings-tree-append__spinner"
              aria-hidden="true"
            />
            <p className="openings-tree-append__label">Loading…</p>
          </div>
        )}
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

  // --- Click-to-move state -------------------------------------------------
  // A clicked-and-selected source square plus the legal-move hint styles it
  // paints (matching the other boards). Drag-to-move never touches these; they
  // only drive the click path and the dots overlay.
  const [selectedSquare, setSelectedSquare] = useState<string | null>(null);
  const [optionSquares, setOptionSquares] = useState<
    Record<string, React.CSSProperties>
  >({});
  const clearMoveHints = useCallback(() => {
    setSelectedSquare(null);
    setOptionSquares({});
  }, []);

  const columns = view?.columns ?? null;
  const selectionLine = view?.selectionLine ?? [];
  const columnCount = columns?.length ?? 0;
  // An off-tree board move is loading when the deepest rendered column has a
  // move selected at its ply (it's in the line) that is NOT among its nodes —
  // i.e. the move isn't in the displayed (stale) response. Its card will settle
  // into THIS column, so the spinner goes inside it (below the cards) rather
  // than as a standalone appended column (g-42md). In-tree forward selection
  // (deepest column already shows the selected card) and color switch / empty
  // frontier (no move selected at the deepest ply) keep the append column.
  const lastColumn = columns?.[columns.length - 1] ?? null;
  const offTreeLoading =
    appendStatus === "loading" &&
    lastColumn?.kind === "moves" &&
    selectionLine[lastColumn.lineIndex] !== undefined &&
    !lastColumn.nodes.some((node) => node.isSelected);
  // The append placeholder registers as the column after the last real one, so
  // the connector loop draws the unconnected (dangling-arrow) stub from the
  // selected card out toward the column being fetched while it loads. The
  // off-tree case has no append column (the spinner lives inside the frontier
  // column), so it contributes no extra connector column.
  const showAppendColumn = appendStatus === "loading" && !offTreeLoading;
  const connectorColumnCount = columnCount + (showAppendColumn ? 1 : 0);
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
    columnCount: connectorColumnCount,
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

  // Vertical companion to the horizontal autoscroll: a selection made on the
  // board can land far down a long children column, so bring the deepest
  // selected card into view within its own column scroller. Touches only
  // scrollTop (the effect above owns scrollLeft) and no-ops when the card is
  // already visible, so it never fights a manual scroll between selections.
  useEffect(() => {
    const activeIdx = selectionLine.length;
    const scroller = columnElsRef.current.get(activeIdx);
    const card = selectedNodeElsRef.current.get(activeIdx);
    if (!scroller || !card) return;
    const sRect = scroller.getBoundingClientRect();
    const cRect = card.getBoundingClientRect();
    const pad = 12;
    if (cRect.top < sRect.top + pad) {
      scroller.scrollTop -= sRect.top + pad - cRect.top;
    } else if (cRect.bottom > sRect.bottom - pad) {
      scroller.scrollTop += cRect.bottom - (sRect.bottom - pad);
    }
    // Same triggers as the horizontal effect: a sibling switch changes the
    // selected node without changing columnCount, so selectionKey is needed.
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

  // Left arrow walks up the tree one ply (drop the deepest move), which steps
  // the board back one move since the FEN is replayed from the selection line.
  // No-op at the root, while typing in a field, or with a modifier held (so
  // Cmd/Alt+Left still triggers browser Back).
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "ArrowLeft") return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return;
      }
      if (selectionLine.length === 0) return;
      event.preventDefault();
      selectLine(selectionLine.slice(0, -1));
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
    // selectLine is re-created each render but closes over the live route, so it
    // only needs re-binding when the selection line itself changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectionLine]);

  // Perspective switch keeps the shared line; the hook refetches (color in key)
  // and the board orientation flips immediately, before the refetch resolves.
  const switchColor = (color: OpeningPlayerColor) => {
    if (color === playerColor) {
      return;
    }
    setSearchParams(buildOpeningsSearchParams({ playerColor: color, moves }));
  };

  const startDrill = (p: {
    targetFen: string;
    line: string[];
    displayName: string | null;
    eco: string | null;
  }) => {
    navigate("/play", { state: { drillSetup: { ...p, playerColor } } });
  };

  // Board → tree: accept any LEGAL move on the deepest (settled) position. An
  // in-tree frontier move selects its existing node; any other legal move
  // extends the line as a user-selected (third type) move the backend then
  // resolves and renders (g-obh5). Only an illegal move (resolveDrop → null)
  // is refused, so board and tree can never diverge on a legal move. Shared by
  // both entry points: drag-drop and click-to-move.
  const applyBoardMove = (from: string, to: string): boolean => {
    if (!view || !isSettled) {
      return false;
    }
    const uci = resolveDrop(view.board.fen, from, to);
    if (!uci) {
      return false; // illegal
    }
    const frontier = view.columns.find(
      (column) =>
        column.kind === "moves" &&
        column.lineIndex === view.selectionLine.length,
    );
    const target = frontier?.nodes.find(
      (node) => node.uci === uci && node.isNavigable,
    );
    // In-tree frontier move selects its node; otherwise extend the line. The
    // optimistic board state (piece stays because we return true) reconciles
    // with the refetched view.board.fen once the new line settles.
    selectLine(target ? target.selectLine : view.selectionLine.concat(uci));
    return true;
  };

  // Drag-drop entry point: an illegal drag (applyBoardMove → false) snaps back.
  const handlePieceDrop = ({
    sourceSquare,
    targetSquare,
  }: PieceDropHandlerArgs): boolean => {
    if (!targetSquare) {
      return false;
    }
    const moved = applyBoardMove(sourceSquare, targetSquare);
    if (moved) {
      clearMoveHints();
    }
    return moved;
  };

  // Paint legal-move hints for a clicked piece on the deepest position: a dot
  // for a quiet move, a red ring for a capture, plus a yellow tint on the
  // source. Mirrors AnalysisBoard.getMoveOptions / ChessGame. Returns whether
  // the square has any legal move (i.e. is worth selecting).
  const getMoveOptions = (square: string): boolean => {
    if (!view || !isSquare(square)) {
      return false;
    }
    let chess;
    let moves;
    try {
      chess = new Chess(view.board.fen);
      moves = chess.moves({ square, verbose: true });
    } catch {
      return false;
    }
    if (moves.length === 0) {
      return false;
    }
    const sourcePiece = chess.get(square);
    const newSquares: Record<string, React.CSSProperties> = {};
    for (const move of moves) {
      const target = chess.get(move.to);
      const isCapture =
        sourcePiece != null &&
        target != null &&
        target.color !== sourcePiece.color;
      newSquares[move.to] = {
        background: isCapture
          ? "rgba(255, 0, 0, 0.4)"
          : "radial-gradient(circle, rgba(0,0,0,.1) 25%, transparent 25%)",
        borderRadius: "50%",
      };
    }
    newSquares[square] = { background: "rgba(255, 255, 0, 0.4)" };
    setOptionSquares(newSquares);
    return true;
  };

  // Click-to-select / click-to-move on the openings board. With a square
  // already selected, click a destination to move (illegal → fall through to
  // re-selection); otherwise click an own-color piece with legal moves to
  // select it and show the hints.
  const handleSquareClick = ({ square }: SquareHandlerArgs) => {
    if (!view || !isSettled) {
      return;
    }
    if (selectedSquare) {
      const moved = applyBoardMove(selectedSquare, square);
      if (moved) {
        clearMoveHints();
        return;
      }
      // Illegal — fall through to (re)selection.
    }
    if (!isSquare(square)) {
      clearMoveHints();
      return;
    }
    let chess;
    try {
      chess = new Chess(view.board.fen);
    } catch {
      clearMoveHints();
      return;
    }
    const piece = chess.get(square);
    if (piece && piece.color === chess.turn() && getMoveOptions(square)) {
      setSelectedSquare(square);
      return;
    }
    clearMoveHints();
  };

  // Board context changed — position (tree click, left-arrow nav,
  // canonicalization, completed move) OR perspective (a color switch flips
  // orientation at the SAME root FEN) → drop any in-progress click selection so
  // stale dots never linger on the new/flipped board. playerColor is a dep
  // precisely because a White→Black switch at the root leaves board.fen
  // unchanged, so fen alone would miss it.
  const boardFen = view?.board.fen ?? null;
  useEffect(() => {
    clearMoveHints();
  }, [boardFen, playerColor, clearMoveHints]);

  const colorLabel = playerColor === "white" ? "White" : "Black";
  const lastMove = view?.board.lastMove ?? null;
  const lastMoveStyles: Record<string, React.CSSProperties> = lastMove
    ? {
        [lastMove.from]: LAST_MOVE_HIGHLIGHT,
        [lastMove.to]: LAST_MOVE_HIGHLIGHT,
      }
    : {};
  // Click-to-move hints (selected square + legal dots) layer over the last-move
  // highlight; an option square wins on overlap.
  const squareStyles = { ...lastMoveStyles, ...optionSquares };

  return (
    <main className="app-shell openings-page">
      <AppNav />

      <div className="constrained-content">
        <section className="openings-tree">
          <header className="openings-tree__header">
            <h1 className="openings-tree__title">Openings Tree</h1>
            <div className="openings-tree__controls-row">
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
              <OpeningsMetricsLegend />
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
          ) : pageStatus === "initializing" ? (
            <section
              className="openings-state openings-state--loading openings-state--initializing"
              aria-live="polite"
              aria-label="Setting up your opening tree"
            >
              <p className="openings-state__title">
                Setting up your {colorLabel.toLowerCase()} opening tree…
              </p>
              <p className="openings-state__body">
                This one-time setup analyzes your games to build the tree. It
                usually takes around half a minute — it will load automatically
                when it's ready.
              </p>
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
                      onSquareClick: handleSquareClick,
                      animationDurationInMs: 150,
                      squareStyles,
                    }}
                  />
                  {/* A move-triggered refetch is in flight, so the board is
                      locked (applyBoardMove no-ops until isSettled) until the
                      new column settles. Veil + spinner signal the wait and
                      swallow drags/clicks so they don't read as ignored. */}
                  {appendStatus === "loading" && (
                    <div
                      className="openings-tree-board__loading"
                      role="status"
                      aria-label="Loading next moves"
                    >
                      <span
                        className="openings-tree-append__spinner"
                        aria-hidden="true"
                      />
                    </div>
                  )}
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
                        {/* Selected (third type) arrowhead: its own color via a
                            class so currentColor inside the marker resolves to
                            the selected hue (the group-color trick can't reach
                            into a marker). */}
                        <marker
                          id="openings-tree-arrowhead--selected"
                          className="openings-tree-arrowhead-selected"
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
                        const style = connectorStyles[i] ?? {
                          width: 2,
                          variant: "default" as const,
                        };
                        // The third move type (board exploration) recolors its
                        // connector: a distinct <g> color (stroke + clamp tips
                        // inherit it) plus a dedicated arrowhead marker, since a
                        // marker's currentColor resolves against the marker, not
                        // the referencing path's group.
                        const isSelectedVariant = style.variant === "selected";
                        const groupClass = isSelectedVariant
                          ? "openings-tree-connector-group--selected"
                          : undefined;
                        const arrowMarker = isSelectedVariant
                          ? "url(#openings-tree-arrowhead--selected)"
                          : "url(#openings-tree-arrowhead)";
                        // When an endpoint's cell is scrolled out of its column,
                        // mark the clamped edge with a small triangle pointing
                        // toward the selection, and dash the line so an
                        // off-screen endpoint reads as "continues past the
                        // visible band."
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
                        // No next move selected yet: draw a short horizontal
                        // stub straight out of the source instead of an elbow
                        // aimed at the column midpoint, capped with a normal
                        // arrowhead so the dangling/frontier state reads as a
                        // direction-of-travel cue.
                        if (c.unconnected) {
                          const STUB = 16;
                          const ARROW = 9;
                          // End the line short of the stub tip so the arrowhead
                          // marker (refX=0) fills the gap and its point lands at
                          // the intended stub length within the 2rem gap.
                          const tx = c.x1 + STUB - (c.off ? 0 : ARROW);
                          const ty = c.y1;
                          const stub = clampTip(c.x1, c.y1, c.off);
                          return (
                            <g key={i} className={groupClass}>
                              <path
                                className="openings-tree-connector"
                                d={`M ${c.x1} ${c.y1} L ${tx} ${ty}`}
                                fill="none"
                                stroke="currentColor"
                                strokeWidth={style.width}
                                strokeDasharray={c.off ? "5 4" : undefined}
                                markerEnd={c.off ? undefined : arrowMarker}
                                opacity={c.off ? 0.5 : 0.9}
                              />
                              {stub && (
                                <path
                                  d={stub}
                                  fill="currentColor"
                                  opacity={0.8}
                                />
                              )}
                            </g>
                          );
                        }
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
                        const tip = clampTip(c.x1, c.y1, c.off);
                        const tip2 = clampTip(x2 - 7, c.y2, c.off2);
                        const clamped = c.off || c.off2;
                        return (
                          <g key={i} className={groupClass}>
                            <path
                              className="openings-tree-connector"
                              d={d}
                              fill="none"
                              stroke="currentColor"
                              strokeWidth={style.width}
                              strokeDasharray={clamped ? "5 4" : undefined}
                              markerEnd={arrowMarker}
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

                    {view.columns.map((column, index, cols) => (
                      <TreeColumnView
                        key={`${column.kind}-${column.lineIndex}`}
                        column={column}
                        columnIndex={index}
                        // Widen the column right of the active (expanded) one so
                        // its collapsed opening names read more easily.
                        isNextAfterActive={
                          index > 0 &&
                          (cols[index - 1]?.nodes.some(
                            (node) => node.isExpanded,
                          ) ??
                            false)
                        }
                        showLoadingFooter={
                          offTreeLoading && index === cols.length - 1
                        }
                        registerColumn={registerColumn}
                        registerSelectedNode={registerSelectedNode}
                        onSelect={selectLine}
                        onStartDrill={startDrill}
                      />
                    ))}

                    {showAppendColumn &&
                      (() => {
                        // The loading column will render the move at its ply in
                        // the requested line — head it now with that move's
                        // label (a real move on forward/deep nav, "—" when the
                        // click only extends one ply past what's loaded).
                        const appendLabel = moveLabelAt(
                          moves,
                          view.selectionLine.length,
                        );
                        return (
                          <div
                            className="openings-tree-column openings-tree-append openings-tree-append--loading"
                            aria-live="polite"
                          >
                            <span
                              className={`openings-tree-column__header${
                                appendLabel
                                  ? " openings-tree-column__header--active"
                                  : ""
                              }`}
                              aria-hidden="true"
                            >
                              {appendLabel ?? "—"}
                            </span>
                            <div
                              className="openings-tree-append__body"
                              ref={(el) =>
                                registerColumn(view.columns.length, el)
                              }
                            >
                              <span
                                className="openings-tree-append__spinner"
                                aria-hidden="true"
                              />
                              <p className="openings-tree-append__label">
                                Loading…
                              </p>
                            </div>
                          </div>
                        );
                      })()}

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
