import React, { type RefObject } from "react";
import type { MoveClassification } from "../workers/analysisUtils";

// ---------------------------------------------------------------------------
// Shared types (re-exported from MoveList.tsx too)
// ---------------------------------------------------------------------------

type Move = {
  san: string;
  classification?: MoveClassification | null;
  eval?: number | null; // centipawns, white perspective
  evalMate?: number | null; // mate-in-N, white perspective (positive = white mates)
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

// ---------------------------------------------------------------------------
// Helpers (moved from MoveList.tsx)
// ---------------------------------------------------------------------------

export const CLASSIFICATION_ICON: Partial<
  Record<MoveClassification, { icon: string; title: string }>
> = {
  best: { icon: "⭐", title: "Best move" },
  excellent: { icon: "!", title: "Excellent move" },
  good: { icon: "✓", title: "Good move" },
  inaccuracy: { icon: "?!", title: "Inaccuracy" },
  mistake: { icon: "?", title: "Mistake" },
  blunder: { icon: "??", title: "Blunder" },
};

/** Format centipawns as compact string: "+1.2", "−3", "0" */
export const formatEval = (cp: number): string => {
  const value = cp / 100;
  const abs = Math.abs(value);
  const num = abs % 1 === 0 ? abs.toFixed(0) : abs.toFixed(1);
  if (value === 0) return "0";
  return `${value > 0 ? "+" : "\u2212"}${num}`;
};

/** Format a white-perspective eval as a compact string, preferring mate codes.
 *  '#' for checkmate on the board (mate === 0), 'M{n}'/'-M{n}' for mate-in-N,
 *  otherwise the centipawn string. Returns "" when neither is available. */
export const formatWhiteEval = (
  cp: number | null | undefined,
  mate: number | null | undefined,
): string => {
  if (mate != null) {
    if (mate === 0) return "#";
    return mate > 0 ? `M${mate}` : `−M${Math.abs(mate)}`;
  }
  if (cp == null) return "";
  return formatEval(cp);
};

/** Format the full eval formula: prevEval ±delta = newEval.
 *  Color reflects advantage for the player (not the side that moved):
 *  green = good for player, red = bad for player.
 *  When either endpoint is a mate score, the signed delta is meaningless, so we
 *  drop it and render a 'prevEval → newEval' arrow instead. */
const formatEvalFormula = (
  prevCp: number | null | undefined,
  prevMate: number | null | undefined,
  currentCp: number | null | undefined,
  currentMate: number | null | undefined,
  playerColor: "white" | "black",
): React.ReactNode => {
  // Cross-mate-boundary (or mate↔mate): drop the delta, show an arrow.
  if (prevMate != null || currentMate != null) {
    return (
      <>
        {formatWhiteEval(prevCp, prevMate)}
        {" → "}
        {formatWhiteEval(currentCp, currentMate)}
      </>
    );
  }
  // Both endpoints are centipawn scores (guaranteed non-null by caller).
  const prevCpVal = prevCp!;
  const currentCpVal = currentCp!;
  const deltaCp = currentCpVal - prevCpVal;
  const absDelta = Math.abs(deltaCp / 100);
  const deltaStr = absDelta % 1 === 0 ? absDelta.toFixed(0) : absDelta.toFixed(1);
  const sign = deltaCp >= 0 ? "+" : "\u2212";
  const goodForPlayer = playerColor === "white" ? deltaCp >= 0 : deltaCp <= 0;
  return (
    <>
      {formatEval(prevCpVal)}
      {" "}
      <span className={`eval-delta ${goodForPlayer ? "eval-delta--pos" : "eval-delta--neg"}`}>
        {sign}{deltaStr}
      </span>
      {" = "}
      {formatEval(currentCpVal)}
    </>
  );
};

const classificationClass = (c?: MoveClassification | null): string => {
  if (!c) return "";
  return `move-${c}`;
};

// ---------------------------------------------------------------------------
// MoveRow props & component
// ---------------------------------------------------------------------------

export type MoveRowProps = {
  pairNumber: number;
  white: Move;
  black?: Move;
  whiteIdx: number;
  blackIdx: number;
  prevWhiteEval: number | null | undefined;
  prevBlackEval: number | null | undefined;
  prevWhiteEvalMate: number | null | undefined;
  prevBlackEvalMate: number | null | undefined;
  isWhiteSelected: boolean;
  isBlackSelected: boolean;
  whiteBubbles: MoveMessage[];
  blackBubbles: MoveMessage[];
  isLastBubbleRow: boolean;
  analyzingWhite: boolean;
  analyzingBlack: boolean;
  freshWhite: boolean;
  freshBlack: boolean;
  onFreshAnimationDone?: (index: number) => void;
  playerColor: "white" | "black";
  tappedIconIndex: number | null;
  revealedSrsFailIndex: number | null;
  isInteractionDisabled?: boolean;
  onMoveClick: (index: number) => void;
  onIconTap: (index: number) => void;
  onRevealSrsFail?: (detail: SrsFailDetail, moveIndex: number) => void;
  selectedMoveRef: RefObject<HTMLButtonElement | null>;
  lastMessageRef: RefObject<HTMLDivElement | null>;
  splitMode?: "white-only" | "black-only";
};

const MoveRowInner = ({
  pairNumber,
  white,
  black,
  whiteIdx,
  blackIdx,
  prevWhiteEval,
  prevBlackEval,
  prevWhiteEvalMate,
  prevBlackEvalMate,
  isWhiteSelected,
  isBlackSelected,
  whiteBubbles,
  blackBubbles,
  isLastBubbleRow,
  analyzingWhite,
  analyzingBlack,
  freshWhite,
  freshBlack,
  onFreshAnimationDone,
  playerColor,
  tappedIconIndex,
  revealedSrsFailIndex,
  isInteractionDisabled = false,
  onMoveClick,
  onIconTap,
  onRevealSrsFail,
  selectedMoveRef,
  lastMessageRef,
  splitMode,
}: MoveRowProps) => {
  const renderMoveCell = (
    move: Move,
    index: number,
    side: "white" | "black",
    isSelected: boolean,
    isAnalyzing: boolean,
    prevEval: number | null | undefined,
    prevEvalMate: number | null | undefined,
    fresh: boolean,
  ) => {
    const colorClass = classificationClass(move.classification);
    const iconInfo = move.classification
      ? CLASSIFICATION_ICON[move.classification]
      : undefined;

    const celebrateBest = fresh && move.classification === "best" && !!iconInfo;
    const popClass = fresh && iconInfo && !celebrateBest
      ? " move-icon--pop"
      : "";
    const buttonCelebrateClass = celebrateBest ? " move-button--celebrate-best" : "";
    const sanTextCelebrateClass = celebrateBest ? " move-san__text--celebrate-best" : "";
    const iconCelebrateClass = celebrateBest ? " move-icon--celebrate-best" : "";

    return (
      <button
        ref={isSelected ? selectedMoveRef : null}
        className={`move-button move-col-${side} ${colorClass}${buttonCelebrateClass} ${isSelected ? "selected" : ""}`}
        type="button"
        disabled={isInteractionDisabled}
        onClick={() => onMoveClick(index)}
      >
        {celebrateBest && (
          <span className="move-best-fx" aria-hidden="true">
            <span className="move-best-fx__plate" />
          </span>
        )}
        <span className="move-san">
          {iconInfo && (
            <span className={celebrateBest ? "move-icon-stage move-icon-stage--celebrate-best" : "move-icon-stage"}>
              {celebrateBest && (
                <>
                  <span className="move-icon-stage__burst" aria-hidden="true" />
                  <span className="move-icon-stage__ring" aria-hidden="true" />
                  <span className="move-icon-stage__spark move-icon-stage__spark--1" aria-hidden="true" />
                  <span className="move-icon-stage__spark move-icon-stage__spark--2" aria-hidden="true" />
                  <span className="move-icon-stage__spark move-icon-stage__spark--3" aria-hidden="true" />
                  <span
                    className="move-icon-stage__tail"
                    aria-hidden="true"
                    onAnimationEnd={(event) => {
                      if (event.target !== event.currentTarget) {
                        return;
                      }
                      onFreshAnimationDone?.(index);
                    }}
                  />
                </>
              )}
              <span
                className={`move-icon move-icon--${move.classification}${popClass}${iconCelebrateClass}`}
                title={iconInfo.title}
                onClick={(e) => {
                  e.stopPropagation();
                  if (isInteractionDisabled) {
                    return;
                  }
                  onIconTap(index);
                }}
                onAnimationEnd={
                  popClass
                    ? () => onFreshAnimationDone?.(index)
                    : undefined
                }
              >
                {tappedIconIndex === index ? iconInfo.title : iconInfo.icon}
              </span>
            </span>
          )}
          {celebrateBest && <span className="move-san__connector" aria-hidden="true" />}
          <span className={`move-san__text${sanTextCelebrateClass}`}>{move.san}</span>
          {isAnalyzing && <span className="move-analyzing-spinner" />}
        </span>
        <span className="move-eval">
          {(move.eval != null || move.evalMate != null) &&
          (prevEval != null || prevEvalMate != null)
            ? formatEvalFormula(
                prevEval,
                prevEvalMate,
                move.eval,
                move.evalMate,
                playerColor,
              )
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
      const isRevealed = revealedSrsFailIndex === moveIndex;

      if (msg.variant === "srs-fail" && msg.srsFailDetail) {
        return (
          <div
            ref={isLastBubbleRow ? lastMessageRef : null}
            key={msg.key}
            className={`move-bubble move-bubble--srs-fail ${arrowClass}`}
          >
            <button
              type="button"
              className={`srs-fail-icon ${isRevealed ? "srs-fail-icon--revealed" : ""}`}
              disabled={isInteractionDisabled}
              onClick={() => {
                if (isInteractionDisabled) {
                  return;
                }
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
          ref={isLastBubbleRow ? lastMessageRef : null}
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

  // Case 0: Variation split — only render one side
  if (splitMode === "white-only") {
    return (
      <React.Fragment key={pairNumber}>
        <span className="move-number">{pairNumber}</span>
        {renderMoveCell(white, whiteIdx, "white", isWhiteSelected, analyzingWhite, prevWhiteEval, prevWhiteEvalMate, freshWhite)}
        <span className="move-button-placeholder move-placeholder-dots">…</span>
        {whiteBubbles.length > 0 && renderBubbleMessages(whiteBubbles, whiteIdx, "white")}
      </React.Fragment>
    );
  }
  if (splitMode === "black-only") {
    return (
      <React.Fragment key={pairNumber}>
        <span className="move-number" />
        <span className="move-button-placeholder" />
        {black ? (
          renderMoveCell(black, blackIdx, "black", isBlackSelected, analyzingBlack, prevBlackEval, prevBlackEvalMate, freshBlack)
        ) : (
          <span className="move-button-placeholder" />
        )}
        {blackBubbles.length > 0 &&
          renderBubbleMessages(blackBubbles, blackIdx, "black")}
      </React.Fragment>
    );
  }

  // Case 1: White has bubble messages — split the row
  if (whiteBubbles.length > 0) {
    return (
      <React.Fragment key={pairNumber}>
        <span className="move-number">{pairNumber}</span>
        {renderMoveCell(white, whiteIdx, "white", isWhiteSelected, analyzingWhite, prevWhiteEval, prevWhiteEvalMate, freshWhite)}
        <span className="move-button-placeholder move-placeholder-dots">
          …
        </span>
        {renderBubbleMessages(whiteBubbles, whiteIdx, "white")}

        {black && (
          <>
            <span className="move-number" />
            <span className="move-button-placeholder" />
            {renderMoveCell(black, blackIdx, "black", isBlackSelected, analyzingBlack, prevBlackEval, prevBlackEvalMate, freshBlack)}
            {blackBubbles.length > 0 &&
              renderBubbleMessages(blackBubbles, blackIdx, "black")}
          </>
        )}
      </React.Fragment>
    );
  }

  // Case 2: Only black has bubble messages (or no bubbles) — keep pair together
  return (
    <React.Fragment key={pairNumber}>
      <span className="move-number">{pairNumber}</span>
      {renderMoveCell(white, whiteIdx, "white", isWhiteSelected, analyzingWhite, prevWhiteEval, prevWhiteEvalMate, freshWhite)}
      {black ? (
        renderMoveCell(black, blackIdx, "black", isBlackSelected, analyzingBlack, prevBlackEval, prevBlackEvalMate, freshBlack)
      ) : (
        <span className="move-button-placeholder" />
      )}
      {blackBubbles.length > 0 &&
        renderBubbleMessages(blackBubbles, blackIdx, "black")}
    </React.Fragment>
  );
};

// ---------------------------------------------------------------------------
// Custom areEqual for React.memo
// ---------------------------------------------------------------------------

function areEqual(prev: MoveRowProps, next: MoveRowProps): boolean {
  // Split mode
  if (prev.splitMode !== next.splitMode) return false;

  // Referential checks on move objects (stable if Step 2a works)
  if (prev.white !== next.white) return false;
  if (prev.black !== next.black) return false;

  // Selection (decomposed to booleans — cheap)
  if (prev.isWhiteSelected !== next.isWhiteSelected) return false;
  if (prev.isBlackSelected !== next.isBlackSelected) return false;

  // Player color affects eval formula coloring
  if (prev.playerColor !== next.playerColor) return false;

  // Modal interaction blocking must propagate to mounted rows
  if (prev.isInteractionDisabled !== next.isInteractionDisabled) return false;

  // Bubble arrays (stable if Step 2b works)
  if (prev.whiteBubbles !== next.whiteBubbles) return false;
  if (prev.blackBubbles !== next.blackBubbles) return false;
  if (prev.isLastBubbleRow !== next.isLastBubbleRow) return false;

  // Analysis spinners
  if (prev.analyzingWhite !== next.analyzingWhite) return false;
  if (prev.analyzingBlack !== next.analyzingBlack) return false;

  // Fresh animation flags
  if (prev.freshWhite !== next.freshWhite) return false;
  if (prev.freshBlack !== next.freshBlack) return false;

  // Prev evals for formula
  if (prev.prevWhiteEval !== next.prevWhiteEval) return false;
  if (prev.prevBlackEval !== next.prevBlackEval) return false;
  if (prev.prevWhiteEvalMate !== next.prevWhiteEvalMate) return false;
  if (prev.prevBlackEvalMate !== next.prevBlackEvalMate) return false;

  // tappedIconIndex — only matters if it involves this row's indices
  const prevTapRelevant =
    prev.tappedIconIndex === prev.whiteIdx || prev.tappedIconIndex === prev.blackIdx;
  const nextTapRelevant =
    next.tappedIconIndex === next.whiteIdx || next.tappedIconIndex === next.blackIdx;
  if (prevTapRelevant || nextTapRelevant) {
    if (prev.tappedIconIndex !== next.tappedIconIndex) return false;
  }

  // revealedSrsFailIndex — only matters if it involves this row's indices
  const prevRevealRelevant =
    prev.revealedSrsFailIndex === prev.whiteIdx || prev.revealedSrsFailIndex === prev.blackIdx;
  const nextRevealRelevant =
    next.revealedSrsFailIndex === next.whiteIdx || next.revealedSrsFailIndex === next.blackIdx;
  if (prevRevealRelevant || nextRevealRelevant) {
    if (prev.revealedSrsFailIndex !== next.revealedSrsFailIndex) return false;
  }

  return true;
}

const MoveRow = React.memo(MoveRowInner, areEqual);
MoveRow.displayName = "MoveRow";

export default MoveRow;
