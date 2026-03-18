import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MoveClassification } from "../workers/analysisUtils";
import MoveRow, { formatEval } from "./MoveRow";
import type { MoveMessage, SrsFailDetail } from "./MoveRow";

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
};

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
}: MoveListProps) => {
  const moveListRef = useRef<HTMLDivElement>(null);
  const selectedMoveRef = useRef<HTMLButtonElement>(null);
  const lastMessageRef = useRef<HTMLDivElement>(null);
  const [tappedIconIndex, setTappedIconIndex] = useState<number | null>(null);

  // Effective index for display purposes (null means at the end)
  const effectiveIndex = currentIndex ?? moves.length - 1;

  const canGoBack = moves.length > 0 && effectiveIndex > -1;
  const canGoForward = moves.length > 0 && effectiveIndex < moves.length - 1;

  const handlePrev = useCallback(() => {
    if (!canGoBack) return;
    onNavigate(effectiveIndex - 1); // -1 is valid (starting position)
  }, [canGoBack, effectiveIndex, onNavigate]);

  const handleNext = useCallback(() => {
    if (!canGoForward) return;
    const newIndex = effectiveIndex + 1;
    // If we've reached the end, use null to indicate "live" position
    onNavigate(newIndex >= moves.length - 1 ? null : newIndex);
  }, [canGoForward, effectiveIndex, moves.length, onNavigate]);

  const handleMoveClick = useCallback(
    (index: number) => {
      setTappedIconIndex(null);
      // If clicking on the last move, set to null (live position)
      onNavigate(index === moves.length - 1 ? null : index);
    },
    [moves.length, onNavigate],
  );

  const handleIconTap = useCallback(
    (index: number) => {
      setTappedIconIndex((prev) => (prev === index ? null : index));
    },
    [],
  );

  const handleStartPosition = () => {
    if (moves.length > 0) {
      onNavigate(-1); // -1 indicates starting position (before any moves)
    }
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

  const isAtStart = effectiveIndex === -1;
  const isAtLatest = currentIndex === null;
  const showAddButton =
    Boolean(onAddSelectedMove) &&
    moves.length > 0 &&
    effectiveIndex >= 0 &&
    canAddSelectedMove;

  return (
    <div className="move-list-container">
      <div className="move-list-scroll" ref={moveListRef}>
        {moves.length === 0 ? (
          <p className="move-list-empty">No moves yet</p>
        ) : (
          <div className="move-list-grid">
            <span className="move-list-header move-list-header-eval">
              {effectiveIndex >= 0 && moves[effectiveIndex]?.eval != null
                ? formatEval(moves[effectiveIndex].eval!)
                : ""}
            </span>
            <span className="move-list-header">
              {playerColor === "white" ? "You" : "Engine"}
            </span>
            <span className="move-list-header">
              {playerColor === "black" ? "You" : "Engine"}
            </span>
            {movePairs.map((pair, pairIndex) => {
              const whiteIdx = pairIndex * 2;
              const blackIdx = pairIndex * 2 + 1;
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
                  key={pair.number}
                  pairNumber={pair.number}
                  white={pair.white}
                  black={pair.black}
                  whiteIdx={whiteIdx}
                  blackIdx={blackIdx}
                  prevWhiteEval={prevWhiteEval}
                  prevBlackEval={prevBlackEval}
                  isWhiteSelected={whiteIdx === effectiveIndex}
                  isBlackSelected={blackIdx === effectiveIndex}
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
          onClick={() => onNavigate(null)}
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
