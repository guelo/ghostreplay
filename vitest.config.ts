import path from 'node:path'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

const resolveShim = (relative: string) =>
  path.resolve(__dirname, 'src', 'shims', 'node', relative)

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      path: resolveShim('path.ts'),
      fs: resolveShim('fs.ts'),
      worker_threads: resolveShim('workerThreads.ts'),
      perf_hooks: resolveShim('perfHooks.ts'),
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: true,
    // Fake-timer + debounced-cache tests flush promises/microtasks via
    // vi.advanceTimersByTimeAsync(0). Under parallel worker load on CI / the
    // pre-push hook, that interleaving can exceed the 5s default and time out
    // even though nothing is broken (g-k7y8). Give those waits more headroom.
    testTimeout: 15000,
    // Many test files spawn real Web Workers, so running one vitest worker per
    // core oversubscribes the CPU and starves fake-timer/microtask flushing,
    // making debounced-cache tests flaky (g-k7y8). Cap concurrency to keep the
    // timer/microtask interleaving deterministic without serializing the suite.
    maxWorkers: 2,
    include: ['src/**/*.{test,spec}.{js,mjs,cjs,ts,mts,cts,jsx,tsx}'],
  },
})
