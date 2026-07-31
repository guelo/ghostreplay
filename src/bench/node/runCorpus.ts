#!/usr/bin/env node

import { randomUUID } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { arch, cpus, platform, release, totalmem } from 'node:os'
import { basename, dirname, relative, resolve, sep } from 'node:path'
import { performance } from 'node:perf_hooks'
import { fileURLToPath } from 'node:url'
import {
  BENCH_SCHEMA_VERSION,
  serializeJsonl,
  zeroedDivergences,
  zeroedRejections,
} from '../benchRecord'
import type {
  BenchMoveRecord,
  BenchRecord,
  BenchRunRecord,
} from '../benchRecord'
import { parseJsonl } from '../benchRecord'
import { benchFileProblems } from '../benchFile'
import { summarize } from '../summarize'
import {
  BROWSER_ENGINE_IDENTITY,
  BROWSER_ENGINE_RESOURCES,
} from '../../workers/browserEngineIdentity'
import { corpusSha256, loadCorpus } from './corpus'
import type { CorpusPosition } from './corpus'
import { measureCurrentPosition } from './currentProtocol'
import type {
  CurrentMoveRecordInput,
  CurrentProtocolEngine,
} from './currentProtocol'
import {
  REFERENCE_DEPTHS,
  adjudicatePosition,
} from './references'
import type {
  PositionReferences,
  ReferenceEngine,
  ReferenceDepths,
} from './references'
import {
  REFERENCE_ARTIFACT_SCHEMA_VERSION,
  referenceArtifactProblems,
  summarizeReferences,
} from './referenceArtifact'
import type { ReferenceArtifact, ReferenceSource } from './referenceArtifact'
import { NodeStockfish } from './stockfish'

type Mode = 'current' | 'references'

export type CliConfig = {
  mode: Mode
  output: string
  diagnostic: boolean
  depth: number
  from: number
  limit: number | null
}

type CorpusEngine = CurrentProtocolEngine & ReferenceEngine & {
  close: () => void
}

export type CorpusRunnerDependencies = {
  createEngine: () => Promise<CorpusEngine>
  measureCurrent: (input: CurrentMoveRecordInput) => Promise<BenchMoveRecord>
  adjudicate: (
    engine: ReferenceEngine,
    position: CorpusPosition,
    depths: ReferenceDepths,
  ) => Promise<PositionReferences>
  gitRevision: () => string
  gitDirty: () => boolean
}

const usage = () => [
  'Usage: npm run bench:corpus -- --mode current|references --output PATH [options]',
  '',
  'Options:',
  '  --diagnostic   allow a sliced/shallow run outside docs/analysis (never evidence)',
  '  --depth N      current depth, or diagnostic reference base depth (default 17/4)',
  '  --from N       zero-based corpus offset (diagnostic only)',
  '  --limit N      number of rows (diagnostic only)',
  '',
  'Interrupted runs resume automatically from a validated sibling checkpoint.',
].join('\n')

const intArg = (name: string, raw: string | undefined, minimum: number): number => {
  const value = Number(raw)
  if (!Number.isInteger(value) || value < minimum) {
    throw new Error(`${name} must be an integer >= ${minimum}`)
  }
  return value
}

export const parseCorpusArgs = (args: readonly string[]): CliConfig => {
  const allowed = new Set([
    '--mode',
    '--output',
    '--diagnostic',
    '--depth',
    '--from',
    '--limit',
  ])
  const values = new Map<string, string | true>()
  for (let index = 0; index < args.length; index += 1) {
    const key = args[index]
    if (!key.startsWith('--')) throw new Error(`unexpected argument ${key}\n${usage()}`)
    if (!allowed.has(key)) throw new Error(`unknown option ${key}\n${usage()}`)
    if (values.has(key)) throw new Error(`option ${key} was supplied more than once`)
    if (key === '--diagnostic') {
      values.set(key, true)
      continue
    }
    const value = args[index + 1]
    if (!value || value.startsWith('--')) throw new Error(`${key} needs a value`)
    values.set(key, value)
    index += 1
  }

  const mode = values.get('--mode')
  if (mode !== 'current' && mode !== 'references') {
    throw new Error(`--mode must be current or references\n${usage()}`)
  }
  const output = values.get('--output')
  if (typeof output !== 'string') throw new Error(`--output is required\n${usage()}`)
  const diagnostic = values.get('--diagnostic') === true
  const from = intArg('--from', String(values.get('--from') ?? 0), 0)
  const limit = values.has('--limit')
    ? intArg('--limit', String(values.get('--limit')), 1)
    : null
  if (!diagnostic && (from !== 0 || limit !== null)) {
    throw new Error('--from/--limit require --diagnostic; evidence runs cover the full corpus')
  }
  if (!diagnostic && values.has('--depth')) {
    throw new Error('evidence depths are fixed at current=17 and references=26/27; --depth requires --diagnostic')
  }
  const depthDefault = mode === 'references' && diagnostic ? 4 : 17
  const depth = intArg('--depth', String(values.get('--depth') ?? depthDefault), 1)
  const resolvedOutput = resolve(output)
  const expectedExtension = mode === 'current' ? '.jsonl' : '.json'
  if (!resolvedOutput.endsWith(expectedExtension)) {
    throw new Error(`${mode} output must end in ${expectedExtension}`)
  }
  return {
    mode,
    output: resolvedOutput,
    diagnostic,
    depth,
    from,
    limit,
  }
}

const modulePath = fileURLToPath(import.meta.url)
const repoRoot = resolve(dirname(modulePath), '..', '..', '..')
const analysisDir = resolve(repoRoot, 'docs', 'analysis')

const isUnder = (parent: string, child: string) => {
  const path = relative(parent, child)
  return path === '' || (!path.startsWith(`..${sep}`) && path !== '..')
}

const gitText = (args: string[]): string =>
  execFileSync('git', args, { cwd: repoRoot, encoding: 'utf8' }).trim()

const gitRevision = () => gitText(['rev-parse', 'HEAD'])
const gitDirty = () => gitText(['status', '--porcelain', '--untracked-files=all']).length > 0
const osLabel = () => `${platform()} ${release()} ${arch()}`
const cpuLabel = () => cpus()[0]?.model ?? 'unknown CPU'

const defaultDependencies: CorpusRunnerDependencies = {
  createEngine: () => NodeStockfish.create(),
  measureCurrent: measureCurrentPosition,
  adjudicate: adjudicatePosition,
  gitRevision,
  gitDirty,
}

type SourceSnapshot = {
  gitRevision: string
  gitDirty: boolean
}

type CheckpointBinding = {
  mode: Mode
  diagnostic: boolean
  depth: number
  from: number
  limit: number | null
  corpusSha256: string
  selectedPositionIds: string[]
  source: SourceSnapshot
  referenceDepths: ReferenceDepths | null
}

type CurrentCheckpoint = {
  kind: 'ghostreplay-grade-corpus-checkpoint'
  schemaVersion: 1
  mode: 'current'
  binding: CheckpointBinding
  elapsedMs: number
  header: BenchRunRecord
  moves: BenchMoveRecord[]
}

type ReferenceCheckpoint = {
  kind: 'ghostreplay-grade-corpus-checkpoint'
  schemaVersion: 1
  mode: 'references'
  binding: CheckpointBinding
  createdAtIso: string
  source: ReferenceSource
  depths: ReferenceDepths
  rows: PositionReferences[]
}

type CorpusCheckpoint = CurrentCheckpoint | ReferenceCheckpoint

const atomicWrite = (path: string, bytes: string) => {
  mkdirSync(dirname(path), { recursive: true })
  const temporary = `${path}.tmp-${process.pid}-${randomUUID()}`
  try {
    writeFileSync(temporary, bytes, { flag: 'wx' })
    renameSync(temporary, path)
  } finally {
    if (existsSync(temporary)) rmSync(temporary)
  }
}

const assertNewOutput = (path: string) => {
  if (existsSync(path)) throw new Error(`refusing to overwrite existing artifact ${path}`)
}

const writeNewFile = (path: string, bytes: string) => {
  assertNewOutput(path)
  atomicWrite(path, bytes)
}

export const corpusCheckpointPath = (output: string): string =>
  resolve(dirname(output), `.${basename(output)}.checkpoint.json`)

const canonicalJson = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (value !== null && typeof value === 'object') {
    return `{${Object.keys(value as Record<string, unknown>)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson((value as Record<string, unknown>)[key])}`)
      .join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}

const writeCheckpoint = (path: string, checkpoint: CorpusCheckpoint) => {
  atomicWrite(path, `${JSON.stringify(checkpoint, null, 2)}\n`)
}

const readCheckpoint = (path: string): unknown => {
  try {
    return JSON.parse(readFileSync(path, 'utf8')) as unknown
  } catch (error) {
    throw new Error(
      `cannot read checkpoint ${path}: ${error instanceof Error ? error.message : String(error)}`,
    )
  }
}

const failedMove = (input: {
  runId: string
  seq: number
  position: CorpusPosition
  depth: number
  cohort: 'cold' | 'warm'
  runElapsedMs: number
  workerBootMs: number | null
  e2eMs: number
  error: string
}): BenchMoveRecord => ({
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
  e2eMs: input.e2eMs,
  runElapsedMs: input.runElapsedMs,
  workerBootMs: input.workerBootMs,
  resetMs: null,
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
  error: input.error,
})

const selectedPositions = (config: CliConfig): {
  all: CorpusPosition[]
  selected: CorpusPosition[]
} => {
  const all = loadCorpus().positions
  const end = config.limit === null ? all.length : config.from + config.limit
  return { all, selected: all.slice(config.from, end) }
}

const diagnosticDepths = (base: number): ReferenceDepths => ({
  primaryRoot: base,
  primaryRestricted: base + 1,
  biasRoot: base + 1,
  biasPostPlayed: base,
  biasResolution: base + 1,
})

const referenceSource = (source: SourceSnapshot): ReferenceSource => ({
  ...source,
  corpusSha256: corpusSha256(),
  engineVersion: BROWSER_ENGINE_IDENTITY.engine_version,
  engineBuild: BROWSER_ENGINE_IDENTITY.engine_build,
  evalFileId: BROWSER_ENGINE_IDENTITY.eval_file_id,
  npmPackage: 'stockfish@18.0.7',
  hashMb: 128,
  threads: 1,
  nodeVersion: process.version,
  os: osLabel(),
})

const buildBinding = (
  config: CliConfig,
  selected: readonly CorpusPosition[],
  source: SourceSnapshot,
  depths: ReferenceDepths | null,
): CheckpointBinding => ({
  mode: config.mode,
  diagnostic: config.diagnostic,
  depth: config.depth,
  from: config.from,
  limit: config.limit,
  corpusSha256: corpusSha256(),
  selectedPositionIds: selected.map((position) => position.id),
  source,
  referenceDepths: depths,
})

const checkpointBaseProblems = (
  value: unknown,
  mode: Mode,
  binding: CheckpointBinding,
): string[] => {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return ['checkpoint is not an object']
  }
  const record = value as Record<string, unknown>
  const problems: string[] = []
  if (record.kind !== 'ghostreplay-grade-corpus-checkpoint') {
    problems.push('checkpoint kind is invalid')
  }
  if (record.schemaVersion !== 1) problems.push('checkpoint schemaVersion is invalid')
  if (record.mode !== mode) problems.push(`checkpoint mode is not ${mode}`)
  if (canonicalJson(record.binding) !== canonicalJson(binding)) {
    problems.push('checkpoint does not match this corpus, source, depth, or slice')
  }
  return problems
}

const loadCurrentCheckpoint = (
  path: string,
  binding: CheckpointBinding,
  selected: readonly CorpusPosition[],
): CurrentCheckpoint | null => {
  if (!existsSync(path)) return null
  const value = readCheckpoint(path)
  const problems = checkpointBaseProblems(value, 'current', binding)
  if (value === null || typeof value !== 'object' || Array.isArray(value) ||
      problems.length > 0) {
    throw new Error(`refusing incompatible checkpoint ${path}:\n- ${problems.join('\n- ')}`)
  }
  const candidate = value as CurrentCheckpoint
  if (!Array.isArray(candidate.moves)) {
    problems.push('current checkpoint moves are missing')
  } else {
    try {
      parseJsonl(serializeJsonl([candidate.header, ...candidate.moves]))
    } catch (error) {
      problems.push(
        `current checkpoint records are invalid: ${
          error instanceof Error ? error.message : String(error)
        }`,
      )
    }
    for (let index = 0; index < candidate.moves.length; index += 1) {
      const move = candidate.moves[index]
      if (index >= selected.length ||
          move?.seq !== index ||
          move?.positionId !== selected[index].id ||
          move?.runId !== candidate.header?.runId) {
        problems.push(`current checkpoint row ${index} is not the selected corpus prefix`)
        break
      }
    }
  }
  if (candidate.header?.source?.gitRevision !== binding.source.gitRevision ||
      candidate.header?.source?.gitDirty !== binding.source.gitDirty) {
    problems.push('current checkpoint header source does not match its binding')
  }
  if (candidate.header?.depth?.requested !== binding.depth) {
    problems.push('current checkpoint header depth does not match its binding')
  }
  if (!Number.isFinite(candidate.elapsedMs) || candidate.elapsedMs < 0) {
    problems.push('current checkpoint elapsedMs is invalid')
  }
  if (problems.length > 0) {
    throw new Error(`refusing incompatible checkpoint ${path}:\n- ${problems.join('\n- ')}`)
  }
  return candidate
}

const loadReferenceCheckpoint = (
  path: string,
  binding: CheckpointBinding,
  all: readonly CorpusPosition[],
  selected: readonly CorpusPosition[],
  source: ReferenceSource,
  depths: ReferenceDepths,
): ReferenceCheckpoint | null => {
  if (!existsSync(path)) return null
  const value = readCheckpoint(path)
  const problems = checkpointBaseProblems(value, 'references', binding)
  if (value === null || typeof value !== 'object' || Array.isArray(value) ||
      problems.length > 0) {
    throw new Error(`refusing incompatible checkpoint ${path}:\n- ${problems.join('\n- ')}`)
  }
  const candidate = value as ReferenceCheckpoint
  if (!Array.isArray(candidate.rows)) {
    problems.push('reference checkpoint rows are missing')
  } else {
    for (let index = 0; index < candidate.rows.length; index += 1) {
      if (index >= selected.length ||
          candidate.rows[index]?.positionId !== selected[index].id) {
        problems.push(`reference checkpoint row ${index} is not the selected corpus prefix`)
        break
      }
    }
    try {
      const partial: ReferenceArtifact = {
        kind: 'ghostreplay-grade-references',
        schemaVersion: REFERENCE_ARTIFACT_SCHEMA_VERSION,
        complete: false,
        createdAtIso: candidate.createdAtIso,
        source: candidate.source,
        depths: candidate.depths,
        rows: candidate.rows,
        summary: summarizeReferences(candidate.rows, all),
      }
      problems.push(...referenceArtifactProblems(partial, all))
    } catch (error) {
      problems.push(
        `reference checkpoint rows are invalid: ${
          error instanceof Error ? error.message : String(error)
        }`,
      )
    }
  }
  if (canonicalJson(candidate.source) !== canonicalJson(source)) {
    problems.push('reference checkpoint source metadata changed')
  }
  if (canonicalJson(candidate.depths) !== canonicalJson(depths)) {
    problems.push('reference checkpoint depths changed')
  }
  if (problems.length > 0) {
    throw new Error(`refusing incompatible checkpoint ${path}:\n- ${problems.join('\n- ')}`)
  }
  return candidate
}

const assertSourceUnchanged = (
  config: CliConfig,
  source: SourceSnapshot,
  dependencies: CorpusRunnerDependencies,
) => {
  if (config.diagnostic) return
  const current: SourceSnapshot = {
    gitRevision: dependencies.gitRevision(),
    gitDirty: dependencies.gitDirty(),
  }
  if (canonicalJson(current) !== canonicalJson(source)) {
    throw new Error(
      'git source changed during the evidence run; final artifact was not published and the checkpoint was retained',
    )
  }
}

const currentHeader = (
  config: CliConfig,
  all: readonly CorpusPosition[],
  source: SourceSnapshot,
): BenchRunRecord => {
  const runId = randomUUID()
  const warnings = [
    'Node corpus timing is desktop diagnostics only; it is never mobile evidence.',
    'One deterministic correctness pass; §10.4 device repeat/cooling rules do not apply.',
    ...(config.diagnostic ? ['Diagnostic slice/depth: completion is stopped and not evidence.'] : []),
  ]
  return {
    kind: 'run',
    schemaVersion: BENCH_SCHEMA_VERSION,
    runId,
    harness: 'node',
    build: 'bundled',
    startedAtIso: new Date().toISOString(),
    engine: {
      engineVersion: BROWSER_ENGINE_IDENTITY.engine_version,
      engineBuild: BROWSER_ENGINE_IDENTITY.engine_build,
      evalFileId: BROWSER_ENGINE_IDENTITY.eval_file_id,
      threads: BROWSER_ENGINE_RESOURCES.threads,
      hashMb: BROWSER_ENGINE_RESOURCES.hash_mb,
    },
    depth: {
      baseline: config.depth,
      maxDevice: config.depth,
      session: config.depth,
      requested: config.depth,
    },
    environment: {
      userAgent: null,
      uaData: null,
      hardwareConcurrency: cpus().length,
      deviceMemory: totalmem() / (1024 ** 3),
      platform: osLabel(),
      screen: null,
      devicePixelRatio: null,
      timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      nodeVersion: process.version,
      os: osLabel(),
    },
    source: {
      gitRevision: source.gitRevision,
      gitDirty: source.gitDirty,
      workerBundleFile: 'node_modules/stockfish/bin/stockfish-18-lite-single.wasm',
      workerBundleSha256: BROWSER_ENGINE_IDENTITY.engine_build,
    },
    device: {
      label: `${cpuLabel()} · Node ${process.version} · ${osLabel()}`,
      notes: `corpus_sha256=${corpusSha256()}`,
    },
    plan: {
      mode: 'corpus',
      arms: ['current'],
      repeats: 1,
      positionSetId: config.diagnostic ? 'grade-corpus-v1-diagnostic' : 'grade-corpus-v1',
      positionCount: all.length,
      blockCount: 1,
      plannedItems: all.length,
      warmup: false,
      armOrderBalanced: true,
      blockCooldownMs: 0,
    },
    methodWarnings: warnings,
    countersAre: 'observer-recomputed',
  }
}

const runCurrent = async (
  config: CliConfig,
  dependencies: CorpusRunnerDependencies,
  source: SourceSnapshot,
) => {
  const { all, selected } = selectedPositions(config)
  if (selected.length === 0) throw new Error('selected corpus slice is empty')
  const checkpointPath = corpusCheckpointPath(config.output)
  const binding = buildBinding(config, selected, source, null)
  const restored = loadCurrentCheckpoint(checkpointPath, binding, selected)
  const checkpoint: CurrentCheckpoint = restored ?? {
    kind: 'ghostreplay-grade-corpus-checkpoint',
    schemaVersion: 1,
    mode: 'current',
    binding,
    elapsedMs: 0,
    header: currentHeader(config, all, source),
    moves: [],
  }
  const resumed = checkpoint.moves.length > 0
  if (resumed) {
    const warning =
      'Capture resumed from an atomic per-row checkpoint; desktop timing spans engine boots.'
    if (!checkpoint.header.methodWarnings.includes(warning)) {
      checkpoint.header.methodWarnings.push(warning)
      checkpoint.header.device.notes += '; checkpoint_resumed=true'
    }
  }
  writeCheckpoint(checkpointPath, checkpoint)

  const firstNewIndex = checkpoint.moves.length
  const runStarted = performance.now() - checkpoint.elapsedMs
  if (firstNewIndex < selected.length) {
    const bootStarted = performance.now()
    const engine = await dependencies.createEngine()
    const bootMs = performance.now() - bootStarted
    try {
      for (let index = firstNewIndex; index < selected.length; index += 1) {
        const corpusIndex = config.from + index
        const position = selected[index]
        const restarted = resumed && index === firstNewIndex
        const cohort = corpusIndex === 0 || restarted ? 'cold' : 'warm'
        const rowBootMs = corpusIndex === 0 || restarted ? bootMs : null
        process.stdout.write(
          `[${corpusIndex + 1}/${all.length}] current ${position.id}\n`,
        )
        const started = performance.now()
        let move: BenchMoveRecord
        try {
          move = await dependencies.measureCurrent({
            engine,
            position,
            depth: config.depth,
            runId: checkpoint.header.runId,
            seq: index,
            runElapsedMs: started - runStarted,
            cohort,
            workerBootMs: rowBootMs,
          })
          if (restarted) {
            move = {
              ...move,
              cohort: 'cold',
              workerRestarted: true,
              workerBootMs: bootMs,
            }
          }
        } catch (error) {
          move = failedMove({
            runId: checkpoint.header.runId,
            seq: index,
            position,
            depth: config.depth,
            cohort,
            runElapsedMs: started - runStarted,
            workerBootMs: rowBootMs,
            e2eMs: performance.now() - started,
            error: error instanceof Error ? error.message : String(error),
          })
          if (restarted) move = { ...move, workerRestarted: true }
        }
        checkpoint.moves.push(move)
        checkpoint.elapsedMs = performance.now() - runStarted
        writeCheckpoint(checkpointPath, checkpoint)
      }
    } finally {
      engine.close()
    }
  }

  const summary = summarize(checkpoint.header.runId, checkpoint.moves, {
    completion: config.diagnostic ? 'stopped' : 'complete',
    plannedItems: all.length,
    planWarnings: checkpoint.header.methodWarnings,
  })
  const records: BenchRecord[] = [checkpoint.header, ...checkpoint.moves, summary]
  const bytes = serializeJsonl(records)
  const parsed = parseJsonl(bytes)
  const problems = benchFileProblems(parsed)
  if (problems.length > 0) {
    throw new Error(`refusing invalid JSONL:\n- ${problems.join('\n- ')}`)
  }
  assertSourceUnchanged(config, source, dependencies)
  writeNewFile(config.output, bytes)
  rmSync(checkpointPath)
}

const runReferences = async (
  config: CliConfig,
  dependencies: CorpusRunnerDependencies,
  sourceSnapshot: SourceSnapshot,
) => {
  const { all, selected } = selectedPositions(config)
  if (selected.length === 0) throw new Error('selected corpus slice is empty')
  const depths = config.diagnostic ? diagnosticDepths(config.depth) : REFERENCE_DEPTHS
  const source = referenceSource(sourceSnapshot)
  const checkpointPath = corpusCheckpointPath(config.output)
  const binding = buildBinding(config, selected, sourceSnapshot, depths)
  const checkpoint: ReferenceCheckpoint = loadReferenceCheckpoint(
    checkpointPath,
    binding,
    all,
    selected,
    source,
    depths,
  ) ?? {
    kind: 'ghostreplay-grade-corpus-checkpoint',
    schemaVersion: 1,
    mode: 'references',
    binding,
    createdAtIso: new Date().toISOString(),
    source,
    depths,
    rows: [],
  }
  writeCheckpoint(checkpointPath, checkpoint)

  if (checkpoint.rows.length < selected.length) {
    const engine = await dependencies.createEngine()
    try {
      for (let index = checkpoint.rows.length; index < selected.length; index += 1) {
        const corpusIndex = config.from + index
        const position = selected[index]
        process.stdout.write(
          `[${corpusIndex + 1}/${all.length}] references ${position.id}\n`,
        )
        const row = await dependencies.adjudicate(engine, position, depths)
        if (row.positionId !== position.id) {
          throw new Error(
            `reference row ${row.positionId} does not match selected position ${position.id}`,
          )
        }
        checkpoint.rows.push(row)
        writeCheckpoint(checkpointPath, checkpoint)
      }
    } finally {
      engine.close()
    }
  }

  const artifact: ReferenceArtifact = {
    kind: 'ghostreplay-grade-references',
    schemaVersion: REFERENCE_ARTIFACT_SCHEMA_VERSION,
    complete: !config.diagnostic,
    createdAtIso: checkpoint.createdAtIso,
    source: checkpoint.source,
    depths,
    rows: checkpoint.rows,
    summary: summarizeReferences(checkpoint.rows, all),
  }
  const problems = referenceArtifactProblems(artifact, all)
  if (problems.length > 0) {
    throw new Error(`refusing invalid reference artifact:\n- ${problems.join('\n- ')}`)
  }
  assertSourceUnchanged(config, sourceSnapshot, dependencies)
  writeNewFile(config.output, `${JSON.stringify(artifact, null, 2)}\n`)
  rmSync(checkpointPath)
}

export const runCorpusCli = async (
  args: readonly string[],
  overrides: Partial<CorpusRunnerDependencies> = {},
) => {
  const config = parseCorpusArgs(args)
  assertNewOutput(config.output)
  if (config.diagnostic && isUnder(analysisDir, config.output)) {
    throw new Error(`diagnostic output must be outside ${analysisDir}`)
  }
  if (!config.diagnostic && !isUnder(analysisDir, config.output)) {
    throw new Error(`evidence output must be under ${analysisDir}`)
  }
  const dependencies = { ...defaultDependencies, ...overrides }
  const source: SourceSnapshot = {
    gitRevision: dependencies.gitRevision(),
    gitDirty: dependencies.gitDirty(),
  }
  if (!config.diagnostic && source.gitDirty) {
    throw new Error(
      'evidence runs require a clean git tree so source.gitRevision identifies every input byte',
    )
  }
  if (config.mode === 'current') {
    await runCurrent(config, dependencies, source)
  } else {
    await runReferences(config, dependencies, source)
  }
  process.stdout.write(`wrote ${config.output}\n`)
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(modulePath)) {
  runCorpusCli(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`)
    process.exitCode = 1
  })
}
