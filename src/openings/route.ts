import type { OpeningPlayerColor } from "../utils/api";

/**
 * URL contract for the `/openings` tree page. The tree route's query
 * construction and parsing go through this module so components never hand-build
 * the string.
 *
 * The canonical (tree) form is `color=white|black` + repeated `move=<uci>`. The
 * legacy `opening=<normalized FEN>` deep-link entry is honored only when no
 * `move` is present and is rewritten to the resolved `move=` line on response.
 */
export type OpeningRoute = {
  playerColor: OpeningPlayerColor;
  /** Tree line: emits one repeated `move=<uci>` per entry. Source of truth. */
  moves?: string[];
  /** Legacy FEN deep-link entry: emits `opening=<fen>` when no `moves`. */
  opening?: string;
};

/**
 * Build the `/openings` query params from a route. Two mutually-exclusive modes,
 * checked in order:
 *   1. Tree mode (`moves`): repeated `move=<uci>`; `opening` ignored.
 *   2. Legacy FEN-entry mode (`opening`): `opening=<fen>` only.
 * `color` is always emitted first.
 */
export function buildOpeningsSearchParams(route: OpeningRoute): URLSearchParams {
  const params = new URLSearchParams();
  params.set("color", route.playerColor);

  if (route.moves?.length) {
    for (const move of route.moves) {
      params.append("move", move);
    }
    return params;
  }

  if (route.opening) {
    params.set("opening", route.opening);
  }

  return params;
}

export type ParsedOpeningsRoute = {
  /** Exact `white`/`black` choice; missing or invalid → unselected (`null`). */
  playerColor: OpeningPlayerColor | null;
  /** Repeated `move=` values in URL order; absent → `[]`. */
  moves: string[];
  /** Legacy FEN entry (`opening=`); meaningful only when `moves` is empty. */
  opening: string | null;
};

/**
 * Parse `/openings` query params into the tree-contract shape. Only exact
 * `white` and `black` color values are selected; missing or invalid values are
 * left unselected. Any legacy `path` param is intentionally ignored — the tree
 * contract drops it.
 */
export function parseOpeningsSearchParams(
  params: URLSearchParams,
): ParsedOpeningsRoute {
  const rawColor = params.get("color");

  return {
    playerColor:
      rawColor === "white" || rawColor === "black" ? rawColor : null,
    moves: params.getAll("move"),
    opening: params.get("opening"),
  };
}

/**
 * Compute the canonical replacement params when the tree API returns a
 * `canonical_line`, applying a "replace URL only if it differs" rule. Returns
 * `null` when the current URL is already canonical so the page can skip the
 * redundant history write.
 *
 * Comparison is on the full `.toString()`, so this also rewrites a legacy
 * `opening=<fen>` link to the resolved `move=` line, strips stale
 * `opening`/`path`/extra `move` params, reorders params, and drops unknown
 * params. Callers supply an explicitly valid `playerColor`; unselected routes
 * never reach settled tree canonicalization.
 */
export function buildCanonicalReplacement(
  currentParams: URLSearchParams,
  playerColor: OpeningPlayerColor,
  canonicalLine: string[],
): URLSearchParams | null {
  const canonical = buildOpeningsSearchParams({
    playerColor,
    moves: canonicalLine,
  });

  return canonical.toString() === currentParams.toString() ? null : canonical;
}
