/**
 * Feedback entry point — opens our hosted Featurebase portal in a new tab.
 *
 * Why a link-out instead of the in-app widget: the app is cross-origin isolated
 * (`Cross-Origin-Embedder-Policy: require-corp`, required for the Stockfish WASM
 * `SharedArrayBuffer`), and Featurebase's embeddable widget — its `sdk.js` and
 * the cross-origin overlay iframe it injects — cannot load under that policy.
 * The widget is also a paid-plan feature (the free plan returns "Not available
 * with the free plan"). The hosted portal (`https://<org>.featurebase.app`)
 * works on the free plan and needs no account, so the lowest-friction option is
 * to open it in a new tab. See bead g-7xej for the full investigation.
 */

const DEFAULT_ORG = 'ghostchess'

/** Org subdomain for the hosted portal (`https://<org>.featurebase.app`). */
function org(): string {
  return import.meta.env.VITE_PUBLIC_FEATUREBASE_ORG || DEFAULT_ORG
}

/** Opt-out switch so the feature can be force-disabled per environment/test. */
function isDisabled(): boolean {
  return import.meta.env.VITE_PUBLIC_FEATUREBASE_DISABLED === 'true'
}

/** Public URL of the hosted feedback portal. */
export function feedbackPortalUrl(): string {
  return `https://${org()}.featurebase.app`
}

/**
 * Open the hosted feedback portal in a new tab. No-ops when disabled or when
 * there is no DOM (SSR/tests), so the nav button can call it unconditionally.
 * `noopener,noreferrer` severs the opener reference for safety.
 */
export function openFeedbackWidget(): void {
  if (typeof window === 'undefined' || isDisabled()) return
  window.open(feedbackPortalUrl(), '_blank', 'noopener,noreferrer')
}
