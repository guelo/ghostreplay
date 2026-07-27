/**
 * Device/browser identification for the run header (g-two-search-grade §10.4:
 * "record hardware, OS, and browser").
 *
 * Everything here is best-effort and never throws: a missing signal is recorded
 * as null, because "unknown" and "absent" are different facts and the operator's
 * `device.label` is the authoritative hardware description either way.
 */

import type { BenchBuildMode, BenchEnvironment } from '../benchRecord'

/**
 * Whether the measured worker came from a production build or the dev server.
 *
 * §10.1 requires the device runner to load the ACTUAL BUNDLED worker, so a dev
 * run must be self-identifying in the data rather than in someone's memory of
 * how they started the page.
 */
export const detectBuildMode = (): BenchBuildMode =>
  import.meta.env?.DEV ? 'dev' : 'bundled'

type UaDataLike = {
  brands?: Array<{ brand: string; version: string }>
  platform?: string
}

export const describeEnvironment = (nav?: Navigator): BenchEnvironment => {
  const n =
    nav ??
    (typeof navigator !== 'undefined' ? navigator : undefined)

  const uaData = (n as (Navigator & { userAgentData?: UaDataLike }) | undefined)?.userAgentData
  const brands = uaData?.brands
    ?.map((entry) => `${entry.brand} ${entry.version}`)
    .join('; ')

  let timeZone: string | null = null
  try {
    timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone ?? null
  } catch {
    timeZone = null
  }

  return {
    userAgent: n?.userAgent ?? null,
    uaData: brands ? `${brands}${uaData?.platform ? ` (${uaData.platform})` : ''}` : null,
    hardwareConcurrency:
      typeof n?.hardwareConcurrency === 'number' ? n.hardwareConcurrency : null,
    deviceMemory:
      typeof (n as (Navigator & { deviceMemory?: number }) | undefined)?.deviceMemory === 'number'
        ? (n as Navigator & { deviceMemory?: number }).deviceMemory ?? null
        : null,
    platform: (n as (Navigator & { platform?: string }) | undefined)?.platform ?? null,
    screen:
      typeof screen !== 'undefined' && screen
        ? `${screen.width}x${screen.height}`
        : null,
    devicePixelRatio: typeof devicePixelRatio === 'number' ? devicePixelRatio : null,
    timeZone,
  }
}
