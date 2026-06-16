import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// posthog-js is a singleton with side effects on init; mock it so tests never
// touch the network and we can assert on the calls our wrapper makes.
const initMock = vi.fn()
const identifyMock = vi.fn()
const resetMock = vi.fn()

vi.mock('posthog-js', () => ({
  default: { init: initMock, identify: identifyMock, reset: resetMock },
}))

describe('analytics/posthog', () => {
  beforeEach(() => {
    // The module keeps `enabled` state at module scope; reset between tests so
    // each test starts from a clean, uninitialized singleton.
    vi.resetModules()
    initMock.mockClear()
    identifyMock.mockClear()
    resetMock.mockClear()
    vi.unstubAllEnvs()
  })

  afterEach(() => {
    vi.unstubAllEnvs()
  })

  it('does not initialize when no token is present', async () => {
    vi.stubEnv('VITE_PUBLIC_POSTHOG_PROJECT_TOKEN', '')
    vi.stubEnv('VITE_PUBLIC_POSTHOG_DISABLED', '')
    const mod = await import('./posthog')
    mod.initAnalytics()
    expect(initMock).not.toHaveBeenCalled()
    expect(mod.isAnalyticsEnabled()).toBe(false)
  })

  it('does not initialize when explicitly disabled even with a token', async () => {
    vi.stubEnv('VITE_PUBLIC_POSTHOG_PROJECT_TOKEN', 'phc_test')
    vi.stubEnv('VITE_PUBLIC_POSTHOG_DISABLED', 'true')
    const mod = await import('./posthog')
    mod.initAnalytics()
    expect(initMock).not.toHaveBeenCalled()
    expect(mod.isAnalyticsEnabled()).toBe(false)
  })

  it('no-ops identify/reset when disabled', async () => {
    vi.stubEnv('VITE_PUBLIC_POSTHOG_PROJECT_TOKEN', '')
    const mod = await import('./posthog')
    mod.initAnalytics()
    mod.identifyUser('1', { username: 'x', is_anonymous: true })
    mod.resetAnalytics()
    expect(identifyMock).not.toHaveBeenCalled()
    expect(resetMock).not.toHaveBeenCalled()
  })

  it('initializes with the expected config when a token is present', async () => {
    vi.stubEnv('VITE_PUBLIC_POSTHOG_PROJECT_TOKEN', 'phc_test')
    vi.stubEnv('VITE_PUBLIC_POSTHOG_HOST', 'https://eu.i.posthog.com')
    vi.stubEnv('VITE_PUBLIC_POSTHOG_DISABLED', '')
    const mod = await import('./posthog')
    mod.initAnalytics()
    expect(mod.isAnalyticsEnabled()).toBe(true)
    expect(initMock).toHaveBeenCalledTimes(1)
    expect(initMock).toHaveBeenCalledWith(
      'phc_test',
      expect.objectContaining({
        api_host: 'https://eu.i.posthog.com',
        person_profiles: 'identified_only',
        capture_pageview: 'history_change',
        disable_session_recording: true,
        autocapture: true,
      }),
    )
  })

  it('defaults the host when VITE_PUBLIC_POSTHOG_HOST is unset', async () => {
    vi.stubEnv('VITE_PUBLIC_POSTHOG_PROJECT_TOKEN', 'phc_test')
    vi.stubEnv('VITE_PUBLIC_POSTHOG_HOST', '')
    vi.stubEnv('VITE_PUBLIC_POSTHOG_DISABLED', '')
    const mod = await import('./posthog')
    mod.initAnalytics()
    expect(initMock).toHaveBeenCalledWith(
      'phc_test',
      expect.objectContaining({ api_host: 'https://us.i.posthog.com' }),
    )
  })

  it('is idempotent — initializes only once', async () => {
    vi.stubEnv('VITE_PUBLIC_POSTHOG_PROJECT_TOKEN', 'phc_test')
    vi.stubEnv('VITE_PUBLIC_POSTHOG_DISABLED', '')
    const mod = await import('./posthog')
    mod.initAnalytics()
    mod.initAnalytics()
    expect(initMock).toHaveBeenCalledTimes(1)
  })

  it('forwards identify/reset to the singleton when enabled', async () => {
    vi.stubEnv('VITE_PUBLIC_POSTHOG_PROJECT_TOKEN', 'phc_test')
    vi.stubEnv('VITE_PUBLIC_POSTHOG_DISABLED', '')
    const mod = await import('./posthog')
    mod.initAnalytics()
    mod.identifyUser('42', { username: 'ghost_x', is_anonymous: true })
    expect(identifyMock).toHaveBeenCalledWith('42', {
      username: 'ghost_x',
      is_anonymous: true,
    })
    mod.resetAnalytics()
    expect(resetMock).toHaveBeenCalledTimes(1)
  })
})
