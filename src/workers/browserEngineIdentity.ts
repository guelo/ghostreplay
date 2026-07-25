/**
 * GENERATED FILE — do not edit by hand.
 * Run `npm run gen:engine-identity` after any change to the bundled Stockfish.
 *
 * The fixed half of this device's browser-game-v2 provenance (g-mk1d): the
 * identity of the engine artifact this app actually ships. `engineBuild` is the
 * SHA-256 of node_modules/stockfish/bin/stockfish-18-lite-single.wasm (stockfish@18.0.7); because the
 * lite-single build embeds its single net, that hash transitively pins the network
 * as well, and `evalFileId` records WHICH network it is (there is no standalone
 * .nnue file to hash).
 *
 * A CI test re-hashes the bundled WASM and cross-checks these values against the
 * backend registry, so an engine bump on one side alone fails loudly instead of
 * silently mislabeling uploaded evidence.
 */
export const BROWSER_ENGINE_IDENTITY = {
  engine_version: '18',
  engine_build: 'a8fbc05ec6920b56d7485826dcb02c5ffd2826bcbf751cf973046f237a9096f1',
  eval_file_id: 'nn-9067e33176e8.nnue:9067e33176e8c5edb7aa8db6a3aedd012f84a1f39872e86357c6c2d0993f314d',
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
