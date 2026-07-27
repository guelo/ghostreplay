/**
 * The run configuration, and the guard that refuses an invalid one
 * (g-two-search-grade §10.4).
 *
 * A benchmark's controls are only controls if the run actually applied them, and
 * a number that arrives as `NaN` applies as nothing at all: `NaN > 0` is false,
 * so a cooldown of `NaN` neither sleeps nor warns, and `JSON.stringify` writes it
 * into the file as `null` — a run that skipped its thermal control while
 * reporting a clean method. That is the same failure the `observedMatchRate.m`
 * NaN bug had one layer down, and the fix is the same: refuse it at the boundary
 * instead of letting it serialize.
 *
 * Refusals, not warnings — unlike `method.ts`. A one-repeat run is a legitimate
 * smoke test that must merely say so; `--repeats banana` is not a run at all, and
 * measuring for forty minutes before discovering that is the expensive failure.
 */

import type { BenchArm, BenchSourceStamp } from '../benchRecord'
import { BENCH_ARMS } from '../benchRecord'
import type { BenchMode } from './schedule'
import type { BenchPositionSetId } from './positions'
import { MAX_THERMAL_PLIES } from './positions'

export type BenchRunConfig = {
  deviceLabel: string
  notes: string
  mode: BenchMode
  positionSetId: BenchPositionSetId
  thermalPlies?: number
  repeats: number
  arms: BenchArm[]
  /**
   * Prepend a priming measurement to each sequence block so every position in the
   * set gets a warm row (see `schedule.ts`). Required for the warm half of a
   * paired cold/warm capture.
   */
  warmup?: boolean
  /**
   * Idle time between blocks.
   *
   * Every block after the first otherwise starts on the heat the previous one
   * deposited, and the summary pools them — so this matters to a single-arm run
   * repeated three times just as much as to an arm comparison. §10.4
   * counterbalances the arm ORDER, which removes the bias of one arm always
   * running second, but averaging heat across arms is not the same as shedding it.
   * `method.MIN_BLOCK_COOLDOWN_MS` is the point below which a gap is not a
   * cooldown at all.
   */
  blockCooldownMs?: number
  /** Defaults to `sessionAnalysisDepth()` — what production would ask for. */
  depth?: number
  moveTimeoutMs?: number
  readyTimeoutMs?: number
  /** How long to wait for the §15.1 C7 `bench-ready` acknowledgement. */
  benchHandshakeTimeoutMs?: number
  /** Driver-supplied build provenance, merged over the build-time stamp. */
  source?: Partial<BenchSourceStamp>
}

export const BENCH_MODES: readonly BenchMode[] = ['sequence', 'cold']

export const BENCH_POSITION_SET_IDS: readonly BenchPositionSetId[] = ['smoke-6', 'thermal-40']

/**
 * Above this a single analyze-move can run for hours on a phone, which is a
 * typo rather than an intent — the §10.2 reference depths (26/27) belong to the
 * Node corpus harness, and even they sit under this.
 */
export const MAX_BENCH_DEPTH = 30

/** Far enough above `MIN_BLOCK_COOLDOWN_MS` to allow any real protocol; a day is a typo. */
export const MAX_BLOCK_COOLDOWN_MS = 86_400_000

/**
 * A typed-in number, as the operator actually typed it.
 *
 * Blank means "not set", so the documented default applies. Everything else is
 * parsed and returned AS PARSED — `NaN` included — because the obvious
 * alternative, `Number(input.value) || fallback`, answers an unreadable entry
 * with a plausible number: a typed `0` becomes the default 40 plies, `nope`
 * becomes 3 repeats, and the run header records the substitute as though it had
 * been requested. The browser's own `min`/`max` do not catch this either, since
 * the page starts the run from a `type="button"` and never checks form validity.
 *
 * Refusing the value is `configProblems`' job. This function's only job is to
 * not destroy the evidence that there is something to refuse.
 */
export const typedNumber = (raw: string): number | undefined =>
  raw.trim() === '' ? undefined : Number(raw)

/** The part of an `<input>` this module reads — structurally satisfied by `HTMLInputElement`. */
export type NumberField = { value: string; validity: { badInput: boolean } }

/**
 * The same value, read at the DOM boundary — where "blank" has two meanings.
 *
 * `<input type="number">` runs the HTML value-sanitization algorithm: type `e`
 * into one and `.value` reads back as `''`, identical to an untouched field, so
 * `typedNumber` alone would report an unreadable entry as "not set" and the
 * default would apply silently. Only `validity.badInput` distinguishes them.
 *
 * The page's controls are `type="text" inputmode="numeric"` precisely so the raw
 * text survives to be refused; this check is what keeps that decision from being
 * quietly undone by a control switched back to `type="number"` later. jsdom does
 * not model `badInput` (it sanitizes the value but reports `false`), so the
 * page-level regression test pins the control type instead.
 */
export const typedNumberField = (field: NumberField): number | undefined =>
  field.validity.badInput ? NaN : typedNumber(field.value)

const isInteger = (value: unknown): value is number =>
  typeof value === 'number' && Number.isInteger(value)

/**
 * The offending value, readably.
 *
 * `JSON.stringify(NaN)` is the string `"null"`, which is exactly the confusion
 * this guard exists to prevent — a refusal that reports `got null` for a typed
 * `nope` reads like a missing field rather than an unreadable one.
 */
const show = (value: unknown): string =>
  typeof value === 'number' && !Number.isFinite(value) ? String(value) : JSON.stringify(value)

const intProblem = (
  name: string,
  value: unknown,
  bounds: { min: number; max: number },
): string | null => {
  if (!isInteger(value)) {
    return `${name} must be a whole number, got ${show(value)}`
  }
  if (value < bounds.min || value > bounds.max) {
    return `${name} must be between ${bounds.min} and ${bounds.max}, got ${value}`
  }
  return null
}

/**
 * Every reason this configuration cannot produce a valid measurement.
 *
 * All of them, not the first: an operator fixing a phone run by hand should see
 * the whole list at once rather than one field per attempt.
 */
export const configProblems = (config: BenchRunConfig): string[] => {
  const problems: string[] = []
  const push = (problem: string | null) => {
    if (problem) problems.push(problem)
  }

  if (typeof config.deviceLabel !== 'string' || config.deviceLabel.trim().length === 0) {
    // The one thing a run header cannot be reconstructed without, and the schema
    // requires it — so a run that could not produce a readable file must not start.
    problems.push('deviceLabel is required: a run header that cannot name its hardware is not evidence')
  }

  if (!BENCH_MODES.includes(config.mode)) {
    // Otherwise an unknown mode falls through `planBlocks`' `=== 'cold'` test and
    // runs as `sequence` while the run header records the string verbatim — a
    // file that names a mode it did not run.
    problems.push(`mode must be one of ${BENCH_MODES.join(', ')}, got ${JSON.stringify(config.mode)}`)
  }

  if (!BENCH_POSITION_SET_IDS.includes(config.positionSetId)) {
    // `buildPositionSet` treats anything that is not `smoke-6` as the thermal
    // sequence, so an unknown id silently measures a different set than the file
    // claims.
    problems.push(
      `positionSetId must be one of ${BENCH_POSITION_SET_IDS.join(', ')}, got ${JSON.stringify(config.positionSetId)}`,
    )
  }

  if (!Array.isArray(config.arms) || config.arms.length === 0) {
    problems.push('at least one arm must be selected: there is nothing to measure')
  } else {
    for (const arm of config.arms) {
      if (!BENCH_ARMS.includes(arm)) {
        problems.push(`unknown arm ${JSON.stringify(arm)} (known: ${BENCH_ARMS.join(', ')})`)
      }
    }
    if (new Set(config.arms).size !== config.arms.length) {
      // A repeated arm doubles that arm's blocks and breaks the counterbalanced
      // rotation, while `armOrderBalanced` still reports the order as balanced.
      problems.push(`arms must be unique, got ${config.arms.join(', ')}`)
    }
  }

  if (config.mode === 'cold' && config.warmup) {
    // `planBlocks` gives every cold measurement its own worker, so a priming row
    // would defeat the mode and it correctly ignores the flag — but the run header
    // still recorded `warmup: true`, leaving a file that claims a priming
    // measurement it does not contain.
    problems.push('warmup cannot be combined with mode=cold: every cold measurement already gets a fresh worker, so no priming row is scheduled')
  }

  push(intProblem('repeats', config.repeats, { min: 1, max: 1_000 }))

  if (config.thermalPlies !== undefined) {
    // Bounded by the stored game, because `buildThermalPositions` caps a longer
    // request rather than refusing it: `--plies 500` would otherwise measure 60
    // and say nothing about the difference.
    push(intProblem('thermalPlies', config.thermalPlies, { min: 1, max: MAX_THERMAL_PLIES }))
  }
  if (config.blockCooldownMs !== undefined) {
    push(intProblem('blockCooldownMs', config.blockCooldownMs, { min: 0, max: MAX_BLOCK_COOLDOWN_MS }))
  }
  if (config.depth !== undefined) {
    push(intProblem('depth', config.depth, { min: 1, max: MAX_BENCH_DEPTH }))
  }
  if (config.moveTimeoutMs !== undefined) {
    push(intProblem('moveTimeoutMs', config.moveTimeoutMs, { min: 1, max: 3_600_000 }))
  }
  if (config.readyTimeoutMs !== undefined) {
    push(intProblem('readyTimeoutMs', config.readyTimeoutMs, { min: 1, max: 3_600_000 }))
  }
  if (config.benchHandshakeTimeoutMs !== undefined) {
    push(intProblem('benchHandshakeTimeoutMs', config.benchHandshakeTimeoutMs, { min: 1, max: 600_000 }))
  }

  return problems
}
