import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

describe('analytics/featurebase', () => {
  beforeEach(() => {
    // The module reads env at call time; reset modules + env stubs between tests.
    vi.resetModules()
    vi.unstubAllEnvs()
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    vi.restoreAllMocks()
  })

  it('builds the portal URL from the org env, defaulting to ghostchess', async () => {
    vi.stubEnv('VITE_PUBLIC_FEATUREBASE_ORG', '')
    const mod = await import('./featurebase')
    expect(mod.feedbackPortalUrl()).toBe('https://ghostchess.featurebase.app')
  })

  it('uses a custom org subdomain when set', async () => {
    vi.stubEnv('VITE_PUBLIC_FEATUREBASE_ORG', 'acme')
    const mod = await import('./featurebase')
    expect(mod.feedbackPortalUrl()).toBe('https://acme.featurebase.app')
  })

  it('opens the portal in a new tab with noopener', async () => {
    vi.stubEnv('VITE_PUBLIC_FEATUREBASE_ORG', '')
    vi.stubEnv('VITE_PUBLIC_FEATUREBASE_DISABLED', '')
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null)
    const mod = await import('./featurebase')

    mod.openFeedbackWidget()

    expect(openSpy).toHaveBeenCalledTimes(1)
    expect(openSpy).toHaveBeenCalledWith(
      'https://ghostchess.featurebase.app',
      '_blank',
      'noopener,noreferrer',
    )
  })

  it('no-ops when explicitly disabled', async () => {
    vi.stubEnv('VITE_PUBLIC_FEATUREBASE_DISABLED', 'true')
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null)
    const mod = await import('./featurebase')

    mod.openFeedbackWidget()

    expect(openSpy).not.toHaveBeenCalled()
  })
})
