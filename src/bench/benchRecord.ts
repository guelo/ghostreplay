/**
 * The ONE benchmark record schema, shared by both harnesses (g-two-search-grade
 * §10.1).
 *
 * The browser device runner (g-grade-device-runner) and the Node corpus harness
 * (g-grade-corpus-harness) emit the SAME newline-delimited JSON so their results
 * are directly comparable. Comparability is obtained by construction — both
 * import these types and `validateBenchRecord` — not by two hand-kept copies of a
 * schema drifting apart across two beads.
 *
 * §15.2 KEEPS this module after a rejection verdict: the harnesses, the corpus,
 * the references, and the JSONL results all survive the prototype.
 */

import type { SnapshotDivergenceReason, SnapshotRejection } from '../workers/pvSnapshots'
import { SNAPSHOT_REJECTIONS } from '../workers/pvSnapshots'
import type { AnalysisStopReason } from '../workers/analysisMessages'
import type { MoveClassification } from '../workers/analysisUtils'

/**
 * Bumped whenever a field changes meaning or disappears. Readers must refuse an
 * unknown version rather than silently misreading an older run — a benchmark file
 * outlives the code that wrote it, and a misread row is a wrong adoption verdict.
 *
 * v2: `gameWeightedMedianMs` (a weighted average of the two splits' medians)
 * became `gameWeighted`, which reports median/p90/p95/worst as quantiles of ONE
 * defined distribution — §11's gate names a p95 improvement, and an average of
 * two p95s is not a p95 of anything.
 */
export const BENCH_SCHEMA_VERSION = 2

/** Which harness produced the row. Node timing is never mobile evidence (§10.1). */
export type BenchHarness = 'device' | 'node'

/**
 * Whether the measured worker came from a production Rollup build or from the
 * dev server's unbundled ESM.
 *
 * Recorded rather than assumed: §10.1 requires the device runner to load the
 * ACTUAL BUNDLED worker, and a dev-server run is a convenience check, not a
 * measurement. Keeping the distinction in the data means a dev run can never be
 * quoted as a device baseline by mistake.
 */
export type BenchBuildMode = 'bundled' | 'dev'

/**
 * The protocol arm a row was produced by. `current` is the shipping three-search
 * protocol; the candidate arms are added by g-grade-kill-gate / g-grade-variant-b
 * and are refused by the runner until the worker acknowledges bench mode (§15.1
 * C7), so a row can never be labelled with an arm that did not actually run.
 */
export type BenchArm = 'current' | 'variantA' | 'variantB'

/** Every arm the schema can label a row with, for validation and for UI order. */
export const BENCH_ARMS: readonly BenchArm[] = ['current', 'variantA', 'variantB']

/** First move on a fresh worker (cold WASM/JIT) versus every later move. */
export type BenchCohort = 'cold' | 'warm'

/** Which of the analyze-move's up-to-three searches a phase is. */
export type BenchPhaseName = 'root' | 'post-played' | 'post-best' | 'other'

export type BenchSnapshotOutcome =
  | { accepted: true; depth: number }
  | { accepted: false; reason: SnapshotRejection }

/**
 * Where the measured bundle came from.
 *
 * A committed baseline outlives the branch that produced it, and §11's gate
 * compares two protocols by their numbers — so a file that cannot name the
 * orchestration bytes it measured is not evidence, it is an anecdote. The git
 * revision is injected at build time (so a phone run carries it too); the digest
 * is added by the scripted driver, which can read `dist`.
 *
 * `gitDirty` matters as much as the revision: a dirty tree means the revision
 * under-specifies the bundle, and the run is a working-copy measurement.
 */
export type BenchSourceStamp = {
  gitRevision: string | null
  gitDirty: boolean | null
  /** The built worker chunk, whose filename carries Rollup's content hash. */
  workerBundleFile: string | null
  workerBundleSha256: string | null
}

/**
 * One search of one analyze-move, as observed on the worker's own UCI transcript.
 *
 * `timeMs`/`nodes` are the ENGINE's own counters from the last info line of the
 * phase; `wallMs` is the host clock across the same `go`→`bestmove` window. Both
 * are kept: the engine clock is the comparable per-phase cost, the host clock
 * carries the postMessage and scheduling overhead a user actually waits through.
 */
export type BenchPhaseRecord = {
  index: number
  name: BenchPhaseName
  /** The `moves` segment of the phase's `position` command; `[]` for the root. */
  moves: string[]
  requestedDepth: number | null
  bestmove: string | null
  nodes: number | null
  timeMs: number | null
  nps: number | null
  hashfull: number | null
  reachedDepth: number | null
  seldepth: number | null
  wallMs: number
  infoLines: number
  /** Info lines that passed §4's score+pv+exact filter into snapshot assembly. */
  admittedLines: number
  /**
   * Whether the engine actually answered `bestmove` for this search.
   *
   * False means the phase was still open when the move ended — a worker error or
   * a harness timeout. §4 acceptance is then UNDEFINED for it, not failed.
   */
  terminated: boolean
  /**
   * §4.2 acceptance recomputed from the observed transcript, or null when the
   * search never terminated.
   *
   * Null rather than a rejection reason: judging a truncated search against the
   * requested depth would manufacture rejection counts the worker never
   * recorded, and those counts feed §12 step 9's adoption decision.
   */
  snapshot: BenchSnapshotOutcome | null
  /**
   * How the legacy accumulators and the atomic selector disagreed on this
   * search, or null when they agreed — or when the search never terminated, for
   * the same reason `snapshot` is null (§4.3). Kept as the REASON, not a
   * boolean, because §10.4 reports `legacy_selector_divergence` split by
   * rejection reason.
   */
  legacyDivergence: SnapshotDivergenceReason | null
  /** True when a `stop` was observed while this phase was open. */
  stopObserved: boolean
}

/** The worker's `analysis` response, verbatim. */
export type BenchAnalysisResult = {
  bestMove: string
  bestLine: string[]
  bestEval: number | null
  playedEval: number | null
  bestEvalMate: number | null
  playedEvalMate: number | null
  delta: number | null
  classification: MoveClassification | null
  canonical: boolean
  capFired: boolean
  stopReason: AnalysisStopReason
  reachedDepth: number | null
}

export type BenchEnvironment = {
  userAgent: string | null
  /** navigator.userAgentData brands/platform when the browser exposes them. */
  uaData: string | null
  hardwareConcurrency: number | null
  /** Chromium-only; `null` means UNKNOWN, never "small" (deviceAnalysisTier). */
  deviceMemory: number | null
  platform: string | null
  screen: string | null
  devicePixelRatio: number | null
  timeZone: string | null
  /** Node harness only. */
  nodeVersion?: string | null
  os?: string | null
}

export type BenchRunRecord = {
  kind: 'run'
  schemaVersion: number
  runId: string
  harness: BenchHarness
  build: BenchBuildMode
  startedAtIso: string
  /** The engine artifact and its fixed resources (browserEngineIdentity.ts). */
  engine: {
    engineVersion: string
    engineBuild: string
    evalFileId: string
    threads: number
    hashMb: number
  }
  depth: {
    baseline: number
    maxDevice: number
    /** What `sessionAnalysisDepth()` picked on this device. */
    session: number
    /** What the runner actually asked for (usually the session depth). */
    requested: number
  }
  environment: BenchEnvironment
  /** Which orchestration bytes this run measured. */
  source: BenchSourceStamp
  device: {
    /** Operator-supplied hardware/OS/browser label, e.g. "iPhone 15, iOS 18.4, Safari". */
    label: string
    notes: string
  }
  plan: {
    mode: string
    arms: BenchArm[]
    repeats: number
    positionSetId: string
    positionCount: number
    blockCount: number
    /** Measurements the plan calls for — compare with the summary's actual count. */
    plannedItems: number
    /** A discarded priming measurement precedes the set, so every row is warm. */
    warmup: boolean
    /**
     * Whether the arm order is actually counterbalanced (§10.4). Rotation only
     * balances when `repeats` is a multiple of the arm count: 3 repeats over 2
     * arms gives one arm the first slot twice, which confounds protocol with
     * accumulated heat on a throttling device.
     */
    armOrderBalanced: boolean
    /** Idle time between blocks, so heat does not carry across arms. */
    blockCooldownMs: number
  }
  /**
   * Ways this run departs from §10.4's method, computed from the plan.
   *
   * Present in the header so a crashed run (no summary row) still says whether it
   * was method-valid. The summary repeats these and adds outcome-level ones.
   */
  methodWarnings: string[]
  /**
   * §4 counters here are OBSERVER-side: recomputed from the worker's logged UCI
   * transcript with the same `pvSnapshots` module the worker runs, not read out
   * of the worker's own counter instance (which it does not export). The inputs
   * are identical; the note exists so a reader never mistakes them for a
   * worker-internal readout.
   */
  countersAre: 'observer-recomputed'
}

export type BenchMoveRecord = {
  kind: 'move'
  schemaVersion: number
  runId: string
  seq: number
  blockIndex: number
  repeat: number
  arm: BenchArm
  /** The arm's slot within this repeat — §10.4's counterbalanced protocol order. */
  orderIndex: number
  positionId: string
  fen: string
  playedMove: string
  playerColor: 'white' | 'black'
  /** 1-based index within a thermal sequence, for the latency-by-move-index graph. */
  thermalIndex: number | null
  cohort: BenchCohort
  /**
   * A priming measurement that exists only to warm the worker, so the position it
   * duplicates gets a warm row too. Excluded from every summary statistic.
   */
  warmup: boolean
  /**
   * This measurement ran on a freshly constructed engine after a failure on the
   * row before it — either because the harness rebuilt the worker (timeout, fatal
   * error) or because the worker rebuilt its own engine (`engineRebuilt` below).
   *
   * Such a row's engine is genuinely cold whatever the schedule said, so `cohort`
   * is forced to `cold` here and this flag records that the cohort came from a
   * failure recovery rather than from the plan.
   */
  workerRestarted: boolean
  /**
   * The worker destroyed and rebuilt its own Stockfish sub-worker DURING this
   * measurement — its deadline-grace or reset-timeout path (`analysisWorker.ts`
   * `destroyEngine`).
   *
   * Read off the transcript, because the worker reports that failure as a
   * REQUEST-scoped error: without it the next row's brand-new engine would be
   * recorded as `warm` with no boot cost, which is the same warm-median inflation
   * a harness-side rebuild used to cause. The row after this one is therefore
   * `cold` with `workerRestarted: true`.
   */
  engineRebuilt: boolean
  requestedDepth: number
  /** Host clock: analyze-move postMessage → `analysis` response. */
  e2eMs: number
  /**
   * Host clock from the run's start to this measurement's start.
   *
   * The thermal axis: it is how much heat the device has had time to accumulate,
   * which `thermalIndex` alone does not say once a run has several blocks.
   */
  runElapsedMs: number
  /** Worker construction → `ready`. Non-null on a block's cold row and after a restart. */
  workerBootMs: number | null
  /** `ucinewgame` → `readyok` for this request, from the transcript. */
  resetMs: number | null
  phases: BenchPhaseRecord[]
  searchCount: number
  totalNodes: number | null
  totalEngineMs: number | null
  result: BenchAnalysisResult | null
  /** `result.bestMove === playedMove` — the §3.4 cost model's `m`. */
  pEqualsB: boolean | null
  rejections: Record<SnapshotRejection, number>
  legacySelectorDivergence: number
  divergenceByReason: Record<SnapshotDivergenceReason, number>
  /** Liveness pings observed, proving the heartbeat ran (§10.1). */
  progressPings: number
  streamingPings: number
  error: string | null
}

export type BenchLatencyStats = {
  n: number
  medianMs: number
  p90Ms: number
  p95Ms: number
  worstMs: number
  medianNodes: number | null
}

/**
 * End-to-end latency over the GAME-WEIGHTED distribution — §10.4's "game-weighted
 * at observed m", and what §11's performance gate ("median improvement at least
 * 25%", "p95 improvement at least 20%") is read off.
 *
 * ONE distribution, not four separately-derived numbers: the warm P===B rows
 * carry total weight `m` and the warm P!==B rows total weight `1 - m`, and every
 * statistic below is a quantile of that mixture. The previous schema reported
 * only a median, as `m * median(P===B) + (1 - m) * median(P!==B)` — a defensible
 * expected cost, but not a quantile, so it had no honest p95 counterpart and a
 * reader computing one would get a materially different number depending on which
 * construction they picked. Fixing the estimator in the file removes that choice.
 *
 * Because `m` is observed on the same warm rows, the mixture is the pooled warm
 * sample and these values equal the `warm`/`all` cell's by construction. That is
 * the point rather than a redundancy: the gate has one named place to read, and
 * the equality is what says the weighting was not applied twice.
 *
 * Every statistic is null when either split is empty — an all-cold run, or a set
 * on which the engine never disagreed — rather than degrading to a one-sided
 * average that would read as a game.
 */
export type BenchGameWeighted = {
  arm: BenchArm
  /** The mixture weight actually used: the warm rows' observed P===B share. */
  m: number | null
  /** Warm rows the mixture is built from. */
  n: number
  medianMs: number | null
  p90Ms: number | null
  p95Ms: number | null
  worstMs: number | null
}

/**
 * Whether the run finished its plan.
 *
 * A partial run's numbers can look perfectly ordinary — the same medians over a
 * subset of the schedule — so the file has to say so itself. Only `complete` is
 * quotable as a baseline.
 */
export type BenchCompletion = 'complete' | 'stopped'

export type BenchSummaryRecord = {
  kind: 'summary'
  schemaVersion: number
  runId: string
  completion: BenchCompletion
  /** From the plan, versus what actually got measured (warm-up rows excluded). */
  plannedItems: number
  measuredItems: number
  warmupItems: number
  /** The header's plan warnings plus any the outcome added (§10.4). */
  methodWarnings: string[]
  /** One entry per (arm × cohort × P===B split), plus `all` roll-ups. */
  cells: Array<{
    arm: BenchArm
    cohort: BenchCohort | 'all'
    split: 'p-equals-b' | 'p-differs' | 'all'
    stats: BenchLatencyStats
  }>
  /**
   * Observed P===B share over non-error warm rows, per arm — null when there are
   * no warm rows to observe it on (an all-cold run).
   *
   * Explicitly nullable rather than NaN: `JSON.stringify` writes NaN as `null`,
   * so a `number`-typed field would have made every all-cold file silently
   * schema-invalid and indistinguishable from a real zero-information result.
   */
  observedMatchRate: Array<{ arm: BenchArm; m: number | null; n: number }>
  /** §11's performance gate, per arm — see `BenchGameWeighted`. */
  gameWeighted: BenchGameWeighted[]
  rejections: Record<SnapshotRejection, number>
  legacySelectorDivergence: number
  divergenceByReason: Record<SnapshotDivergenceReason, number>
  errors: number
}

export type BenchRecord = BenchRunRecord | BenchMoveRecord | BenchSummaryRecord

export const zeroedRejections = (): Record<SnapshotRejection, number> =>
  SNAPSHOT_REJECTIONS.reduce(
    (acc, reason) => {
      acc[reason] = 0
      return acc
    },
    {} as Record<SnapshotRejection, number>,
  )

export const zeroedDivergences = (): Record<SnapshotDivergenceReason, number> => ({
  ...zeroedRejections(),
  accepted: 0,
})

/** Sum b into a, in place, over every key of a. */
export const addCounters = <K extends string>(
  a: Record<K, number>,
  b: Record<K, number>,
): Record<K, number> => {
  for (const key of Object.keys(a) as K[]) {
    a[key] += b[key] ?? 0
  }
  return a
}

export const serializeJsonl = (records: readonly BenchRecord[]): string =>
  records.map((record) => JSON.stringify(record)).join('\n') + (records.length > 0 ? '\n' : '')

/**
 * Parse a JSONL benchmark file, validating every row.
 *
 * Throws on the first bad row rather than skipping it: a benchmark file with a
 * silently dropped row produces a plausible-looking summary over an unknown
 * subset, which is worse than no summary at all.
 */
export const parseJsonl = (text: string): BenchRecord[] =>
  text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
    .map((line, index) => {
      let parsed: unknown
      try {
        parsed = JSON.parse(line)
      } catch {
        throw new Error(`bench JSONL line ${index + 1}: not valid JSON`)
      }
      const error = validateBenchRecord(parsed)
      if (error) {
        throw new Error(`bench JSONL line ${index + 1}: ${error}`)
      }
      return parsed as BenchRecord
    })

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const isFinite_ = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value)

/** Structural checks that read as a list rather than as nested `if`s. */
type FieldCheck = (value: unknown) => boolean

const CHECKS = {
  string: (value: unknown) => typeof value === 'string',
  nonEmptyString: (value: unknown) => typeof value === 'string' && value.length > 0,
  stringOrNull: (value: unknown) => value === null || typeof value === 'string',
  boolean: (value: unknown) => typeof value === 'boolean',
  booleanOrNull: (value: unknown) => value === null || typeof value === 'boolean',
  number: isFinite_,
  numberOrNull: (value: unknown) => value === null || isFinite_(value),
  integer: (value: unknown) => typeof value === 'number' && Number.isInteger(value),
  nonNegativeInteger: (value: unknown) =>
    typeof value === 'number' && Number.isInteger(value) && value >= 0,
  arm: (value: unknown) => BENCH_ARMS.includes(value as BenchArm),
  stringArray: (value: unknown) => Array.isArray(value) && value.every((item) => typeof item === 'string'),
  object: isRecord,
} satisfies Record<string, FieldCheck>

/** First field of `shape` that `value` does not satisfy, as a reason string. */
const checkFields = (
  where: string,
  value: Record<string, unknown>,
  shape: Record<string, FieldCheck>,
): string | null => {
  for (const [field, check] of Object.entries(shape)) {
    if (!check(value[field])) {
      return `${where}.${field} is missing or malformed (got ${JSON.stringify(value[field]) ?? 'undefined'})`
    }
  }
  return null
}

/** Every key of `keys` present and a non-negative finite count. */
const checkCounters = (
  where: string,
  value: unknown,
  keys: readonly string[],
): string | null => {
  if (!isRecord(value)) {
    return `${where} is missing`
  }
  for (const key of keys) {
    if (!CHECKS.nonNegativeInteger(value[key])) {
      return `${where}.${key} is not a count`
    }
  }
  return null
}

const DIVERGENCE_KEYS: readonly string[] = [...SNAPSHOT_REJECTIONS, 'accepted']

/** `value` is one of `allowed`, or null. */
const oneOfOrNull = (allowed: readonly string[]): FieldCheck =>
  (value) => value === null || (typeof value === 'string' && allowed.includes(value))

const oneOf = (allowed: readonly string[]): FieldCheck =>
  (value) => typeof value === 'string' && allowed.includes(value)

const PHASE_SHAPE: Record<string, FieldCheck> = {
  index: CHECKS.nonNegativeInteger,
  name: oneOf(['root', 'post-played', 'post-best', 'other']),
  moves: CHECKS.stringArray,
  requestedDepth: CHECKS.numberOrNull,
  bestmove: CHECKS.stringOrNull,
  nodes: CHECKS.numberOrNull,
  timeMs: CHECKS.numberOrNull,
  nps: CHECKS.numberOrNull,
  hashfull: CHECKS.numberOrNull,
  reachedDepth: CHECKS.numberOrNull,
  seldepth: CHECKS.numberOrNull,
  wallMs: CHECKS.number,
  infoLines: CHECKS.nonNegativeInteger,
  admittedLines: CHECKS.nonNegativeInteger,
  terminated: CHECKS.boolean,
  stopObserved: CHECKS.boolean,
  // §4.3's reason, not a boolean: §10.4 reports divergence split by it, so an
  // unknown string here would land in a bucket no reader knows to look in.
  legacyDivergence: oneOfOrNull(DIVERGENCE_KEYS),
}

/**
 * The two string unions a row copies verbatim out of the worker's `analysis`
 * response.
 *
 * Exhaustive records rather than plain lists: the compiler fails HERE if either
 * union gains, loses, or renames a member, so the validator cannot quietly stop
 * covering a value it once covered. Same argument as `legacyDivergence` — a
 * reader splitting rows by classification or by why the search stopped would
 * otherwise find an unrecognized string in a bucket nobody looks in.
 */
const CLASSIFICATION_NAMES: Record<MoveClassification, true> = {
  best: true,
  excellent: true,
  good: true,
  inaccuracy: true,
  mistake: true,
  blunder: true,
}

const STOP_REASON_NAMES: Record<AnalysisStopReason, true> = {
  bestmove: true,
  deadline: true,
}

/** The worker's `analysis` response as a row records it — present or explicitly null. */
const RESULT_SHAPE: Record<string, FieldCheck> = {
  bestMove: CHECKS.nonEmptyString,
  bestLine: CHECKS.stringArray,
  bestEval: CHECKS.numberOrNull,
  playedEval: CHECKS.numberOrNull,
  bestEvalMate: CHECKS.numberOrNull,
  playedEvalMate: CHECKS.numberOrNull,
  delta: CHECKS.numberOrNull,
  classification: oneOfOrNull(Object.keys(CLASSIFICATION_NAMES)),
  canonical: CHECKS.boolean,
  capFired: CHECKS.boolean,
  stopReason: oneOf(Object.keys(STOP_REASON_NAMES)),
  reachedDepth: CHECKS.numberOrNull,
}

/**
 * Everything a run header says about the machine it ran on.
 *
 * Every field is nullable because a browser may withhold any of them — but the
 * KEY must be there: absent and "the browser would not say" are different
 * answers, and only one of them can be compared across two devices.
 */
const ENVIRONMENT_SHAPE: Record<string, FieldCheck> = {
  userAgent: CHECKS.stringOrNull,
  uaData: CHECKS.stringOrNull,
  hardwareConcurrency: CHECKS.numberOrNull,
  deviceMemory: CHECKS.numberOrNull,
  platform: CHECKS.stringOrNull,
  screen: CHECKS.stringOrNull,
  devicePixelRatio: CHECKS.numberOrNull,
  timeZone: CHECKS.stringOrNull,
}

/** §4.2 acceptance for one phase: accepted at a depth, or rejected for a named reason. */
const snapshotProblem = (where: string, value: unknown): string | null => {
  if (value === null) {
    return null
  }
  if (!isRecord(value) || typeof value.accepted !== 'boolean') {
    return `${where} must be null or say whether the snapshot was accepted`
  }
  if (value.accepted) {
    return CHECKS.number(value.depth) ? null : `${where}.depth is not a number`
  }
  return SNAPSHOT_REJECTIONS.includes(value.reason as (typeof SNAPSHOT_REJECTIONS)[number])
    ? null
    : `${where}.reason is not a named §4 rejection (got ${JSON.stringify(value.reason)})`
}

const MOVE_SHAPE: Record<string, FieldCheck> = {
  seq: CHECKS.nonNegativeInteger,
  blockIndex: CHECKS.nonNegativeInteger,
  repeat: CHECKS.nonNegativeInteger,
  arm: CHECKS.arm,
  orderIndex: CHECKS.nonNegativeInteger,
  positionId: CHECKS.nonEmptyString,
  fen: CHECKS.nonEmptyString,
  playedMove: CHECKS.nonEmptyString,
  thermalIndex: CHECKS.numberOrNull,
  warmup: CHECKS.boolean,
  workerRestarted: CHECKS.boolean,
  engineRebuilt: CHECKS.boolean,
  requestedDepth: CHECKS.number,
  e2eMs: CHECKS.number,
  runElapsedMs: CHECKS.number,
  workerBootMs: CHECKS.numberOrNull,
  resetMs: CHECKS.numberOrNull,
  searchCount: CHECKS.nonNegativeInteger,
  totalNodes: CHECKS.numberOrNull,
  totalEngineMs: CHECKS.numberOrNull,
  pEqualsB: CHECKS.booleanOrNull,
  legacySelectorDivergence: CHECKS.nonNegativeInteger,
  progressPings: CHECKS.nonNegativeInteger,
  streamingPings: CHECKS.nonNegativeInteger,
  error: CHECKS.stringOrNull,
}

const RUN_PLAN_SHAPE: Record<string, FieldCheck> = {
  mode: CHECKS.nonEmptyString,
  repeats: CHECKS.nonNegativeInteger,
  positionSetId: CHECKS.nonEmptyString,
  positionCount: CHECKS.nonNegativeInteger,
  blockCount: CHECKS.nonNegativeInteger,
  plannedItems: CHECKS.nonNegativeInteger,
  warmup: CHECKS.boolean,
  armOrderBalanced: CHECKS.boolean,
  // The field a `NaN` cooldown used to arrive in as `null`: a run that skipped
  // its thermal control while reporting a clean method (`device/config.ts`).
  blockCooldownMs: CHECKS.nonNegativeInteger,
}

const SUMMARY_SHAPE: Record<string, FieldCheck> = {
  plannedItems: CHECKS.nonNegativeInteger,
  measuredItems: CHECKS.nonNegativeInteger,
  warmupItems: CHECKS.nonNegativeInteger,
  legacySelectorDivergence: CHECKS.nonNegativeInteger,
  errors: CHECKS.nonNegativeInteger,
}

/**
 * Structural validation shared by both harnesses. Returns a reason string, or
 * null when the record is well-formed.
 *
 * Every DECLARED field is required, and every number must be finite. Not
 * pedantry: this is the only guard between a committed file and a reader
 * quoting it, and the fields it used to skip are exactly the ones an adoption
 * verdict is read off — `gameWeighted`, the §4 counters, `errors`. A file
 * missing them parses cleanly and then answers `undefined` to the question the
 * gate asks. `JSON.stringify` also writes `NaN` as `null`, so a finiteness check
 * is the only thing standing between an empty sample and a number-shaped hole.
 *
 * What it deliberately does NOT check is agreement BETWEEN rows — that a
 * summary's counts match its move rows, that sequence numbers are contiguous.
 * Row validity is a per-line property; those are file-level, and live in
 * `benchFile.ts`.
 */
export const validateBenchRecord = (value: unknown): string | null => {
  if (!isRecord(value)) {
    return 'not an object'
  }
  if (value.schemaVersion !== BENCH_SCHEMA_VERSION) {
    return `unsupported schemaVersion ${String(value.schemaVersion)} (expected ${BENCH_SCHEMA_VERSION})`
  }
  if (typeof value.runId !== 'string' || value.runId.length === 0) {
    return 'missing runId'
  }

  switch (value.kind) {
    case 'run': {
      if (value.harness !== 'device' && value.harness !== 'node') {
        return 'invalid harness'
      }
      if (value.build !== 'bundled' && value.build !== 'dev') {
        return 'invalid build'
      }
      if (value.countersAre !== 'observer-recomputed') {
        return 'run must declare countersAre: "observer-recomputed"'
      }
      if (!CHECKS.nonEmptyString(value.startedAtIso)) {
        return 'missing startedAtIso'
      }
      if (!isRecord(value.engine)) {
        return 'missing engine identity'
      }
      const engine = checkFields('engine', value.engine, {
        engineVersion: CHECKS.string,
        engineBuild: CHECKS.nonEmptyString,
        evalFileId: CHECKS.string,
        threads: CHECKS.number,
        hashMb: CHECKS.number,
      })
      if (engine) return engine
      if (!isRecord(value.depth)) {
        return 'missing depth policy'
      }
      const depth = checkFields('depth', value.depth, {
        baseline: CHECKS.number,
        maxDevice: CHECKS.number,
        session: CHECKS.number,
        requested: CHECKS.number,
      })
      if (depth) return depth
      if (!isRecord(value.environment)) {
        return 'missing environment'
      }
      const environment = checkFields('environment', value.environment, ENVIRONMENT_SHAPE)
      if (environment) return environment
      if (!isRecord(value.device)) {
        return 'missing device'
      }
      const device = checkFields('device', value.device, {
        // Non-empty: "what hardware produced this" is the one thing a run header
        // cannot be reconstructed without.
        label: CHECKS.nonEmptyString,
        notes: CHECKS.string,
      })
      if (device) return device
      if (!isRecord(value.source)) {
        return 'missing source stamp'
      }
      const source = checkFields('source', value.source, {
        gitRevision: CHECKS.stringOrNull,
        gitDirty: CHECKS.booleanOrNull,
        workerBundleFile: CHECKS.stringOrNull,
        workerBundleSha256: CHECKS.stringOrNull,
      })
      if (source) return source
      if (!isRecord(value.plan)) {
        return 'missing plan'
      }
      const plan = checkFields('plan', value.plan, RUN_PLAN_SHAPE)
      if (plan) return plan
      if (!Array.isArray(value.plan.arms) || value.plan.arms.length === 0) {
        return 'plan.arms is missing or empty'
      }
      for (const arm of value.plan.arms) {
        if (!CHECKS.arm(arm)) return `plan.arms contains unknown arm ${JSON.stringify(arm)}`
      }
      if (!CHECKS.stringArray(value.methodWarnings)) {
        return 'missing methodWarnings'
      }
      return null
    }
    case 'move': {
      const move = checkFields('move', value, MOVE_SHAPE)
      if (move) return move
      if (value.playerColor !== 'white' && value.playerColor !== 'black') {
        return 'invalid playerColor'
      }
      if (value.cohort !== 'cold' && value.cohort !== 'warm') {
        return 'invalid cohort'
      }
      if (value.result !== null) {
        if (!isRecord(value.result)) {
          return 'result must be an object or null'
        }
        const result = checkFields('result', value.result, RESULT_SHAPE)
        if (result) return result
      }
      const rejections = checkCounters('move.rejections', value.rejections, SNAPSHOT_REJECTIONS)
      if (rejections) return rejections
      const divergences = checkCounters(
        'move.divergenceByReason',
        value.divergenceByReason,
        DIVERGENCE_KEYS,
      )
      if (divergences) return divergences
      if (!Array.isArray(value.phases)) {
        return 'missing phases'
      }
      for (const phase of value.phases) {
        if (!isRecord(phase)) return 'malformed phase'
        const where = `phase[${String(phase.index)}]`
        const problem = checkFields(where, phase, PHASE_SHAPE)
        if (problem) return problem
        const snapshot = snapshotProblem(`${where}.snapshot`, phase.snapshot)
        if (snapshot) return snapshot
      }
      return null
    }
    case 'summary': {
      if (value.completion !== 'complete' && value.completion !== 'stopped') {
        return 'missing completion'
      }
      const summary = checkFields('summary', value, SUMMARY_SHAPE)
      if (summary) return summary
      if (!CHECKS.stringArray(value.methodWarnings)) {
        return 'missing methodWarnings'
      }
      const rejections = checkCounters('summary.rejections', value.rejections, SNAPSHOT_REJECTIONS)
      if (rejections) return rejections
      const divergences = checkCounters(
        'summary.divergenceByReason',
        value.divergenceByReason,
        DIVERGENCE_KEYS,
      )
      if (divergences) return divergences

      if (!Array.isArray(value.observedMatchRate)) {
        return 'missing observedMatchRate'
      }
      for (const entry of value.observedMatchRate) {
        if (!isRecord(entry)) return 'malformed observedMatchRate entry'
        const problem = checkFields('observedMatchRate', entry, {
          arm: CHECKS.arm,
          m: CHECKS.numberOrNull,
          n: CHECKS.nonNegativeInteger,
        })
        if (problem) return problem
      }

      // §11's gate is read off this array; a missing one answers `undefined` to
      // the adoption question rather than failing.
      if (!Array.isArray(value.gameWeighted)) {
        return 'missing gameWeighted'
      }
      for (const entry of value.gameWeighted) {
        if (!isRecord(entry)) return 'malformed gameWeighted entry'
        const problem = checkFields('gameWeighted', entry, {
          arm: CHECKS.arm,
          m: CHECKS.numberOrNull,
          n: CHECKS.nonNegativeInteger,
          medianMs: CHECKS.numberOrNull,
          p90Ms: CHECKS.numberOrNull,
          p95Ms: CHECKS.numberOrNull,
          worstMs: CHECKS.numberOrNull,
        })
        if (problem) return problem
      }

      if (!Array.isArray(value.cells)) {
        return 'missing cells'
      }
      for (const cell of value.cells) {
        if (!isRecord(cell) || !isRecord(cell.stats)) return 'malformed summary cell'
        const where = `summary cell ${String(cell.arm)}/${String(cell.cohort)}/${String(cell.split)}`
        // The three coordinates a reader JOINS on: an unrecognized one is a cell
        // that silently answers no query, which for §11's gate reads as absence.
        const key = checkFields(where, cell, {
          arm: CHECKS.arm,
          cohort: oneOf(['cold', 'warm', 'all']),
          split: oneOf(['p-equals-b', 'p-differs', 'all']),
        })
        if (key) return key
        const problem = checkFields(where, cell.stats, {
          n: CHECKS.nonNegativeInteger,
          // A NaN written by JSON.stringify arrives as `null`: a null where a
          // number belongs is an unreadable cell, not an empty one.
          medianMs: CHECKS.number,
          p90Ms: CHECKS.number,
          p95Ms: CHECKS.number,
          worstMs: CHECKS.number,
          medianNodes: CHECKS.numberOrNull,
        })
        if (problem) return problem
      }
      return null
    }
    default:
      return `unknown kind ${String(value.kind)}`
  }
}
