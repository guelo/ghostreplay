/**
 * Run schedule: repeats, counterbalanced protocol order, and cold/warm cohorts
 * (g-two-search-grade §10.4).
 *
 * A BLOCK is one (repeat, arm) pair and owns exactly one freshly constructed
 * analysis worker. That is what makes the cohorts meaningful: the engine
 * sub-worker and its WASM instance are built once per analysis worker, so the
 * only way to measure a cold start is to start one. Item 0 of a block is `cold`;
 * everything after it is `warm`.
 *
 * Blocks are ordered by repeat, and the arm order ROTATES with the repeat (a
 * Latin square). With one arm that is degenerate, but the rotation is what stops
 * a later two-arm run from confounding arm with position in the sequence — by
 * which point the device has warmed up and any fixed order would bias whichever
 * arm always ran second.
 *
 * The rotation only BALANCES when the repeat count is a multiple of the arm
 * count: 3 repeats over 2 arms yields AB, BA, AB, giving the first arm the first
 * slot twice. `method.armOrderBalanced` decides that, the runner records it in
 * the run header, and an unbalanced order is a method warning rather than a
 * silent bias — see `method.ts`.
 */

import type { BenchArm, BenchCohort } from '../benchRecord'
import type { BenchPosition } from './positions'

export type BenchMode =
  /** One block per (repeat, arm); the whole set runs on one warm worker. */
  | 'sequence'
  /** Every measurement gets its own fresh worker — an all-cold cohort. */
  | 'cold'

export type BenchSchedulePlan = {
  arms: BenchArm[]
  repeats: number
  positions: BenchPosition[]
  mode: BenchMode
  /**
   * Prepend one priming measurement to each sequence block, so that EVERY
   * position in the set gets a warm row.
   *
   * Without it the block's cold row is always the set's first position, which
   * therefore never gets measured warm — and a per-position cold-versus-warm
   * comparison silently covers one position fewer than the set. The warm-up
   * duplicates position 0, is itself the block's cold row, and is excluded from
   * every summary statistic.
   */
  warmup?: boolean
}

export type BenchScheduleItem = {
  position: BenchPosition
  cohort: BenchCohort
  /** Index of this item within its block. */
  itemIndex: number
  /** A priming duplicate of position 0; measured, recorded, never summarized. */
  warmup: boolean
}

export type BenchBlock = {
  blockIndex: number
  repeat: number
  arm: BenchArm
  /** The arm's slot within this repeat — the counterbalanced order. */
  orderIndex: number
  items: BenchScheduleItem[]
}

/**
 * Rotate the arm order by the repeat index so each arm occupies each slot
 * equally often across repeats.
 */
export const counterbalancedArms = (arms: readonly BenchArm[], repeat: number): BenchArm[] => {
  if (arms.length === 0) {
    return []
  }
  const offset = ((repeat % arms.length) + arms.length) % arms.length
  return [...arms.slice(offset), ...arms.slice(0, offset)]
}

export const planBlocks = (plan: BenchSchedulePlan): BenchBlock[] => {
  const blocks: BenchBlock[] = []
  const repeats = Math.max(1, Math.floor(plan.repeats))

  for (let repeat = 0; repeat < repeats; repeat += 1) {
    const armOrder = counterbalancedArms(plan.arms, repeat)
    armOrder.forEach((arm, orderIndex) => {
      if (plan.mode === 'cold') {
        // Every measurement is its own block, so every measurement gets a fresh
        // worker and lands in the cold cohort. A warm-up would defeat the mode.
        plan.positions.forEach((position) => {
          blocks.push({
            blockIndex: blocks.length,
            repeat,
            arm,
            orderIndex,
            items: [{ position, cohort: 'cold', itemIndex: 0, warmup: false }],
          })
        })
        return
      }

      const warmupItems: BenchScheduleItem[] =
        plan.warmup && plan.positions.length > 0
          ? [{ position: plan.positions[0], cohort: 'cold', itemIndex: 0, warmup: true }]
          : []

      blocks.push({
        blockIndex: blocks.length,
        repeat,
        arm,
        orderIndex,
        items: [
          ...warmupItems,
          ...plan.positions.map((position, index) => ({
            position,
            // With a warm-up the cold slot is already spent, so every measured
            // position — including the first — is warm.
            cohort: (warmupItems.length === 0 && index === 0 ? 'cold' : 'warm') as BenchCohort,
            itemIndex: index + warmupItems.length,
            warmup: false,
          })),
        ],
      })
    })
  }

  return blocks
}

/** Everything that will be posted to a worker, warm-ups included. */
export const totalItems = (blocks: readonly BenchBlock[]): number =>
  blocks.reduce((sum, block) => sum + block.items.length, 0)

/** Measurements the plan calls for — what the summary's `measuredItems` is judged against. */
export const plannedMeasurements = (blocks: readonly BenchBlock[]): number =>
  blocks.reduce((sum, block) => sum + block.items.filter((item) => !item.warmup).length, 0)
