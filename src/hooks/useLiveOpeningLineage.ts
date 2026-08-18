import { useEffect, useMemo, useRef, useState } from "react";
import type { OpeningLineageItem } from "../utils/api";
import { loadOpeningRootIndex } from "../openings/openingRootsLoader";
import {
  deriveLiveOpeningLineage,
  mergeServerLineageState,
  type LiveOpeningRootIndex,
  type PendingScoreOccurrence,
} from "../openings/deriveLiveLineage";

const EMPTY_INDEX: LiveOpeningRootIndex = new Map();
const EMPTY_PENDING_SCORE_INDICES: ReadonlySet<number> = new Set();
const EMPTY_PENDING_SCORE_OCCURRENCES: readonly PendingScoreOccurrence[] = [];
const EMPTY_EXPIRED_SCORE_KEYS: ReadonlySet<string> = new Set();

/** Local upload/lineage convergence gets the same finite UX treatment as the
 *  cold-cache poll: a failed upload must not leave a permanent spinner. */
export const LOCAL_SCORE_PENDING_TIMEOUT_MS = 30_000;

/** Minimal shape needed from a local move record. */
interface PlayedMove {
  san: string;
  fen: string;
}

interface ScopedPendingScoreOccurrence extends PendingScoreOccurrence {
  scopedKey: string;
}

function useBoundedPendingScoreIndices(
  occurrences: readonly PendingScoreOccurrence[],
  scopeKey: string | null,
): ReadonlySet<number> {
  const timersRef = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const [expiredKeys, setExpiredKeys] = useState<ReadonlySet<string>>(
    EMPTY_EXPIRED_SCORE_KEYS,
  );

  const scopedOccurrences = useMemo<ScopedPendingScoreOccurrence[]>(
    () =>
      occurrences.map((occurrence) => ({
        ...occurrence,
        scopedKey: JSON.stringify([scopeKey, occurrence.occurrenceKey]),
      })),
    [occurrences, scopeKey],
  );

  const currentKeys = useMemo(
    () =>
      new Set(scopedOccurrences.map((occurrence) => occurrence.scopedKey)),
    [scopedOccurrences],
  );

  // Expiration belongs to one uninterrupted appearance. Reset stale markers
  // during render so a reverted card (or the same crossing in a new session)
  // gets a fresh budget when it appears again. The guarded update settles in
  // one re-render and avoids a state-synchronizing effect.
  let effectiveExpiredKeys = expiredKeys;
  if ([...expiredKeys].some((key) => !currentKeys.has(key))) {
    const retained = new Set(
      [...expiredKeys].filter((key) => currentKeys.has(key)),
    );
    effectiveExpiredKeys =
      retained.size === 0 ? EMPTY_EXPIRED_SCORE_KEYS : retained;
    setExpiredKeys(effectiveExpiredKeys);
  }

  useEffect(() => {
    // A resolved, reverted, or prior-session occurrence no longer owns an
    // external timer; its expired marker was reconciled during render above.
    for (const [key, timer] of timersRef.current) {
      if (!currentKeys.has(key)) {
        clearTimeout(timer);
        timersRef.current.delete(key);
      }
    }

    for (const occurrence of scopedOccurrences) {
      if (
        timersRef.current.has(occurrence.scopedKey) ||
        effectiveExpiredKeys.has(occurrence.scopedKey)
      ) {
        continue;
      }
      const timer = setTimeout(() => {
        timersRef.current.delete(occurrence.scopedKey);
        setExpiredKeys((previous) => {
          if (previous.has(occurrence.scopedKey)) return previous;
          const next = new Set(previous);
          next.add(occurrence.scopedKey);
          return next;
        });
      }, LOCAL_SCORE_PENDING_TIMEOUT_MS);
      timersRef.current.set(occurrence.scopedKey, timer);
    }
  }, [currentKeys, effectiveExpiredKeys, scopedOccurrences]);

  useEffect(
    () => () => {
      for (const timer of timersRef.current.values()) clearTimeout(timer);
      timersRef.current.clear();
    },
    [],
  );

  // A primitive membership key preserves the Set reference across equivalent
  // server responses even when their arrays are newly allocated.
  const visibleIndicesKey = scopedOccurrences
    .filter((occurrence) => !effectiveExpiredKeys.has(occurrence.scopedKey))
    .map((occurrence) => occurrence.index)
    .join(",");
  return useMemo(() => {
    if (visibleIndicesKey === "") return EMPTY_PENDING_SCORE_INDICES;
    return new Set(visibleIndicesKey.split(",").map(Number));
  }, [visibleIndicesKey]);
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
  pendingScopeKey: string | null = null,
): {
  lineage: OpeningLineageItem[];
  startPly: number;
  /** Per-occurrence loading state for locally-visible cards that the server
   *  lineage has not hydrated yet. Distinct from a loaded null score. */
  pendingScoreIndices: ReadonlySet<number>;
} {
  const [roots, setRoots] = useState<LiveOpeningRootIndex>(EMPTY_INDEX);

  useEffect(() => {
    let cancelled = false;
    loadOpeningRootIndex()
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

  const mergedState = useMemo(
    () =>
      local === null
        ? {
            lineage: serverLineage,
            pendingScoreOccurrences: EMPTY_PENDING_SCORE_OCCURRENCES,
          }
        : mergeServerLineageState(local, serverLineage),
    [local, serverLineage],
  );

  const unresolvedScoreIndices = useBoundedPendingScoreIndices(
    mergedState.pendingScoreOccurrences,
    pendingScopeKey,
  );

  const startPly = useMemo(() => deriveStartPly(moveHistory), [moveHistory]);

  return {
    lineage: mergedState.lineage,
    startPly,
    pendingScoreIndices: unresolvedScoreIndices,
  };
}
