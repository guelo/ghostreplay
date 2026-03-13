import React, { useCallback, useEffect, useRef, useState } from "react";
import type { MoveClassification } from "../workers/analysisUtils";
import { ANNOTATION_SYMBOL } from "../workers/analysisUtils";

type Move = {
  san: string;
  classification?: MoveClassification | null;
  eval?: number | null; // centipawns, white perspective
};

export type SrsFailDetail = {
  userMoveSan: string;
  bestMoveSan: string;
  userMoveUci: string;
  bestMoveUci: string;
};

export type SrsStats = {
  passCount: number;
  failCount: number;
  streak: number;
};

export type MoveMessage = {
  key: string;
  variant: "srs-pass" | "srs-fail";
  text: string;
  srsFailDetail?: SrsFailDetail;
  srsStats?: SrsStats;
};

const CLASSIFICATION_ICON: Partial<
  Record<MoveClassification, { icon: string; title: string }>
> = {
  best: { icon: "⭐", title: "Best move" },
  great: { icon: "!", title: "Great move" },
  good: { icon: "✓", title: "Good move" },
  inaccuracy: { icon: "?", title: "Inaccuracy" },
  blunder: { icon: "??", title: "Blunder" },
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

/** Format centipawns as compact string: "+1.2", "−3", "0" */
const formatEval = (cp: number): string => {
  const value = cp / 100;
  const abs = Math.abs(value);
  const num = abs % 1 === 0 ? abs.toFixed(0) : abs.toFixed(1);
  if (value === 0) return "0";
  return `${value > 0 ? "+" : "\u2212"}${num}`;
};

/** Format the full eval formula: prevEval ±delta = newEval.
 *  Color reflects advantage for the player (not the side that moved):
 *  green = good for player, red = bad for player. */
const formatEvalFormula = (
  prevCp: number,
  currentCp: number,
  playerColor: "white" | "black",
): React.ReactNode => {
  const deltaCp = currentCp - prevCp;
  const absDelta = Math.abs(deltaCp / 100);
  const deltaStr = absDelta % 1 === 0 ? absDelta.toFixed(0) : absDelta.toFixed(1);
  const sign = deltaCp >= 0 ? "+" : "\u2212";
  // Eval is from white's perspective, so positive delta is good for white player, bad for black player
  const goodForPlayer = playerColor === "white" ? deltaCp >= 0 : deltaCp <= 0;
  return (
    <>
      {formatEval(prevCp)}
      {" "}
      <span className={`eval-delta ${goodForPlayer ? "eval-delta--pos" : "eval-delta--neg"}`}>
        {sign}{deltaStr}
      </span>
      {" = "}
      {formatEval(currentCp)}
    </>
  );
};

const classificationClass = (c?: MoveClassification | null): string => {
  if (!c) return "";
  return `move-${c}`;
};

const EMPTY_MESSAGES: ReadonlyMap<number, MoveMessage[]> = new Map();

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

  const handleMoveClick = (index: number) => {
    setTappedIconIndex(null);
    // If clicking on the last move, set to null (live position)
    onNavigate(index === moves.length - 1 ? null : index);
  };

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
    if (selectedMoveRef.current && moveListRef.current) {
      selectedMoveRef.current.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      });
    }
  }, [effectiveIndex]);

  // Auto-scroll latest message into view
  useEffect(() => {
    if (lastMessageRef.current && moveListRef.current) {
      lastMessageRef.current.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      });
    }
  }, [messages]);

  // Group moves into pairs (white move, black move)
  const movePairs: { number: number; white: Move; black?: Move }[] = [];
  for (let i = 0; i < moves.length; i += 2) {
    movePairs.push({
      number: Math.floor(i / 2) + 1,
      white: moves[i],
      black: moves[i + 1],
    });
  }

  const isAtStart = effectiveIndex === -1;
  const isAtLatest = currentIndex === null;
  const showAddButton =
    Boolean(onAddSelectedMove) &&
    moves.length > 0 &&
    effectiveIndex >= 0 &&
    canAddSelectedMove;

  // Track the last message ref target for auto-scroll
  let lastBubbleMsgIndex = -1;
  for (const [idx] of messages) {
    if (idx > lastBubbleMsgIndex) {
      lastBubbleMsgIndex = idx;
    }
  }

  /** Get the messages for a move (rendered as full-width rows) */
  const getBubbleMessages = (index: number): MoveMessage[] => {
    return messages.get(index) ?? [];
  };

  const renderMoveCell = (
    move: Move,
    index: number,
    side: "white" | "black",
  ) => {
    const isSelected = index === effectiveIndex;
    const isAnalyzing = analyzingIndices?.has(index) ?? false;
    const annotation = move.classification
      ? ANNOTATION_SYMBOL[move.classification]
      : "";
    const colorClass = classificationClass(move.classification);
    const iconInfo = move.classification
      ? CLASSIFICATION_ICON[move.classification]
      : undefined;
    // Previous eval from white's perspective (first move uses 0 as baseline)
    const prevEval = index > 0 ? moves[index - 1].eval : 0;

    return (
      <button
        ref={isSelected ? selectedMoveRef : null}
        className={`move-button move-col-${side} ${colorClass} ${isSelected ? "selected" : ""}`}
        type="button"
        onClick={() => handleMoveClick(index)}
      >
        <span className="move-san">
          {iconInfo && (
            <span
              className={`move-icon move-icon--${move.classification}`}
              title={iconInfo.title}
              onClick={(e) => {
                e.stopPropagation();
                setTappedIconIndex(tappedIconIndex === index ? null : index);
              }}
            >
              {tappedIconIndex === index ? iconInfo.title : iconInfo.icon}
            </span>
          )}
          {annotation}
          {move.san}
          {isAnalyzing && <span className="move-analyzing-spinner" />}
        </span>
        <span className="move-eval">
          {move.eval != null && prevEval != null
            ? formatEvalFormula(prevEval, move.eval, playerColor)
            : ""}
        </span>
      </button>
    );
  };

  const renderBubbleMessages = (
    msgs: MoveMessage[],
    moveIndex: number,
    side: "white" | "black",
  ) => {
    const arrowClass = side === "black" ? "move-bubble--arrow-right" : "";
    return msgs.map((msg) => {
      const isLast = moveIndex === lastBubbleMsgIndex;

      if (msg.variant === "srs-fail" && msg.srsFailDetail) {
        const isRevealed = revealedSrsFailIndex === moveIndex;
        return (
          <div
            ref={isLast ? lastMessageRef : null}
            key={msg.key}
            className={`move-bubble move-bubble--srs-fail ${arrowClass}`}
          >
            <button
              type="button"
              className={`srs-fail-icon ${isRevealed ? "srs-fail-icon--revealed" : ""}`}
              onClick={() => {
                if (!isRevealed && onRevealSrsFail && msg.srsFailDetail) {
                  onRevealSrsFail(msg.srsFailDetail, moveIndex);
                }
              }}
              title="Click to see what you should have played"
            >
              <span className="srs-fail-icon__symbol">!</span>
            </button>
            <div className="srs-fail-body">
              <span className="srs-fail-body__label">{msg.text}</span>
              {msg.srsStats && (
                <span className="srs-stats">
                  pass/fail: {msg.srsStats.passCount}/{msg.srsStats.failCount} ·
                  streak {msg.srsStats.streak}
                </span>
              )}
              {isRevealed && msg.srsFailDetail && (
                <div className="srs-fail-body__detail">
                  <p>
                    You played:{" "}
                    <strong className="srs-fail-body__bad">
                      {msg.srsFailDetail.userMoveSan}
                    </strong>
                  </p>
                  <p>
                    Best was:{" "}
                    <span className="srs-fail-body__best">
                      {msg.srsFailDetail.bestMoveSan}
                    </span>
                  </p>
                </div>
              )}
            </div>
          </div>
        );
      }

      return (
        <div
          ref={isLast ? lastMessageRef : null}
          key={msg.key}
          className={`move-bubble move-bubble--${msg.variant} ${arrowClass}`}
        >
          <span>{msg.text}</span>
          {msg.srsStats && (
            <span className="srs-stats">
              pass/fail: {msg.srsStats.passCount}/{msg.srsStats.failCount}
              <br />
              streak {msg.srsStats.streak}
            </span>
          )}
        </div>
      );
    });
  };

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
              const whiteBubbles = getBubbleMessages(whiteIdx);
              const blackBubbles = pair.black
                ? getBubbleMessages(blackIdx)
                : [];

              // Case 1: White has bubble messages — split the row
              if (whiteBubbles.length > 0) {
                return (
                  <React.Fragment key={pair.number}>
                    {/* White move with "..." placeholder for black */}
                    <span className="move-number">{pair.number}</span>
                    {renderMoveCell(pair.white, whiteIdx, "white")}
                    <span className="move-button-placeholder move-placeholder-dots">
                      …
                    </span>
                    {renderBubbleMessages(whiteBubbles, whiteIdx, "white")}

                    {/* Black move shifted to its own row */}
                    {pair.black && (
                      <>
                        <span className="move-number" />
                        <span className="move-button-placeholder" />
                        {renderMoveCell(pair.black, blackIdx, "black")}
                        {blackBubbles.length > 0 &&
                          renderBubbleMessages(blackBubbles, blackIdx, "black")}
                      </>
                    )}
                  </React.Fragment>
                );
              }

              // Case 2: Only black has bubble messages (or no bubbles) — keep pair together
              return (
                <React.Fragment key={pair.number}>
                  <span className="move-number">{pair.number}</span>
                  {renderMoveCell(pair.white, whiteIdx, "white")}
                  {pair.black ? (
                    renderMoveCell(pair.black, blackIdx, "black")
                  ) : (
                    <span className="move-button-placeholder" />
                  )}
                  {blackBubbles.length > 0 &&
                    renderBubbleMessages(blackBubbles, blackIdx, "black")}
                </React.Fragment>
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

export default MoveList;
