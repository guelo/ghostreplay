import React, {
  memo,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { captureEvent } from "../../../analytics/posthog";
import OpeningsTreeExplorer, {
  type OpeningsTreeActionTarget,
} from "../../OpeningsTreeExplorer";
import type { OpeningsTreeRoute } from "../../../hooks/useOpeningsTree";
import type {
  OpeningPlayerColor,
  OpeningRootItem,
} from "../../../utils/api";

type OpeningFamily = { family_name: string; roots: OpeningRootItem[] };

export type OpeningPickerSelection = {
  opening: OpeningRootItem;
  line: string[] | null;
};

type OpeningPickerProps = {
  openingFamilies: OpeningFamily[] | null;
  selectedOpening: OpeningRootItem | null;
  selectedLine: string[] | null;
  playerColor: OpeningPlayerColor;
  disabled?: boolean;
  isLoading?: boolean;
  onSelect: (selection: OpeningPickerSelection) => void;
};

type PickerMode = "list" | "tree";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "area[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type=\"hidden\"])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "iframe",
  "object",
  "embed",
  "[contenteditable=\"true\"]",
  "[tabindex]:not([tabindex=\"-1\"])",
].join(", ");

function isVisibleFocusable(element: HTMLElement, dialog: HTMLElement): boolean {
  for (
    let current: HTMLElement | null = element;
    current && current !== dialog;
    current = current.parentElement
  ) {
    if (
      current.hidden ||
      current.getAttribute("aria-hidden") === "true" ||
      current.hasAttribute("inert")
    ) {
      return false;
    }
    const style = window.getComputedStyle(current);
    if (style.display === "none" || style.visibility === "hidden") {
      return false;
    }
  }
  return true;
}

function rootLabel(root: OpeningRootItem): string {
  return `${root.eco ? `${root.eco} — ` : ""}${root.opening_name}`;
}

function initialTreeRoute(
  playerColor: OpeningPlayerColor,
  selectedOpening: OpeningRootItem | null,
  selectedLine: string[] | null,
): OpeningsTreeRoute {
  if (selectedLine !== null) {
    return {
      playerColor,
      moves: [...selectedLine],
      opening: null,
    };
  }
  if (selectedOpening) {
    return {
      playerColor,
      moves: [],
      opening: selectedOpening.opening_key,
    };
  }
  return { playerColor, moves: [], opening: null };
}

/**
 * Tentative tree route. It mounts only while the modal's Tree panel is visible,
 * so closing or switching to List discards unconfirmed exploration.
 */
function OpeningPickerTree({
  playerColor,
  selectedOpening,
  selectedLine,
  onConfirm,
}: {
  playerColor: OpeningPlayerColor;
  selectedOpening: OpeningRootItem | null;
  selectedLine: string[] | null;
  onConfirm: (selection: OpeningPickerSelection) => void;
}) {
  const [route, setRoute] = useState<OpeningsTreeRoute>(() =>
    initialTreeRoute(playerColor, selectedOpening, selectedLine),
  );

  const selectLine = useCallback(
    (line: string[]) => {
      captureEvent("opening_explored", {
        source: "drill_picker",
        from_key: route.moves.join(","),
        to_key: line.join(","),
        depth: line.length,
        player_color: playerColor,
      });
      setRoute({ playerColor, moves: [...line], opening: null });
    },
    [playerColor, route.moves],
  );

  const adoptCanonicalLine = useCallback(
    (line: string[]) => {
      setRoute((current) => {
        const alreadyCanonical =
          current.opening === null &&
          current.moves.length === line.length &&
          current.moves.every((move, index) => move === line[index]);
        return alreadyCanonical
          ? current
          : { playerColor, moves: [...line], opening: null };
      });
    },
    [playerColor],
  );

  const confirm = useCallback(
    (target: OpeningsTreeActionTarget) => {
      const line = [...target.line];
      captureEvent("drill_opening_selected", {
        source: "tree",
        opening_key: target.targetFen,
        depth: line.length,
        player_color: playerColor,
      });
      onConfirm({
        opening: {
          opening_key: target.targetFen,
          opening_name: target.displayName ?? "Custom line",
          opening_family: "",
          eco: target.eco,
          depth: line.length,
        },
        line,
      });
    },
    [onConfirm, playerColor],
  );

  const expandedAction = useMemo(
    () => ({ label: "Use this opening", onSelect: confirm }),
    [confirm],
  );

  return (
    <OpeningsTreeExplorer
      route={route}
      onSelectLine={selectLine}
      onCanonicalLine={adoptCanonicalLine}
      expandedAction={expandedAction}
    />
  );
}

const OpeningPicker = ({
  openingFamilies,
  selectedOpening,
  selectedLine,
  playerColor,
  disabled = false,
  isLoading = false,
  onSelect,
}: OpeningPickerProps) => {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<PickerMode>("list");
  const [query, setQuery] = useState("");
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [popoverStyle, setPopoverStyle] = useState<React.CSSProperties>({});

  const id = useId().replace(/:/g, "");
  const listPanelId = `opening-picker-list-${id}`;
  const treePanelId = `opening-picker-tree-${id}`;
  const activePanelId = mode === "tree" ? treePanelId : listPanelId;

  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);

  // Filtered families preserving API grouping/order.
  const filteredFamilies = useMemo(() => {
    if (!openingFamilies) return [];
    const q = query.trim().toLowerCase();
    if (!q) return openingFamilies;
    return openingFamilies
      .map((family) => ({
        ...family,
        roots: family.roots.filter(
          (root) =>
            root.opening_name.toLowerCase().includes(q) ||
            (root.eco ?? "").toLowerCase().includes(q),
        ),
      }))
      .filter((family) => family.roots.length > 0);
  }, [openingFamilies, query]);

  const flatRoots = useMemo(
    () => filteredFamilies.flatMap((family) => family.roots),
    [filteredFamilies],
  );

  const closePicker = useCallback(() => {
    setOpen(false);
    setMode("list");
    triggerRef.current?.focus();
  }, []);

  // ---- Anchored List positioning ------------------------------------------
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
    if (!open || mode !== "list") return;
    updatePosition();
  }, [open, mode, updatePosition]);

  useEffect(() => {
    if (!open || mode !== "list") return;
    const handle = () => updatePosition();
    window.addEventListener("resize", handle);
    window.addEventListener("scroll", handle, true);
    return () => {
      window.removeEventListener("resize", handle);
      window.removeEventListener("scroll", handle, true);
    };
  }, [open, mode, updatePosition]);

  // List outside-click. Tree dismissal is owned by its full-viewport backdrop.
  useEffect(() => {
    if (!open || mode !== "list") return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (
        popoverRef.current?.contains(target) ||
        triggerRef.current?.contains(target)
      ) {
        return;
      }
      closePicker();
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [closePicker, mode, open]);

  // Capture-phase Escape keeps the parent start overlay open. Tree also traps
  // Tab/Shift+Tab within the dialog.
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        closePicker();
        return;
      }
      if (mode !== "tree" || event.key !== "Tab") return;

      const dialog = popoverRef.current;
      if (!dialog) return;
      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ).filter((element) => isVisibleFocusable(element, dialog));
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !dialog.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [closePicker, mode, open]);

  // Focus the active panel's first control.
  useEffect(() => {
    if (!open) return;
    if (mode === "list") searchRef.current?.focus();
    else closeRef.current?.focus();
  }, [open, mode]);

  // The near-fullscreen Tree layer owns the viewport while mounted.
  useEffect(() => {
    if (!open || mode !== "tree") return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [open, mode]);

  const openPicker = useCallback(() => {
    if (disabled || isLoading || openingFamilies === null) return;
    setActiveKey(selectedOpening?.opening_key ?? flatRoots[0]?.opening_key ?? null);
    setOpen(true);
  }, [disabled, flatRoots, isLoading, openingFamilies, selectedOpening]);

  const selectRoot = useCallback(
    (root: OpeningRootItem) => {
      captureEvent("drill_opening_selected", {
        source: "list",
        opening_key: root.opening_key,
        depth: root.depth,
        player_color: playerColor,
      });
      onSelect({ opening: root, line: null });
      closePicker();
    },
    [closePicker, onSelect, playerColor],
  );

  const confirmTreeSelection = useCallback(
    (selection: OpeningPickerSelection) => {
      onSelect(selection);
      closePicker();
    },
    [closePicker, onSelect],
  );

  // The active option must stay within the filtered list, otherwise Enter has
  // no target after the active row is filtered out.
  const resolvedActiveKey = flatRoots.some((root) => root.opening_key === activeKey)
    ? activeKey
    : (flatRoots[0]?.opening_key ?? null);

  const handleListKeyDown = (event: React.KeyboardEvent) => {
    if (flatRoots.length === 0) return;
    const index = flatRoots.findIndex(
      (root) => root.opening_key === resolvedActiveKey,
    );
    if (event.key === "ArrowDown") {
      event.preventDefault();
      const next =
        flatRoots[Math.min(flatRoots.length - 1, index + 1)] ?? flatRoots[0];
      setActiveKey(next.opening_key);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      const previous =
        flatRoots[Math.max(0, index - 1)] ?? flatRoots[0];
      setActiveKey(previous.opening_key);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const active =
        flatRoots.find((root) => root.opening_key === resolvedActiveKey) ??
        flatRoots[0];
      if (active) selectRoot(active);
    }
  };

  const loadFailed = !isLoading && openingFamilies === null;
  const triggerLabel = selectedOpening
    ? rootLabel(selectedOpening)
    : isLoading
      ? "Loading openings..."
      : loadFailed
        ? "Failed to load openings"
        : "Select opening";

  const toggle = (
    <div className="opening-picker__toggle segmented-toggle" role="tablist">
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
        aria-selected={mode === "tree"}
        className={`chess-button toggle${mode === "tree" ? " active" : ""}`}
        onClick={() => setMode("tree")}
      >
        Tree
      </button>
    </div>
  );

  return (
    <div className="opening-picker">
      <button
        ref={triggerRef}
        type="button"
        className="opening-picker__trigger"
        role="combobox"
        aria-expanded={open}
        aria-controls={activePanelId}
        aria-haspopup={mode === "tree" ? "dialog" : "listbox"}
        disabled={disabled || isLoading || loadFailed}
        onClick={() => (open ? closePicker() : openPicker())}
      >
        <span className="opening-picker__trigger-label">{triggerLabel}</span>
        <span className="opening-picker__trigger-caret" aria-hidden="true">
          ▾
        </span>
      </button>

      {open &&
        createPortal(
          mode === "list" ? (
            <div
              ref={popoverRef}
              id={listPanelId}
              className="opening-picker__popover opening-picker__popover--list"
              style={popoverStyle}
            >
              {toggle}
              <input
                ref={searchRef}
                type="search"
                className="opening-picker__search"
                placeholder="Search openings..."
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={handleListKeyDown}
              />
              <div className="opening-picker__list" role="listbox">
                {flatRoots.length === 0 ? (
                  <div className="opening-picker__empty">No openings found</div>
                ) : (
                  filteredFamilies.map((family) => (
                    <div
                      key={family.family_name}
                      className="opening-picker__group"
                    >
                      <div className="opening-picker__group-label">
                        {family.family_name}
                      </div>
                      {family.roots.map((root) => {
                        const isSelected =
                          root.opening_key === selectedOpening?.opening_key;
                        const isActive =
                          root.opening_key === resolvedActiveKey;
                        return (
                          <div
                            key={root.opening_key}
                            role="option"
                            aria-selected={isSelected}
                            className={`opening-picker__row${
                              isActive ? " opening-picker__row--active" : ""
                            }${
                              isSelected
                                ? " opening-picker__row--selected"
                                : ""
                            }`}
                            onMouseEnter={() =>
                              setActiveKey(root.opening_key)
                            }
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
            </div>
          ) : (
            <div
              className="opening-picker__tree-backdrop"
              onMouseDown={(event) => {
                if (event.target === event.currentTarget) closePicker();
              }}
            >
              <div
                ref={popoverRef}
                id={treePanelId}
                className="opening-picker__tree-modal"
                role="dialog"
                aria-modal="true"
                aria-label="Choose an opening from the tree"
                tabIndex={-1}
                onMouseDown={(event) => event.stopPropagation()}
              >
                <div className="opening-picker__tree-chrome">
                  {toggle}
                  <button
                    ref={closeRef}
                    type="button"
                    className="chess-button"
                    onClick={closePicker}
                  >
                    Close
                  </button>
                </div>
                <div className="opening-picker__tree-body">
                  <OpeningPickerTree
                    key={`${playerColor}\u0000${
                      selectedOpening?.opening_key ?? ""
                    }\u0000${
                      selectedLine === null
                        ? "<registered>"
                        : selectedLine.join("\u0000")
                    }`}
                    playerColor={playerColor}
                    selectedOpening={selectedOpening}
                    selectedLine={selectedLine}
                    onConfirm={confirmTreeSelection}
                  />
                </div>
              </div>
            </div>
          ),
          document.body,
        )}
    </div>
  );
};

export default memo(OpeningPicker);
