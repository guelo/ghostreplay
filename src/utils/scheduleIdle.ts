// Schedule low-priority work off the critical interaction path. Uses
// requestIdleCallback where available and falls back to a short timeout so the
// callback still runs in environments (Safari, jsdom) without idle scheduling.
// Returns a cancel function.

type IdleWindow = typeof globalThis & {
  requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number
  cancelIdleCallback?: (handle: number) => void
}

export const scheduleIdle = (callback: () => void, timeout = 200): (() => void) => {
  const w = globalThis as IdleWindow
  if (typeof w.requestIdleCallback === "function") {
    const handle = w.requestIdleCallback(callback, { timeout })
    return () => w.cancelIdleCallback?.(handle)
  }
  const handle = setTimeout(callback, timeout)
  return () => clearTimeout(handle)
}
