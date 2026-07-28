/**
 * The candidate registry — what this worker build can actually dispatch
 * (g-two-search-grade §15.1 C7).
 *
 * `Partial`, and holding only what is implemented: `bench-ready` advertises
 * exactly these keys, so the device runner refuses `variantB` until
 * g-grade-variant-b lands it rather than silently measuring the current protocol
 * under a candidate's label.
 */

import type { CandidateArm } from './benchMessages'
import type { CandidateProtocol } from './contract'
import { variantA } from './variantA'

export const CANDIDATE_PROTOCOLS: Partial<Record<CandidateArm, CandidateProtocol>> = {
  variantA,
}

/** The arms `bench-ready` advertises, in a stable order. */
export const availableCandidateArms = (): CandidateArm[] =>
  (Object.keys(CANDIDATE_PROTOCOLS) as CandidateArm[]).sort()
