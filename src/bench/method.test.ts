import { describe, expect, it } from 'vitest'
import {
  MIN_BLOCK_COOLDOWN_MS,
  MIN_REPEATS,
  MIN_THERMAL_PLIES,
  armOrderBalanced,
  outcomeWarnings,
  planWarnings,
} from './method'
import type { BenchMethodPlan } from './method'

const validPlan: BenchMethodPlan = {
  repeats: MIN_REPEATS,
  armCount: 1,
  // §10.4's three repeats are three blocks, so a valid plan must also cool
  // between them — otherwise only the first repeat starts on a cooled device.
  blockCount: MIN_REPEATS,
  armOrderBalanced: true,
  blockCooldownMs: MIN_BLOCK_COOLDOWN_MS,
  thermalPlies: MIN_THERMAL_PLIES,
  build: 'bundled',
  requestedDepth: 17,
  sessionDepth: 17,
  source: {
    gitRevision: 'a'.repeat(40),
    gitDirty: false,
    workerBundleFile: null,
    workerBundleSha256: null,
  },
}

describe('planWarnings', () => {
  it('is silent for a run that satisfies §10.4', () => {
    expect(planWarnings(validPlan)).toEqual([])
  })

  it('flags too few repeats and too short a thermal sequence', () => {
    const warnings = planWarnings({ ...validPlan, repeats: 1, thermalPlies: 2 })

    expect(warnings.join(' ')).toMatch(/repeats=1 is below/)
    expect(warnings.join(' ')).toMatch(/2 plies/)
  })

  it('does not apply the ply minimum to a set that is not a sequence', () => {
    expect(planWarnings({ ...validPlan, thermalPlies: null })).toEqual([])
  })

  it('flags a dev build, since §10.1 requires the bundled worker', () => {
    expect(planWarnings({ ...validPlan, build: 'dev' }).join(' ')).toMatch(/unbundled worker/)
  })

  it('flags a depth override, since those timings are not what production produces', () => {
    expect(planWarnings({ ...validPlan, requestedDepth: 12 }).join(' ')).toMatch(
      /overrides this device's session depth 17/,
    )
  })

  it('flags repeated blocks with no cooldown even when only one arm runs', () => {
    // The standard control run: one arm, three repeats, back-to-back. Only the
    // first repeat begins cooled, and the summary pools all three — so this must
    // warn even though there is no arm comparison to confound.
    const warnings = planWarnings({ ...validPlan, blockCooldownMs: 0 })

    expect(warnings.join(' ')).toMatch(/3 blocks run back-to-back with no cooldown/)
    expect(warnings.join(' ')).toMatch(/only the first began cooled/)
  })

  it('says a multi-arm run without cooldown confounds arm with heat', () => {
    const warnings = planWarnings({
      ...validPlan,
      armCount: 2,
      repeats: 4,
      blockCount: 8,
      blockCooldownMs: 0,
    })

    expect(warnings.join(' ')).toMatch(/no cooldown between blocks/)
    expect(warnings.join(' ')).toMatch(/heat an earlier one left behind/)
  })

  it('asks nothing of a single-block run, which cannot carry heat forward', () => {
    expect(
      planWarnings({ ...validPlan, repeats: 1, blockCount: 1, blockCooldownMs: 0 }).join(' '),
    ).not.toMatch(/cooldown/)
  })

  it('refuses to accept a token cooldown as a thermal control', () => {
    expect(planWarnings({ ...validPlan, blockCooldownMs: 1 }).join(' ')).toMatch(
      /too short to shed a block's heat/,
    )
  })

  it('flags an unidentifiable or dirty bundle', () => {
    expect(
      planWarnings({ ...validPlan, source: { ...validPlan.source, gitRevision: null } }).join(' '),
    ).toMatch(/no git revision/)
    expect(
      planWarnings({ ...validPlan, source: { ...validPlan.source, gitDirty: true } }).join(' '),
    ).toMatch(/dirty working tree/)
  })
})

describe('outcomeWarnings', () => {
  it('is silent for a complete, error-free run', () => {
    expect(
      outcomeWarnings({ completion: 'complete', plannedItems: 40, measuredItems: 40, errors: 0 }),
    ).toEqual([])
  })

  it('flags a stopped run, a short run, and errored measurements', () => {
    expect(
      outcomeWarnings({ completion: 'stopped', plannedItems: 40, measuredItems: 7, errors: 0 }).join(' '),
    ).toMatch(/stopped after 7 of 40/)
    expect(
      outcomeWarnings({ completion: 'complete', plannedItems: 40, measuredItems: 39, errors: 0 }).join(' '),
    ).toMatch(/only 39 of 40/)
    expect(
      outcomeWarnings({ completion: 'complete', plannedItems: 40, measuredItems: 40, errors: 2 }).join(' '),
    ).toMatch(/2 measurement\(s\) errored/)
  })
})

describe('armOrderBalanced', () => {
  it('requires the repeat count to divide by the arm count', () => {
    expect(armOrderBalanced(1, 3)).toBe(true)
    expect(armOrderBalanced(2, 3)).toBe(false)
    expect(armOrderBalanced(2, 4)).toBe(true)
    expect(armOrderBalanced(3, 3)).toBe(true)
    expect(armOrderBalanced(3, 4)).toBe(false)
  })
})
