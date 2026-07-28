/**
 * §15.1 C7 two-key opt-in — the HOST half (g-two-search-grade §15.1).
 *
 * Benchmark mode is enabled at worker init AND the candidate is chosen by a
 * per-message selector; either key alone does nothing. The selector field is
 * declared here, on a bench-only message type, and deliberately NOT on the
 * production `AnalyzeMoveMessage` — production callers cannot express it.
 *
 * §15.2 DELETES this module (and the runner plumbing that uses it) on a
 * rejection verdict.
 *
 * The MESSAGE TYPES live with the worker half, in
 * `src/workers/candidates/benchMessages.ts`, and are re-exported here for this
 * module's existing callers. One declaration, not two: a benchmark that
 * mislabels its arm is worse than one that does not run, and two hand-kept
 * copies of a wire format are how a mislabel gets in. `bench → workers` is the
 * allowed import direction (`isolation.test.ts` forbids only the reverse), and
 * §15.2 deletes the whole `candidates/` directory with this module.
 *
 * A build WITHOUT the worker-side half leaves `bench-init` unanswered — an
 * unknown message type falls through the worker's `switch` with no runtime
 * effect — so `enableBenchMode` resolves to NO arms and the runner refuses any
 * non-default arm rather than silently measuring the current protocol under a
 * candidate's label.
 */

import type { AnalyzeMoveMessage } from '../workers/analysisMessages'
import type {
  BenchAnalyzeMoveMessage,
  BenchInitMessage,
  BenchReadyMessage,
} from '../workers/candidates/benchMessages'
import type { BenchArm } from './benchRecord'

export type {
  BenchAnalyzeMoveMessage,
  BenchInitMessage,
  BenchReadyMessage,
} from '../workers/candidates/benchMessages'

/** The arm that needs no opt-in: today's shipping three-search protocol. */
export const DEFAULT_ARM: BenchArm = 'current'

/** The subset of `Worker` the handshake needs, so tests can pass a fake. */
export type BenchWorkerLike = {
  postMessage: (message: unknown) => void
  addEventListener: (type: 'message', listener: (event: MessageEvent) => void) => void
  removeEventListener: (type: 'message', listener: (event: MessageEvent) => void) => void
}

export const BENCH_HANDSHAKE_TIMEOUT_MS = 3_000

/**
 * Key 1: ask the worker to enter benchmark mode and report which arms it can
 * dispatch.
 *
 * Resolves to `[]` when the worker does not answer within the timeout — the
 * expected outcome against any build without the worker-side C7 half. Never
 * rejects: "no arms" is a normal, reportable state, not a runner failure.
 */
export const enableBenchMode = (
  worker: BenchWorkerLike,
  timeoutMs = BENCH_HANDSHAKE_TIMEOUT_MS,
): Promise<string[]> =>
  new Promise((resolve) => {
    let settled = false
    const finish = (arms: string[]) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      worker.removeEventListener('message', onMessage)
      resolve(arms)
    }
    const onMessage = (event: MessageEvent) => {
      const data = event.data as BenchReadyMessage | { type?: string }
      if (data && data.type === 'bench-ready') {
        finish(Array.isArray((data as BenchReadyMessage).arms) ? (data as BenchReadyMessage).arms : [])
      }
    }
    const timer = setTimeout(() => finish([]), timeoutMs)
    worker.addEventListener('message', onMessage)
    worker.postMessage({ type: 'bench-init', bench: true } satisfies BenchInitMessage)
  })

/**
 * Key 2: stamp the arm on one analyze-move.
 *
 * The default arm returns the request UNCHANGED — same object shape, no `arm`
 * key — so a `current` run posts exactly what production posts (C1). Any other
 * arm adds the selector, which only a bench-mode worker acts on.
 */
export const buildAnalyzeMessage = (
  request: AnalyzeMoveMessage,
  arm: BenchArm = DEFAULT_ARM,
): BenchAnalyzeMoveMessage =>
  arm === DEFAULT_ARM ? request : { ...request, arm }

/**
 * Whether this run may proceed with `arm`, given what the handshake advertised.
 *
 * Returns an error string to surface to the operator, or null when the arm is
 * runnable. This is the guard that keeps an unacknowledged handshake from
 * producing rows labelled with an arm the worker never dispatched.
 */
export const armUnavailableReason = (arm: BenchArm, availableArms: readonly string[]): string | null => {
  if (arm === DEFAULT_ARM) {
    return null
  }
  if (availableArms.includes(arm)) {
    return null
  }
  return (
    `arm "${arm}" is not available in this worker build ` +
    `(bench mode advertised: ${availableArms.length > 0 ? availableArms.join(', ') : 'none'}). ` +
    'variantB lands in g-grade-variant-b; until then only variantA is dispatchable.'
  )
}
