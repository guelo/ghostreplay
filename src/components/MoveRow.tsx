import React, { type RefObject } from "react";
import type { MoveClassification } from "../workers/analysisUtils";
import MoveMessages from "./MoveMessages";
import {
  CLASSIFICATION_ICON,
  classificationClass,
  formatWhiteEval,
} from "./MoveRow.helpers";

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
// MoveRow props & component
// ---------------------------------------------------------------------------

export type MoveRowProps = {
  pairNumber: number;
  white: Move;
  black?: Move;
  whiteIdx: number;
  blackIdx: number;
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
          {formatWhiteEval(move.eval, move.evalMate)}
        </span>
      </button>
    );
  };

  const renderBubbleMessages = (
    msgs: MoveMessage[],
    moveIndex: number,
    side: "white" | "black",
  ) => (
    <MoveMessages
      msgs={msgs}
      moveIndex={moveIndex}
      side={side}
      revealedSrsFailIndex={revealedSrsFailIndex}
      isInteractionDisabled={isInteractionDisabled}
      onRevealSrsFail={onRevealSrsFail}
      lastMessageRef={isLastBubbleRow ? lastMessageRef : undefined}
    />
  );

  // Case 0: Variation split — only render one side
  if (splitMode === "white-only") {
    return (
      <React.Fragment key={pairNumber}>
        <span className="move-number">{pairNumber}</span>
        {renderMoveCell(white, whiteIdx, "white", isWhiteSelected, analyzingWhite, freshWhite)}
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
          renderMoveCell(black, blackIdx, "black", isBlackSelected, analyzingBlack, freshBlack)
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
        {renderMoveCell(white, whiteIdx, "white", isWhiteSelected, analyzingWhite, freshWhite)}
        <span className="move-button-placeholder move-placeholder-dots">
          …
        </span>
        {renderBubbleMessages(whiteBubbles, whiteIdx, "white")}

        {black && (
          <>
            <span className="move-number" />
            <span className="move-button-placeholder" />
            {renderMoveCell(black, blackIdx, "black", isBlackSelected, analyzingBlack, freshBlack)}
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
      {renderMoveCell(white, whiteIdx, "white", isWhiteSelected, analyzingWhite, freshWhite)}
      {black ? (
        renderMoveCell(black, blackIdx, "black", isBlackSelected, analyzingBlack, freshBlack)
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
