#!/usr/bin/env node
/**
 * Generate src/workers/browserEngineIdentity.ts from the ACTUALLY BUNDLED engine
 * artifact (g-mk1d §2.1).
 *
 * Why generated rather than hand-maintained: `verify_identity` accepts any
 * SYNTACTICALLY valid dynamic value, so a stale hand-copied `engine_build` — one
 * that no longer describes the shipped WASM after an engine bump — still
 * identity-verifies and silently mislabels every uploaded row. Deriving the hash
 * from the artifact removes that failure mode at the repository level.
 *
 * `engine_build` = SHA-256 of the bundled WASM (the same content-addressed
 * executable-hash semantics the backend registry uses). Because the lite-single
 * build EMBEDS its net, hashing the WASM transitively pins the network too.
 * `eval_file_id` is copied from scripts/browser-engine-manifest.json — there is no
 * standalone .nnue file to hash.
 *
 * Usage: npm run gen:engine-identity
 */
import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')

export const readManifest = () =>
  JSON.parse(readFileSync(resolve(repoRoot, 'scripts/browser-engine-manifest.json'), 'utf8'))

/** SHA-256 of the bundled WASM named by the manifest. */
export const hashBundledWasm = (manifest = readManifest()) =>
  createHash('sha256')
    .update(readFileSync(resolve(repoRoot, manifest.wasm_path)))
    .digest('hex')

const OUTPUT = 'src/workers/browserEngineIdentity.ts'

const render = (manifest, engineBuild) => `/**
 * GENERATED FILE — do not edit by hand.
 * Run \`npm run gen:engine-identity\` after any change to the bundled Stockfish.
 *
 * The fixed half of this device's browser-game-v2 provenance (g-mk1d): the
 * identity of the engine artifact this app actually ships. \`engineBuild\` is the
 * SHA-256 of ${manifest.wasm_path} (${manifest.npm_package}); because the
 * lite-single build embeds its single net, that hash transitively pins the network
 * as well, and \`evalFileId\` records WHICH network it is (there is no standalone
 * .nnue file to hash).
 *
 * A CI test re-hashes the bundled WASM and cross-checks these values against the
 * backend registry, so an engine bump on one side alone fails loudly instead of
 * silently mislabeling uploaded evidence.
 */
export const BROWSER_ENGINE_IDENTITY = {
  engine_version: '${manifest.engine_version}',
  engine_build: '${engineBuild}',
  eval_file_id: '${manifest.eval_file_id}',
} as const

/**
 * The in-game analysis worker's fixed UCI options (analysisWorker.ts): Hash 128,
 * Threads at the engine default of 1. MultiPV is 1 and is part of the SERVER-
 * stamped fixed identity half, so it is deliberately absent here — a client never
 * self-reports a fixed field.
 */
export const BROWSER_ENGINE_RESOURCES = {
  threads: 1,
  hash_mb: 128,
} as const
`

const main = () => {
  const manifest = readManifest()
  const engineBuild = hashBundledWasm(manifest)
  writeFileSync(resolve(repoRoot, OUTPUT), render(manifest, engineBuild))
  console.log(`wrote ${OUTPUT} (engine_build=${engineBuild})`)
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main()
}
