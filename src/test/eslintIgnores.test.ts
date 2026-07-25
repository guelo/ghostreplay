import { execFileSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { ESLint } from 'eslint'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

// `npm run lint` is `eslint .`, so the globalIgnores list in eslint.config.js is
// the only thing keeping the walk out of churning trees that a concurrent agent
// can delete mid-scan — which aborts the whole run with ENOENT and flakes the
// pre-push lint gate (g-eslint-skip-backend). What matters is that eslint never
// descends into those trees, not merely that their .ts files would be skipped:
// a file-scoped pattern like 'backend/**/*.ts' hides nothing eslint was linting
// anyway and leaves the walk, and the race, exactly where they were. So the
// pruning is proved against a throwaway tree below rather than with
// isPathIgnored, which cannot tell the two spellings apart.

// Trees that must stay out of the walk. Keep in sync with eslint.config.js when
// a new one starts churning.
const prunedTrees = [
  'dist',
  'backend',
  '.beads',
  'coverage',
  'tmp',
  '.test-results',
  'test-results',
  'blob-report',
  'playwright-report',
  'e2e/screenshots/output',
]

// The globalIgnores patterns as node actually evaluates them. Read out of a
// child process because eslint.config.js is untyped JS that `tsc -b` refuses to
// import from a typed test.
const globallyIgnored: string[] = JSON.parse(
  execFileSync(
    process.execPath,
    [
      '-e',
      "import('./eslint.config.js').then((m) => process.stdout.write(JSON.stringify(" +
        'm.default.filter((c) => c.ignores && !c.files).flatMap((c) => c.ignores))))',
    ],
    { cwd: process.cwd(), encoding: 'utf8' },
  ),
)

// A stand-in repo: one .js probe at each depth of every tree that must be
// pruned, plus a control outside them. eslint lints **/*.js out of the box, so
// a probe comes back in the results if and only if eslint walked into its
// directory.
const CONTROL = path.join('linted', 'probe.js')
const probesIn = (tree: string) => [
  path.join(tree, 'probe.js'),
  path.join(tree, 'nested', 'probe.js'),
]

const fixtureRoot = mkdtempSync(path.join(tmpdir(), 'eslint-prune-'))
for (const probe of [CONTROL, ...prunedTrees.flatMap(probesIn)]) {
  const absolute = path.join(fixtureRoot, probe)
  mkdirSync(path.dirname(absolute), { recursive: true })
  writeFileSync(absolute, 'export const probe = 1\n')
}
afterAll(() => rmSync(fixtureRoot, { recursive: true, force: true }))

describe('eslint global ignores', () => {
  let walked: string[]

  beforeAll(async () => {
    const results = await new ESLint({
      cwd: fixtureRoot,
      overrideConfigFile: true,
      overrideConfig: [{ ignores: globallyIgnored }],
    }).lintFiles(['.'])
    walked = results.map((result) => path.relative(fixtureRoot, result.filePath))
  })

  it('walks the fixture at all', () => {
    expect(walked).toContain(CONTROL)
  })

  it.each(prunedTrees)('never descends into %s', (tree) => {
    expect(walked).not.toContain(probesIn(tree)[0])
    expect(walked).not.toContain(probesIn(tree)[1])
  })

  it('hides no tracked TypeScript anywhere in the repo', async () => {
    const eslint = new ESLint()
    const tracked = execFileSync(
      'git',
      ['ls-files', '-z', '--', '*.ts', '*.tsx'],
      { encoding: 'utf8' },
    )
      .split('\0')
      .filter(Boolean)
    expect(tracked.length).toBeGreaterThan(0)

    const ignored: string[] = []
    for (const file of tracked) {
      if (await eslint.isPathIgnored(file)) ignored.push(file)
    }
    expect(ignored).toEqual([])
  })
})
