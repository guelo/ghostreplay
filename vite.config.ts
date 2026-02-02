import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const resolveShim = (relative: string) =>
  path.resolve(__dirname, 'src', 'shims', 'node', relative)

// https://vite.dev/config/
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
  server: {
    headers: {
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
    },
  },
  preview: {
    headers: {
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
    },
  },
})
