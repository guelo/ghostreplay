import { describe, expect, it } from 'vitest'
import {
  BENCH_SCHEMA_VERSION,
  addCounters,
  parseJsonl,
  serializeJsonl,
  validateBenchRecord,
  zeroedDivergences,
  zeroedRejections,
} from './benchRecord'
import type { BenchMoveRecord, BenchRunRecord, BenchSummaryRecord } from './benchRecord'

const runRecord: BenchRunRecord = {
  kind: 'run',
  schemaVersion: BENCH_SCHEMA_VERSION,
  runId: 'run-1',
  harness: 'device',
  build: 'bundled',
  startedAtIso: '2026-07-27T00:00:00.000Z',
  engine: {
    engineVersion: '18',
    engineBuild: 'abc',
    evalFileId: 'nn-x',
    threads: 1,
    hashMb: 128,
  },
  depth: { baseline: 17, maxDevice: 17, session: 17, requested: 17 },
  environment: {
    userAgent: 'test',
    uaData: null,
    hardwareConcurrency: 8,
    deviceMemory: null,
    platform: 'MacIntel',
    screen: '1440x900',
    devicePixelRatio: 2,
    timeZone: 'UTC',
  },
  source: {
    gitRevision: 'a'.repeat(40),
    gitDirty: false,
    workerBundleFile: 'dist/assets/analysisWorker-abc123.js',
    workerBundleSha256: 'b'.repeat(64),
  },
  device: { label: 'MacBook', notes: '' },
  plan: {
    mode: 'sequence',
    arms: ['current'],
    repeats: 3,
    positionSetId: 'smoke-6',
    positionCount: 6,
    blockCount: 3,
    plannedItems: 18,
    warmup: false,
    armOrderBalanced: true,
    blockCooldownMs: 0,
  },
  methodWarnings: [],
  countersAre: 'observer-recomputed',
}

const moveRecord: BenchMoveRecord = {
  kind: 'move',
  schemaVersion: BENCH_SCHEMA_VERSION,
  runId: 'run-1',
  seq: 0,
  blockIndex: 0,
  repeat: 0,
  arm: 'current',
  orderIndex: 0,
  positionId: 'smoke:start-e4',
  fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
  playedMove: 'e2e4',
  playerColor: 'white',
  thermalIndex: null,
  cohort: 'cold',
  warmup: false,
  workerRestarted: false,
  engineRebuilt: false,
  requestedDepth: 17,
  e2eMs: 2500,
  runElapsedMs: 40,
  workerBootMs: 900,
  resetMs: 12,
  phases: [],
  searchCount: 0,
  totalNodes: null,
  totalEngineMs: null,
  result: null,
  pEqualsB: null,
  rejections: zeroedRejections(),
  legacySelectorDivergence: 0,
  divergenceByReason: zeroedDivergences(),
  progressPings: 0,
  streamingPings: 0,
  error: null,
}

describe('bench JSONL', () => {
  it('round-trips records', () => {
    const text = serializeJsonl([runRecord, moveRecord])
    expect(text.endsWith('\n')).toBe(true)
    expect(parseJsonl(text)).toEqual([runRecord, moveRecord])
  })

  it('serializes an empty run to an empty string', () => {
    expect(serializeJsonl([])).toBe('')
    expect(parseJsonl('')).toEqual([])
  })

  it('throws on a malformed row rather than skipping it', () => {
    // A silently dropped row yields a plausible summary over an unknown subset,
    // which is worse than refusing to read the file.
    expect(() => parseJsonl('{not json}')).toThrow(/line 1/)
    expect(() => parseJsonl(`${JSON.stringify({ ...moveRecord, phases: undefined })}`)).toThrow(
      /missing phases/,
    )
  })
})

describe('validateBenchRecord', () => {
  const summaryRecord: BenchSummaryRecord = {
    kind: 'summary',
    schemaVersion: BENCH_SCHEMA_VERSION,
    runId: 'run-1',
    completion: 'complete',
    plannedItems: 6,
    measuredItems: 6,
    warmupItems: 0,
    methodWarnings: [],
    cells: [],
    observedMatchRate: [{ arm: 'current', m: 0.4, n: 5 }],
    gameWeighted: [
      { arm: 'current', m: 0.4, n: 5, medianMs: 900, p90Ms: 1200, p95Ms: 1400, worstMs: 1400 },
    ],
    rejections: zeroedRejections(),
    legacySelectorDivergence: 0,
    divergenceByReason: zeroedDivergences(),
    errors: 0,
  }

  it('accepts the three record kinds', () => {
    expect(validateBenchRecord(runRecord)).toBeNull()
    expect(validateBenchRecord(moveRecord)).toBeNull()
    expect(validateBenchRecord(summaryRecord)).toBeNull()
  })

  it('refuses a summary that does not say whether it is complete', () => {
    // A partial run's numbers look exactly like a finished one's.
    const { completion, ...withoutCompletion } = summaryRecord
    expect(completion).toBe('complete')
    expect(validateBenchRecord(withoutCompletion)).toMatch(/missing completion/)
  })

  it('refuses a summary that omits the numbers the adoption gate is read off', () => {
    // The old validator required only `cells`, `completion` and the match rate —
    // so a summary with no §11 statistics and no §4 counters parsed cleanly and
    // then answered `undefined` to every question §12 step 9 asks.
    for (const field of [
      'gameWeighted',
      'rejections',
      'divergenceByReason',
      'legacySelectorDivergence',
      'errors',
      'measuredItems',
    ] as const) {
      const { [field]: removed, ...without } = summaryRecord
      expect(removed, field).toBeDefined()
      expect(validateBenchRecord(without), field).toMatch(new RegExp(field))
    }
  })

  it('refuses a game-weighted entry whose p95 arrived as a NaN-shaped null', () => {
    expect(
      validateBenchRecord({
        ...summaryRecord,
        gameWeighted: [{ arm: 'current', m: 0.4, n: 5, medianMs: 900, p90Ms: 1200, p95Ms: NaN, worstMs: 1400 }],
      }),
    ).toMatch(/gameWeighted\.p95Ms/)
  })

  it('refuses a run whose plan cannot say what cooldown it applied', () => {
    // `--cooldown nope` used to reach the file as `null`: no sleep, no warning,
    // and a thermal control the run never had.
    expect(
      validateBenchRecord({
        ...runRecord,
        plan: { ...runRecord.plan, blockCooldownMs: NaN },
      }),
    ).toMatch(/plan\.blockCooldownMs/)
  })

  it('refuses a row labelled with an arm the schema does not know', () => {
    expect(validateBenchRecord({ ...moveRecord, arm: 'variantZ' })).toMatch(/move\.arm/)
    expect(
      validateBenchRecord({ ...runRecord, plan: { ...runRecord.plan, arms: ['variantZ'] } }),
    ).toMatch(/unknown arm/)
  })

  it('refuses a NaN-serialized statistic but accepts an explicit null match rate', () => {
    // JSON.stringify turns NaN into null, so an all-cold run used to emit
    // `m: null` against a `number` field — invalid, and indistinguishable from a
    // real zero. Null is now the honest value for "no warm rows"; a null where a
    // latency belongs is still a broken cell.
    expect(
      validateBenchRecord({ ...summaryRecord, observedMatchRate: [{ arm: 'current', m: null, n: 0 }] }),
    ).toBeNull()
    expect(
      validateBenchRecord({ ...summaryRecord, observedMatchRate: [{ arm: 'current', m: NaN, n: 1 }] }),
    ).toMatch(/observedMatchRate\.m/)
    expect(
      validateBenchRecord({
        ...summaryRecord,
        cells: [
          {
            arm: 'current',
            cohort: 'all',
            split: 'all',
            stats: { n: 0, medianMs: null, p90Ms: 1, p95Ms: 1, worstMs: 1, medianNodes: null },
          },
        ],
      }),
    ).toMatch(/current\/all\/all\.medianMs/)
  })

  it('refuses a run header with no provenance', () => {
    const { source, ...withoutSource } = runRecord
    expect(source.gitRevision).not.toBeNull()
    expect(validateBenchRecord(withoutSource)).toMatch(/missing source stamp/)
  })

  it('refuses a move row that does not say whether it is a warm-up', () => {
    const { warmup, ...withoutWarmup } = moveRecord
    expect(warmup).toBe(false)
    expect(validateBenchRecord(withoutWarmup)).toMatch(/warmup/)
  })

  it('refuses a move row that does not say whether the engine was rebuilt under it', () => {
    // Whether a row ran on a rebuilt engine decides its cohort, and the cohort is
    // what §11's gate is read off.
    const { engineRebuilt, ...withoutFlag } = moveRecord
    expect(engineRebuilt).toBe(false)
    expect(validateBenchRecord(withoutFlag)).toMatch(/engineRebuilt/)
  })

  it('validates the nested records too, not just the top-level fields', () => {
    // A row that parses but whose result, phase or environment is a bare `{}`
    // reads as a measurement and answers `undefined` to every question asked of
    // it — the same failure as a missing top-level field, one level down.
    expect(validateBenchRecord({ ...runRecord, environment: {} })).toMatch(/environment\./)
    expect(
      validateBenchRecord({ ...runRecord, device: { label: 'MacBook' } }),
    ).toMatch(/device\.notes/)
    expect(validateBenchRecord({ ...runRecord, device: { label: '', notes: '' } })).toMatch(
      /device\.label/,
    )
    expect(validateBenchRecord({ ...moveRecord, result: {} })).toMatch(/result\.bestMove/)
  })

  it('refuses a result whose classification or stop reason is not a named one', () => {
    const result = {
      bestMove: 'e2e4',
      bestLine: ['e2e4', 'e7e5'],
      bestEval: 24,
      playedEval: 24,
      bestEvalMate: null,
      playedEvalMate: null,
      delta: 0,
      classification: 'best',
      canonical: true,
      capFired: false,
      stopReason: 'bestmove',
      reachedDepth: 17,
    }

    expect(validateBenchRecord({ ...moveRecord, result })).toBeNull()
    expect(validateBenchRecord({ ...moveRecord, result: { ...result, classification: null } })).toBeNull()
    expect(validateBenchRecord({ ...moveRecord, result: { ...result, stopReason: 'deadline' } })).toBeNull()
    // Both are string unions a reader splits rows by: an unrecognized value is
    // not a new category, it is a row nobody counts.
    expect(
      validateBenchRecord({ ...moveRecord, result: { ...result, classification: 'brilliant' } }),
    ).toMatch(/result\.classification/)
    expect(
      validateBenchRecord({ ...moveRecord, result: { ...result, stopReason: 'stopped' } }),
    ).toMatch(/result\.stopReason/)
  })

  it('refuses a phase whose §4 verdict cannot be read', () => {
    const phase = {
      index: 0,
      name: 'root',
      moves: [],
      requestedDepth: 17,
      bestmove: 'e2e4',
      nodes: 1000,
      timeMs: 200,
      nps: 5000,
      hashfull: 10,
      reachedDepth: 17,
      seldepth: 20,
      wallMs: 210,
      infoLines: 17,
      admittedLines: 17,
      terminated: true,
      stopObserved: false,
      snapshot: { accepted: true, depth: 17 },
      legacyDivergence: null,
    }

    expect(validateBenchRecord({ ...moveRecord, phases: [phase] })).toBeNull()
    // §10.4 reports divergence split BY reason, so an unknown one lands in a
    // bucket no reader looks in.
    expect(
      validateBenchRecord({ ...moveRecord, phases: [{ ...phase, legacyDivergence: 'wat' }] }),
    ).toMatch(/legacyDivergence/)
    expect(validateBenchRecord({ ...moveRecord, phases: [{ ...phase, snapshot: {} }] })).toMatch(
      /snapshot must be null or say whether/,
    )
    expect(
      validateBenchRecord({
        ...moveRecord,
        phases: [{ ...phase, snapshot: { accepted: false, reason: 'made-up' } }],
      }),
    ).toMatch(/not a named §4 rejection/)
    // An unnamed phase would join to no §10.4 per-phase report.
    expect(validateBenchRecord({ ...moveRecord, phases: [{ ...phase, name: 'fourth' }] })).toMatch(
      /\.name/,
    )
  })

  it('refuses a summary cell whose coordinates no reader can join on', () => {
    const stats = { n: 1, medianMs: 1, p90Ms: 1, p95Ms: 1, worstMs: 1, medianNodes: null }

    expect(
      validateBenchRecord({
        ...summaryRecord,
        cells: [{ arm: 'current', cohort: 'lukewarm', split: 'all', stats }],
      }),
    ).toMatch(/\.cohort/)
    expect(
      validateBenchRecord({
        ...summaryRecord,
        cells: [{ arm: 'current', cohort: 'warm', split: 'p-equals', stats }],
      }),
    ).toMatch(/\.split/)
  })

  it('refuses an unknown schema version', () => {
    expect(validateBenchRecord({ ...runRecord, schemaVersion: 99 })).toMatch(
      /unsupported schemaVersion 99/,
    )
  })

  it('refuses an unknown kind and a missing runId', () => {
    expect(validateBenchRecord({ ...runRecord, kind: 'phase' })).toMatch(/unknown kind/)
    expect(validateBenchRecord({ ...runRecord, runId: '' })).toMatch(/missing runId/)
  })

  it('pins the schema version, so the Node harness cannot drift silently', () => {
    // Both harnesses import this constant; changing it is a deliberate act that
    // must be paired with a reader update, not an accident.
    expect(BENCH_SCHEMA_VERSION).toBe(2)
  })
})

describe('counter helpers', () => {
  it('sums counter maps in place', () => {
    const a = zeroedRejections()
    a['pv-short'] = 2
    const b = zeroedRejections()
    b['pv-short'] = 3
    b['bounded'] = 1

    expect(addCounters(a, b)['pv-short']).toBe(5)
    expect(a.bounded).toBe(1)
  })

  it('adds the `accepted` bucket to the divergence map', () => {
    expect(zeroedDivergences().accepted).toBe(0)
    expect(Object.keys(zeroedDivergences())).toContain('iteration-mismatch')
  })
})
