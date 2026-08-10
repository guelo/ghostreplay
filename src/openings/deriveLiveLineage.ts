import { normalize_fen } from "../utils/fen";
import type {
  OpeningLineageItem,
  OpeningRootItem,
  OpeningRootsListResponse,
} from "../utils/api";

/**
 * Client-side mirror of the backend's `played_opening_chain_indexed`
 * (backend/app/opening_roots.py), used to render opening cards on the SAME tick
 * as the move that crossed a root — before any server round-trip (g-a5v3).
 *
 * Why this exists: the server lineage is not causally ordered with the move.
 * Moves become uploadable only after local analysis resolves, then flush on a
 * 3s interval, so a card gated on the server response can appear seconds late —
 * or not until the NEXT move re-arms the poll. Deriving from local move history
 * makes the server pure enrichment (scores), never the display trigger.
 *
 * The cost of this approach is a second implementation of the chain walk that
 * can drift from the backend's. That is held in check by a shared-fixture
 * parity test (deriveLiveLineage.parity.test.ts); if you change the walk here,
 * change it there and re-run that test.
 */

/** A root registry flattened to a key -> root lookup, matching `OpeningRoots`. */
export type LiveOpeningRootIndex = Map<string, OpeningRootItem>;

/**
 * Flatten the `/api/openings/roots` family listing into a key lookup. The
 * response groups roots by family; the chain walk only ever needs key -> root.
 */
export function buildRootIndex(
  response: OpeningRootsListResponse | null | undefined,
): LiveOpeningRootIndex {
  const index: LiveOpeningRootIndex = new Map();
  for (const family of response?.families ?? []) {
    for (const root of family.roots) {
      index.set(root.opening_key, root);
    }
  }
  return index;
}

/** A locally-derived lineage item, plus the crossing index used to merge in
 *  server scores. Score fields are null until the server hydrates them. */
export interface LiveOpeningLineageItem extends OpeningLineageItem {
  /** Index into the local move history of the move that crossed this root.
   *  Part of the merge identity: a root crossed twice (non-consecutively)
   *  yields the same `opening_key` at two different indices. */
  crossingIndex: number;
}

/** Minimal shape this walk needs from a local move record. */
interface PlayedMove {
  san: string;
  /** Position AFTER this move. */
  fen: string;
}

/**
 * Walk played positions in MOVE ORDER, appending each boundary opening root
 * crossed together with the move index that crossed it.
 *
 * Mirrors `played_opening_chain_indexed` exactly:
 *  - Positions are normalized to the 4-field opening key via the shared
 *    `normalize_fen` (which gates the en-passant square on legality, matching
 *    the backend's `_fen_from_board`).
 *  - Keys absent from the registry are skipped.
 *  - Only CONSECUTIVE repeats are deduped. A root reached, left, and reached
 *    again is retained with its own index — that is what stops the second
 *    crossing from collapsing onto the first crossing's SAN prefix.
 *  - Order comes from the move-order walk, NOT from `depth` (graph BFS depth is
 *    not authoritative played order).
 */
export function deriveLiveOpeningLineage(
  moveHistory: readonly PlayedMove[],
  roots: LiveOpeningRootIndex,
): LiveOpeningLineageItem[] {
  const chain: Array<{ root: OpeningRootItem; index: number }> = [];
  for (let index = 0; index < moveHistory.length; index += 1) {
    const fen = moveHistory[index]?.fen;
    if (!fen) continue;
    const root = roots.get(normalize_fen(fen));
    if (!root) continue;
    // Consecutive-repeat dedupe ONLY.
    if (chain.length > 0 && chain[chain.length - 1].root.opening_key === root.opening_key) {
      continue;
    }
    chain.push({ root, index });
  }

  return chain.map(({ root, index }, position) => ({
    opening_key: root.opening_key,
    opening_name: root.opening_name,
    opening_family: root.opening_family,
    eco: root.eco,
    depth: root.depth,
    // Scores arrive later, from the server (see mergeServerLineage).
    score: null,
    confidence: null,
    coverage: null,
    sample_size: null,
    game_count: null,
    path: chain.slice(0, position).map((entry) => entry.root.opening_key),
    // The player's SAN prefix up to and including the crossing move.
    moves: moveHistory.slice(0, index + 1).map((move) => move.san),
    crossingIndex: index,
  }));
}

/** Merge identity: opening_key ALONE is insufficient — a non-consecutively
 *  repeated root appears at two crossing indices and a key-only merge would
 *  hydrate both from one server row, defeating the retention rule above. */
const mergeKey = (openingKey: string, crossingIndex: number): string =>
  `${openingKey}@${crossingIndex}`;

function indexServerLineage(
  server: readonly OpeningLineageItem[] | null | undefined,
): Map<string, OpeningLineageItem> {
  const scoresByKey = new Map<string, OpeningLineageItem>();
  for (const item of server ?? []) {
    scoresByKey.set(mergeKey(item.opening_key, item.moves.length - 1), item);
  }
  return scoresByKey;
}

export interface PendingScoreOccurrence {
  index: number;
  occurrenceKey: string;
}

export interface MergedServerLineageState {
  lineage: LiveOpeningLineageItem[];
  /** Locally-derived occurrences that have no authoritative server row yet.
   *  A matching `score: null` row is settled/unscored, not pending. */
  pendingScoreOccurrences: readonly PendingScoreOccurrence[];
}

/**
 * Hydrate locally-derived cards with server-supplied scores.
 *
 * The local lineage is authoritative for STRUCTURE (which cards exist, their
 * names, paths, and SAN prefixes); the server is authoritative only for SCORES.
 * A shorter or empty server lineage therefore can never remove a locally
 * visible card — it just leaves those cards unscored.
 *
 * Server items carry no crossing index, so it is recovered from `moves.length`:
 * the SAN prefix runs up to and including the crossing move, so the index is
 * `moves.length - 1`. This holds only when both sides walk the same move list,
 * which is exactly what the parity test pins down.
 */
export function mergeServerLineageState(
  local: LiveOpeningLineageItem[],
  server: readonly OpeningLineageItem[] | null | undefined,
): MergedServerLineageState {
  const scoresByKey = indexServerLineage(server);
  let changed = false;
  const pendingScoreOccurrences: PendingScoreOccurrence[] = [];
  const merged = local.map((item, index) => {
    const occurrenceKey = mergeKey(item.opening_key, item.crossingIndex);
    const match = scoresByKey.get(occurrenceKey);
    if (!match) {
      pendingScoreOccurrences.push({ index, occurrenceKey });
      return item;
    }
    changed = true;
    return {
      ...item,
      score: match.score,
      confidence: match.confidence,
      coverage: match.coverage,
      sample_size: match.sample_size,
      game_count: match.game_count,
    };
  });

  // Preserve referential identity when nothing hydrated, so consumers memoizing
  // on the lineage array do not re-render on every poll that changes nothing.
  return {
    lineage: changed ? merged : local,
    pendingScoreOccurrences,
  };
}

/** Compatibility projection for consumers that only need hydrated rows. */
export function mergeServerLineage(
  local: LiveOpeningLineageItem[],
  server: readonly OpeningLineageItem[] | null | undefined,
): LiveOpeningLineageItem[] {
  return mergeServerLineageState(local, server).lineage;
}
