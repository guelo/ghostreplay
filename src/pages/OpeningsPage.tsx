import { useEffect } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Chessboard } from "react-chessboard";
import type { PieceDropHandlerArgs } from "react-chessboard";
import AppNav from "../components/AppNav";
import OpeningTreeNodeCard from "../components/OpeningTreeNodeCard";
import {
  buildCanonicalReplacement,
  buildOpeningsSearchParams,
  parseOpeningsSearchParams,
} from "../openings/route";
import { resolveDrop, type DisplayColumn } from "../openings/treeView";
import { useOpeningsTree } from "../hooks/useOpeningsTree";
import type { OpeningPlayerColor } from "../utils/api";
import "../App.css";

const COLOR_OPTIONS: Array<{ label: string; value: OpeningPlayerColor }> = [
  { label: "White", value: "white" },
  { label: "Black", value: "black" },
];

const LAST_MOVE_HIGHLIGHT: React.CSSProperties = {
  background: "rgba(56, 189, 248, 0.35)",
};

/** One vertical column of tree node cards; the deepest selected node expands. */
function TreeColumnView({
  column,
  onSelect,
  onStartDrill,
}: {
  column: DisplayColumn;
  onSelect: (line: string[]) => void;
  onStartDrill: (openingKey: string) => void;
}) {
  return (
    <div
      className="openings-tree-column"
      data-testid="tree-column"
      data-line-index={column.lineIndex}
    >
      {column.nodes.map((node) => {
        if (node.isExpanded) {
          const drillKey = node.view.drillOpeningKey;
          return (
            <OpeningTreeNodeCard
              key={node.key}
              variant="expanded"
              node={node.view}
              onStartDrill={
                drillKey != null ? () => onStartDrill(drillKey) : undefined
              }
            />
          );
        }

        return (
          <OpeningTreeNodeCard
            key={node.key}
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
      })}
    </div>
  );
}

/**
 * `/openings` — chesstree.net-style horizontal move tree synced with a board.
 * The page parses the URL line, drives {@link useOpeningsTree}, renders the
 * board + columns of {@link OpeningTreeNodeCard}, and owns selection / board
 * drops / perspective / canonicalization / Start Drill / the five states.
 * Detailed viewport layout is a sibling bead (g-tree-layout).
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
  // of the shorter line each node computes.
  const selectLine = (newLine: string[]) => {
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
    <main className="app-shell">
      <AppNav />

      <div className="constrained-content">
        <section className="openings-tree">
          <header className="openings-tree__header">
            <h1 className="openings-tree__title">Opening Tree</h1>
            <div
              className="openings-color-picker"
              role="group"
              aria-label="Playing as"
            >
              <span className="openings-tree__color-label">Playing as:</span>
              {COLOR_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  className={`openings-color-picker__button${
                    playerColor === option.value
                      ? " openings-color-picker__button--active"
                      : ""
                  }`}
                  aria-pressed={playerColor === option.value}
                  onClick={() => switchColor(option.value)}
                >
                  {option.label}
                </button>
              ))}
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
                <div className="openings-tree-columns">
                  {Array.from({ length: 3 }).map((_, index) => (
                    <div
                      key={index}
                      className="openings-tree-column openings-tree-column--skeleton"
                      aria-hidden="true"
                    />
                  ))}
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

                <div
                  className="openings-tree-columns"
                  aria-label={`${colorLabel} opening tree`}
                >
                  {view.columns.map((column) => (
                    <TreeColumnView
                      key={`${column.kind}-${column.lineIndex}`}
                      column={column}
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
            </>
          )}
        </section>
      </div>
    </main>
  );
}

export default OpeningsPage;
