import { randomUUID } from 'node:crypto'
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import {
  BENCH_SCHEMA_VERSION,
  parseJsonl,
  zeroedDivergences,
  zeroedRejections,
} from '../benchRecord'
import type { BenchMoveRecord } from '../benchRecord'
import { benchFileProblems } from '../benchFile'
import { loadCorpus } from './corpus'
import type { CorpusPosition } from './corpus'
import type { CurrentMoveRecordInput } from './currentProtocol'
import type {
  AcceptedReference,
  PositionReferences,
  ReferenceEngine,
} from './references'
import type { ReferenceArtifact } from './referenceArtifact'
import { referenceArtifactProblems } from './referenceArtifact'
import {
  corpusCheckpointPath,
  parseCorpusArgs,
  runCorpusCli,
} from './runCorpus'

const temporaryDirectories: string[] = []

const temporaryDirectory = (): string => {
  const directory = mkdtempSync(join(tmpdir(), 'ghostreplay-corpus-runner-'))
  temporaryDirectories.push(directory)
  return directory
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true })
  }
})

const fakeEngine = () => ({
  reset: async () => 1,
  search: async () => {
    throw new Error('fake writer test must not search')
  },
  close: () => undefined,
})

const fakeMove = (input: CurrentMoveRecordInput): BenchMoveRecord => ({
  kind: 'move',
  schemaVersion: BENCH_SCHEMA_VERSION,
  runId: input.runId,
  seq: input.seq,
  blockIndex: 0,
  repeat: 0,
  arm: 'current',
  orderIndex: 0,
  positionId: input.position.id,
  fen: input.position.fen,
  playedMove: input.position.playedMove,
  playerColor: input.position.playerColor,
  thermalIndex: null,
  cohort: input.cohort,
  warmup: false,
  workerRestarted: false,
  engineRebuilt: false,
  requestedDepth: input.depth,
  e2eMs: 1,
  runElapsedMs: input.runElapsedMs,
  workerBootMs: input.workerBootMs,
  resetMs: 1,
  phases: [],
  searchCount: 0,
  totalNodes: 1,
  totalEngineMs: 1,
  result: {
    bestMove: input.position.playedMove,
    bestLine: [input.position.playedMove],
    bestEval: 0,
    playedEval: 0,
    bestEvalMate: null,
    playedEvalMate: null,
    delta: 0,
    classification: 'best',
    canonical: true,
    capFired: false,
    stopReason: 'bestmove',
    reachedDepth: input.depth,
  },
  pEqualsB: true,
  rejections: zeroedRejections(),
  legacySelectorDivergence: 0,
  divergenceByReason: zeroedDivergences(),
  progressPings: 0,
  streamingPings: 0,
  error: null,
})

const fakeReferenceRow = (position: CorpusPosition): PositionReferences => {
  const accepted: AcceptedReference = {
    status: 'accepted',
    bestMove: position.playedMove,
    playedMove: position.playedMove,
    bestRoot: { type: 'cp', value: 0 },
    playedRoot: { type: 'cp', value: 0 },
    bestPost: { type: 'cp', value: 0 },
    playedPost: { type: 'cp', value: 0 },
    deltaCp: 0,
    classification: 'best',
    bestLine: [position.playedMove],
    resolutionUsed: false,
    pEqualsB: true,
  }
  return {
    positionId: position.id,
    primary: { resetMs: 1, phases: [], verdict: accepted },
    bias: { resetMs: 1, phases: [], verdict: accepted },
    adjudication: { status: 'adjudicated', reference: accepted },
  }
}

const sourceDependencies = {
  gitRevision: () => 'a'.repeat(40),
  gitDirty: () => false,
}

describe('Node corpus CLI contract', () => {
  it('parses an explicit diagnostic slice', () => {
    expect(parseCorpusArgs([
      '--mode',
      'references',
      '--diagnostic',
      '--depth',
      '6',
      '--from',
      '2',
      '--limit',
      '3',
      '--output',
      '/tmp/references.json',
    ])).toEqual({
      mode: 'references',
      diagnostic: true,
      depth: 6,
      from: 2,
      limit: 3,
      output: resolve('/tmp/references.json'),
    })
  })

  it('keeps full current and reference evidence at fixed depths', () => {
    expect(() => parseCorpusArgs([
      '--mode',
      'current',
      '--depth',
      '18',
      '--output',
      'current.jsonl',
    ])).toThrow('--depth requires --diagnostic')
    expect(() => parseCorpusArgs([
      '--mode',
      'references',
      '--depth',
      '25',
      '--output',
      'references.json',
    ])).toThrow('--depth requires --diagnostic')
  })

  it('refuses sliced evidence, unknown/repeated options, and wrong extensions', () => {
    expect(() => parseCorpusArgs([
      '--mode',
      'current',
      '--limit',
      '1',
      '--output',
      'current.jsonl',
    ])).toThrow('--from/--limit require --diagnostic')
    expect(() => parseCorpusArgs([
      '--mode',
      'current',
      '--wat',
      '1',
      '--output',
      'current.jsonl',
    ])).toThrow('unknown option --wat')
    expect(() => parseCorpusArgs([
      '--mode',
      'current',
      '--mode',
      'current',
      '--output',
      'current.jsonl',
    ])).toThrow('option --mode was supplied more than once')
    expect(() => parseCorpusArgs([
      '--mode',
      'references',
      '--output',
      'references.jsonl',
    ])).toThrow('references output must end in .json')
  })

  it('writes and validates the current JSONL path end to end', async () => {
    const output = join(temporaryDirectory(), 'current.jsonl')

    await runCorpusCli([
      '--mode',
      'current',
      '--diagnostic',
      '--depth',
      '6',
      '--limit',
      '2',
      '--output',
      output,
    ], {
      ...sourceDependencies,
      createEngine: async () => fakeEngine(),
      measureCurrent: async (input) => fakeMove(input),
    })

    const records = parseJsonl(readFileSync(output, 'utf8'))
    expect(benchFileProblems(records)).toEqual([])
    expect(records[0]).toMatchObject({
      kind: 'run',
      depth: { baseline: 6, maxDevice: 6, session: 6, requested: 6 },
    })
    expect(records.at(-1)).toMatchObject({
      kind: 'summary',
      completion: 'stopped',
      measuredItems: 2,
    })
    expect(existsSync(corpusCheckpointPath(output))).toBe(false)
  })

  it('writes and validates the reference JSON path end to end', async () => {
    const output = join(temporaryDirectory(), 'references.json')

    await runCorpusCli([
      '--mode',
      'references',
      '--diagnostic',
      '--depth',
      '4',
      '--limit',
      '2',
      '--output',
      output,
    ], {
      ...sourceDependencies,
      createEngine: async () => fakeEngine(),
      adjudicate: async (_engine, position) => fakeReferenceRow(position),
    })

    const artifact = JSON.parse(readFileSync(output, 'utf8')) as ReferenceArtifact
    expect(referenceArtifactProblems(
      artifact,
      loadCorpus().positions,
    )).toEqual([])
    expect(artifact).toMatchObject({
      complete: false,
      depths: {
        primaryRoot: 4,
        primaryRestricted: 5,
        biasRoot: 5,
        biasPostPlayed: 4,
        biasResolution: 5,
      },
      summary: { total: 2, adjudicated: 2 },
    })
    expect(existsSync(corpusCheckpointPath(output))).toBe(false)
  })

  it('resumes the reference writer without rerunning checkpointed rows', async () => {
    const output = join(temporaryDirectory(), 'references.json')
    const calls: string[] = []
    let failSecondRow = true

    const dependencies = {
      ...sourceDependencies,
      createEngine: async () => fakeEngine(),
      adjudicate: async (
        _engine: ReferenceEngine,
        position: CorpusPosition,
      ) => {
        calls.push(position.id)
        if (failSecondRow && calls.length === 2) {
          throw new Error('simulated process interruption')
        }
        return fakeReferenceRow(position)
      },
    }
    const args = [
      '--mode',
      'references',
      '--diagnostic',
      '--depth',
      '4',
      '--limit',
      '2',
      '--output',
      output,
    ] as const

    await expect(runCorpusCli(args, dependencies)).rejects.toThrow(
      'simulated process interruption',
    )
    expect(existsSync(output)).toBe(false)
    expect(existsSync(corpusCheckpointPath(output))).toBe(true)

    failSecondRow = false
    await runCorpusCli(args, dependencies)

    const selectedIds = loadCorpus().positions.slice(0, 2).map((position) => position.id)
    expect(calls).toEqual([selectedIds[0], selectedIds[1], selectedIds[1]])
    expect(existsSync(output)).toBe(true)
    expect(existsSync(corpusCheckpointPath(output))).toBe(false)
  })

  it('publishes a completed current checkpoint without rerunning its rows', async () => {
    const output = join(temporaryDirectory(), 'current.jsonl')
    let engineCreates = 0
    let measured = 0
    const dependencies = {
      ...sourceDependencies,
      createEngine: async () => {
        engineCreates += 1
        return {
          ...fakeEngine(),
          close: () => {
            throw new Error('simulated shutdown interruption')
          },
        }
      },
      measureCurrent: async (input: CurrentMoveRecordInput) => {
        measured += 1
        return fakeMove(input)
      },
    }
    const args = [
      '--mode',
      'current',
      '--diagnostic',
      '--depth',
      '6',
      '--limit',
      '2',
      '--output',
      output,
    ] as const

    await expect(runCorpusCli(args, dependencies)).rejects.toThrow(
      'simulated shutdown interruption',
    )
    expect(measured).toBe(2)
    expect(existsSync(corpusCheckpointPath(output))).toBe(true)

    await runCorpusCli(args, dependencies)

    expect(engineCreates).toBe(1)
    expect(measured).toBe(2)
    expect(benchFileProblems(parseJsonl(readFileSync(output, 'utf8')))).toEqual([])
    expect(existsSync(corpusCheckpointPath(output))).toBe(false)
  })

  it('rejects an existing output before creating the engine', async () => {
    const output = join(temporaryDirectory(), 'current.jsonl')
    writeFileSync(output, 'already here\n')
    let engineCreates = 0

    await expect(runCorpusCli([
      '--mode',
      'current',
      '--diagnostic',
      '--output',
      output,
    ], {
      ...sourceDependencies,
      createEngine: async () => {
        engineCreates += 1
        return fakeEngine()
      },
    })).rejects.toThrow('refusing to overwrite existing artifact')

    expect(engineCreates).toBe(0)
  })

  it('refuses diagnostic output under the committed analysis directory', async () => {
    const output = resolve(
      'docs',
      'analysis',
      `diagnostic-runner-test-${randomUUID()}.jsonl`,
    )
    let engineCreates = 0

    await expect(runCorpusCli([
      '--mode',
      'current',
      '--diagnostic',
      '--output',
      output,
    ], {
      ...sourceDependencies,
      createEngine: async () => {
        engineCreates += 1
        return fakeEngine()
      },
    })).rejects.toThrow('diagnostic output must be outside')

    expect(engineCreates).toBe(0)
    expect(existsSync(output)).toBe(false)
  })
})
