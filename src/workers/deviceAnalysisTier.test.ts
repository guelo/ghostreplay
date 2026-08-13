import { describe, it, expect, beforeEach } from 'vitest'
import {
  BASELINE_DEPTH,
  MAX_DEVICE_DEPTH,
  computeDeviceAnalysisDepth,
  resetSessionAnalysisDepthForTests,
  sessionAnalysisDepth,
} from './deviceAnalysisTier'

/** A minimal navigator stand-in; `deviceMemory` omitted means "signal absent". */
const nav = (hardwareConcurrency: unknown, deviceMemory?: number) =>
  ({ hardwareConcurrency, ...(deviceMemory !== undefined ? { deviceMemory } : {}) }) as
    unknown as Navigator

describe('computeDeviceAnalysisDepth', () => {
  it('never returns below the baseline or above the ceiling', () => {
    const cases = [
      nav(1, 0.25),
      nav(4, 4),
      nav(8, 8),
      nav(64, 64),
      nav(undefined),
      nav(0),
    ]
    for (const n of cases) {
      const depth = computeDeviceAnalysisDepth(n)
      expect(depth).toBeGreaterThanOrEqual(BASELINE_DEPTH)
      expect(depth).toBeLessThanOrEqual(MAX_DEVICE_DEPTH)
    }
  })

  it('tiers up monotonically with cores', () => {
    // Asserted as an ORDERING rather than exact depths so the test still means
    // something after the ceiling is raised past parity behind the latency gate.
    const low = computeDeviceAnalysisDepth(nav(2, 2))
    const mid = computeDeviceAnalysisDepth(nav(4, 4))
    const high = computeDeviceAnalysisDepth(nav(8, 8))
    expect(low).toBe(BASELINE_DEPTH)
    expect(mid).toBeGreaterThanOrEqual(low)
    expect(high).toBeGreaterThanOrEqual(mid)
  })

  it('does not let an absent deviceMemory signal veto a tier-up', () => {
    // `deviceMemory` is Chromium-only. Treating undefined as "small" would pin
    // every Safari/Firefox device to the baseline forever.
    expect(computeDeviceAnalysisDepth(nav(8))).toBe(computeDeviceAnalysisDepth(nav(8, 8)))
    expect(computeDeviceAnalysisDepth(nav(4))).toBe(computeDeviceAnalysisDepth(nav(4, 4)))
  })

  it('holds a many-core device back when the memory signal says it is small', () => {
    expect(computeDeviceAnalysisDepth(nav(16, 2))).toBe(BASELINE_DEPTH)
  })

  it.each([undefined, 0, Number.NaN, -1, 'eight'])(
    'falls back to the baseline for hardwareConcurrency=%s',
    (cores) => {
      expect(computeDeviceAnalysisDepth(nav(cores))).toBe(BASELINE_DEPTH)
    },
  )

  it('returns the baseline instead of throwing when the navigator global is absent', () => {
    // Guards the `nav = navigator` default-expression trap: that default is
    // evaluated at CALL time and throws wherever the global is missing (workers,
    // SSR, some test envs) — defeating the fallback it appears to provide.
    const original = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
    delete (globalThis as { navigator?: Navigator }).navigator
    try {
      expect(() => computeDeviceAnalysisDepth()).not.toThrow()
      expect(computeDeviceAnalysisDepth()).toBe(BASELINE_DEPTH)
    } finally {
      if (original) Object.defineProperty(globalThis, 'navigator', original)
    }
  })

  it('returns the baseline when reading a signal throws', () => {
    const hostile = {
      get hardwareConcurrency(): number {
        throw new Error('blocked by privacy setting')
      },
    } as unknown as Navigator
    expect(computeDeviceAnalysisDepth(hostile)).toBe(BASELINE_DEPTH)
  })

  it('prefers an explicit navigator argument over the global', () => {
    expect(computeDeviceAnalysisDepth(nav(1, 1))).toBe(BASELINE_DEPTH)
  })
})

describe('sessionAnalysisDepth', () => {
  beforeEach(() => {
    resetSessionAnalysisDepthForTests()
  })

  it('computes once and returns the same depth for the whole session', () => {
    // Load-bearing for provenance homogeneity: every row a session uploads must
    // claim the SAME search_limit_value, or per-slot coalescing could pair one
    // upload's numbers with another upload's depth claim.
    const first = sessionAnalysisDepth()
    expect(sessionAnalysisDepth()).toBe(first)
    expect(sessionAnalysisDepth()).toBe(first)
  })

  it('stays within the declared bounds', () => {
    const depth = sessionAnalysisDepth()
    expect(depth).toBeGreaterThanOrEqual(BASELINE_DEPTH)
    expect(depth).toBeLessThanOrEqual(MAX_DEVICE_DEPTH)
  })

  it('keeps in-game analysis below the visible d21 tier', () => {
    // Depth 21 is reserved for analysis-board reuse. The evidence policy relies
    // on every in-game tier being strictly shallower when d21 supersedes it.
    expect(MAX_DEVICE_DEPTH).toBeLessThan(21)
  })

  it('is exactly the historical default while the ceiling is at parity', () => {
    // The parity landing must be a strict no-op: with MAX_DEVICE_DEPTH pinned at
    // the baseline, no device can search deeper than it did before. Delete this
    // assertion when the ceiling is raised behind the latency acceptance gate.
    expect(MAX_DEVICE_DEPTH).toBe(BASELINE_DEPTH)
    expect(sessionAnalysisDepth()).toBe(17)
  })
})
