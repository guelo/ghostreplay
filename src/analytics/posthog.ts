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

const DEFAULT_HOST = 'https://us.i.posthog.com'

let enabled = false

function isDisabled(): boolean {
  return import.meta.env.VITE_PUBLIC_POSTHOG_DISABLED === 'true'
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
  if (!token || isDisabled()) return
  posthog.init(token, {
    api_host: import.meta.env.VITE_PUBLIC_POSTHOG_HOST || DEFAULT_HOST,
    // A recent `defaults` date opts into current PostHog defaults, which makes
    // `capture_pageview: 'history_change'` (SPA route views) the default; we
    // also set it explicitly so the behavior is pinned regardless of the date.
    defaults: '2026-01-30',
    person_profiles: 'identified_only',
    capture_pageview: 'history_change',
    autocapture: true,
    disable_session_recording: true,
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

export { posthog }
