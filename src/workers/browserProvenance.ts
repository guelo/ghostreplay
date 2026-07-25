import type { BrowserAnalysisProvenance } from '../types/analysis'
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
 * Call this ONLY for a fresh local search that both reached its configured limit
 * and has not been rewritten with non-worker facts; see `AnalysisResult.provenance`.
 */
export const buildBrowserProvenance = (
  searchDepth: number,
): BrowserAnalysisProvenance => ({
  ...BROWSER_ENGINE_IDENTITY,
  search_limit_type: 'depth',
  search_limit_value: searchDepth,
  ...BROWSER_ENGINE_RESOURCES,
})
