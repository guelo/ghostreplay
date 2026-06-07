import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { MoveListProps } from "./MoveList";
import { CLASSIFICATION_ICON, classificationClass } from "./MoveRow";
import ControlsRow from "./ControlsRow";
import MoveMessages from "./MoveMessages";

const EMPTY_MESSAGES: ReadonlyMap<number, import("./MoveRow").MoveMessage[]> = new Map();

type PopupPos = { left: number; top: number; placement: "below" | "above" };
const POPUP_WIDTH = 260;
const POPUP_MARGIN = 8;

const HorizontalMoveList = ({
  moves,
  currentIndex,
  onNavigate,
  canAddSelectedMove = false,
  isAddingSelectedMove = false,
  onAddSelectedMove,
  messages = EMPTY_MESSAGES,
  analyzingIndices,
  freshlyResolvedIndices,
  onFreshAnimationDone,
  onRevealSrsFail,
  revealedSrsFailIndex = null,
  onResign,
  isResignDisabled = false,
  onRevert,
  isRevertDisabled = false,
  onFlipBoard,
  onReset,
  isGameActive = false,
  isInteractionDisabled = false,
  suppressKeyboardNavigation = false,
  selectedVarNodeId,
  onVarSelect,
  variationTree,
  navigateUp,
  navigateDown,
}: MoveListProps) => {
  const stripRef = useRef<HTMLDivElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const selectedTokenRef = useRef<HTMLSpanElement | null>(null);
  const triggerRefs = useRef<Map<number, HTMLButtonElement>>(new Map());
  const [openPopupIndex, setOpenPopupIndex] = useState<number | null>(null);
  const [popupPos, setPopupPos] = useState<PopupPos | null>(null);

  const isVariationActive = !!(selectedVarNodeId && onVarSelect && navigateUp && navigateDown);

  const effectiveIndex = currentIndex ?? moves.length - 1;

  const canGoBack = isVariationActive ? true : moves.length > 0 && effectiveIndex > -1;
  const canGoForward = isVariationActive
    ? navigateDown!(selectedVarNodeId!) != null
    : moves.length > 0 && effectiveIndex < moves.length - 1;

  const closePopup = useCallback(() => setOpenPopupIndex(null), []);

  const handlePrev = useCallback(() => {
    if (isInteractionDisabled) return;
    closePopup();
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
    onNavigate(effectiveIndex - 1);
  }, [isInteractionDisabled, closePopup, isVariationActive, canGoBack, effectiveIndex, onNavigate, selectedVarNodeId, navigateUp, onVarSelect]);

  const handleNext = useCallback(() => {
    if (isInteractionDisabled) return;
    closePopup();
    if (isVariationActive) {
      const nextId = navigateDown!(selectedVarNodeId!);
      if (nextId) onVarSelect!(nextId);
      return;
    }
    if (!canGoForward) return;
    const newIndex = effectiveIndex + 1;
    onNavigate(newIndex >= moves.length - 1 ? null : newIndex);
  }, [isInteractionDisabled, closePopup, isVariationActive, canGoForward, effectiveIndex, moves.length, onNavigate, selectedVarNodeId, navigateDown, onVarSelect]);

  const handleMoveClick = useCallback(
    (index: number) => {
      if (isInteractionDisabled) return;
      closePopup();
      if (isVariationActive) onVarSelect!(null);
      onNavigate(index === moves.length - 1 ? null : index);
    },
    [isInteractionDisabled, closePopup, moves.length, onNavigate, isVariationActive, onVarSelect],
  );

  // Autoscroll the strip to the latest move only when a new move is appended
  // (not on navigation — back/forward must not yank the scroll position).
  const prevMovesLenRef = useRef(moves.length);
  useEffect(() => {
    if (moves.length > prevMovesLenRef.current) {
      const strip = stripRef.current;
      if (strip) strip.scrollLeft = strip.scrollWidth;
    }
    prevMovesLenRef.current = moves.length;
  }, [moves.length]);

  // Keyboard navigation (ArrowLeft / ArrowRight)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (suppressKeyboardNavigation) return;
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
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
  }, [handlePrev, handleNext, suppressKeyboardNavigation]);

  // Branch-off point in main line when a variation is active.
  const branchPointIndex =
    isVariationActive && variationTree
      ? variationTree.nodes.get(selectedVarNodeId!)?.parentGameIndex ?? -1
      : -1;

  // Keep the selected move (or variation branch-point) scrolled into view as
  // the user navigates.
  useEffect(() => {
    const strip = stripRef.current;
    const el = selectedTokenRef.current;
    if (!strip || !el) return;
    const stripRect = strip.getBoundingClientRect();
    const elRect = el.getBoundingClientRect();
    if (elRect.left < stripRect.left) {
      strip.scrollLeft -= stripRect.left - elRect.left + 8;
    } else if (elRect.right > stripRect.right) {
      strip.scrollLeft += elRect.right - stripRect.right + 8;
    }
  }, [effectiveIndex, selectedVarNodeId, branchPointIndex]);

  // Dismiss popup on any position-identity change (currentIndex OR variation
  // node). Adjust-state-during-render pattern avoids a cascading effect render.
  const positionKey = `${currentIndex}|${selectedVarNodeId ?? ""}`;
  const [prevPositionKey, setPrevPositionKey] = useState(positionKey);
  if (prevPositionKey !== positionKey) {
    setPrevPositionKey(positionKey);
    if (openPopupIndex !== null) setOpenPopupIndex(null);
  }

  const computePopupPos = useCallback((index: number): PopupPos | null => {
    const trigger = triggerRefs.current.get(index);
    if (!trigger) return null;
    const rect = trigger.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const left = Math.max(POPUP_MARGIN, Math.min(rect.left, vw - POPUP_WIDTH - POPUP_MARGIN));
    // Use the rendered popup's actual height when available (it may exceed the
    // estimate once multiple messages or revealed SRS-fail detail expand it),
    // falling back to a conservative estimate before the first measurement.
    const popupHeight = popupRef.current?.getBoundingClientRect().height || 120;
    const flipAbove = rect.bottom + popupHeight > vh && rect.top - popupHeight > POPUP_MARGIN;
    const top = flipAbove ? rect.top : rect.bottom;
    return { left, top, placement: flipAbove ? "above" : "below" };
  }, []);

  // Position the popup when opened, and keep it in sync with scroll/resize and
  // popup content size changes (e.g. revealing SRS-fail detail).
  useLayoutEffect(() => {
    if (openPopupIndex == null) return;
    const update = () => {
      const pos = computePopupPos(openPopupIndex);
      if (!pos) {
        setOpenPopupIndex(null);
        return;
      }
      setPopupPos(pos);
    };
    update();
    const strip = stripRef.current;
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    strip?.addEventListener("scroll", update);
    // Re-measure & reposition when the popup's own size changes.
    const ro =
      typeof ResizeObserver !== "undefined" && popupRef.current
        ? new ResizeObserver(() => update())
        : null;
    if (ro && popupRef.current) ro.observe(popupRef.current);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      strip?.removeEventListener("scroll", update);
      ro?.disconnect();
    };
  }, [openPopupIndex, computePopupPos]);

  // Outside-click dismissal.
  useEffect(() => {
    if (openPopupIndex == null) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if ((e.target as HTMLElement).closest?.(".h-move-popup")) return;
      if ((e.target as HTMLElement).closest?.(".h-move-msg")) return;
      void target;
      setOpenPopupIndex(null);
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [openPopupIndex]);

  const tokens = useMemo(() => {
    return moves.map((move, idx) => ({
      move,
      idx,
      number: idx % 2 === 0 ? idx / 2 + 1 : null,
    }));
  }, [moves]);

  const popupMsgs = openPopupIndex != null ? messages.get(openPopupIndex) : undefined;

  return (
    <div className="h-move-list">
      <ControlsRow
        onResign={onResign}
        isResignDisabled={isResignDisabled}
        onRevert={onRevert}
        isRevertDisabled={isRevertDisabled}
        onFlipBoard={onFlipBoard}
        onReset={onReset}
        isGameActive={isGameActive}
        isInteractionDisabled={isInteractionDisabled}
        canAddSelectedMove={canAddSelectedMove}
        isAddingSelectedMove={isAddingSelectedMove}
        onAddSelectedMove={onAddSelectedMove}
        effectiveIndex={effectiveIndex}
      />
      <div className="h-move-list__row">
        <button
          className="h-move-arrow"
          type="button"
          onClick={handlePrev}
          disabled={isInteractionDisabled || !canGoBack}
          title="Previous move (←)"
          aria-label="Previous move"
        >
          ‹
        </button>
        <div className="h-move-list__strip" ref={stripRef}>
          {moves.length === 0 ? (
            <span className="h-move-empty">No moves yet</span>
          ) : (
            tokens.map(({ move, idx, number }) => {
              const isSelected = !isVariationActive && idx === effectiveIndex;
              const isBranchPoint = isVariationActive && branchPointIndex >= 0 && idx === branchPointIndex;
              const iconInfo = move.classification ? CLASSIFICATION_ICON[move.classification] : undefined;
              const fresh = freshlyResolvedIndices?.has(idx) ?? false;
              const analyzing = analyzingIndices?.has(idx) ?? false;
              const msgs = messages.get(idx);
              return (
                <span
                  className="h-move-token"
                  key={idx}
                  ref={isSelected || isBranchPoint ? selectedTokenRef : null}
                >
                  {iconInfo && (
                    <span
                      className={`h-move-badge${number != null ? " h-move-badge--numbered" : ""} move-icon move-icon--${move.classification}${fresh ? " move-icon--pop" : ""}`}
                      title={iconInfo.title}
                      onAnimationEnd={fresh ? () => onFreshAnimationDone?.(idx) : undefined}
                      aria-hidden="true"
                    >
                      {iconInfo.icon}
                    </span>
                  )}
                  {number != null && <span className="h-move-num">{number}</span>}
                  <button
                    type="button"
                    className={`h-move ${classificationClass(move.classification)}${isSelected ? " selected" : ""}${isBranchPoint ? " branch-point" : ""}`}
                    disabled={isInteractionDisabled}
                    onClick={() => handleMoveClick(idx)}
                  >
                    <span className="h-move-san">{move.san}</span>
                    {analyzing && <span className="move-analyzing-spinner h-move-spinner" />}
                  </button>
                  {msgs && msgs.length > 0 && (
                    <button
                      type="button"
                      className="h-move-msg"
                      ref={(el) => {
                        if (el) triggerRefs.current.set(idx, el);
                        else triggerRefs.current.delete(idx);
                      }}
                      aria-label="Show move note"
                      onClick={() =>
                        setOpenPopupIndex((prev) => (prev === idx ? null : idx))
                      }
                    >
                      •
                    </button>
                  )}
                </span>
              );
            })
          )}
        </div>
        <button
          className="h-move-arrow"
          type="button"
          onClick={handleNext}
          disabled={isInteractionDisabled || !canGoForward}
          title="Next move (→)"
          aria-label="Next move"
        >
          ›
        </button>
      </div>
      {openPopupIndex != null &&
        popupMsgs &&
        popupMsgs.length > 0 &&
        createPortal(
          <div
            ref={popupRef}
            className={`h-move-popup${popupPos ? ` h-move-popup--${popupPos.placement}` : ""}`}
            style={{
              position: "fixed",
              left: popupPos?.left ?? 0,
              top:
                popupPos == null || popupPos.placement === "below"
                  ? popupPos?.top ?? 0
                  : undefined,
              bottom:
                popupPos && popupPos.placement === "above"
                  ? window.innerHeight - popupPos.top
                  : undefined,
              width: POPUP_WIDTH,
              // Hidden until measured & positioned to avoid a flash at (0,0).
              visibility: popupPos ? "visible" : "hidden",
            }}
            role="dialog"
          >
            <button
              type="button"
              className="h-move-popup__close"
              onClick={closePopup}
              aria-label="Close"
            >
              ×
            </button>
            <MoveMessages
              msgs={popupMsgs}
              moveIndex={openPopupIndex}
              revealedSrsFailIndex={revealedSrsFailIndex}
              isInteractionDisabled={isInteractionDisabled}
              onRevealSrsFail={onRevealSrsFail}
            />
          </div>,
          document.body,
        )}
    </div>
  );
};

export default React.memo(HorizontalMoveList);
