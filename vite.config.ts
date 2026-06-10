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
    proxy: {
      // Forward API calls to the backend so LAN clients (e.g. a phone on the
      // same wifi) that resolve the API base to "/api" reach the backend
      // instead of hitting the Vite dev server (which 404s).
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  preview: {
    headers: {
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
    },
  },
})
