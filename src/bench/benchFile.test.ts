import { describe, expect, it } from 'vitest'
import { benchFileProblems } from './benchFile'
import { BENCH_SCHEMA_VERSION, zeroedDivergences, zeroedRejections } from './benchRecord'
import type { BenchMoveRecord, BenchRecord, BenchRunRecord } from './benchRecord'
import { summarize } from './summarize'

const header: BenchRunRecord = {
  kind: 'run',
  schemaVersion: BENCH_SCHEMA_VERSION,
  runId: 'run-1',
  harness: 'device',
  build: 'bundled',
  startedAtIso: '2026-07-27T00:00:00.000Z',
  engine: { engineVersion: '18', engineBuild: 'abc', evalFileId: 'nn-x', threads: 1, hashMb: 128 },
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
    repeats: 1,
    positionSetId: 'smoke-6',
    positionCount: 2,
    blockCount: 1,
    plannedItems: 2,
    warmup: false,
    armOrderBalanced: true,
    blockCooldownMs: 60_000,
  },
  methodWarnings: ['repeats=1 is below §10.4'],
  countersAre: 'observer-recomputed',
}

const move = (seq: number, e2eMs: number, pEqualsB: boolean): BenchMoveRecord => ({
  kind: 'move',
  schemaVersion: BENCH_SCHEMA_VERSION,
  runId: 'run-1',
  seq,
  blockIndex: 0,
  repeat: 0,
  arm: 'current',
  orderIndex: 0,
  positionId: `p-${seq}`,
  fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
  playedMove: 'e2e4',
  playerColor: 'white',
  thermalIndex: null,
  cohort: 'warm',
  warmup: false,
  workerRestarted: false,
  engineRebuilt: false,
  requestedDepth: 17,
  e2eMs,
  runElapsedMs: seq * 1000,
  workerBootMs: null,
  resetMs: 10,
  phases: [],
  searchCount: 3,
  totalNodes: 1000,
  totalEngineMs: 900,
  result: {
    bestMove: 'e2e4',
    bestLine: ['e2e4', 'e7e5'],
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
  pEqualsB,
  rejections: zeroedRejections(),
  legacySelectorDivergence: 0,
  divergenceByReason: zeroedDivergences(),
  progressPings: 2,
  streamingPings: 1,
  error: null,
})

const buildFile = (moves: BenchMoveRecord[] = [move(0, 800, true), move(1, 1200, false)]): BenchRecord[] => [
  header,
  ...moves,
  summarize('run-1', moves, {
    completion: 'complete',
    plannedItems: header.plan.plannedItems,
    planWarnings: header.methodWarnings,
  }),
]

describe('benchFileProblems', () => {
  it('passes a file whose summary is what its rows produce', () => {
    expect(benchFileProblems(buildFile())).toEqual([])
  })

  it('catches a deleted move row, which every per-row check accepts', () => {
    // The rows that remain are individually valid, and the summary a reader
    // quotes still names a median — over a set that is no longer in the file.
    const file = buildFile()
    const withoutSecondMove = [file[0], file[1], file[3]]

    expect(benchFileProblems(withoutSecondMove).join(' ')).toMatch(/summary accounts for 2 rows/)
  })

  it('catches an edited summary statistic', () => {
    const file = buildFile()
    const summary = file[3]
    if (summary.kind !== 'summary') throw new Error('fixture')
    const tampered = {
      ...summary,
      gameWeighted: [{ ...summary.gameWeighted[0], medianMs: 1 }],
    }

    expect(benchFileProblems([file[0], file[1], file[2], tampered]).join(' ')).toMatch(
      /not what these move rows produce/,
    )
  })

  it('catches rows spliced in from another run', () => {
    const file = buildFile()
    const foreign = { ...move(1, 1200, false), runId: 'run-2' }

    expect(benchFileProblems([file[0], file[1], foreign, file[3]]).join(' ')).toMatch(
      /does not belong to run run-1/,
    )
  })

  it('refuses a `complete` run that did not measure its whole plan', () => {
    // The trimming that survives every other check: drop the LAST row and
    // regenerate. Sequence numbers stay contiguous, the counts still agree, the
    // summary still recomputes — and `complete` still says the file is quotable.
    const moves = [move(0, 800, true)]
    const trimmed = [
      header,
      ...moves,
      summarize('run-1', moves, {
        completion: 'complete',
        plannedItems: header.plan.plannedItems,
        planWarnings: header.methodWarnings,
      }),
    ]

    expect(benchFileProblems(trimmed).join(' ')).toMatch(
      /says complete but measured 1 of 2 planned measurements/,
    )
  })

  it('accepts a stopped run that covered only part of its plan', () => {
    // Partial coverage is a legitimate state — it just has to say so.
    const moves = [move(0, 800, true)]
    const stopped = [
      header,
      ...moves,
      summarize('run-1', moves, {
        completion: 'stopped',
        plannedItems: header.plan.plannedItems,
        planWarnings: header.methodWarnings,
      }),
    ]

    expect(benchFileProblems(stopped)).toEqual([])
  })

  it('catches a gap in the sequence numbers', () => {
    const moves = [move(0, 800, true), move(2, 1200, false)]

    expect(benchFileProblems(buildFile(moves)).join(' ')).toMatch(/not a contiguous sequence/)
  })

  it('calls a file with no summary a crashed run rather than a measurement', () => {
    expect(benchFileProblems([header, move(0, 800, true)]).join(' ')).toMatch(
      /expected exactly 1 summary, found 0/,
    )
  })

  it('requires the header first and the summary last', () => {
    const file = buildFile()

    expect(benchFileProblems([file[1], file[0], file[2], file[3]]).join(' ')).toMatch(
      /run header must be the first record/,
    )
    expect(benchFileProblems([file[0], file[1], file[3], file[2]]).join(' ')).toMatch(
      /summary must be the last record/,
    )
  })
})
