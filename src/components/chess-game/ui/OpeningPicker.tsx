import React, {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { Chess } from "chess.js";
import { Chessboard } from "react-chessboard";
import type { PieceDropHandlerArgs } from "react-chessboard";
import type { OpeningRootItem } from "../../../utils/api";
import { normalize_fen } from "../../../utils/fen";

type OpeningFamily = { family_name: string; roots: OpeningRootItem[] };

type OpeningPickerProps = {
  openingFamilies: OpeningFamily[] | null;
  selectedOpening: OpeningRootItem | null;
  disabled?: boolean;
  isLoading?: boolean;
  onSelect: (opening: OpeningRootItem | null) => void;
};

type PickerMode = "list" | "board";

function rootLabel(root: OpeningRootItem): string {
  return `${root.eco ? `${root.eco} — ` : ""}${root.opening_name}`;
}

const OpeningPicker = ({
  openingFamilies,
  selectedOpening,
  disabled = false,
  isLoading = false,
  onSelect,
}: OpeningPickerProps) => {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<PickerMode>("list");
  const [query, setQuery] = useState("");
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [popoverStyle, setPopoverStyle] = useState<React.CSSProperties>({});

  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);

  // opening_key (4-field FEN) -> root, for board-position resolution.
  const index = useMemo(() => {
    const map = new Map<string, OpeningRootItem>();
    openingFamilies?.forEach((family) => {
      family.roots.forEach((root) => map.set(root.opening_key, root));
    });
    return map;
  }, [openingFamilies]);

  // Filtered families preserving API grouping/order.
  const filteredFamilies = useMemo(() => {
    if (!openingFamilies) return [];
    const q = query.trim().toLowerCase();
    if (!q) return openingFamilies;
    return openingFamilies
      .map((family) => ({
        ...family,
        roots: family.roots.filter(
          (r) =>
            r.opening_name.toLowerCase().includes(q) ||
            (r.eco ?? "").toLowerCase().includes(q),
        ),
      }))
      .filter((family) => family.roots.length > 0);
  }, [openingFamilies, query]);

  const flatRoots = useMemo(
    () => filteredFamilies.flatMap((f) => f.roots),
    [filteredFamilies],
  );

  // ---- Board mode state ----
  const chessRef = useRef(new Chess());
  const [boardFen, setBoardFen] = useState(
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  );
  const [moveStack, setMoveStack] = useState<string[]>([]);
  const [boardMatch, setBoardMatch] = useState<OpeningRootItem | null>(null);
  const [boardMessage, setBoardMessage] = useState<string | null>(null);

  const recomputeBoard = useCallback(() => {
    const chess = chessRef.current;
    setBoardFen(chess.fen());
    const key = normalize_fen(chess.fen());
    const root = index.get(key);
    if (root) {
      setBoardMatch(root);
      setBoardMessage(null);
      onSelect(root);
    } else if (chess.history().length > 0) {
      setBoardMessage("No named opening for this position");
    } else {
      setBoardMatch(null);
      setBoardMessage(null);
    }
  }, [index, onSelect]);

  const handleBoardDrop = useCallback(
    ({ sourceSquare, targetSquare }: PieceDropHandlerArgs) => {
      if (!targetSquare) return false;
      const chess = chessRef.current;
      try {
        const move = chess.move({
          from: sourceSquare,
          to: targetSquare,
          promotion: "q",
        });
        if (!move) return false;
        setMoveStack((prev) => [...prev, move.san]);
        recomputeBoard();
        return true;
      } catch {
        return false;
      }
    },
    [recomputeBoard],
  );

  const handleBoardUndo = useCallback(() => {
    const chess = chessRef.current;
    if (chess.history().length === 0) return;
    chess.undo();
    setMoveStack((prev) => prev.slice(0, -1));
    recomputeBoard();
  }, [recomputeBoard]);

  const handleBoardReset = useCallback(() => {
    chessRef.current.reset();
    setMoveStack([]);
    setBoardMatch(null);
    setBoardMessage(null);
    setBoardFen(chessRef.current.fen());
  }, []);

  // ---- Popover positioning ----
  const updatePosition = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const margin = 8;
    const desiredMax = 360;
    const spaceBelow = window.innerHeight - rect.bottom - margin;
    const spaceAbove = rect.top - margin;
    const placeAbove = spaceBelow < 240 && spaceAbove > spaceBelow;
    const maxHeight = Math.min(
      desiredMax,
      placeAbove ? spaceAbove : spaceBelow,
    );
    const width = Math.max(rect.width, 280);
    let left = rect.left;
    if (left + width > window.innerWidth - margin) {
      left = Math.max(margin, window.innerWidth - margin - width);
    }
    setPopoverStyle({
      position: "fixed",
      left,
      width,
      maxHeight,
      ...(placeAbove
        ? { bottom: window.innerHeight - rect.top + margin }
        : { top: rect.bottom + margin }),
    });
  }, []);

  useLayoutEffect(() => {
    if (!open) return;
    updatePosition();
  }, [open, mode, updatePosition]);

  useEffect(() => {
    if (!open) return;
    const handle = () => updatePosition();
    window.addEventListener("resize", handle);
    window.addEventListener("scroll", handle, true);
    return () => {
      window.removeEventListener("resize", handle);
      window.removeEventListener("scroll", handle, true);
    };
  }, [open, updatePosition]);

  // Outside-click + Escape close, focus restore.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        popoverRef.current?.contains(target) ||
        triggerRef.current?.contains(target)
      ) {
        return;
      }
      setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown, true);
    };
  }, [open]);

  // Focus the search input when the list opens.
  useEffect(() => {
    if (open && mode === "list") {
      searchRef.current?.focus();
    }
  }, [open, mode]);

  const openPopover = useCallback(() => {
    if (disabled || isLoading || openingFamilies === null) return;
    setActiveKey(selectedOpening?.opening_key ?? flatRoots[0]?.opening_key ?? null);
    setOpen(true);
  }, [disabled, isLoading, openingFamilies, selectedOpening, flatRoots]);

  const selectRoot = useCallback(
    (root: OpeningRootItem | null) => {
      onSelect(root);
      setOpen(false);
      triggerRef.current?.focus();
    },
    [onSelect],
  );

  // The active option must stay within the filtered list, otherwise Enter has
  // no target after the active row is filtered out. Derive it (rather than store
  // it) so narrowing the filter falls back to the first visible row.
  const resolvedActiveKey = flatRoots.some((r) => r.opening_key === activeKey)
    ? activeKey
    : (flatRoots[0]?.opening_key ?? null);

  const handleListKeyDown = (e: React.KeyboardEvent) => {
    if (flatRoots.length === 0) return;
    const idx = flatRoots.findIndex((r) => r.opening_key === resolvedActiveKey);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      const next = flatRoots[Math.min(flatRoots.length - 1, idx + 1)] ?? flatRoots[0];
      setActiveKey(next.opening_key);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      const prev = flatRoots[Math.max(0, idx - 1)] ?? flatRoots[0];
      setActiveKey(prev.opening_key);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const active =
        flatRoots.find((r) => r.opening_key === resolvedActiveKey) ?? flatRoots[0];
      if (active) selectRoot(active);
    }
  };

  // null families while not loading means the fetch failed.
  const loadFailed = !isLoading && openingFamilies === null;
  const triggerLabel = isLoading
    ? "Loading openings..."
    : loadFailed
      ? "Failed to load openings"
      : selectedOpening
        ? rootLabel(selectedOpening)
        : "Select opening";

  return (
    <div className="opening-picker">
      <button
        ref={triggerRef}
        type="button"
        className="opening-picker__trigger"
        role="combobox"
        aria-expanded={open}
        aria-controls="opening-picker-popover"
        aria-haspopup="listbox"
        disabled={disabled || isLoading || loadFailed}
        onClick={() => (open ? setOpen(false) : openPopover())}
      >
        <span className="opening-picker__trigger-label">{triggerLabel}</span>
        <span className="opening-picker__trigger-caret" aria-hidden="true">▾</span>
      </button>

      {open &&
        createPortal(
          <div
            ref={popoverRef}
            id="opening-picker-popover"
            className="opening-picker__popover"
            style={popoverStyle}
          >
            <div className="opening-picker__toggle" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={mode === "list"}
                className={`chess-button toggle${mode === "list" ? " active" : ""}`}
                onClick={() => setMode("list")}
              >
                List
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={mode === "board"}
                className={`chess-button toggle${mode === "board" ? " active" : ""}`}
                onClick={() => setMode("board")}
              >
                Board
              </button>
            </div>

            {mode === "list" ? (
              <>
                <input
                  ref={searchRef}
                  type="search"
                  className="opening-picker__search"
                  placeholder="Search openings..."
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={handleListKeyDown}
                />
                <div className="opening-picker__list" role="listbox">
                  {flatRoots.length === 0 ? (
                    <div className="opening-picker__empty">No openings found</div>
                  ) : (
                    filteredFamilies.map((family) => (
                      <div key={family.family_name} className="opening-picker__group">
                        <div className="opening-picker__group-label">
                          {family.family_name}
                        </div>
                        {family.roots.map((root) => {
                          const isSelected =
                            root.opening_key === selectedOpening?.opening_key;
                          const isActive = root.opening_key === resolvedActiveKey;
                          return (
                            <div
                              key={root.opening_key}
                              role="option"
                              aria-selected={isSelected}
                              className={`opening-picker__row${
                                isActive ? " opening-picker__row--active" : ""
                              }${isSelected ? " opening-picker__row--selected" : ""}`}
                              onMouseEnter={() => setActiveKey(root.opening_key)}
                              onClick={() => selectRoot(root)}
                            >
                              {rootLabel(root)}
                            </div>
                          );
                        })}
                      </div>
                    ))
                  )}
                </div>
              </>
            ) : (
              <div className="opening-picker__board-mode">
                <div className="opening-picker__board">
                  <Chessboard
                    options={{
                      position: boardFen,
                      onPieceDrop: handleBoardDrop,
                      allowDragging: true,
                    }}
                  />
                </div>
                <div className="opening-picker__board-controls">
                  <button
                    type="button"
                    className="chess-button"
                    onClick={handleBoardUndo}
                    disabled={moveStack.length === 0}
                  >
                    Undo
                  </button>
                  <button
                    type="button"
                    className="chess-button"
                    onClick={handleBoardReset}
                    disabled={moveStack.length === 0}
                  >
                    Reset
                  </button>
                </div>
                {boardMatch && (
                  <div className="opening-picker__match">{rootLabel(boardMatch)}</div>
                )}
                {boardMessage && (
                  <div className="opening-picker__match opening-picker__match--none">
                    {boardMessage}
                  </div>
                )}
                {moveStack.length > 0 && (
                  <div className="opening-picker__moves">{moveStack.join(" ")}</div>
                )}
              </div>
            )}
          </div>,
          document.body,
        )}
    </div>
  );
};

export default memo(OpeningPicker);
