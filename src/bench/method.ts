/**
 * Whether a run satisfies §10.4's measurement method — computed, and written into
 * the run's own records (g-two-search-grade §10.4, §11).
 *
 * The controls this checks are not ceremony. Three repeats is what makes a median
 * a median rather than one sample; forty plies is what makes a thermal curve show
 * throttling; a counterbalanced arm order is what stops "variant A is faster"
 * from meaning "variant A ran first, on a cooler phone". A run that skips them
 * still produces a plausible-looking summary, so the file has to say so itself —
 * an adoption verdict is read off these numbers months later, by someone who was
 * not in the room when the run was configured.
 *
 * Warnings, not refusals: a one-repeat smoke run is a legitimate way to check the
 * instrument. It just must never be quotable as evidence by accident.
 *
 * §15.2 KEEPS this module: it is the method, not the prototype.
 */

import type { BenchBuildMode, BenchCompletion, BenchSourceStamp } from './benchRecord'

/** §10.4: "at least 3 repeats per position". */
export const MIN_REPEATS = 3

/** §10.4: "use at least a 40-move sequence and graph latency by move index". */
export const MIN_THERMAL_PLIES = 40

/**
 * Below this an idle gap is not a cooldown.
 *
 * A phone sheds a block's heat in tens of seconds, not in one — and a knob that
 * silences a validity warning at `--cooldown 1` is worse than no knob at all,
 * because the file then claims a thermal control the run never had.
 */
export const MIN_BLOCK_COOLDOWN_MS = 30_000

export type BenchMethodPlan = {
  repeats: number
  armCount: number
  /**
   * Blocks the run will execute — (repeat × arm) pairs in sequence mode, one per
   * measurement in cold mode. Not derivable from `repeats × armCount`, which is
   * why the runner passes the scheduled count rather than a product.
   */
  blockCount: number
  armOrderBalanced: boolean
  blockCooldownMs: number
  /** Length of the thermal sequence, or null when this set is not one. */
  thermalPlies: number | null
  build: BenchBuildMode
  requestedDepth: number
  sessionDepth: number
  source: BenchSourceStamp
}

export type BenchMethodOutcome = {
  completion: BenchCompletion
  plannedItems: number
  measuredItems: number
  errors: number
}

/** Method departures visible from the plan alone, before the run starts. */
export const planWarnings = (plan: BenchMethodPlan): string[] => {
  const warnings: string[] = []

  if (plan.repeats < MIN_REPEATS) {
    warnings.push(
      `repeats=${plan.repeats} is below §10.4's minimum of ${MIN_REPEATS}: not enough samples per position for a median`,
    )
  }

  if (plan.thermalPlies !== null && plan.thermalPlies < MIN_THERMAL_PLIES) {
    warnings.push(
      `thermal sequence is ${plan.thermalPlies} plies; §10.4 asks for at least ${MIN_THERMAL_PLIES}, below which the curve cannot show throttling`,
    )
  }

  if (plan.armCount === 0) {
    warnings.push('no arms selected: nothing was measured')
  }

  if (plan.armCount > 1 && !plan.armOrderBalanced) {
    warnings.push(
      `arm order is not counterbalanced: ${plan.repeats} repeats over ${plan.armCount} arms cannot give every arm each slot equally often, so protocol stays confounded with run order — use a multiple of ${plan.armCount}`,
    )
  }

  // Heat accumulates across BLOCKS, not across arms — a single-arm run repeated
  // three times is three blocks back-to-back, of which only the first began on a
  // cooled device, and the summary pools all three. Gating this on `armCount > 1`
  // let exactly the standard control run (one arm, three repeats) report a clean
  // method while measuring a thermal ramp.
  if (plan.blockCount > 1 && plan.blockCooldownMs === 0) {
    warnings.push(
      plan.armCount > 1
        ? `${plan.blockCount} blocks run back-to-back with no cooldown between blocks, so a later arm is measured on the heat an earlier one left behind — counterbalancing the order averages that bias rather than removing it`
        : `${plan.blockCount} blocks run back-to-back with no cooldown between blocks, so only the first began cooled; the later blocks are measured on accumulated heat and then pooled with it (compare runElapsedMs across repeats)`,
    )
  } else if (
    plan.blockCount > 1 &&
    plan.blockCooldownMs > 0 &&
    plan.blockCooldownMs < MIN_BLOCK_COOLDOWN_MS
  ) {
    warnings.push(
      `blockCooldownMs=${plan.blockCooldownMs} is too short to shed a block's heat (at least ${MIN_BLOCK_COOLDOWN_MS}ms), so every block after the first still starts warm`,
    )
  }

  if (plan.build === 'dev') {
    warnings.push(
      'measured the dev server\'s unbundled worker; §10.1 requires the actual bundled worker, so this is a convenience check and not a baseline',
    )
  }

  if (plan.requestedDepth !== plan.sessionDepth) {
    warnings.push(
      `requested depth ${plan.requestedDepth} overrides this device's session depth ${plan.sessionDepth}, so these timings are not what production would produce here`,
    )
  }

  if (plan.source.gitRevision === null) {
    warnings.push(
      'no git revision recorded, so the orchestration bytes measured cannot be identified later',
    )
  } else if (plan.source.gitDirty) {
    warnings.push(
      `built from a dirty working tree at ${plan.source.gitRevision}, so the revision under-specifies what ran`,
    )
  }

  return warnings
}

/** Method departures only visible once the run has ended. */
export const outcomeWarnings = (outcome: BenchMethodOutcome): string[] => {
  const warnings: string[] = []

  if (outcome.completion === 'stopped') {
    warnings.push(
      `run was stopped after ${outcome.measuredItems} of ${outcome.plannedItems} planned measurements, so its coverage of the position set is partial`,
    )
  } else if (outcome.measuredItems < outcome.plannedItems) {
    warnings.push(
      `only ${outcome.measuredItems} of ${outcome.plannedItems} planned measurements were recorded`,
    )
  }

  if (outcome.errors > 0) {
    warnings.push(
      `${outcome.errors} measurement(s) errored and are excluded from every statistic`,
    )
  }

  return warnings
}

/** True when rotating the arm order can give every arm each slot equally often. */
export const armOrderBalanced = (armCount: number, repeats: number): boolean =>
  armCount <= 1 || repeats % armCount === 0
