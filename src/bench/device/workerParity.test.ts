import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * The device runner's premise is that it measures the SHIPPING worker
 * (g-two-search-grade §10.1): "the browser runner must exercise production
 * orchestration... reimplementing orchestration would benchmark the harness
 * rather than the shipping worker."
 *
 * That premise starts at construction. If production ever changes how it builds
 * the analysis worker — a different module, different options, a factory — and
 * the bench keeps the old call, every device number silently describes something
 * production no longer runs. This test fails in that case, which is the signal to
 * update `workerFactory.ts` (or point it at whatever production now uses).
 */
const read = (path: string) => readFileSync(resolve(__dirname, '../../..', path), 'utf8')

/**
 * Normalize a worker construction so only MEANING is compared: whitespace,
 * quote style, and trailing commas are formatting, and the relative depth of the
 * module specifier necessarily differs by directory.
 */
const constructions = (source: string): string[] =>
  [...source.matchAll(/new Worker\(([\s\S]*?)\)\s*\n/g)]
    .map((match) => match[1])
    .filter((text) => text.includes('analysisWorker'))
    .map((text) =>
      text
        .replace(/\s+/g, '')
        .replace(/"/g, "'")
        .replace(/,(?=[)}])/g, '')
        .replace(/,$/, '')
        .replace(/(\.\.\/)+workers\/analysisWorker\.ts/, 'workers/analysisWorker.ts'),
    )

const PRODUCTION_SITES = [
  'src/services/GameAnalysisCoordinator.ts',
  'src/hooks/useMoveAnalysis.ts',
]

describe('analysis worker construction parity', () => {
  it('finds exactly one construction at each production call site', () => {
    for (const path of PRODUCTION_SITES) {
      expect(constructions(read(path)), path).toHaveLength(1)
    }
  })

  it('builds the worker exactly as production does', () => {
    const bench = constructions(read('src/bench/device/workerFactory.ts'))
    expect(bench).toHaveLength(1)

    for (const path of PRODUCTION_SITES) {
      expect(constructions(read(path))[0], `${path} vs bench workerFactory`).toBe(bench[0])
    }
  })

  it('keeps the module type, which decides how the worker is bundled', () => {
    expect(constructions(read('src/bench/device/workerFactory.ts'))[0]).toContain(
      "{type:'module'}",
    )
  })
})
