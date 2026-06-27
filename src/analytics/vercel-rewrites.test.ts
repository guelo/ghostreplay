import { describe, it, expect } from 'vitest'
import vercelConfig from '../../vercel.json'

// Guards the PostHog reverse-proxy rewrite ORDER in repo-root vercel.json.
// Ordering is the most fragile part of the fix (Vercel rewrites are
// first-match-wins): the two `us-assets` rules must precede the broad `/ingest`
// rule, and all three must precede the SPA catch-all — otherwise remote config
// mis-routes to the ingestion host or `/ingest/*` is swallowed by index.html.
//
// The broad rule MUST use the raw-regex `/ingest/(.*)` + `$1`, NOT the
// path-segment `:path*` form: Vercel's `:path*` does not match a trailing
// slash, so the SDK's trailing-slash ingestion endpoints (`/e/`, `/i/v0/e/`,
// `/decide/`, `/flags/`, `/batch/`) fall through to the SPA catch-all and a
// POST to static index.html returns 405 (g-ml4u). `(.*)` matches slashes.
//
// Vite resolves this JSON import relative to the test file at transform time, so
// it's robust regardless of cwd or the jsdom test environment.
describe('vercel.json PostHog rewrites', () => {
  const rewrites = (
    vercelConfig as { rewrites: Array<{ source: string; destination: string }> }
  ).rewrites

  const idx = (source: string) => rewrites.findIndex((r) => r.source === source)
  const dest = (source: string) =>
    rewrites.find((r) => r.source === source)?.destination

  it('routes the static-assets and remote-config paths to the assets host', () => {
    expect(dest('/ingest/static/:path*')).toBe(
      'https://us-assets.i.posthog.com/static/:path*',
    )
    expect(dest('/ingest/array/:path*')).toBe(
      'https://us-assets.i.posthog.com/array/:path*',
    )
  })

  it('routes the broad ingestion path to the ingestion host via slash-agnostic regex', () => {
    // `(.*)` (not `:path*`) so trailing-slash endpoints match — see file header.
    expect(dest('/ingest/(.*)')).toBe('https://us.i.posthog.com/$1')
  })

  it('orders the assets rules before the broad ingestion rule', () => {
    expect(idx('/ingest/static/:path*')).toBeGreaterThanOrEqual(0)
    expect(idx('/ingest/array/:path*')).toBeGreaterThanOrEqual(0)
    expect(idx('/ingest/static/:path*')).toBeLessThan(idx('/ingest/(.*)'))
    expect(idx('/ingest/array/:path*')).toBeLessThan(idx('/ingest/(.*)'))
  })

  it('orders all PostHog rules before the SPA catch-all', () => {
    const catchAll = idx('/(.*)')
    expect(catchAll).toBeGreaterThanOrEqual(0)
    expect(idx('/ingest/static/:path*')).toBeLessThan(catchAll)
    expect(idx('/ingest/array/:path*')).toBeLessThan(catchAll)
    expect(idx('/ingest/(.*)')).toBeLessThan(catchAll)
  })
})
