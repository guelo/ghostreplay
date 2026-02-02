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
    include: ['src/**/*.{test,spec}.{js,mjs,cjs,ts,mts,cts,jsx,tsx}'],
  },
})
