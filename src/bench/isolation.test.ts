import { readFileSync, readdirSync } from 'node:fs'
import { relative, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * The benchmark harness must be unreachable from the app.
 *
 * `analysisWorker.test.ts` pins that no production path calls
 * `selectAtomicSnapshot` (g-two-search-grade §4.3, §15.1 C9), and exempts
 * `src/bench` from that list because the harness only REPORTS §4.2 acceptance
 * over a transcript it observed. That exemption is only sound while the harness
 * cannot run inside the app — so this test is the other half of it: nothing
 * outside `src/bench` may import from `src/bench`.
 *
 * It is also what keeps §15.2's removal cheap. Deleting the runner on a
 * rejection verdict must not require untangling app code.
 */
const SRC_ROOT = resolve(__dirname, '..')

const sourceFiles = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = resolve(dir, entry.name)
    if (entry.isDirectory()) return sourceFiles(full)
    return /\.tsx?$/.test(entry.name) ? [full] : []
  })

/** `import … from '../bench/x'`, `export … from './bench/x'`, `import('../bench/x')`. */
const IMPORTS_BENCH = /(?:from\s*|import\s*\(\s*)['"](?:\.{1,2}\/)+bench\//

describe('benchmark harness isolation', () => {
  it('is not imported by anything outside src/bench', () => {
    const importers = sourceFiles(SRC_ROOT)
      .map((file) => relative(SRC_ROOT, file))
      .filter((file) => !file.startsWith('bench/'))
      .filter((file) => IMPORTS_BENCH.test(readFileSync(resolve(SRC_ROOT, file), 'utf8')))
      .sort()

    expect(importers).toEqual([])
  })

  it('detects an importer, so the check above cannot pass vacuously', () => {
    expect(IMPORTS_BENCH.test("import { runBench } from '../bench/device/runner'")).toBe(true)
    expect(IMPORTS_BENCH.test("const m = await import('./bench/summarize')")).toBe(true)
    expect(IMPORTS_BENCH.test("import { x } from '../workers/pvSnapshots'")).toBe(false)
  })
})
