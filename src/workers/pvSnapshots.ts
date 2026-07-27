import type { EngineInfo, EngineScore, EngineScoreBound } from './stockfishMessages'
import { compareRootScores } from './compareRootScores'

/**
 * ONE `info` line's self-consistent reading of ONE slot of ONE search iteration
 * (g-two-search-grade §4).
 *
 * A grading score is usable only when score, PV, depth, and bound came from the
 * SAME line. The worker's legacy selector instead accumulates `lastScore`,
 * `lastPv`, and `lastDepth` independently and combines them at `bestmove`, which
 * can staple a score from one iteration onto a PV from another. This record is
 * the atomic alternative; it is INSTRUMENTATION ONLY until §12 step 9 (see
 * `recordLegacySelectorDivergence`).
 */
export type PvSnapshot = {
  depth: number
  seldepth: number | null
  multipv: number
  score: EngineScore
  bound: EngineScoreBound
  pv: string[]
  nodes: number | null
  timeMs: number | null
  /** Monotone per-search emission counter over ADMITTED lines. */
  seq: number
}

/**
 * The closed set of §4.2 slot-acceptance failures.
 *
 * `mate-zero` is not in §4.2's named list, which enumerates eight rejections but
 * also requires that a root-frame mate 0 be rejected. Folding it into `bounded`
 * or `pv-mismatch` would mislabel an invalid score as a bound or a PV defect, so
 * it carries its own counter; §6.3's recovery matrix needs a row for it.
 */
export type SnapshotRejection =
  | 'no-slot'
  | 'bounded'
  | 'stale-depth'
  | 'pv-mismatch'
  | 'pv-short'
  | 'slot-set-mismatch'
  | 'iteration-mismatch'
  | 'slot-order-disagreement'
  | 'mate-zero'

export const SNAPSHOT_REJECTIONS: readonly SnapshotRejection[] = [
  'no-slot',
  'bounded',
  'stale-depth',
  'pv-mismatch',
  'pv-short',
  'slot-set-mismatch',
  'iteration-mismatch',
  'slot-order-disagreement',
  'mate-zero',
]

/**
 * How the atomic selector differed from the legacy accumulators. A rejection
 * reason names a row the atomic selector would have declined; `accepted` names a
 * row where both selectors produced a value and the values disagree.
 */
export type SnapshotDivergenceReason = SnapshotRejection | 'accepted'

export type SnapshotCounters = {
  /** §4.2 rejections, counted whether or not a divergence was being measured. */
  rejections: Record<SnapshotRejection, number>
  /** Rows where the legacy selector and the atomic selector would disagree. */
  legacy_selector_divergence: number
  divergence_by_reason: Record<SnapshotDivergenceReason, number>
  /** Complete batches stored, and batches dropped by the corroboration check. */
  batches_complete: number
  batches_corroboration_failed: number
  /**
   * Searches whose request was canceled before their `bestmove` landed. A cancel
   * emits no row (§6.3 case 9a), so it is tallied here and deliberately excluded
   * from `legacy_selector_divergence` — otherwise §10.4's drift report would
   * count searches that never produced anything for the two selectors to disagree
   * about.
   */
  searches_canceled: number
}

const zeroedRejections = (): Record<SnapshotRejection, number> =>
  SNAPSHOT_REJECTIONS.reduce(
    (acc, reason) => {
      acc[reason] = 0
      return acc
    },
    {} as Record<SnapshotRejection, number>,
  )

const emptyCounters = (): SnapshotCounters => ({
  rejections: zeroedRejections(),
  legacy_selector_divergence: 0,
  divergence_by_reason: { ...zeroedRejections(), accepted: 0 },
  batches_complete: 0,
  batches_corroboration_failed: 0,
  searches_canceled: 0,
})

let counters: SnapshotCounters = emptyCounters()

/** Diagnostics only. Nothing that can alter an emitted value reads these. */
export const getSnapshotCounters = (): SnapshotCounters => counters

export const resetSnapshotCounters = () => {
  counters = emptyCounters()
}

/**
 * Per-search assembly state (§4.1).
 *
 * `(depth, slot)` does NOT identify an iteration — aspiration and bounded
 * re-searches re-emit the same depth — so a map keyed on depth and slot could
 * pair slot 1 from a later pass with slot 2 from an earlier one. Assembly is by
 * emission order instead.
 */
export type SnapshotAssembler = {
  /** K: the slots 1..K a batch must hold to be complete. */
  requiredSlots: number
  seq: number
  currentBatch: PvSnapshot[]
  /** Latch so a K+1th slot cannot re-store the already-completed slice. */
  currentComplete: boolean
  /** Last COMPLETE batch at each depth; a re-search supersedes what it re-searched. */
  completeBatches: Map<number, PvSnapshot[]>
  /** Batches that completed but failed corroboration, over this search alone. */
  iterationMismatches: number
}

export const createSnapshotAssembler = (requiredSlots = 1): SnapshotAssembler => ({
  requiredSlots,
  seq: 0,
  currentBatch: [],
  currentComplete: false,
  completeBatches: new Map(),
  iterationMismatches: 0,
})

/**
 * Corroboration, NOT identity (§4.1).
 *
 * MultiPV lines within one iteration share a search and report a RUNNING count,
 * so slots are not required to agree on `nodes` — only to be non-decreasing in
 * emission order. Absent tokens leave the rule standing on emission order alone;
 * present and decreasing means the slots came from different iterations.
 */
const isCorroborated = (slots: PvSnapshot[]): boolean => {
  let lastNodes: number | null = null
  let lastTimeMs: number | null = null

  for (const slot of slots) {
    if (slot.nodes !== null) {
      if (lastNodes !== null && slot.nodes < lastNodes) {
        return false
      }
      lastNodes = slot.nodes
    }
    if (slot.timeMs !== null) {
      if (lastTimeMs !== null && slot.timeMs < lastTimeMs) {
        return false
      }
      lastTimeMs = slot.timeMs
    }
  }

  return true
}

const tryCompleteBatch = (assembler: SnapshotAssembler) => {
  if (assembler.currentComplete) {
    return
  }

  const required = assembler.requiredSlots
  if (assembler.currentBatch.length < required) {
    return
  }
  for (let index = 0; index < required; index += 1) {
    if (assembler.currentBatch[index].multipv !== index + 1) {
      return
    }
  }

  const slots = assembler.currentBatch.slice(0, required)
  const depth = slots[0].depth
  assembler.currentComplete = true

  if (!isCorroborated(slots)) {
    assembler.iterationMismatches += 1
    counters.batches_corroboration_failed += 1
    // A completed batch supersedes the earlier pass at its depth whether or not
    // it corroborates. Dropping only the new slice would leave the pass it
    // replaced selectable — a cross-pass result presented as the depth's latest,
    // which is exactly what §4.1 exists to prevent. So the depth goes with it,
    // and `iteration-mismatch` becomes the selector's answer for that depth
    // rather than a silent fallback to superseded data.
    assembler.completeBatches.delete(depth)
    return
  }

  counters.batches_complete += 1
  assembler.completeBatches.set(depth, slots)
}

/**
 * Admit one parsed `info` line into assembly, returning the snapshot it produced
 * or null when the line was filtered out.
 *
 * A line enters assembly only when it carries BOTH a score and a PV AND is exact.
 * Score-only, PV-only, and bounded lines never enter, though the caller may still
 * use them for streaming and heartbeat state. Filtering before assembly is what
 * keeps an aspiration fail-high line from opening or polluting a batch.
 */
export const admitInfoLine = (
  assembler: SnapshotAssembler,
  info: EngineInfo,
): PvSnapshot | null => {
  if (info.depth === undefined || !info.score || !info.pv || info.pv.length === 0) {
    return null
  }
  if (info.bound !== 'exact') {
    return null
  }

  const snapshot: PvSnapshot = {
    depth: info.depth,
    seldepth: info.seldepth ?? null,
    multipv: info.multipv ?? 1,
    score: info.score,
    bound: info.bound,
    // Copied, not aliased: the worker also hands this array to its legacy `lastPv`
    // accumulator, and instrumentation must share no mutable state with the
    // selector it is measuring.
    pv: [...info.pv],
    nodes: info.nodes ?? null,
    timeMs: info.time ?? null,
    seq: assembler.seq,
  }
  assembler.seq += 1

  const last = assembler.currentBatch[assembler.currentBatch.length - 1]
  // Start a new batch when the depth changes, or when the slot fails to strictly
  // increase. The second rule closes the re-search hole: every aspiration pass
  // restarts at slot 1, so a repeat slot 1 OPENS a batch rather than overwriting
  // the old one. With K=1 each admitted line opens its own batch, which is
  // correct — every line is a complete single-slot iteration.
  if (!last || last.depth !== snapshot.depth || snapshot.multipv <= last.multipv) {
    assembler.currentBatch = [snapshot]
    assembler.currentComplete = false
  } else {
    assembler.currentBatch.push(snapshot)
  }

  tryCompleteBatch(assembler)

  return snapshot
}

export type AtomicSelectionRequest = {
  assembler: Pick<SnapshotAssembler, 'requiredSlots' | 'completeBatches'>
  requestedDepth: number
  /** The engine's `bestmove`, which slot 1's PV must begin with. */
  bestMove: string
  capFired: boolean
  stopReason: 'bestmove' | 'deadline'
  /**
   * The exact move set the batch's slots must cover, for a restricted search.
   * Null/omitted for an unrestricted root search, which is checked against
   * `bestMove` alone.
   */
  requiredMoves?: string[] | null
  /**
   * Whether a PV's first move ends the game, which exempts it from the
   * two-move-minimum rule. Protocol-neutral: the caller owns the position.
   */
  endsGame?: (uci: string) => boolean
}

export type AtomicSelection =
  | { accepted: true; depth: number; slots: PvSnapshot[] }
  | { accepted: false; reason: SnapshotRejection }

const sameMoveSet = (got: string[], want: string[]): boolean => {
  if (got.length !== want.length || new Set(got).size !== got.length) {
    return false
  }
  const sortedGot = [...got].sort()
  const sortedWant = [...want].sort()
  return sortedGot.every((move, index) => move === sortedWant[index])
}

/** §4.2, evaluated against ONE assembled batch. Null means every slot is acceptable. */
const evaluateBatch = (
  slots: PvSnapshot[],
  request: AtomicSelectionRequest,
): SnapshotRejection | null => {
  const required = request.assembler.requiredSlots

  for (let index = 0; index < required; index += 1) {
    const slot = slots[index]
    if (!slot || slot.multipv !== index + 1 || slot.pv.length === 0) {
      return 'no-slot'
    }
    if (slot.bound !== 'exact') {
      return 'bounded'
    }
    // An untruncated forced mate may legitimately finish BELOW the target depth:
    // the engine stops early because there is nothing left to find. A capped or
    // deadline-stopped search gets no such exemption.
    const forcedMate =
      slot.score.type === 'mate' && request.stopReason === 'bestmove' && !request.capFired
    if (slot.depth < request.requestedDepth && !forcedMate) {
      return 'stale-depth'
    }
    if (slot.score.type === 'mate' && slot.score.value === 0) {
      return 'mate-zero'
    }
    if (slot.pv.length < 2 && !(request.endsGame?.(slot.pv[0]) ?? false)) {
      return 'pv-short'
    }
  }

  // Assembly cannot build a mixed-depth batch, so this is defence in depth: the
  // forced-mate exemption accepts below-target depths per slot, and without this
  // it would be the one route to pairing slot 1 at depth 15 with slot 2 at 14.
  if (slots.slice(0, required).some((slot) => slot.depth !== slots[0].depth)) {
    return 'iteration-mismatch'
  }

  const firstMoves = slots.slice(0, required).map((slot) => slot.pv[0])

  if (request.requiredMoves && !sameMoveSet(firstMoves, request.requiredMoves)) {
    return 'slot-set-mismatch'
  }
  if (firstMoves[0] !== request.bestMove) {
    return 'pv-mismatch'
  }
  for (let index = 0; index + 1 < required; index += 1) {
    if (compareRootScores(slots[index].score, slots[index + 1].score) < 0) {
      return 'slot-order-disagreement'
    }
  }

  return null
}

/**
 * §4.2 slot acceptance over the deepest acceptable complete batch.
 *
 * INSTRUMENTATION ONLY in this bead: no production path calls this, and
 * `analysisWorker.ts` does not even name it. Candidate arms (§12 step 3) are its
 * first callers; production switches to it at §12 step 9, never before.
 *
 * All required slots come from ONE assembled batch, so neither a cross-depth
 * pairing under the forced-mate exemption nor a cross-pass pairing is expressible.
 */
export const selectAtomicSnapshot = (request: AtomicSelectionRequest): AtomicSelection => {
  const reject = (reason: SnapshotRejection): AtomicSelection => {
    counters.rejections[reason] += 1
    return { accepted: false, reason }
  }

  const depths = [...request.assembler.completeBatches.keys()].sort((a, b) => b - a)
  if (depths.length === 0) {
    // "No complete batch at any acceptable depth, or a complete batch failed the
    // monotonicity check" — both land here, since a batch that fails
    // corroboration is never stored.
    return reject('iteration-mismatch')
  }

  let deepestReason: SnapshotRejection | null = null
  for (const depth of depths) {
    const slots = request.assembler.completeBatches.get(depth)!
    const reason = evaluateBatch(slots, request)
    if (reason === null) {
      return { accepted: true, depth, slots }
    }
    // Report the deepest candidate's failure: a shallower batch failing
    // `stale-depth` says nothing the deepest one did not already say.
    if (deepestReason === null) {
      deepestReason = reason
    }
  }

  return reject(deepestReason!)
}

/** What the legacy accumulators produced for the same search. */
export type LegacySelection = {
  score: EngineScore | null
  pv: string[] | null
  reachedDepth: number | null
}

const sameScore = (a: EngineScore | null, b: EngineScore): boolean =>
  a !== null && a.type === b.type && a.value === b.value

const samePv = (a: string[] | null, b: string[]): boolean =>
  a !== null && a.length === b.length && a.every((move, index) => move === b[index])

/**
 * Measure — never change — the gap between the two selectors (§4.3).
 *
 * Returns void by construction: this is the only atomic-selector call the shared
 * worker makes, so no selection can reach an emitted value even by accident.
 * What it counts is exactly what §12 step 9 would change, before it changes
 * anything, and it belongs in the §10.4 drift report.
 */
export const recordLegacySelectorDivergence = (
  request: AtomicSelectionRequest,
  legacy: LegacySelection,
): void => {
  const selection = selectAtomicSnapshot(request)

  let reason: SnapshotDivergenceReason
  if (!selection.accepted) {
    reason = selection.reason
  } else {
    const slot = selection.slots[0]
    const agrees =
      sameScore(legacy.score, slot.score) &&
      samePv(legacy.pv, slot.pv) &&
      legacy.reachedDepth === selection.depth
    if (agrees) {
      return
    }
    reason = 'accepted'
  }

  counters.legacy_selector_divergence += 1
  counters.divergence_by_reason[reason] += 1
}

/**
 * A search whose request was canceled, recorded INSTEAD of a divergence.
 *
 * Cancellation is not a degraded result, it is the absence of one: analyzeMove
 * unwinds via AnalysisCanceledError and posts nothing. Running the selector over
 * a partial canceled search would charge its `stale-depth` to the drift report as
 * though a row had disagreed, so the search is tallied here and never selected.
 */
export const recordCanceledSearch = (): void => {
  counters.searches_canceled += 1
}
