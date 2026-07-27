import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Build stamp for the analysis benchmark (g-grade-device-runner). A committed
// baseline JSONL has to name the orchestration bytes it measured, and a phone run
// has no shell to ask git — so the revision is injected here, at build time. Read
// only by src/bench; the app never references these, so they cost the app bundle
// nothing.
const git = (args: string[]): string | null => {
  try {
    // stderr ignored: outside a git checkout this is expected to fail, and a build
    // log should not carry a scary git error for a field only the benchmark reads.
    return execFileSync('git', args, {
      cwd: __dirname,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim()
  } catch {
    return null
  }
}
const gitRevision = git(['rev-parse', 'HEAD'])
// A dirty tree means the revision under-specifies the bundle, which the benchmark
// reports as a method warning rather than leaving a reader to assume otherwise.
const gitDirty = gitRevision === null ? null : git(['status', '--porcelain']) !== ''

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
  define: {
    __BENCH_GIT_REV__: JSON.stringify(gitRevision),
    __BENCH_GIT_DIRTY__: JSON.stringify(gitDirty),
  },
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
      // The analysis device benchmark (g-grade-device-runner) is a SECOND html
      // entry, built only under BENCH=1. §10.1 requires it to load the actual
      // bundled worker, so it cannot live on the dev server alone — but it must
      // also never ship: an unlisted page in a production deploy is a footgun,
      // and gating the input keeps a normal `npm run build` emitting exactly one
      // entry. Use `npm run bench:device:build && npm run bench:device:preview`.
      input:
        process.env.BENCH === '1'
          ? {
              main: path.resolve(__dirname, 'index.html'),
              bench: path.resolve(__dirname, 'bench', 'device', 'index.html'),
            }
          : undefined,
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
