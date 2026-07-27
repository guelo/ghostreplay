/**
 * Latency statistics over benchmark rows (g-two-search-grade §10.4, §11).
 *
 * §11's performance gate is stated on the GAME-WEIGHTED end-to-end median and
 * p95, split by P===B, so the weighting is computed here once rather than
 * re-derived by every reader of the JSONL. Shared by both harnesses.
 */

import type {
  BenchArm,
  BenchCohort,
  BenchCompletion,
  BenchGameWeighted,
  BenchLatencyStats,
  BenchMoveRecord,
  BenchSummaryRecord,
} from './benchRecord'
import { BENCH_SCHEMA_VERSION, addCounters, zeroedDivergences, zeroedRejections } from './benchRecord'
import { outcomeWarnings } from './method'

/**
 * What the rows alone cannot say: whether they are all of them.
 *
 * Required rather than optional, because a summary that does not state its
 * completion is indistinguishable from a complete one — and that is exactly the
 * mistake this field exists to prevent.
 */
export type BenchSummaryContext = {
  completion: BenchCompletion
  plannedItems: number
  /** Plan-level method warnings from `method.planWarnings`. */
  planWarnings: string[]
}

/**
 * Nearest-rank percentile on the sorted sample (no interpolation).
 *
 * Deliberately not a linear-interpolation percentile: every reported value is
 * then an OBSERVED latency, so "p95 = 4.2s" names a move that actually took
 * 4.2s. Interpolated tails invent a number no move produced, which is the wrong
 * property for a gate that a human has to sanity-check against a device.
 */
export const percentile = (sortedValues: readonly number[], fraction: number): number => {
  if (sortedValues.length === 0) {
    return NaN
  }
  const rank = Math.ceil(fraction * sortedValues.length)
  const index = Math.min(sortedValues.length - 1, Math.max(0, rank - 1))
  return sortedValues[index]
}

export const median = (values: readonly number[]): number => {
  if (values.length === 0) {
    return NaN
  }
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

/** One observation and how much of the distribution it stands for. */
export type WeightedSample = { value: number; weight: number }

const sortedByValue = (samples: readonly WeightedSample[]): WeightedSample[] =>
  samples.filter((sample) => sample.weight > 0).sort((a, b) => a.value - b.value)

const totalWeight = (samples: readonly WeightedSample[]): number =>
  samples.reduce((sum, sample) => sum + sample.weight, 0)

/**
 * Percentile of a weighted sample — the weighted form of `percentile` above, and
 * deliberately the same estimator: the smallest observation whose cumulative
 * weight reaches the fraction, with no interpolation, so the reported value is
 * one a move actually produced.
 *
 * With equal weights it returns exactly what `percentile` returns. That identity
 * is load-bearing: it is what makes the game-weighted cell and the pooled cell
 * agree when `m` is the sample's own share, instead of differing by a tick for
 * reasons no reader could see.
 */
export const weightedPercentile = (
  samples: readonly WeightedSample[],
  fraction: number,
): number => {
  const sorted = sortedByValue(samples)
  const total = totalWeight(sorted)
  if (sorted.length === 0 || !(total > 0)) {
    return NaN
  }
  // Relative, because the weights are fractions that do not sum to exactly 1 in
  // binary: an exact-boundary comparison would otherwise fall to the next
  // observation on a sample size that happens to divide badly.
  const epsilon = total * 1e-9
  const target = fraction * total
  let cumulative = 0
  for (const sample of sorted) {
    cumulative += sample.weight
    if (cumulative >= target - epsilon) {
      return sample.value
    }
  }
  return sorted[sorted.length - 1].value
}

/**
 * Median of a weighted sample, matching `median`: when the half-weight boundary
 * falls exactly BETWEEN two observations the two are averaged, which for equal
 * weights is the ordinary even-sample-size median.
 */
export const weightedMedian = (samples: readonly WeightedSample[]): number => {
  const sorted = sortedByValue(samples)
  const total = totalWeight(sorted)
  if (sorted.length === 0 || !(total > 0)) {
    return NaN
  }
  const epsilon = total * 1e-9
  const half = total / 2
  let cumulative = 0
  for (let index = 0; index < sorted.length; index += 1) {
    cumulative += sorted[index].weight
    if (cumulative > half + epsilon) {
      return sorted[index].value
    }
    if (cumulative >= half - epsilon) {
      const next = sorted[index + 1]
      return next ? (sorted[index].value + next.value) / 2 : sorted[index].value
    }
  }
  return sorted[sorted.length - 1].value
}

export const latencyStats = (rows: readonly BenchMoveRecord[]): BenchLatencyStats => {
  const latencies = rows.map((row) => row.e2eMs).sort((a, b) => a - b)
  const nodes = rows
    .map((row) => row.totalNodes)
    .filter((value): value is number => value !== null)

  return {
    n: rows.length,
    medianMs: median(latencies),
    p90Ms: percentile(latencies, 0.9),
    p95Ms: percentile(latencies, 0.95),
    worstMs: latencies.length > 0 ? latencies[latencies.length - 1] : NaN,
    medianNodes: nodes.length > 0 ? median(nodes) : null,
  }
}

/**
 * Rows that count as measurements.
 *
 * An errored row has no latency worth reporting, and a warm-up row is a priming
 * duplicate of position 0 whose only job was to spend the block's cold slot —
 * counting it would double-weight that position in every statistic.
 */
export const usableRows = (rows: readonly BenchMoveRecord[]): BenchMoveRecord[] =>
  rows.filter((row) => row.error === null && row.result !== null && !row.warmup)

const COHORTS: Array<BenchCohort | 'all'> = ['all', 'cold', 'warm']
const SPLITS: Array<'all' | 'p-equals-b' | 'p-differs'> = ['all', 'p-equals-b', 'p-differs']

const matchesCohort = (row: BenchMoveRecord, cohort: BenchCohort | 'all') =>
  cohort === 'all' || row.cohort === cohort

const matchesSplit = (row: BenchMoveRecord, split: 'all' | 'p-equals-b' | 'p-differs') => {
  if (split === 'all') return true
  if (row.pEqualsB === null) return false
  return split === 'p-equals-b' ? row.pEqualsB : !row.pEqualsB
}

/**
 * Build the run's summary record.
 *
 * The game-weighted statistics mix the WARM P===B and P!==B rows at the warm
 * cohort's observed match rate `m` and report quantiles of that one distribution
 * (`BenchGameWeighted`). Warm, because a game's moves after the first are warm
 * and the cold row would otherwise be counted twice — once in its own cohort and
 * once in the weighting.
 */
export const summarize = (
  runId: string,
  rows: readonly BenchMoveRecord[],
  context: BenchSummaryContext,
): BenchSummaryRecord => {
  const usable = usableRows(rows)
  const arms = [...new Set(rows.map((row) => row.arm))] as BenchArm[]

  const cells: BenchSummaryRecord['cells'] = []
  for (const arm of arms) {
    const armRows = usable.filter((row) => row.arm === arm)
    for (const cohort of COHORTS) {
      for (const split of SPLITS) {
        const cellRows = armRows.filter(
          (row) => matchesCohort(row, cohort) && matchesSplit(row, split),
        )
        if (cellRows.length === 0) {
          continue
        }
        cells.push({ arm, cohort, split, stats: latencyStats(cellRows) })
      }
    }
  }

  const observedMatchRate = arms.map((arm) => {
    const warm = usable.filter(
      (row) => row.arm === arm && row.cohort === 'warm' && row.pEqualsB !== null,
    )
    return {
      arm,
      // Null, not NaN: JSON.stringify writes NaN as `null` anyway, and an
      // all-cold run legitimately has no warm rows to observe `m` on.
      m: warm.length > 0 ? warm.filter((row) => row.pEqualsB).length / warm.length : null,
      n: warm.length,
    }
  })

  const gameWeighted = arms.map((arm): BenchGameWeighted => {
    const warm = usable.filter((row) => row.arm === arm && row.cohort === 'warm')
    const equal = warm.filter((row) => row.pEqualsB === true).map((row) => row.e2eMs)
    const differs = warm.filter((row) => row.pEqualsB === false).map((row) => row.e2eMs)
    if (equal.length === 0 || differs.length === 0) {
      // No mixture exists: reporting one split as if it were a game is exactly
      // the overstatement the game weighting exists to prevent.
      return { arm, m: null, n: warm.length, medianMs: null, p90Ms: null, p95Ms: null, worstMs: null }
    }
    const m = equal.length / (equal.length + differs.length)
    // Each split carries its share of the distribution, spread evenly over the
    // rows that make it up.
    const samples: WeightedSample[] = [
      ...equal.map((value) => ({ value, weight: m / equal.length })),
      ...differs.map((value) => ({ value, weight: (1 - m) / differs.length })),
    ]
    return {
      arm,
      m,
      n: equal.length + differs.length,
      medianMs: weightedMedian(samples),
      p90Ms: weightedPercentile(samples, 0.9),
      p95Ms: weightedPercentile(samples, 0.95),
      // Reduced rather than spread: a corpus run's sample can outgrow the
      // argument limit, and the worst case is where that would first bite.
      worstMs: [...equal, ...differs].reduce((worst, value) => Math.max(worst, value), -Infinity),
    }
  })

  const rejections = zeroedRejections()
  const divergenceByReason = zeroedDivergences()
  let legacySelectorDivergence = 0
  // §4 counters over MEASURED rows only. A warm-up is a discarded priming
  // duplicate, so a rejection it happened to record is no more part of the run's
  // adoption diagnostics (§12 step 9) than its latency is part of the median.
  // Errored rows DO count: a phase that terminated before the failure recorded a
  // real acceptance, and dropping it would understate what the worker did.
  for (const row of rows.filter((row) => !row.warmup)) {
    addCounters(rejections, row.rejections)
    addCounters(divergenceByReason, row.divergenceByReason)
    legacySelectorDivergence += row.legacySelectorDivergence
  }

  const warmupItems = rows.filter((row) => row.warmup).length
  const measuredItems = rows.length - warmupItems
  // Every failure, warm-up rows included: a warm-up that errored says the block
  // it primed started from a broken worker.
  const errors = rows.filter((row) => row.error !== null).length

  return {
    kind: 'summary',
    schemaVersion: BENCH_SCHEMA_VERSION,
    runId,
    completion: context.completion,
    plannedItems: context.plannedItems,
    measuredItems,
    warmupItems,
    methodWarnings: [
      ...context.planWarnings,
      ...outcomeWarnings({
        completion: context.completion,
        plannedItems: context.plannedItems,
        measuredItems,
        errors,
      }),
    ],
    cells,
    observedMatchRate,
    gameWeighted,
    rejections,
    legacySelectorDivergence,
    divergenceByReason,
    errors,
  }
}

export type BenchMoveIndexPoint = { thermalIndex: number; medianMs: number; n: number }

/**
 * Median e2e latency per thermal index, ONE SERIES PER ARM — §10.4's
 * latency-by-move-index graph.
 *
 * Split by arm rather than pooled: pooling two protocols into one curve hides the
 * very difference the graph exists to show, and would read as a single device
 * curve. Repeats stay pooled within an arm — that is what the median is over.
 */
export const latencySeriesByMoveIndex = (
  rows: readonly BenchMoveRecord[],
): Array<{ arm: BenchArm; points: BenchMoveIndexPoint[] }> => {
  const byArm = new Map<BenchArm, Map<number, number[]>>()
  for (const row of usableRows(rows)) {
    if (row.thermalIndex === null) continue
    const byIndex = byArm.get(row.arm) ?? new Map<number, number[]>()
    const bucket = byIndex.get(row.thermalIndex) ?? []
    bucket.push(row.e2eMs)
    byIndex.set(row.thermalIndex, bucket)
    byArm.set(row.arm, byIndex)
  }

  return [...byArm.entries()].map(([arm, byIndex]) => ({
    arm,
    points: [...byIndex.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([thermalIndex, values]) => ({
        thermalIndex,
        medianMs: median(values),
        n: values.length,
      })),
  }))
}
