import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getOpeningTree,
  type OpeningPlayerColor,
  type TreeResponse,
} from "../utils/api";
import { buildTreeView, type TreeView } from "../openings/treeView";

/**
 * Narrow data-flow hook for the `/openings` tree page. Owns the request
 * lifecycle, a local response cache, the stale-request guard, and the
 * page-vs-append status split. Chess instances and DOM effects stay OUTSIDE any
 * store — there is deliberately no Zustand here.
 *
 * See g-tree-page-state's design for the full state machine. The two keys are
 * deliberately different: `requestKey` (lookup, drives whether we fetch) gives a
 * legacy `opening=<fen>` link a distinct key from root so it can never
 * short-circuit the backend FEN→line resolution, while `canonicalKey` (store)
 * lets backtracking to a prefix of a canonical line hit the cache.
 */

export type PageStatus = "loading" | "ready" | "error";
export type AppendStatus = "idle" | "loading" | "error";

export interface OpeningsTreeRoute {
  playerColor: OpeningPlayerColor;
  moves: string[];
  opening: string | null;
}

export interface UseOpeningsTreeResult {
  view: TreeView | null;
  pageStatus: PageStatus;
  appendStatus: AppendStatus;
  error: string | null;
  /** Canonical line for the CURRENT route, only while settled; else null. */
  canonicalLine: string[] | null;
  /** The rendered view is settled and canonical for the current route. */
  isSettled: boolean;
  /** Batch snapshot time of the displayed response; null => book-only. */
  batchComputedAt: string | null;
  retry: () => void;
}

/** Everything needed to build the view + describe the current request status. */
interface RenderState {
  /** The route this render state was computed for. `isSettled`/`canonicalLine`
   *  are only trusted while this matches the current route — see below. */
  routeKey: string;
  response: TreeResponse | null;
  selectionLine: string[];
  loadedThroughPly: number;
  isExactResponseLine: boolean;
  pageStatus: PageStatus;
  appendStatus: AppendStatus;
  error: string | null;
  canonicalLine: string[] | null;
  isSettled: boolean;
}

interface Displayed {
  response: TreeResponse;
  /** The full canonical line the response represents (its superset extent). */
  line: string[];
}

function makeRequestKey(
  color: OpeningPlayerColor,
  moves: string[],
  opening: string | null,
): string {
  const linePart = moves.length
    ? `m:${moves.join(" ")}`
    : `o:${opening ?? ""}`;
  return `${color}\n${linePart}`;
}

function makeCanonicalKey(
  color: OpeningPlayerColor,
  canonicalLine: string[],
): string {
  return `${color}\nm:${canonicalLine.join(" ")}`;
}

/** Identity of a route: a settled render for routeKey A must not be read as the
 *  canonical answer for route B (the render state lags the URL by one frame). */
function makeRouteKey(
  color: OpeningPlayerColor,
  movesKey: string,
  opening: string | null,
): string {
  return `${color}\n${movesKey}\n${opening ?? ""}`;
}

/** Length of the shared leading prefix of two UCI lines. */
function commonPrefix(left: string[], right: string[]): number {
  let i = 0;
  while (i < left.length && i < right.length && left[i] === right[i]) {
    i += 1;
  }
  return i;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function settledRender(
  routeKey: string,
  response: TreeResponse,
  line: string[],
  isExactResponseLine: boolean,
): RenderState {
  return {
    routeKey,
    response,
    selectionLine: line,
    loadedThroughPly: line.length,
    isExactResponseLine,
    pageStatus: "ready",
    appendStatus: "idle",
    error: null,
    canonicalLine: line,
    isSettled: true,
  };
}

export function useOpeningsTree(
  route: OpeningsTreeRoute,
): UseOpeningsTreeResult {
  const { playerColor, moves, opening } = route;
  const movesKey = JSON.stringify(moves);

  const cacheRef = useRef<Map<string, TreeResponse>>(new Map());
  const displayedRef = useRef<Displayed | null>(null);
  const versionRef = useRef(0);
  const [retryNonce, setRetryNonce] = useState(0);

  const currentRouteKey = makeRouteKey(playerColor, movesKey, opening);

  const [render, setRender] = useState<RenderState>(() => ({
    routeKey: currentRouteKey,
    response: null,
    selectionLine: moves,
    loadedThroughPly: 0,
    isExactResponseLine: false,
    pageStatus: "loading",
    appendStatus: "idle",
    error: null,
    canonicalLine: null,
    isSettled: false,
  }));

  const retry = useCallback(() => {
    setRetryNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    // Bump the version on EVERY route change so any in-flight resolution from a
    // prior run is dropped by the guard even when this run does not fetch. The
    // AbortController additionally cancels the network for the common case.
    versionRef.current += 1;
    const version = versionRef.current;
    const requestKey = makeRequestKey(playerColor, moves, opening);
    const routeKey = makeRouteKey(playerColor, movesKey, opening);

    // Step 1 — exact cache hit: render settled, no network.
    const cached = cacheRef.current.get(requestKey);
    if (cached) {
      displayedRef.current = { response: cached, line: cached.canonical_line };
      setRender(settledRender(routeKey, cached, cached.canonical_line, true));
      return;
    }

    const displayed = displayedRef.current;
    const requestedLine = moves;

    // Step 2 — prefix no-fetch path. All three guards are required:
    //   - opening == null: a legacy opening=<fen> (moves=[], a prefix of any
    //     line) must still fetch so the backend resolves the FEN→line.
    //   - same color: a color switch must re-hydrate color-specific metrics.
    //   - prefix: the displayed response is a superset of the requested line.
    if (
      opening == null &&
      displayed != null &&
      displayed.response.player_color === playerColor &&
      commonPrefix(displayed.line, requestedLine) === requestedLine.length
    ) {
      setRender({
        routeKey,
        response: displayed.response,
        selectionLine: requestedLine,
        loadedThroughPly: requestedLine.length,
        isExactResponseLine: false,
        pageStatus: "ready",
        appendStatus: "idle",
        error: null,
        // A prefix of a canonical line is itself canonical for THIS route —
        // expose the requested line, not the deeper displayed line, so the page
        // does not canonicalize the URL forward to the deeper line.
        canonicalLine: requestedLine,
        isSettled: true,
      });
      return;
    }

    // Step 3 — extend / sibling / divergent / color-switch: provisional view +
    // fetch. The provisional view renders displayed columns through the
    // divergence, expands the divergence node, and drops everything deeper; the
    // page draws the frontier placeholder.
    const controller = new AbortController();
    if (displayed) {
      const divergence = commonPrefix(displayed.line, requestedLine);
      setRender({
        routeKey,
        response: displayed.response,
        selectionLine: requestedLine.slice(0, divergence + 1),
        loadedThroughPly: divergence,
        isExactResponseLine: false,
        pageStatus: "ready",
        appendStatus: "loading",
        error: null,
        canonicalLine: null,
        isSettled: false,
      });
    } else {
      setRender({
        routeKey,
        response: null,
        selectionLine: requestedLine,
        loadedThroughPly: 0,
        isExactResponseLine: false,
        pageStatus: "loading",
        appendStatus: "idle",
        error: null,
        canonicalLine: null,
        isSettled: false,
      });
    }

    getOpeningTree({ playerColor, moves, opening }, { signal: controller.signal })
      .then((response) => {
        if (versionRef.current !== version) {
          return; // superseded by a newer route
        }
        // Step 4 — resolve: store under both keys and render settled. Using
        // canonical_line as the selection line renders a backend truncation of a
        // stale/invalid input line as the canonical line.
        cacheRef.current.set(
          makeCanonicalKey(playerColor, response.canonical_line),
          response,
        );
        cacheRef.current.set(requestKey, response);
        displayedRef.current = {
          response,
          line: response.canonical_line,
        };
        setRender(
          settledRender(routeKey, response, response.canonical_line, true),
        );
      })
      .catch((error: unknown) => {
        if (isAbortError(error) || versionRef.current !== version) {
          return;
        }
        // Step 5 — reject. No displayed columns → page error (full retry); else
        // the frontier placeholder becomes an inline error and the existing
        // columns are left untouched.
        const message =
          error instanceof Error ? error.message : "Failed to load openings";
        if (!displayedRef.current) {
          setRender({
            routeKey,
            response: null,
            selectionLine: requestedLine,
            loadedThroughPly: 0,
            isExactResponseLine: false,
            pageStatus: "error",
            appendStatus: "idle",
            error: message,
            canonicalLine: null,
            isSettled: false,
          });
        } else {
          setRender((prev) => ({
            ...prev,
            appendStatus: "error",
            error: message,
          }));
        }
      });

    return () => {
      controller.abort();
    };
    // `moves` is captured by content via movesKey; reading displayed/cache from
    // refs keeps them out of the deps so the effect runs only on route change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playerColor, movesKey, opening, retryNonce]);

  const view = useMemo(() => {
    if (!render.response) {
      return null;
    }
    return buildTreeView(
      render.response,
      {
        selectionLine: render.selectionLine,
        loadedThroughPly: render.loadedThroughPly,
        isExactResponseLine: render.isExactResponseLine,
      },
      playerColor,
    );
  }, [render, playerColor]);

  // The render state lags the URL by one frame on a route change (the effect
  // runs after render). Only trust the settled/canonical fields when they were
  // computed for the CURRENT route, so the page never canonicalizes the URL
  // using a previous route's line.
  const isCurrent = render.routeKey === currentRouteKey;

  return {
    view,
    pageStatus: render.pageStatus,
    appendStatus: render.appendStatus,
    error: render.error,
    canonicalLine: isCurrent ? render.canonicalLine : null,
    isSettled: isCurrent && render.isSettled,
    batchComputedAt: render.response?.batch_computed_at ?? null,
    retry,
  };
}
