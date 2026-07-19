import { useEffect, useMemo, useState } from "react";
import { getOpeningRoots, type OpeningLineageItem } from "../utils/api";
import {
  buildRootIndex,
  deriveLiveOpeningLineage,
  mergeServerLineage,
  type LiveOpeningRootIndex,
} from "../openings/deriveLiveLineage";

/**
 * Session-lifetime cache of the opening root registry (g-a5v3).
 *
 * The registry is large, immutable for the life of the app, and needed on the
 * very first move of every game — so it is fetched once and shared by every
 * mount rather than re-fetched per game. The in-flight PROMISE is cached (not
 * just the result) so concurrent mounts share a single request.
 *
 * A failure is not cached: the next mount retries. A failed registry only means
 * the live cards fall back to the server lineage (the pre-g-a5v3 behavior), so
 * it must never become permanently stuck.
 */
let rootIndexPromise: Promise<LiveOpeningRootIndex> | null = null;

function loadRootIndex(): Promise<LiveOpeningRootIndex> {
  if (!rootIndexPromise) {
    rootIndexPromise = getOpeningRoots()
      .then(buildRootIndex)
      .catch((err) => {
        rootIndexPromise = null; // allow a retry
        throw err;
      });
  }
  return rootIndexPromise;
}

/** Test seam: drop the cached registry so suites start from a clean slate. */
export function __resetOpeningRootIndexCache() {
  rootIndexPromise = null;
}

const EMPTY_INDEX: LiveOpeningRootIndex = new Map();

/** Minimal shape needed from a local move record. */
interface PlayedMove {
  san: string;
  fen: string;
}

/**
 * Ply of the FIRST move in a local move list, derived from the resulting
 * position's fullmove number + side to move.
 *
 * After White's move N the FEN reads (fullmove N, black to move) -> ply 2N-1.
 * After Black's move N it reads (fullmove N+1, white to move)   -> ply 2N.
 *
 * Deriving this locally (rather than defaulting to 1 until the server answers)
 * is what keeps a drill whose stored moves begin mid-game numbering correctly
 * on the very first render, instead of renumbering when the response lands.
 */
export function deriveStartPly(moveHistory: readonly PlayedMove[]): number {
  const first = moveHistory[0]?.fen;
  if (!first) return 1;
  const parts = first.split(" ");
  const turn = parts[1];
  const fullmove = Number(parts[5]);
  if (!Number.isFinite(fullmove) || fullmove < 1) return 1;
  return turn === "b" ? 2 * fullmove - 1 : 2 * fullmove - 2;
}

/**
 * The opening lineage to DISPLAY during live play.
 *
 * Derived from local move history so a card appears on the same tick as the
 * move that crossed its root — the server round-trip is not causally ordered
 * with the move (analysis must resolve before a move is uploadable, and uploads
 * flush on an interval), so gating display on it makes cards appear seconds
 * late or not until the next move (g-a5v3).
 *
 * The server lineage is merged in for SCORES only and can never remove a card.
 *
 * Falls back to the server lineage whenever the local derivation is unusable
 * (registry still loading or failed to load), so this is strictly an
 * improvement on the previous behavior rather than a new failure mode.
 */
export function useLiveOpeningLineage(
  moveHistory: readonly PlayedMove[],
  serverLineage: OpeningLineageItem[],
): { lineage: OpeningLineageItem[]; startPly: number } {
  const [roots, setRoots] = useState<LiveOpeningRootIndex>(EMPTY_INDEX);

  useEffect(() => {
    let cancelled = false;
    loadRootIndex()
      .then((index) => {
        if (!cancelled) setRoots(index);
      })
      .catch(() => {
        // Non-fatal: without the registry we simply show the server lineage.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const local = useMemo(
    () => (roots.size === 0 ? null : deriveLiveOpeningLineage(moveHistory, roots)),
    [moveHistory, roots],
  );

  const lineage = useMemo(
    () => (local === null ? serverLineage : mergeServerLineage(local, serverLineage)),
    [local, serverLineage],
  );

  const startPly = useMemo(() => deriveStartPly(moveHistory), [moveHistory]);

  return { lineage, startPly };
}
