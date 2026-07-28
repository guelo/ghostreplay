/**
 * §15.1 C7 two-key opt-in — the WORKER-side message types (g-two-search-grade
 * §15.1).
 *
 * These live under `candidates/` because §15.2 deletes this directory whole on a
 * rejection verdict, and the bench handshake is one of the things it names for
 * deletion. `src/bench/benchProtocol.ts` imports them from here rather than
 * keeping a second copy: two hand-kept declarations of one wire format drift,
 * and a benchmark that mislabels its arm is worse than one that does not run.
 *
 * bench → workers is the allowed direction (`src/bench/isolation.test.ts`
 * forbids only the reverse), so nothing here may import from `src/bench`.
 */

import type { AnalysisProtocol, AnalyzeMoveMessage } from '../analysisMessages'

/**
 * A protocol a candidate arm can be selected as — every `AnalysisProtocol`
 * except the shipping one.
 *
 * Derived rather than restated, so the arm list and the response discriminator
 * cannot disagree about what a candidate is.
 */
export type CandidateArm = Exclude<AnalysisProtocol, 'legacy'>

const CANDIDATE_ARMS: Record<CandidateArm, true> = {
  variantA: true,
  variantB: true,
}

/**
 * Whether an arbitrary wire value names a candidate arm AT ALL.
 *
 * Separate from whether this build can DISPATCH it (`candidates/index.ts`): a
 * known-but-unavailable arm and an unknown string are both refused, but only the
 * first is a message the runner could legitimately send to a later build.
 */
export const isCandidateArm = (value: unknown): value is CandidateArm =>
  typeof value === 'string' && Object.hasOwn(CANDIDATE_ARMS, value)

/** Key 1: put the worker into benchmark mode. Answered with `bench-ready`. */
export type BenchInitMessage = {
  type: 'bench-init'
  bench: true
}

export type BenchReadyMessage = {
  type: 'bench-ready'
  /** The candidate arms this worker build can actually dispatch. */
  arms: string[]
}

/**
 * An analyze-move carrying the per-message arm selector.
 *
 * Structurally a production `AnalyzeMoveMessage` plus one optional field, so the
 * worker's existing handler reads it unchanged and the default arm produces a
 * byte-identical message (C1). The selector is deliberately NOT on
 * `AnalyzeMoveMessage` itself — production callers cannot express it.
 */
export type BenchAnalyzeMoveMessage = AnalyzeMoveMessage & {
  arm?: CandidateArm
}
