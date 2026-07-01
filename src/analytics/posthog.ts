import posthog from 'posthog-js'

/**
 * PostHog frontend singleton + analytics helpers.
 *
 * Analytics is OFF unless a project token is present AND it is not explicitly
 * disabled — so it never emits in tests (no token / `VITE_PUBLIC_POSTHOG_DISABLED`)
 * and degrades to a no-op everywhere else. The enable decision lives HERE; the
 * helpers below guard on it so callers can fire identity transitions
 * unconditionally without risking a network call or a warning from an
 * uninitialized SDK (notably `reset()` before `init()`).
 */

// Same-origin reverse proxy (see vercel.json rewrites / vite.config.ts proxy).
// PostHog's endpoints don't satisfy COEP `require-corp` (the app is cross-origin
// isolated for the Stockfish WASM SharedArrayBuffer), so cross-origin requests
// to us.i.posthog.com get dropped with "CORS Failed". Routing through `/ingest`
// makes them same-origin, which is exempt from both CORS and the COEP
// resource-policy check. An absolute VITE_PUBLIC_POSTHOG_HOST (e.g. self-hosted
// or EU) still overrides this and bypasses the proxy.
const DEFAULT_HOST = '/ingest'

let enabled = false

function isDisabled(): boolean {
  return import.meta.env.VITE_PUBLIC_POSTHOG_DISABLED === 'true'
}

/**
 * Global Privacy Control (https://globalprivacycontrol.org/) exposes a
 * `navigator.globalPrivacyControl` boolean when the user's browser/extension
 * asserts a do-not-sell/share preference. Unlike Do Not Track, the PostHog SDK
 * does NOT read it (see node_modules/posthog-js consent.js `_getDnt`, which only
 * checks doNotTrack/msDoNotTrack), so we gate init ourselves: when GPC is set we
 * skip initialization entirely — no SDK load, no cookies, no network — matching
 * the "don't init" posture of an explicit opt-out. (DNT itself is left to the
 * SDK via `respect_dnt: true` below.)
 */
function isGpcSignaled(): boolean {
  return (
    typeof navigator !== 'undefined' &&
    (navigator as Navigator & { globalPrivacyControl?: boolean })
      .globalPrivacyControl === true
  )
}

/**
 * Initialize the PostHog singleton. Idempotent and safe to call when disabled
 * (no token or opt-out) — in that case it does nothing and leaves analytics off.
 * Must run before the app renders (see `main.tsx`) so autocapture/pageviews and
 * the `<PostHogProvider client={posthog}>` see an initialized client.
 */
export function initAnalytics(): void {
  if (enabled) return
  const token = import.meta.env.VITE_PUBLIC_POSTHOG_PROJECT_TOKEN
  if (!token || isDisabled() || isGpcSignaled()) return
  const apiHost = import.meta.env.VITE_PUBLIC_POSTHOG_HOST || DEFAULT_HOST
  posthog.init(token, {
    api_host: apiHost,
    // When proxying via a same-origin path, the SDK can't derive the UI host
    // (it replaces `.i.posthog.com` -> `.posthog.com`, which is garbage for
    // `/ingest`), so pin it. For an absolute api_host the SDK derives its own
    // UI host correctly, so leave ui_host unset to respect EU/self-hosted.
    ...(apiHost.startsWith('/') ? { ui_host: 'https://us.posthog.com' } : {}),
    // A recent `defaults` date opts into current PostHog defaults, which makes
    // `capture_pageview: 'history_change'` (SPA route views) the default; we
    // also set it explicitly so the behavior is pinned regardless of the date.
    defaults: '2026-01-30',
    person_profiles: 'identified_only',
    capture_pageview: 'history_change',
    autocapture: true,
    disable_session_recording: true,
    // Honor Do Not Track: with this set the SDK opts capture out when
    // navigator.doNotTrack / msDoNotTrack / window.doNotTrack is yes-like.
    // (GPC is handled separately in isGpcSignaled() above, which the SDK
    // ignores.)
    respect_dnt: true,
  })
  enabled = true
}

/** True once `initAnalytics()` has successfully initialized the singleton. */
export function isAnalyticsEnabled(): boolean {
  return enabled
}

/**
 * Identify the current user so client events join server events on the same
 * person. `distinct_id` is the backend user id (stable across anon -> claimed),
 * so we just re-identify with updated properties rather than aliasing. No-ops
 * when analytics is disabled.
 */
export function identifyUser(
  distinctId: string,
  properties: { username: string; is_anonymous: boolean },
): void {
  if (!enabled) return
  posthog.identify(distinctId, properties)
}

/**
 * Reset analytics identity (clears the distinct id / person association). Called
 * on logout before re-identifying the new anonymous user. No-ops when disabled.
 */
export function resetAnalytics(): void {
  if (!enabled) return
  posthog.reset()
}

/**
 * Capture a custom analytics event. No-ops when analytics is disabled, so call
 * sites can fire unconditionally without guarding (and tests never emit). The
 * enable decision lives here, NOT at the call site.
 */
export function captureEvent(
  event: string,
  properties?: Record<string, unknown>,
): void {
  if (!enabled) return
  posthog.capture(event, properties)
}

export { posthog }
