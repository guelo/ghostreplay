import { describe, expect, it } from 'vitest'
import {
  latencySeriesByMoveIndex,
  latencyStats,
  median,
  percentile,
  summarize,
  weightedMedian,
  weightedPercentile,
} from './summarize'
import type { BenchSummaryContext } from './summarize'
import { BENCH_SCHEMA_VERSION, zeroedDivergences, zeroedRejections } from './benchRecord'
import type { BenchCohort, BenchMoveRecord } from './benchRecord'

const complete: BenchSummaryContext = {
  completion: 'complete',
  plannedItems: 6,
  planWarnings: [],
}

const baseRow: BenchMoveRecord = {
  kind: 'move',
  schemaVersion: BENCH_SCHEMA_VERSION,
  runId: 'run-1',
  seq: 0,
  blockIndex: 0,
  repeat: 0,
  arm: 'current',
  orderIndex: 0,
  positionId: 'p',
  fen: 'f',
  playedMove: 'e2e4',
  playerColor: 'white',
  thermalIndex: null,
  cohort: 'warm',
  warmup: false,
  workerRestarted: false,
  engineRebuilt: false,
  requestedDepth: 17,
  e2eMs: 0,
  runElapsedMs: 0,
  workerBootMs: null,
  resetMs: null,
  phases: [],
  searchCount: 3,
  totalNodes: 1000,
  totalEngineMs: 900,
  result: {
    bestMove: 'e2e4',
    bestLine: ['e2e4'],
    bestEval: 20,
    playedEval: 20,
    bestEvalMate: null,
    playedEvalMate: null,
    delta: 0,
    classification: 'best',
    canonical: true,
    capFired: false,
    stopReason: 'bestmove',
    reachedDepth: 17,
  },
  pEqualsB: true,
  rejections: zeroedRejections(),
  legacySelectorDivergence: 0,
  divergenceByReason: zeroedDivergences(),
  progressPings: 0,
  streamingPings: 0,
  error: null,
}

const row = (
  overrides: Partial<BenchMoveRecord> & { e2eMs: number; cohort: BenchCohort },
): BenchMoveRecord => ({ ...baseRow, ...overrides })

describe('percentiles', () => {
  it('uses nearest rank so every reported value was actually observed', () => {
    const sorted = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]

    expect(percentile(sorted, 0.9)).toBe(90)
    expect(percentile(sorted, 0.95)).toBe(100)
    expect(percentile(sorted, 0.5)).toBe(50)
    expect(percentile([], 0.5)).toBeNaN()
  })

  it('averages the two middle values of an even sample', () => {
    expect(median([1, 2, 3, 4])).toBe(2.5)
    expect(median([5, 1, 3])).toBe(3)
  })
})

describe('weighted quantiles', () => {
  const equally = (values: number[]) =>
    values.map((value) => ({ value, weight: 1 / values.length }))

  it('reduces to the unweighted estimators when every weight is equal', () => {
    // What lets the game-weighted block and the pooled cell agree exactly rather
    // than by a tick, which is the whole reason the estimator is pinned.
    for (const values of [[10, 20, 30, 40, 50, 60, 70, 80, 90, 100], [1, 2, 3, 4], [5, 1, 3]]) {
      const sorted = [...values].sort((a, b) => a - b)
      expect(weightedMedian(equally(values))).toBe(median(values))
      expect(weightedPercentile(equally(values), 0.9)).toBe(percentile(sorted, 0.9))
      expect(weightedPercentile(equally(values), 0.95)).toBe(percentile(sorted, 0.95))
    }
  })

  it('moves the answer when the weights are not equal', () => {
    const skewed = [
      { value: 100, weight: 0.9 },
      { value: 900, weight: 0.1 },
    ]

    expect(weightedMedian(skewed)).toBe(100)
    expect(weightedPercentile(skewed, 0.95)).toBe(900)
  })

  it('has no answer for an empty or weightless sample', () => {
    expect(weightedMedian([])).toBeNaN()
    expect(weightedPercentile([{ value: 5, weight: 0 }], 0.5)).toBeNaN()
  })
})

describe('latencyStats', () => {
  it('summarizes latency and node counts', () => {
    const stats = latencyStats([
      row({ e2eMs: 300, cohort: 'warm' }),
      row({ e2eMs: 100, cohort: 'warm' }),
      row({ e2eMs: 200, cohort: 'warm' }),
    ])

    expect(stats).toEqual({
      n: 3,
      medianMs: 200,
      p90Ms: 300,
      p95Ms: 300,
      worstMs: 300,
      medianNodes: 1000,
    })
  })
})

describe('summarize', () => {
  const rows = [
    row({ e2eMs: 1000, cohort: 'cold' }),
    row({ e2eMs: 100, cohort: 'warm', pEqualsB: true }),
    row({ e2eMs: 200, cohort: 'warm', pEqualsB: true }),
    row({ e2eMs: 300, cohort: 'warm', pEqualsB: true }),
    row({ e2eMs: 700, cohort: 'warm', pEqualsB: false }),
    row({ e2eMs: 900, cohort: 'warm', pEqualsB: false }),
  ]

  it('splits cells by cohort and by P===B', () => {
    const summary = summarize('run-1', rows, complete)
    const cell = (cohort: string, split: string) =>
      summary.cells.find((entry) => entry.cohort === cohort && entry.split === split)

    expect(cell('cold', 'all')?.stats.n).toBe(1)
    expect(cell('warm', 'p-equals-b')?.stats.medianMs).toBe(200)
    expect(cell('warm', 'p-differs')?.stats.medianMs).toBe(800)
    expect(cell('all', 'all')?.stats.n).toBe(6)
  })

  it('reports every game-weighted statistic §11 gates on, not just the median', () => {
    const summary = summarize('run-1', rows, complete)

    // 3 of 5 warm rows are P===B, over warm latencies 100/200/300 and 700/900.
    expect(summary.observedMatchRate[0].m).toBeCloseTo(0.6)
    expect(summary.gameWeighted[0]).toEqual({
      arm: 'current',
      m: 0.6,
      n: 5,
      medianMs: 300,
      p90Ms: 900,
      p95Ms: 900,
      worstMs: 900,
    })
  })

  it('weights at the warm sample’s own share, so it agrees with the pooled warm cell', () => {
    // The identity is the point: it says the mixture was applied once, and it
    // leaves §11's gate with one number to read rather than two that differ.
    const summary = summarize('run-1', rows, complete)
    const warm = summary.cells.find((cell) => cell.cohort === 'warm' && cell.split === 'all')
    const weighted = summary.gameWeighted[0]

    expect(weighted.medianMs).toBe(warm?.stats.medianMs)
    expect(weighted.p90Ms).toBe(warm?.stats.p90Ms)
    expect(weighted.p95Ms).toBe(warm?.stats.p95Ms)
    expect(weighted.worstMs).toBe(warm?.stats.worstMs)
  })

  it('declines to weight when either split is empty', () => {
    const summary = summarize('run-1', [row({ e2eMs: 100, cohort: 'warm', pEqualsB: true })], complete)

    expect(summary.gameWeighted[0]).toEqual({
      arm: 'current',
      m: null,
      n: 1,
      medianMs: null,
      p90Ms: null,
      p95Ms: null,
      worstMs: null,
    })
  })

  it('excludes errored rows from stats but counts them', () => {
    const summary = summarize('run-1', [
      ...rows,
      row({ e2eMs: 0, cohort: 'warm', error: 'timed out', result: null, pEqualsB: null }),
    ], complete)

    expect(summary.errors).toBe(1)
    expect(summary.cells.find((cell) => cell.cohort === 'all' && cell.split === 'all')?.stats.n).toBe(6)
  })

  it('rolls up rejection and divergence counters across measured rows', () => {
    const rejections = zeroedRejections()
    rejections['pv-short'] = 2
    const divergences = zeroedDivergences()
    divergences['pv-short'] = 2

    const summary = summarize('run-1', [
      row({ e2eMs: 100, cohort: 'warm', rejections, divergenceByReason: divergences, legacySelectorDivergence: 2 }),
    ], complete)

    expect(summary.rejections['pv-short']).toBe(2)
    expect(summary.divergenceByReason['pv-short']).toBe(2)
    expect(summary.legacySelectorDivergence).toBe(2)
  })

  it('keeps a warm-up row out of the §4 counters too, not just the latencies', () => {
    // §12 step 9 reads these counters to decide adoption, and a discarded priming
    // duplicate is no more part of that diagnosis than it is part of the median.
    const rejections = zeroedRejections()
    rejections['pv-short'] = 3
    const divergences = zeroedDivergences()
    divergences['pv-short'] = 3

    const summary = summarize(
      'run-1',
      [
        row({
          e2eMs: 100,
          cohort: 'cold',
          warmup: true,
          rejections,
          divergenceByReason: divergences,
          legacySelectorDivergence: 3,
        }),
        row({ e2eMs: 100, cohort: 'warm' }),
      ],
      { ...complete, plannedItems: 1 },
    )

    expect(summary.rejections['pv-short']).toBe(0)
    expect(summary.divergenceByReason['pv-short']).toBe(0)
    expect(summary.legacySelectorDivergence).toBe(0)
  })

  it('still counts §4 observations from an errored row, which the worker really made', () => {
    const rejections = zeroedRejections()
    rejections['pv-short'] = 1

    const summary = summarize(
      'run-1',
      [row({ e2eMs: 0, cohort: 'warm', error: 'boom', result: null, pEqualsB: null, rejections })],
      { ...complete, plannedItems: 1 },
    )

    expect(summary.rejections['pv-short']).toBe(1)
  })

  it('reports a null match rate for an all-cold run rather than NaN', () => {
    // JSON.stringify writes NaN as `null`, so a NaN here produced a file that
    // silently violated its own schema — every --mode cold capture.
    const summary = summarize('cold-run', [row({ e2eMs: 100, cohort: 'cold' })], {
      ...complete,
      plannedItems: 1,
    })

    expect(summary.observedMatchRate[0]).toEqual({ arm: 'current', m: null, n: 0 })
    expect(JSON.parse(JSON.stringify(summary)).observedMatchRate[0].m).toBeNull()
  })

  it('excludes warm-up rows from every statistic but records them', () => {
    const summary = summarize(
      'run-1',
      [
        row({ e2eMs: 5000, cohort: 'cold', warmup: true }),
        row({ e2eMs: 100, cohort: 'warm' }),
        row({ e2eMs: 300, cohort: 'warm' }),
      ],
      { ...complete, plannedItems: 2 },
    )

    // The 5000ms priming row must not reach the cold cohort or the roll-up.
    expect(summary.cells.some((cell) => cell.cohort === 'cold')).toBe(false)
    expect(summary.cells.find((cell) => cell.cohort === 'all' && cell.split === 'all')?.stats.medianMs).toBe(200)
    expect(summary.warmupItems).toBe(1)
    expect(summary.measuredItems).toBe(2)
  })

  it('marks a stopped run and says how much of the plan it covered', () => {
    const summary = summarize('run-1', [row({ e2eMs: 100, cohort: 'warm' })], {
      completion: 'stopped',
      plannedItems: 40,
      planWarnings: [],
    })

    expect(summary.completion).toBe('stopped')
    expect(summary.methodWarnings.join(' ')).toMatch(/stopped after 1 of 40/)
  })

  it('carries plan warnings into the summary alongside outcome ones', () => {
    const summary = summarize(
      'run-1',
      [row({ e2eMs: 0, cohort: 'warm', error: 'boom', result: null, pEqualsB: null })],
      { completion: 'complete', plannedItems: 1, planWarnings: ['repeats=1 is below'] },
    )

    expect(summary.methodWarnings[0]).toMatch(/repeats=1/)
    expect(summary.methodWarnings.join(' ')).toMatch(/1 measurement\(s\) errored/)
  })
})

describe('latencySeriesByMoveIndex', () => {
  it('medians repeats at each thermal index, in index order', () => {
    const series = latencySeriesByMoveIndex([
      row({ e2eMs: 500, cohort: 'warm', thermalIndex: 2 }),
      row({ e2eMs: 100, cohort: 'cold', thermalIndex: 1 }),
      row({ e2eMs: 300, cohort: 'warm', thermalIndex: 2 }),
      row({ e2eMs: 900, cohort: 'warm', thermalIndex: null }),
    ])

    expect(series).toEqual([
      {
        arm: 'current',
        points: [
          { thermalIndex: 1, medianMs: 100, n: 1 },
          { thermalIndex: 2, medianMs: 400, n: 2 },
        ],
      },
    ])
  })

  it('keeps arms in separate series so a comparison is not pooled into one curve', () => {
    const series = latencySeriesByMoveIndex([
      row({ e2eMs: 100, cohort: 'warm', thermalIndex: 1, arm: 'current' }),
      row({ e2eMs: 900, cohort: 'warm', thermalIndex: 1, arm: 'variantA' }),
    ])

    expect(series.map((entry) => entry.arm).sort()).toEqual(['current', 'variantA'])
    expect(series.find((entry) => entry.arm === 'current')?.points[0].medianMs).toBe(100)
    expect(series.find((entry) => entry.arm === 'variantA')?.points[0].medianMs).toBe(900)
  })
})
