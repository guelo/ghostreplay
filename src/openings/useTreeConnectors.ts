import { useLayoutEffect, useState, type RefObject } from "react";

/**
 * Measured connector geometry for the `/openings` move tree. Ported from the
 * `TreePrototypePage` measurement loop, restricted to the **selected path** and
 * made reusable so `OpeningsPage` stays readable.
 *
 * This hook is the ONLY DOM-measuring code in the tree page. It returns **pure
 * geometry** — never style. Style (dashed/width) is applied at render time from
 * the model, so a same-line refetch or color switch that only changes edge
 * metadata (`in_book`/`is_observed`/`encounter_count`) re-renders correct
 * connectors WITHOUT a re-measure, and the measure key tracks only what moves
 * the lines (selection + column count), not what colors them.
 */

/**
 * A drawn elbow from the selected cell in one display column to the selected
 * cell in the next (or that column's vertical midpoint when it has no selection
 * yet). Coordinates are in canvas-local space (relative to the positioned
 * `.openings-tree-canvas`), so they survive both horizontal tree scroll and
 * per-column vertical scroll. `off`/`off2` are 0 when the origin/target cell is
 * visible, or ±1 when it has scrolled above (-1) / below (+1) its column's
 * viewport and the endpoint has been clamped to the edge.
 */
export interface Connector {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  off: -1 | 0 | 1;
  off2: -1 | 0 | 1;
}

export interface UseTreeConnectorsArgs {
  /** The positioned frame the SVG overlays and measurements are relative to. */
  canvasRef: RefObject<HTMLDivElement | null>;
  /** The overflow-x scroller wrapping the canvas (scroll events re-measure). */
  scrollRef: RefObject<HTMLDivElement | null>;
  /** Column root elements keyed by display-column index (held behind a ref so
   *  the page can mutate the Map without tripping react-hooks/immutability). */
  columnElsRef: RefObject<Map<number, HTMLElement>>;
  /** Selected-node wrapper elements keyed by display-column index. */
  selectedNodeElsRef: RefObject<Map<number, HTMLElement>>;
  /** Number of display columns; add/remove → re-measure. */
  columnCount: number;
  /** FULL selected line; a same-depth sibling switch re-aims the lines. */
  selectionKey: string;
}

export function useTreeConnectors({
  canvasRef,
  scrollRef,
  columnElsRef,
  selectedNodeElsRef,
  columnCount,
  selectionKey,
}: UseTreeConnectorsArgs): Connector[] {
  const [connectors, setConnectors] = useState<Connector[]>([]);

  useLayoutEffect(() => {
    let frame = 0;
    const measure = () => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const columnEls = columnElsRef.current;
      const selectedNodeEls = selectedNodeElsRef.current;
      const t = canvas.getBoundingClientRect();
      const next: Connector[] = [];
      // Each adjacent display-column pair (i, i+1): the selected node in column
      // i is the connector origin and the parent of column i+1. The registered
      // element is the column's `__nodes` scroller (the header is a separate,
      // non-scrolling sibling), so its rect doubles as the connector x-edge and
      // the y-clamp band — both derive from colR.
      for (let i = 0; i < columnCount - 1; i++) {
        const col = columnEls.get(i);
        const selCell = selectedNodeEls.get(i);
        const childCol = columnEls.get(i + 1);
        if (!col || !selCell || !childCol) continue;

        const colR = col.getBoundingClientRect();
        const cellR = selCell.getBoundingClientRect();
        const childR = childCol.getBoundingClientRect();

        // Origin x is the parent column's right edge (stable even when the cell
        // itself is scrolled out of view). Origin y is the cell's center,
        // clamped to the column's visible scroll band.
        const x1 = colR.right - t.left;
        const cellCenter = cellR.top + cellR.height / 2 - t.top;
        const bandTop = colR.top - t.top;
        const bandBottom = colR.bottom - t.top;
        let y1 = cellCenter;
        let off: Connector["off"] = 0;
        if (cellCenter < bandTop) {
          y1 = bandTop;
          off = -1;
        } else if (cellCenter > bandBottom) {
          y1 = bandBottom;
          off = 1;
        }

        // Target x is the child column's left edge. Aim y at the selected cell's
        // center in the child column (clamped to that column's band, same
        // technique as the origin). When the child column has no selection yet
        // (the freshly-revealed replies column), fall back to its midpoint.
        const x2 = childR.left - t.left;
        const childCell = selectedNodeEls.get(i + 1);
        let y2: number;
        let off2: Connector["off2"] = 0;
        if (childCell) {
          const childCellR = childCell.getBoundingClientRect();
          const childCellCenter =
            childCellR.top + childCellR.height / 2 - t.top;
          const childBandTop = childR.top - t.top;
          const childBandBottom = childR.bottom - t.top;
          y2 = childCellCenter;
          if (childCellCenter < childBandTop) {
            y2 = childBandTop;
            off2 = -1;
          } else if (childCellCenter > childBandBottom) {
            y2 = childBandBottom;
            off2 = 1;
          }
        } else {
          y2 = childR.top + childR.height / 2 - t.top;
        }
        next.push({ x1, y1, x2, y2, off, off2 });
      }
      setConnectors(next);
    };

    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    };

    measure();
    const scroller = scrollRef.current;
    window.addEventListener("resize", schedule);
    // capture: true so vertical scrolls inside individual columns also fire.
    scroller?.addEventListener("scroll", schedule, true);
    // The active column's expanded card embeds a board that can resize after
    // mount. jsdom lacks ResizeObserver, so guard before constructing it.
    let ro: ResizeObserver | undefined;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(schedule);
      if (canvasRef.current) ro.observe(canvasRef.current);
    }

    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", schedule);
      scroller?.removeEventListener("scroll", schedule, true);
      ro?.disconnect();
    };
    // The ref objects are stable; selectionKey + columnCount cover every change
    // that moves a line.
  }, [
    canvasRef,
    scrollRef,
    columnElsRef,
    selectedNodeElsRef,
    columnCount,
    selectionKey,
  ]);

  return connectors;
}
