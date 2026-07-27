/**
 * What the page can say about the bytes it is measuring.
 *
 * `vite.config.ts` injects the git revision and dirty flag at build time, so a
 * phone run — which has no shell, no repo, and no driver — still records which
 * tree produced the bundle it loaded. The driver adds the worker chunk's digest
 * afterwards, because only a process that can read `dist` can hash it.
 *
 * The `typeof` guards matter: under vitest there is no `define`, and referencing
 * an undeclared identifier directly would throw where `typeof` is safe.
 */

import type { BenchSourceStamp } from '../benchRecord'

declare const __BENCH_GIT_REV__: string | null
declare const __BENCH_GIT_DIRTY__: boolean | null

const injected = <T>(read: () => T, fallback: T): T => {
  try {
    return read()
  } catch {
    return fallback
  }
}

export const describeSource = (): BenchSourceStamp => ({
  gitRevision: injected(
    () => (typeof __BENCH_GIT_REV__ === 'undefined' ? null : __BENCH_GIT_REV__),
    null,
  ),
  gitDirty: injected(
    () => (typeof __BENCH_GIT_DIRTY__ === 'undefined' ? null : __BENCH_GIT_DIRTY__),
    null,
  ),
  // Only the scripted driver can hash the built chunk; a hand-run phone capture
  // is bound to its build by the revision above.
  workerBundleFile: null,
  workerBundleSha256: null,
})
