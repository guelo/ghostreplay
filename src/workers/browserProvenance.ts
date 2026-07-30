import type { BrowserAnalysisProvenance } from '../types/analysis'
import type { AnalysisWorkerResponse } from './analysisMessages'
import {
  BROWSER_ENGINE_IDENTITY,
  BROWSER_ENGINE_RESOURCES,
} from './browserEngineIdentity'

/**
 * Compose this device's `browser-game-v2` provenance for a search that ran to the
 * given depth (g-mk1d §2.1).
 *
 * The engine identity half comes from the GENERATED `browserEngineIdentity`
 * module, which is derived by hashing the bundled WASM — never hand-copied — so a
 * stale constant cannot silently mislabel rows after an engine bump. The search
 * half is the caller's session depth.
 *
 * COMPOSITION ONLY — it asks no question about whether the caller has earned the
 * claim. Production code reaches it through `workerTupleProvenance` below and
 * nowhere else (enforced structurally in this module's test); a direct call is how
 * a consumer ends up re-deriving the eligibility rule. Still exported so tests can
 * name the expected claim without round-tripping the gate they are checking.
 */
export const buildBrowserProvenance = (
  searchDepth: number,
): BrowserAnalysisProvenance => ({
  ...BROWSER_ENGINE_IDENTITY,
  search_limit_type: 'depth',
  search_limit_value: searchDepth,
  ...BROWSER_ENGINE_RESOURCES,
})

/**
 * The ONE gate every worker-result consumer must go through to stamp a fresh
 * tuple: this device's provenance when the worker declared the tuple eligible,
 * null otherwise (g-two-search-grade §9.1, g-coord-noncanon-prov).
 *
 * Takes the whole response rather than a boolean ON PURPOSE. Provenance labels
 * BY OMISSION — absent means `browser-game-v1`, present means `browser-game-v2`
 * with a depth claim — so a consumer that re-derives the eligibility rule and
 * forgets a condition mislabels silently. That is exactly how all three
 * consumers came to gate on `capFired` alone and stamp a depth claim on a
 * delta-band fallback tuple whose classification never came from
 * `classifyMoveAdvanced`. The worker owns the rule (`!capFired && canonical` on
 * the legacy arm, `false` on every candidate arm); consumers only pass the
 * message through here, and cannot become eligible by forgetting a condition.
 *
 * `searchDepth` stays the CALLER's to supply: the caller owns the depth it asked
 * the worker for, and therefore owns the claim built from it (see
 * `AnalyzeMoveMessage.depth`).
 *
 * NOT for cache-sourced or canonically-reconciled results — those are not raw
 * worker output and carry null unconditionally (see `reconcileTrustedBest`).
 */
export const workerTupleProvenance = (
  message: Extract<AnalysisWorkerResponse, { type: 'analysis' }>,
  searchDepth: number,
): BrowserAnalysisProvenance | null =>
  message.evidenceEligible ? buildBrowserProvenance(searchDepth) : null
