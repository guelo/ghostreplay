import '@testing-library/jest-dom'
import { expect, afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'
import * as matchers from '@testing-library/jest-dom/matchers'
import { uninstallConsoleCapture } from '../utils/debugLog'

expect.extend(matchers)

// Force analytics OFF for the whole suite so PostHog never initializes or hits
// the network. `initAnalytics()` reads this at call time and no-ops; tests that
// exercise the enabled path override it locally with `vi.stubEnv`.
import.meta.env.VITE_PUBLIC_POSTHOG_DISABLED = 'true'

// Likewise force the Featurebase feedback widget OFF suite-wide so its SDK never
// boots even if a local `.env` sets an App ID (vitest reads local Vite env).
// Enabled-path tests override this explicitly with `vi.stubEnv`.
import.meta.env.VITE_PUBLIC_FEATUREBASE_DISABLED = 'true'

afterEach(() => {
  cleanup()
  // Restore console, remove window listeners, cancel timers, reset buffer +
  // storage so debug-log capture does not leak across tests.
  uninstallConsoleCapture()
  try {
    localStorage.removeItem('gr.debugLog')
    localStorage.removeItem('gr.debugBody')
  } catch {
    /* ignore */
  }
  // Reset matchMedia state so query matches do not leak across tests.
  for (const mql of __mqlRegistry.values()) mql._matches = false
})

// localStorage polyfill for JSDOM (not always a full Storage implementation here)
if (typeof localStorage === 'undefined' || typeof localStorage.removeItem !== 'function') {
  const store = new Map<string, string>()
  const mock: Storage = {
    get length() {
      return store.size
    },
    clear: () => store.clear(),
    getItem: (k) => (store.has(k) ? store.get(k)! : null),
    key: (i) => Array.from(store.keys())[i] ?? null,
    removeItem: (k) => {
      store.delete(k)
    },
    setItem: (k, v) => {
      store.set(k, String(v))
    },
  }
  Object.defineProperty(globalThis, 'localStorage', { value: mock, configurable: true })
}

// ResizeObserver stub for JSDOM
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

// Pointer capture stubs for JSDOM
HTMLElement.prototype.setPointerCapture ??= () => {}
HTMLElement.prototype.releasePointerCapture ??= () => {}

// Reactive matchMedia mock. useSyncExternalStore-based hooks only re-render when
// the registered `change` listener actually fires, so we keep real listener sets
// per query and dispatch to them via setMatchMedia().
type MockMQL = MediaQueryList & {
  _matches: boolean
  _listeners: Set<(e: MediaQueryListEvent) => void>
}
const __mqlRegistry = new Map<string, MockMQL>()

function getOrCreateMql(query: string): MockMQL {
  let mql = __mqlRegistry.get(query)
  if (!mql) {
    const listeners = new Set<(e: MediaQueryListEvent) => void>()
    mql = {
      _matches: false,
      _listeners: listeners,
      media: query,
      get matches() {
        return (this as MockMQL)._matches
      },
      onchange: null,
      addEventListener: (_type: string, cb: (e: MediaQueryListEvent) => void) => {
        listeners.add(cb)
      },
      removeEventListener: (_type: string, cb: (e: MediaQueryListEvent) => void) => {
        listeners.delete(cb)
      },
      addListener: (cb: (e: MediaQueryListEvent) => void) => listeners.add(cb),
      removeListener: (cb: (e: MediaQueryListEvent) => void) => listeners.delete(cb),
      dispatchEvent: () => true,
    } as unknown as MockMQL
    __mqlRegistry.set(query, mql)
  }
  return mql
}

if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  window.matchMedia = ((query: string) => getOrCreateMql(query)) as typeof window.matchMedia
}

/** Test helper: set a media query's match state and notify subscribers. */
export function setMatchMedia(query: string, matches: boolean): void {
  const mql = getOrCreateMql(query)
  mql._matches = matches
  const event = { matches, media: query } as MediaQueryListEvent
  for (const cb of mql._listeners) cb(event)
}
