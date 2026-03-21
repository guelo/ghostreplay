import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MoveClassification } from "../workers/analysisUtils";
import type { VariationTree, VariationNodeId } from "../types/variationTree";
import type { NavigateUpResult } from "../hooks/useVariationTree";
import MoveRow, { formatEval } from "./MoveRow";
import type { MoveMessage, SrsFailDetail } from "./MoveRow";
import VariationLine from "./VariationLine";

// Re-export types that other modules import from MoveList
export type { SrsFailDetail, SrsStats, MoveMessage } from "./MoveRow";

type Move = {
  san: string;
  classification?: MoveClassification | null;
  eval?: number | null; // centipawns, white perspective
};

type MoveListProps = {
  moves: Move[];
  currentIndex: number | null; // null means viewing latest position
  onNavigate: (index: number | null) => void;
  canAddSelectedMove?: boolean;
  isAddingSelectedMove?: boolean;
  onAddSelectedMove?: (index: number) => void;
  messages?: ReadonlyMap<number, MoveMessage[]>;
  analyzingIndices?: ReadonlySet<number>;
  playerColor?: "white" | "black";
  /** Called when user clicks the srs-fail animated icon to reveal arrows */
  onRevealSrsFail?: (detail: SrsFailDetail, moveIndex: number) => void;
  /** Move index of the currently revealed srs-fail (icon stops animating) */
  revealedSrsFailIndex?: number | null;
  // Variation props (all optional for ChessGame path compatibility)
  variationTree?: VariationTree;
  selectedVarNodeId?: VariationNodeId | null;
  onVarSelect?: (nodeId: VariationNodeId | null) => void;
  getAbsolutePly?: (nodeId: VariationNodeId) => number;
  navigateUp?: (nodeId: VariationNodeId) => NavigateUpResult | null;
  navigateDown?: (nodeId: VariationNodeId) => VariationNodeId | null;
  headerEvalOverride?: string | null;
};

type DisplayItem =
  | { type: "move-row"; pairIndex: number; splitMode?: "white-only" | "black-only" }
  | { type: "variation-line"; rootNodeId: VariationNodeId; parentGameIndex: number };

const EMPTY_MESSAGES: ReadonlyMap<number, MoveMessage[]> = new Map();
const EMPTY_BUBBLES: MoveMessage[] = [];

const MoveList = ({
  moves,
  currentIndex,
  onNavigate,
  canAddSelectedMove = false,
  isAddingSelectedMove = false,
  onAddSelectedMove,
  messages = EMPTY_MESSAGES,
  analyzingIndices,
  playerColor = "white",
  onRevealSrsFail,
  revealedSrsFailIndex = null,
  variationTree,
  selectedVarNodeId,
  onVarSelect,
  getAbsolutePly,
  navigateUp,
  navigateDown,
  headerEvalOverride,
}: MoveListProps) => {
  const moveListRef = useRef<HTMLDivElement>(null);
  const selectedMoveRef = useRef<HTMLButtonElement>(null);
  const lastMessageRef = useRef<HTMLDivElement>(null);
  const [tappedIconIndex, setTappedIconIndex] = useState<number | null>(null);

  // Variation mode is active only when the full prop set is present
  const isVariationActive = !!(selectedVarNodeId && onVarSelect && navigateUp && navigateDown);

  // Effective index for display purposes (null means at the end)
  const effectiveIndex = currentIndex ?? moves.length - 1;

  const canGoBack = isVariationActive
    ? true
    : moves.length > 0 && effectiveIndex > -1;
  const canGoForward = isVariationActive
    ? navigateDown!(selectedVarNodeId!) != null
    : moves.length > 0 && effectiveIndex < moves.length - 1;

  const handlePrev = useCallback(() => {
    if (isVariationActive) {
      const result = navigateUp!(selectedVarNodeId!);
      if (result?.type === "game") {
        onVarSelect!(null);
        onNavigate(result.moveIndex);
      } else if (result?.type === "variation") {
        onVarSelect!(result.nodeId);
      }
      return;
    }
    if (!canGoBack) return;
    onNavigate(effectiveIndex - 1); // -1 is valid (starting position)
  }, [isVariationActive, canGoBack, effectiveIndex, onNavigate, selectedVarNodeId, navigateUp, onVarSelect]);

  const handleNext = useCallback(() => {
    if (isVariationActive) {
      const nextId = navigateDown!(selectedVarNodeId!);
      if (nextId) onVarSelect!(nextId);
      return;
    }
    if (!canGoForward) return;
    const newIndex = effectiveIndex + 1;
    // If we've reached the end, use null to indicate "live" position
    onNavigate(newIndex >= moves.length - 1 ? null : newIndex);
  }, [isVariationActive, canGoForward, effectiveIndex, moves.length, onNavigate, selectedVarNodeId, navigateDown, onVarSelect]);

  const handleMoveClick = useCallback(
    (index: number) => {
      setTappedIconIndex(null);
      // Clear variation selection when clicking a main-line move
      if (isVariationActive) {
        onVarSelect!(null);
      }
      // If clicking on the last move, set to null (live position)
      onNavigate(index === moves.length - 1 ? null : index);
    },
    [moves.length, onNavigate, isVariationActive, onVarSelect],
  );

  const handleVarNodeClick = useCallback(
    (nodeId: VariationNodeId) => {
      onVarSelect?.(nodeId);
    },
    [onVarSelect],
  );

  const handleIconTap = useCallback(
    (index: number) => {
      setTappedIconIndex((prev) => (prev === index ? null : index));
    },
    [],
  );

  const handleStartPosition = () => {
    if (isVariationActive) {
      onVarSelect!(null);
    }
    if (moves.length > 0) {
      onNavigate(-1); // -1 indicates starting position (before any moves)
    }
  };

  const handleLatest = () => {
    if (isVariationActive) {
      onVarSelect!(null);
    }
    onNavigate(null);
  };

  // Keyboard navigation
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Don't handle if user is typing in an input
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement
      ) {
        return;
      }

      if (e.key === "ArrowLeft") {
        e.preventDefault();
        handlePrev();
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        handleNext();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [handlePrev, handleNext]);

  // Auto-scroll to selected move
  useEffect(() => {
    if (!selectedMoveRef.current || !moveListRef.current) return;
    const id = requestAnimationFrame(() => {
      selectedMoveRef.current?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      });
    });
    return () => cancelAnimationFrame(id);
  }, [effectiveIndex]);

  // Auto-scroll to selected variation ply
  useEffect(() => {
    if (!isVariationActive || !moveListRef.current) return;
    const el = moveListRef.current.querySelector(".variation-ply--selected");
    if (!el) return;
    const id = requestAnimationFrame(() => {
      el.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    return () => cancelAnimationFrame(id);
  }, [isVariationActive, selectedVarNodeId]);

  // Auto-scroll latest message into view.
  // Diff is computed inside the effect (after commit) to avoid mutating a ref
  // during render, which is unsafe under StrictMode double-render.
  const prevMessagesRef = useRef<ReadonlyMap<number, MoveMessage[]>>(EMPTY_MESSAGES);
  useEffect(() => {
    const prev = prevMessagesRef.current;
    let hasChange = false;
    for (const [idx, arr] of messages) {
      if (arr !== prev.get(idx)) {
        hasChange = true;
        break;
      }
    }
    prevMessagesRef.current = messages;
    if (!hasChange) return;
    if (!lastMessageRef.current || !moveListRef.current) return;
    const id = requestAnimationFrame(() => {
      lastMessageRef.current?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      });
    });
    return () => cancelAnimationFrame(id);
  }, [messages]);

  // Highest message-bearing move index — drives lastMessageRef attachment
  let lastBubbleMsgIndex = -1;
  for (const [idx] of messages) {
    if (idx > lastBubbleMsgIndex) lastBubbleMsgIndex = idx;
  }

  // Group moves into pairs (white move, black move)
  const movePairs = useMemo(() => {
    const pairs: { number: number; white: Move; black?: Move }[] = [];
    for (let i = 0; i < moves.length; i += 2) {
      pairs.push({
        number: Math.floor(i / 2) + 1,
        white: moves[i],
        black: moves[i + 1],
      });
    }
    return pairs;
  }, [moves]);

  // Can we render variation lines? Requires both tree and getAbsolutePly.
  const canRenderVariations = !!(variationTree && getAbsolutePly && variationTree.rootBranches.size > 0);

  // Build display rows: interleave move-row and variation-line items
  const displayRows = useMemo((): DisplayItem[] => {
    if (!canRenderVariations) {
      return movePairs.map((_, pairIndex) => ({ type: "move-row" as const, pairIndex }));
    }

    const items: DisplayItem[] = [];

    // Starting-position branches (parentGameIndex = -1)
    const startBranches = variationTree.rootBranches.get(-1);
    if (startBranches) {
      for (const rootNodeId of startBranches) {
        items.push({ type: "variation-line", rootNodeId, parentGameIndex: -1 });
      }
    }

    for (let pairIndex = 0; pairIndex < movePairs.length; pairIndex++) {
      const whiteIdx = pairIndex * 2;
      const blackIdx = pairIndex * 2 + 1;

      // Branches from white's position (first variation ply is black)
      const whiteBranches = variationTree.rootBranches.get(whiteIdx);
      if (whiteBranches && whiteBranches.length > 0) {
        // Split: white cell, then variations, then black cell (only if black exists)
        items.push({ type: "move-row", pairIndex, splitMode: "white-only" });
        for (const rootNodeId of whiteBranches) {
          items.push({ type: "variation-line", rootNodeId, parentGameIndex: whiteIdx });
        }
        if (movePairs[pairIndex].black) {
          items.push({ type: "move-row", pairIndex, splitMode: "black-only" });
        }
      } else {
        // No whiteIdx branches — emit full pair
        items.push({ type: "move-row", pairIndex });
      }

      // Branches from black's position (first variation ply is white)
      const blackBranches = variationTree.rootBranches.get(blackIdx);
      if (blackBranches) {
        for (const rootNodeId of blackBranches) {
          items.push({ type: "variation-line", rootNodeId, parentGameIndex: blackIdx });
        }
      }
    }

    return items;
  }, [movePairs, canRenderVariations, variationTree]);

  const isAtStart = effectiveIndex === -1 && !isVariationActive;
  const isAtLatest = currentIndex === null && !isVariationActive;
  const showAddButton =
    Boolean(onAddSelectedMove) &&
    moves.length > 0 &&
    effectiveIndex >= 0 &&
    canAddSelectedMove;

  // Header eval: use override when variation active, otherwise main-line eval
  const headerEval = isVariationActive
    ? (headerEvalOverride ?? "")
    : (effectiveIndex >= 0 && moves[effectiveIndex]?.eval != null
        ? formatEval(moves[effectiveIndex].eval!)
        : "");

  return (
    <div className="move-list-container">
      <div className="move-list-scroll" ref={moveListRef}>
        {moves.length === 0 && !canRenderVariations ? (
          <p className="move-list-empty">No moves yet</p>
        ) : (
          <div className="move-list-grid">
            <span className="move-list-header move-list-header-eval">
              {headerEval}
            </span>
            <span className="move-list-header">
              {playerColor === "white" ? "You" : "Engine"}
            </span>
            <span className="move-list-header">
              {playerColor === "black" ? "You" : "Engine"}
            </span>
            {displayRows.map((item, i) => {
              if (item.type === "variation-line") {
                return (
                  <VariationLine
                    key={`var-${item.rootNodeId}`}
                    rootNodeId={item.rootNodeId}
                    tree={variationTree!}
                    selectedNodeId={isVariationActive ? selectedVarNodeId! : null}
                    onNodeClick={handleVarNodeClick}
                    getAbsolutePly={getAbsolutePly!}
                    showPrefix={false}
                  />
                );
              }

              const pair = movePairs[item.pairIndex];
              const whiteIdx = item.pairIndex * 2;
              const blackIdx = item.pairIndex * 2 + 1;
              const whiteBubbles = messages.get(whiteIdx) ?? EMPTY_BUBBLES;
              const blackBubbles = (pair.black ? messages.get(blackIdx) : undefined) ?? EMPTY_BUBBLES;

              // Pre-compute previous evals to avoid cross-row dependency in MoveRow
              const prevWhiteEval = whiteIdx > 0 ? moves[whiteIdx - 1].eval : 0;
              const prevBlackEval = pair.black && whiteIdx >= 0 ? moves[whiteIdx].eval : undefined;

              // Is this row the target for message auto-scroll?
              const isLastBubbleRow =
                lastBubbleMsgIndex === whiteIdx ||
                lastBubbleMsgIndex === blackIdx;

              return (
                <MoveRow
                  key={item.splitMode ? `${pair.number}-${item.splitMode}` : pair.number}
                  pairNumber={pair.number}
                  splitMode={item.splitMode}
                  white={pair.white}
                  black={pair.black}
                  whiteIdx={whiteIdx}
                  blackIdx={blackIdx}
                  prevWhiteEval={prevWhiteEval}
                  prevBlackEval={prevBlackEval}
                  isWhiteSelected={!isVariationActive && whiteIdx === effectiveIndex}
                  isBlackSelected={!isVariationActive && blackIdx === effectiveIndex}
                  whiteBubbles={whiteBubbles}
                  blackBubbles={blackBubbles}
                  isLastBubbleRow={isLastBubbleRow}
                  analyzingWhite={analyzingIndices?.has(whiteIdx) ?? false}
                  analyzingBlack={analyzingIndices?.has(blackIdx) ?? false}
                  playerColor={playerColor}
                  tappedIconIndex={tappedIconIndex}
                  revealedSrsFailIndex={revealedSrsFailIndex}
                  onMoveClick={handleMoveClick}
                  onIconTap={handleIconTap}
                  onRevealSrsFail={onRevealSrsFail}
                  selectedMoveRef={selectedMoveRef}
                  lastMessageRef={lastMessageRef}
                />
              );
            })}
          </div>
        )}
      </div>

      {showAddButton ? (
        <button
          className="move-list-add-button"
          type="button"
          onClick={() => {
            if (onAddSelectedMove && effectiveIndex >= 0) {
              onAddSelectedMove(effectiveIndex);
            }
          }}
          disabled={isAddingSelectedMove}
          title="Add selected move to ghost library"
        >
          {isAddingSelectedMove ? "Adding…" : "Add to Ghost Library"}
        </button>
      ) : null}

      <div className="move-list-nav">
        <button
          className="move-nav-button"
          type="button"
          onClick={handleStartPosition}
          disabled={moves.length === 0 || isAtStart}
          title="Go to starting position"
        >
          ⟨⟨
        </button>
        <button
          className="move-nav-button"
          type="button"
          onClick={handlePrev}
          disabled={!canGoBack}
          title="Previous move (←)"
        >
          ⟨
        </button>
        <button
          className="move-nav-button"
          type="button"
          onClick={handleNext}
          disabled={!canGoForward}
          title="Next move (→)"
        >
          ⟩
        </button>
        <button
          className="move-nav-button"
          type="button"
          onClick={handleLatest}
          disabled={isAtLatest}
          title="Go to current position"
        >
          ⟩⟩
        </button>
      </div>
    </div>
  );
};

export default React.memo(MoveList);
