import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { resolve } from 'node:path'
import { describe, it, expect } from 'vitest'
import {
  BROWSER_ENGINE_IDENTITY,
  BROWSER_ENGINE_RESOURCES,
} from './browserEngineIdentity'
import { buildBrowserProvenance } from './browserProvenance'

/**
 * REPOSITORY DRIFT GUARD (g-mk1d §2.1).
 *
 * The backend accepts any SYNTACTICALLY valid dynamic identity — that is the
 * declared threat model — so a stale committed constant does not fail anywhere at
 * runtime: it silently mislabels every row the fleet uploads. The only place that
 * can be caught is here, by re-deriving the identity from the artifact that is
 * actually bundled and cross-checking it against the backend registry.
 *
 * This closes REPOSITORY drift only. It cannot (and is not meant to) stop a
 * modified client self-reporting a valid-looking identity; that residual is
 * acceptable because forged browser provenance only reorders non-authoritative
 * rows within the browser tier.
 */
const repoRoot = resolve(__dirname, '../..')

const manifest = JSON.parse(
  readFileSync(resolve(repoRoot, 'scripts/browser-engine-manifest.json'), 'utf8'),
) as { wasm_path: string; eval_file_id: string; engine_version: string }

const backendProfiles = readFileSync(
  resolve(repoRoot, 'backend/app/analysis_profiles.py'),
  'utf8',
)

/** Pull a quoted identity constant out of the backend registry source. */
const backendConstant = (name: string) => {
  const match = backendProfiles.match(new RegExp(`"${name}":\\s*\\(?\\s*\\n?\\s*"([^"]*)"(?:\\s*\\n?\\s*"([^"]*)")?`))
  if (!match) throw new Error(`backend constant ${name} not found`)
  return `${match[1]}${match[2] ?? ''}`
}

describe('browserEngineIdentity', () => {
  it('engine_build is the hash of the WASM this app actually bundles', () => {
    const wasm = readFileSync(resolve(repoRoot, manifest.wasm_path))
    const hash = createHash('sha256').update(wasm).digest('hex')
    expect(BROWSER_ENGINE_IDENTITY.engine_build).toBe(hash)
  })

  it('matches the backend registry, which loads the SAME lite-single artifact', () => {
    // browser-game and browser-analysis run one and the same engine binary, so a
    // bump on one side alone is always a bug.
    expect(BROWSER_ENGINE_IDENTITY.engine_build).toBe(backendConstant('engine_build'))
    expect(BROWSER_ENGINE_IDENTITY.eval_file_id).toBe(backendConstant('eval_file_id'))
  })

  it('pins the same network the shared manifest declares', () => {
    // The lite-single build EMBEDS its net, so there is no standalone .nnue file
    // to hash — the WASM hash pins it transitively and the manifest records which
    // network that is.
    expect(BROWSER_ENGINE_IDENTITY.eval_file_id).toBe(manifest.eval_file_id)
    expect(BROWSER_ENGINE_IDENTITY.engine_version).toBe(manifest.engine_version)
  })

  it('declares the resources the in-game worker actually sets', () => {
    expect(BROWSER_ENGINE_RESOURCES).toEqual({ threads: 1, hash_mb: 128 })
  })

  it('self-reports no FIXED identity field', () => {
    // multipv / engine_name / analyzer protocol / digest are server-stamped. A
    // client that could name them could claim an identity it did not earn.
    const provenance = buildBrowserProvenance(17)
    expect(Object.keys(provenance).sort()).toEqual([
      'engine_build',
      'engine_version',
      'eval_file_id',
      'hash_mb',
      'search_limit_type',
      'search_limit_value',
      'threads',
    ])
  })
})

describe('buildBrowserProvenance', () => {
  it('reports the requested depth as a depth-typed search limit', () => {
    expect(buildBrowserProvenance(20)).toMatchObject({
      search_limit_type: 'depth',
      search_limit_value: 20,
      engine_build: BROWSER_ENGINE_IDENTITY.engine_build,
      eval_file_id: BROWSER_ENGINE_IDENTITY.eval_file_id,
    })
  })
})
