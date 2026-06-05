import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { HistoryGame } from "../utils/api";

/** Locale-aware short date: en-US -> 06/05/26, en-GB -> 05/06/26. */
export function formatShortDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, {
    month: "2-digit",
    day: "2-digit",
    year: "2-digit",
  });
}

export function resultLabel(result: string | null): string {
  switch (result) {
    case "checkmate_win":
      return "Win";
    case "checkmate_loss":
      return "Loss";
    case "resign":
      return "Resigned";
    case "draw":
      return "Draw";
    default:
      return result ?? "Unknown";
  }
}

/** Map a game result to a win/loss/draw outcome class for coloring. */
export function resultClass(result: string | null): "win" | "loss" | "draw" {
  switch (result) {
    case "checkmate_win":
      return "win";
    case "checkmate_loss":
    case "resign":
      return "loss";
    default:
      return "draw";
  }
}

interface GameSelectorProps {
  games: HistoryGame[];
  selectedId: string | null;
  onChange: (sessionId: string) => void;
}

function rowLabelParts(game: HistoryGame) {
  return {
    result: resultLabel(game.result),
    outcome: resultClass(game.result),
    elo: game.engine_elo,
    opening: game.opening_name ?? "—",
    date: game.ended_at ? formatShortDate(game.ended_at) : "In progress",
  };
}

function GameSelector({ games, selectedId, onChange }: GameSelectorProps) {
  const [open, setOpen] = useState(false);
  // Index of the keyboard-highlighted ("active") option while the popup is
  // open. Separate from the committed selection — navigating does not change
  // the selection until Enter/Space/click confirms it.
  const [activeIndex, setActiveIndex] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const activeOptionRef = useRef<HTMLLIElement>(null);
  const listId = useId();

  const selected = games.find((g) => g.session_id === selectedId) ?? null;
  const selectedIndex = games.findIndex((g) => g.session_id === selectedId);

  const close = useCallback(() => setOpen(false), []);

  // When opening, start the active highlight on the current selection.
  const openPopup = useCallback(() => {
    setActiveIndex(selectedIndex === -1 ? 0 : selectedIndex);
    setOpen(true);
  }, [selectedIndex]);

  // Click-outside to close (mirrors AnalysisBoard engine-popup pattern).
  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (containerRef.current?.contains(target)) return;
      close();
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [open, close]);

  // Keep the keyboard-active option visible — focus stays on the trigger, so
  // the list won't auto-scroll without this.
  useEffect(() => {
    if (!open) return;
    // Guard for environments (e.g. jsdom) that don't implement scrollIntoView.
    activeOptionRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [open, activeIndex]);

  const select = useCallback(
    (sessionId: string) => {
      if (sessionId !== selectedId) onChange(sessionId);
      setOpen(false);
      triggerRef.current?.focus();
    },
    [onChange, selectedId],
  );

  const moveActive = useCallback(
    (delta: number) => {
      if (games.length === 0) return;
      setActiveIndex((prev) => Math.min(games.length - 1, Math.max(0, prev + delta)));
    },
    [games.length],
  );

  const handleKeyDown = (event: React.KeyboardEvent) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (!open) openPopup();
        else moveActive(1);
        break;
      case "ArrowUp":
        event.preventDefault();
        if (!open) openPopup();
        else moveActive(-1);
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        if (!open) {
          openPopup();
        } else if (games[activeIndex]) {
          select(games[activeIndex].session_id);
        }
        break;
      case "Escape":
        if (open) {
          event.preventDefault();
          close();
        }
        break;
    }
  };

  const renderRowContent = (game: HistoryGame) => {
    const parts = rowLabelParts(game);
    return (
      <>
        <span className="custom-dropdown__result">
          {parts.result} vs {parts.elo}
        </span>
        <span className="custom-dropdown__opening">{parts.opening}</span>
        <span className="custom-dropdown__date">{parts.date}</span>
      </>
    );
  };

  return (
    <div className="custom-dropdown" ref={containerRef}>
      <button
        ref={triggerRef}
        type="button"
        className="custom-dropdown__trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={
          open && games[activeIndex] ? `${listId}-opt-${activeIndex}` : undefined
        }
        onClick={() => (open ? close() : openPopup())}
        onKeyDown={handleKeyDown}
      >
        {selected ? (
          <span
            className={`custom-dropdown__row-inner custom-dropdown__row-inner--${resultClass(selected.result)}`}
          >
            {renderRowContent(selected)}
          </span>
        ) : (
          <span className="custom-dropdown__placeholder">Select a game</span>
        )}
        <span className="custom-dropdown__caret" aria-hidden="true">
          {"▾"}
        </span>
      </button>

      {open && (
        <ul className="custom-dropdown__list" id={listId} role="listbox" tabIndex={-1}>
          {games.map((game, index) => {
            const isSelected = game.session_id === selectedId;
            const isActive = index === activeIndex;
            return (
              <li
                key={game.session_id}
                ref={isActive ? activeOptionRef : undefined}
                id={`${listId}-opt-${index}`}
                role="option"
                aria-selected={isSelected}
                className={`custom-dropdown__option custom-dropdown__row-inner custom-dropdown__row-inner--${resultClass(game.result)}${
                  isSelected ? " custom-dropdown__option--selected" : ""
                }${isActive ? " custom-dropdown__option--active" : ""}`}
                onClick={() => select(game.session_id)}
                onMouseEnter={() => setActiveIndex(index)}
              >
                {renderRowContent(game)}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export default GameSelector;
