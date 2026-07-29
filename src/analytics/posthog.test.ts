import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// posthog-js is a singleton with side effects on init; mock it so tests never
// touch the network and we can assert on the calls our wrapper makes.
const initMock = vi.fn()
const identifyMock = vi.fn()
const resetMock = vi.fn()
const captureMock = vi.fn()

vi.mock('posthog-js', () => ({
  default: {
    init: initMock,
    identify: identifyMock,
    reset: resetMock,
    capture: captureMock,
  },
}))

describe('analytics/posthog', () => {
  beforeEach(() => {
    // The module keeps `enabled` state at module scope; reset between tests so
    // each test starts from a clean, uninitialized singleton.
    vi.resetModules()
    initMock.mockClear()
    identifyMock.mockClear()
    resetMock.mockClear()
    captureMock.mockClear()
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
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

  it('initializes even when Global Privacy Control is signaled', async () => {
    // f763907 removed the pre-init GPC bail: DNT (via the SDK's own respect_dnt)
    // is the only privacy gate now, and navigator.globalPrivacyControl must not
    // block init on its own.
    vi.stubEnv('VITE_PUBLIC_POSTHOG_PROJECT_TOKEN', 'phc_test')
    vi.stubEnv('VITE_PUBLIC_POSTHOG_DISABLED', '')
    vi.stubGlobal('navigator', { globalPrivacyControl: true })
    const mod = await import('./posthog')
    mod.initAnalytics()
    expect(initMock).toHaveBeenCalledTimes(1)
    expect(mod.isAnalyticsEnabled()).toBe(true)
  })

  it('delegates Do Not Track to the SDK via respect_dnt', async () => {
    vi.stubEnv('VITE_PUBLIC_POSTHOG_PROJECT_TOKEN', 'phc_test')
    vi.stubEnv('VITE_PUBLIC_POSTHOG_DISABLED', '')
    const mod = await import('./posthog')
    mod.initAnalytics()
    expect(initMock).toHaveBeenCalledWith(
      'phc_test',
      expect.objectContaining({ respect_dnt: true }),
    )
  })

  it('no-ops identify/reset/capture when disabled', async () => {
    vi.stubEnv('VITE_PUBLIC_POSTHOG_PROJECT_TOKEN', '')
    const mod = await import('./posthog')
    mod.initAnalytics()
    mod.identifyUser('1', { username: 'x', is_anonymous: true })
    mod.resetAnalytics()
    mod.captureEvent('api_request_client', { route: '/api/x' })
    expect(identifyMock).not.toHaveBeenCalled()
    expect(resetMock).not.toHaveBeenCalled()
    expect(captureMock).not.toHaveBeenCalled()
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
        // Honor Do Not Track: the SDK opts capture out when a yes-like
        // doNotTrack/msDoNotTrack signal is present.
        respect_dnt: true,
      }),
    )
    // An absolute host lets the SDK derive its own UI host — we must NOT pin
    // ui_host to the US proxy value (that would hijack EU/self-hosted).
    expect(initMock.mock.calls[0][1].ui_host).toBeUndefined()
  })

  it('defaults to the same-origin proxy when VITE_PUBLIC_POSTHOG_HOST is unset', async () => {
    vi.stubEnv('VITE_PUBLIC_POSTHOG_PROJECT_TOKEN', 'phc_test')
    vi.stubEnv('VITE_PUBLIC_POSTHOG_HOST', '')
    vi.stubEnv('VITE_PUBLIC_POSTHOG_DISABLED', '')
    const mod = await import('./posthog')
    mod.initAnalytics()
    expect(initMock).toHaveBeenCalledWith(
      'phc_test',
      expect.objectContaining({
        api_host: '/ingest',
        // The SDK can't derive a UI host from a same-origin path, so pin it.
        ui_host: 'https://us.posthog.com',
      }),
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

  it('forwards capture to the singleton when enabled', async () => {
    vi.stubEnv('VITE_PUBLIC_POSTHOG_PROJECT_TOKEN', 'phc_test')
    vi.stubEnv('VITE_PUBLIC_POSTHOG_DISABLED', '')
    const mod = await import('./posthog')
    mod.initAnalytics()
    mod.captureEvent('api_request_client', { route: '/api/game/start', ok: true })
    expect(captureMock).toHaveBeenCalledWith('api_request_client', {
      route: '/api/game/start',
      ok: true,
    })
  })
})
