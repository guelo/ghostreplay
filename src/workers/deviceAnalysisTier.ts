/**
 * Per-device in-game analysis depth (g-mk1d §1).
 *
 * The in-game analyzer has always searched to a fixed depth 17 on every device,
 * from the weakest phone to a 16-core desktop. This module picks a depth from
 * cheap, deterministic device signals so stronger devices contribute stronger
 * evidence — while guaranteeing the weakest device gets EXACTLY today's behavior.
 *
 * Two properties are load-bearing:
 *
 * 1. `BASELINE_DEPTH` is a FLOOR, never a target. Any signal failure, any unknown
 *    browser, any thrown lookup falls back to 17, so the change can only ever be
 *    neutral-or-better and never regresses an existing device.
 * 2. The chosen depth is FIXED for the whole page session (see
 *    `sessionAnalysisDepth`). Every browser-game-v2 row a session uploads then
 *    carries the same `search_limit_value`, so per-slot upload coalescing and
 *    incremental/final re-uploads can never mix depths within one session.
 *
 * Deliberately NOT a benchmark: no timing probe, no warm-up search. A benchmark
 * would be nondeterministic, would vary with what else the device is doing at
 * that instant, and would make provenance a function of measurement noise.
 */

/** Today's `DEFAULT_SEARCH_DEPTH`. The floor — never regress below it. */
export const BASELINE_DEPTH = 17

/**
 * Ceiling on the nominal search DEPTH.
 *
 * Currently pinned AT the baseline: this lands as a strict behavioral parity
 * no-op. Raising it is gated on the §1.5 latency acceptance benchmark (per-move
 * p95/p99 on the WEAKEST device each tier admits, including the adversarial
 * 8-core / `deviceMemory`-unavailable cell that `computeDeviceAnalysisDepth`
 * routes to the high tier), and is enabled TOGETHER with the worker's
 * `MAX_ANALYSIS_MS` wall-clock cap — because the depth ceiling alone does NOT
 * bound latency (`go depth N` has no time limit).
 *
 * 21 is reserved for the visible analysis-board reuse path (g-reuse-d21-search),
 * so the tiers must stay strictly below it.
 */
export const MAX_DEVICE_DEPTH = 17

/** The tier ladder, strongest first. Each entry is `[minCores, minMemoryGb, depth]`. */
const TIERS: ReadonlyArray<readonly [number, number, number]> = [
  [8, 8, 20], // high
  [4, 4, 18], // mid
]

const clamp = (depth: number) =>
  Math.min(MAX_DEVICE_DEPTH, Math.max(BASELINE_DEPTH, depth))

/**
 * Pick this device's in-game analysis depth from `navigator` signals.
 *
 * `nav` is OPTIONAL and defaults to `undefined`, NOT to `navigator`. A
 * `nav = navigator` default expression is evaluated at CALL time and throws
 * wherever the `navigator` global is absent (some workers, SSR, test envs) —
 * defeating the very fallback it looks like it provides. The global is read
 * defensively inside the try below instead, and a missing one is simply the low
 * tier.
 *
 * `deviceMemory` is Chromium-only, so `undefined` means UNKNOWN, not "small":
 * vetoing on it would pin every Safari/Firefox device to the baseline forever.
 * The latency risk this opens on a high-core device with no memory signal is the
 * specific target of the §1.5 acceptance gate and the worker's wall-clock cap.
 *
 * Never throws.
 */
export function computeDeviceAnalysisDepth(nav?: Navigator): number {
  try {
    const n =
      nav ??
      (typeof globalThis !== 'undefined'
        ? (globalThis as { navigator?: Navigator }).navigator
        : undefined)
    if (!n) return BASELINE_DEPTH

    const cores = n.hardwareConcurrency
    if (typeof cores !== 'number' || !Number.isFinite(cores) || cores <= 0) {
      return BASELINE_DEPTH
    }
    const mem = (n as Navigator & { deviceMemory?: number }).deviceMemory

    for (const [minCores, minMem, depth] of TIERS) {
      if (cores >= minCores && (mem === undefined || mem >= minMem)) {
        return clamp(depth)
      }
    }
    return BASELINE_DEPTH
  } catch {
    return BASELINE_DEPTH
  }
}

let cachedDepth: number | null = null

/**
 * This page/worker session's analysis depth — computed once, memoized forever.
 *
 * Device signals do not change within a page lifetime, so recomputing per move
 * would only add cost. More importantly, a session-constant depth is what makes
 * provenance homogeneous: every move a session uploads claims the SAME
 * `search_limit_value`, so the deferred scheduler's per-slot last-write-wins
 * coalescing can never produce a slot whose payload and provenance disagree.
 * A page reload recomputes.
 */
export function sessionAnalysisDepth(): number {
  if (cachedDepth === null) {
    cachedDepth = computeDeviceAnalysisDepth()
  }
  return cachedDepth
}

/** Test-only: drop the memoized depth so the next call recomputes. */
export function resetSessionAnalysisDepthForTests(): void {
  cachedDepth = null
}
