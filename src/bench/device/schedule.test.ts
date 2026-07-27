import { describe, expect, it } from 'vitest'
import { counterbalancedArms, planBlocks, plannedMeasurements, totalItems } from './schedule'
import { armOrderBalanced } from '../method'
import type { BenchPosition } from './positions'

const position = (id: string, thermalIndex: number | null = null): BenchPosition => ({
  positionId: id,
  fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
  playedMove: 'e2e4',
  playerColor: 'white',
  thermalIndex,
  label: id,
})

const positions = [position('a', 1), position('b', 2), position('c', 3)]

describe('counterbalancedArms', () => {
  it('rotates the arm order by repeat so each arm visits each slot', () => {
    expect(counterbalancedArms(['current', 'variantA'], 0)).toEqual(['current', 'variantA'])
    expect(counterbalancedArms(['current', 'variantA'], 1)).toEqual(['variantA', 'current'])
    expect(counterbalancedArms(['current', 'variantA'], 2)).toEqual(['current', 'variantA'])
  })

  it('is a no-op for a single arm', () => {
    expect(counterbalancedArms(['current'], 5)).toEqual(['current'])
    expect(counterbalancedArms([], 1)).toEqual([])
  })

  it('only balances when the repeats divide by the arm count', () => {
    // AB, BA, AB — `current` takes the first slot twice, so protocol stays
    // confounded with run order (and therefore with accumulated heat). §10.4's
    // 3-repeat minimum and a 2-arm comparison are not simultaneously satisfiable;
    // the run has to say so instead of implying a balance it does not have.
    expect(armOrderBalanced(2, 3)).toBe(false)
    expect(armOrderBalanced(2, 4)).toBe(true)
    expect(armOrderBalanced(1, 3)).toBe(true)
  })
})

describe('planBlocks (sequence mode)', () => {
  const blocks = planBlocks({ arms: ['current'], repeats: 3, positions, mode: 'sequence' })

  it('produces one block per repeat and arm, covering every position', () => {
    expect(blocks).toHaveLength(3)
    expect(totalItems(blocks)).toBe(9)
    expect(blocks.map((block) => block.repeat)).toEqual([0, 1, 2])
  })

  it('marks only the first item of a block cold', () => {
    // A block owns one freshly constructed worker, so exactly one measurement in
    // it can be a cold start.
    expect(blocks[0].items.map((item) => item.cohort)).toEqual(['cold', 'warm', 'warm'])
  })

  it('preserves position order inside a block so thermal indices stay aligned', () => {
    expect(blocks[1].items.map((item) => item.position.positionId)).toEqual(['a', 'b', 'c'])
  })

  it('records the counterbalanced slot each arm occupied', () => {
    const twoArm = planBlocks({
      arms: ['current', 'variantA'],
      repeats: 2,
      positions,
      mode: 'sequence',
    })

    expect(twoArm.map((block) => [block.repeat, block.arm, block.orderIndex])).toEqual([
      [0, 'current', 0],
      [0, 'variantA', 1],
      [1, 'variantA', 0],
      [1, 'current', 1],
    ])
  })
})

describe('planBlocks (warm-up)', () => {
  const blocks = planBlocks({
    arms: ['current'],
    repeats: 1,
    positions,
    mode: 'sequence',
    warmup: true,
  })

  it('spends the cold slot on a priming duplicate so every position gets a warm row', () => {
    // Without this, position `a` is the block's cold row in every repeat and never
    // gets measured warm — so a per-position cold-vs-warm comparison silently
    // covers one position fewer than the set.
    expect(blocks[0].items.map((item) => [item.position.positionId, item.cohort, item.warmup])).toEqual([
      ['a', 'cold', true],
      ['a', 'warm', false],
      ['b', 'warm', false],
      ['c', 'warm', false],
    ])
  })

  it('does not count the warm-up as a planned measurement', () => {
    expect(totalItems(blocks)).toBe(4)
    expect(plannedMeasurements(blocks)).toBe(3)
  })
})

describe('planBlocks (cold mode)', () => {
  it('gives every measurement its own worker and cohort', () => {
    const blocks = planBlocks({ arms: ['current'], repeats: 2, positions, mode: 'cold' })

    expect(blocks).toHaveLength(6)
    expect(blocks.every((block) => block.items.length === 1)).toBe(true)
    expect(blocks.every((block) => block.items[0].cohort === 'cold')).toBe(true)
  })

  it('ignores a warm-up request, which would defeat the mode', () => {
    const blocks = planBlocks({ arms: ['current'], repeats: 1, positions, mode: 'cold', warmup: true })

    expect(blocks.every((block) => block.items.every((item) => !item.warmup))).toBe(true)
    expect(plannedMeasurements(blocks)).toBe(3)
  })
})

describe('repeat floor', () => {
  it('runs at least one repeat', () => {
    expect(planBlocks({ arms: ['current'], repeats: 0, positions, mode: 'sequence' })).toHaveLength(1)
  })
})
