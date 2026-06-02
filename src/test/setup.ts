import '@testing-library/jest-dom'
import { expect, afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'
import * as matchers from '@testing-library/jest-dom/matchers'
import { uninstallConsoleCapture } from '../utils/debugLog'

expect.extend(matchers)

afterEach(() => {
  cleanup()
  // Restore console, remove window listeners, cancel timers, reset buffer +
  // storage so debug-log capture does not leak across tests.
  uninstallConsoleCapture()
  try {
    localStorage.removeItem('gr.debugLog')
  } catch {
    /* ignore */
  }
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
