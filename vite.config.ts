import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const resolveShim = (relative: string) =>
  path.resolve(__dirname, 'src', 'shims', 'node', relative)

// Same-origin reverse proxy for PostHog (mirrors the vercel.json rewrites).
// The default `api_host` is `/ingest`, so dev AND preview must forward it to
// PostHog or analytics 404s / breaks under the COEP `require-corp` headers both
// servers emit. Order matters — Vite proxy is first-match-wins (object key
// order, preserved by spread), so the two `us-assets` rules MUST precede the
// broad `/ingest` rule. `changeOrigin` so SNI/Host matches upstream; rewrite
// strips the `/ingest` prefix the SDK adds in CUSTOM-region mode.
const posthogProxy = {
  '/ingest/static': {
    target: 'https://us-assets.i.posthog.com',
    changeOrigin: true,
    rewrite: (p: string) => p.replace(/^\/ingest/, ''),
  },
  '/ingest/array': {
    target: 'https://us-assets.i.posthog.com',
    changeOrigin: true,
    rewrite: (p: string) => p.replace(/^\/ingest/, ''),
  },
  '/ingest': {
    target: 'https://us.i.posthog.com',
    changeOrigin: true,
    rewrite: (p: string) => p.replace(/^\/ingest/, ''),
  },
}

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
      ...posthogProxy,
    },
  },
  build: {
    rollupOptions: {
      output: {
        // Split the two large, rarely-changing vendors out of the entry chunk.
        // They are still loaded eagerly (posthog must init before first paint),
        // so this buys cache stability across deploys rather than fewer bytes;
        // the actual first-load win comes from the lazy routes in AppRoutes.tsx.
        manualChunks: {
          'vendor-react': ['react', 'react-dom', 'react-dom/client'],
          'vendor-posthog': ['posthog-js', '@posthog/react'],
        },
      },
    },
  },
  preview: {
    headers: {
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
    },
    proxy: { ...posthogProxy },
  },
})
