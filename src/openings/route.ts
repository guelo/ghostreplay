import type { OpeningPlayerColor } from "../utils/api";

/**
 * URL contract for the `/openings` page. The tree route's query construction
 * (and, once it lands, parsing) goes through this module so components never
 * hand-build the string. NOTE: the current OpeningsPage grid still parses
 * `color`/`opening`/`path` inline; it adopts the parse/canonical helpers here in
 * g-tree-page-state. Until then this module owns construction for all callers
 * and parsing for the new tree route only.
 *
 * The canonical (tree) form is `color=white|black` + repeated `move=<uci>`. The
 * legacy `opening=<normalized FEN>` deep-link entry is honored only when no
 * `move` is present and is rewritten to the resolved `move=` line on response.
 *
 * `openingKey` + `path` is the pre-tree named-grid form, retained so the old
 * OpeningsPage grid keeps working until it is replaced (g-tree-page-state) and
 * removed (g-tree-cleanup). See SPEC §13.5.
 */
export type OpeningRoute = {
  playerColor: OpeningPlayerColor;
  /** Tree line: emits one repeated `move=<uci>` per entry. Source of truth. */
  moves?: string[];
  /** Legacy FEN deep-link entry: emits `opening=<fen>` when no `moves`. */
  opening?: string;
  /** Legacy alias of `opening`; emitted by the old named-grid page. */
  openingKey?: string;
  /** Legacy named path; emitted only in the `openingKey` named-grid mode. */
  path?: string[];
};

/**
 * Build the `/openings` query params from a route. Three mutually-exclusive
 * modes, checked in order:
 *   1. Tree mode (`moves`): repeated `move=<uci>`; all legacy fields ignored.
 *   2. Legacy FEN-entry mode (`opening`): `opening=<fen>` only — never `path`.
 *   3. Legacy named-grid mode (`openingKey`): `opening=<key>` + repeated `path=`.
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
    return params;
  }

  if (route.openingKey) {
    params.set("opening", route.openingKey);
    for (const pathKey of route.path ?? []) {
      params.append("path", pathKey);
    }
  }

  return params;
}

export type ParsedOpeningsRoute = {
  /** Normalized: `black` preserved, missing/garbage → `white`. */
  playerColor: OpeningPlayerColor;
  /** Repeated `move=` values in URL order; absent → `[]`. */
  moves: string[];
  /** Legacy FEN entry (`opening=`); meaningful only when `moves` is empty. */
  opening: string | null;
};

/**
 * Parse `/openings` query params into the tree-contract shape. Mirrors the old
 * page's color rule (`black` preserved, everything else → `white`). Does not
 * surface the legacy `path` param — the tree contract drops it; the old grid
 * page keeps its own inline parse until g-tree-page-state.
 */
export function parseOpeningsSearchParams(
  params: URLSearchParams,
): ParsedOpeningsRoute {
  const rawColor = params.get("color");

  return {
    playerColor: rawColor === "black" ? "black" : "white",
    moves: params.getAll("move"),
    opening: params.get("opening"),
  };
}

/**
 * Compute the canonical replacement params when the tree API returns a
 * `canonical_line`, mirroring the old page's "replace URL if it differs"
 * behavior. Returns `null` when the current URL is already canonical so the
 * page can skip the redundant history write.
 *
 * Comparison is on the full `.toString()`, so this also rewrites a legacy
 * `opening=<fen>` link to the resolved `move=` line, strips stale
 * `opening`/`path`/extra `move` params, reorders params, drops unknown params,
 * and normalizes a bad `color` (because `playerColor` is already normalized by
 * `parseOpeningsSearchParams`).
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
