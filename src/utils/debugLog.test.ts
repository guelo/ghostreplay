import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  clear,
  getEntries,
  installConsoleCapture,
  recordError,
  uninstallConsoleCapture,
} from './debugLog'
import { getNextOpponentMove } from './api'

const STORAGE_KEY = 'gr.debugLog'

function readPersisted(): Array<{ level: string; args: string }> {
  const raw = localStorage.getItem(STORAGE_KEY)
  return raw ? JSON.parse(raw) : []
}

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.removeItem(STORAGE_KEY)
})

afterEach(() => {
  uninstallConsoleCapture()
  vi.useRealTimers()
  localStorage.removeItem(STORAGE_KEY)
})

describe('console capture', () => {
  it('pushes an entry and still calls the original console method', () => {
    const original = console.log
    const spy = vi.fn()
    console.log = spy
    installConsoleCapture()

    console.log('hello', 42)

    const entries = getEntries()
    expect(entries).toHaveLength(1)
    expect(entries[0]).toMatchObject({ level: 'log', args: 'hello 42' })
    expect(spy).toHaveBeenCalledWith('hello', 42)

    uninstallConsoleCapture()
    console.log = original
  })

  it('is idempotent — double install wraps console once', () => {
    installConsoleCapture()
    installConsoleCapture()
    console.log('once')
    expect(getEntries()).toHaveLength(1)
  })

  it('caps the ring buffer at MAX_ENTRIES while ids keep incrementing', () => {
    installConsoleCapture()
    for (let i = 0; i < 600; i++) console.log('m' + i)
    const entries = getEntries()
    expect(entries).toHaveLength(500)
    // Oldest evicted: first surviving entry is m100.
    expect(entries[0].args).toBe('m100')
    expect(entries[0].id).toBe(101)
    expect(entries[entries.length - 1].args).toBe('m599')
  })
})

describe('serialization', () => {
  beforeEach(() => installConsoleCapture())

  it('serializes Error with name/message/stack, not {}', () => {
    console.log(new Error('boom'))
    const args = getEntries()[0].args
    expect(args).toContain('Error: boom')
    expect(args).not.toBe('{}')
  })

  it('serializes DOMException', () => {
    console.log(new DOMException('nope', 'AbortError'))
    expect(getEntries()[0].args).toContain('AbortError: nope')
  })

  it('serializes ErrorEvent fields', () => {
    const ev = new ErrorEvent('error', {
      message: 'oops',
      filename: 'a.js',
      lineno: 12,
    })
    console.log(ev)
    const args = getEntries()[0].args
    expect(args).toContain('ErrorEvent: oops')
    expect(args).toContain('a.js:12')
  })

  it('does not throw on circular objects', () => {
    const obj: Record<string, unknown> = { a: 1 }
    obj.self = obj
    expect(() => console.log(obj)).not.toThrow()
    expect(getEntries()[0].args).toContain('[Circular]')
  })

  it('truncates very long args', () => {
    console.log('x'.repeat(5000))
    expect(getEntries()[0].args).toContain('[truncated]')
    expect(getEntries()[0].args.length).toBeLessThan(2100)
  })
})

describe('persistence', () => {
  beforeEach(() => installConsoleCapture())

  it('debounces writes for log/info/debug', () => {
    console.log('a')
    expect(readPersisted()).toHaveLength(0)
    vi.advanceTimersByTime(500)
    expect(readPersisted()).toHaveLength(1)
  })

  it('flushes immediately for error and warn', () => {
    console.error('bad')
    expect(readPersisted()).toHaveLength(1)
    console.warn('careful')
    expect(readPersisted()).toHaveLength(2)
  })

  it('flushes immediately on window error and unhandledrejection', () => {
    window.dispatchEvent(new ErrorEvent('error', { message: 'crash' }))
    expect(readPersisted().at(-1)?.level).toBe('error')

    const rejection = new Event('unhandledrejection') as PromiseRejectionEvent
    Object.defineProperty(rejection, 'reason', { value: new Error('rej') })
    window.dispatchEvent(rejection)
    expect(readPersisted().at(-1)?.args).toContain('rej')
  })

  it('flushes on pagehide', () => {
    console.log('pending')
    expect(readPersisted()).toHaveLength(0)
    window.dispatchEvent(new Event('pagehide'))
    expect(readPersisted()).toHaveLength(1)
  })
})

describe('hydration', () => {
  it('restores entries and continues ids without reuse', () => {
    installConsoleCapture()
    console.error('first')
    const persistedId = getEntries()[0].id
    uninstallConsoleCapture()

    // Re-seed storage (uninstall cleared the buffer but we kept the key write).
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([{ id: persistedId, ts: Date.now(), level: 'error', args: 'first' }]),
    )

    installConsoleCapture()
    expect(getEntries()).toHaveLength(1)
    console.log('second')
    const ids = getEntries().map((e) => e.id)
    expect(new Set(ids).size).toBe(ids.length)
    expect(ids[1]).toBe(persistedId + 1)
  })
})

describe('store helpers', () => {
  beforeEach(() => installConsoleCapture())

  it('clear empties the buffer and removes the key', () => {
    console.error('x')
    expect(readPersisted().length).toBeGreaterThan(0)
    clear()
    expect(getEntries()).toHaveLength(0)
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('recordError produces an error entry with message/stack', () => {
    recordError(new Error('worker died'), 'stockfish')
    const entry = getEntries()[0]
    expect(entry.level).toBe('error')
    expect(entry.args).toContain('stockfish')
    expect(entry.args).toContain('worker died')
  })
})

describe('fetch capture', () => {
  let originalWindowFetch: typeof window.fetch | undefined

  beforeEach(() => {
    originalWindowFetch = window.fetch
  })

  afterEach(() => {
    uninstallConsoleCapture()
    if (typeof originalWindowFetch === 'function') {
      window.fetch = originalWindowFetch
    } else {
      delete (window as { fetch?: typeof fetch }).fetch
    }
  })

  function netEntries() {
    return getEntries().filter((e) => e.level === 'net')
  }

  it('pushes a net entry on success and returns the response unchanged', async () => {
    const res = new Response('{"a":1}', {
      status: 200,
      headers: { 'x-request-id': 'a1b2c3' },
    })
    const stub = vi.fn().mockResolvedValue(res)
    window.fetch = stub
    installConsoleCapture()

    const returned = await window.fetch(
      'http://localhost:8000/api/openings/tree?player_color=white',
    )

    expect(returned).toBe(res)
    const nets = netEntries()
    expect(nets).toHaveLength(1)
    expect(nets[0].net).toMatchObject({
      method: 'GET',
      url: '/api/openings/tree?player_color=white',
      status: 200,
      ok: true,
      requestId: 'a1b2c3',
    })
    expect(nets[0].net!.durationMs).toBeGreaterThanOrEqual(0)
    expect(nets[0].args).toContain('200')
    expect(nets[0].args).toContain('#a1b2c3')
  })

  it('does not consume the response body', async () => {
    const res = new Response('{"a":1}', { status: 200 })
    window.fetch = vi.fn().mockResolvedValue(res)
    installConsoleCapture()

    const returned = await window.fetch('http://localhost:8000/api/x')
    // The wrapper never read the body, so it is still readable by the caller.
    await expect(returned.text()).resolves.toBe('{"a":1}')
  })

  it('flags an HTTP failure and persists it immediately', () => {
    const res = new Response('{}', { status: 500 })
    window.fetch = vi.fn().mockResolvedValue(res)
    installConsoleCapture()

    return window.fetch('http://localhost:8000/api/game/end').then(() => {
      const nets = netEntries()
      expect(nets).toHaveLength(1)
      expect(nets[0].net).toMatchObject({ status: 500, ok: false, errorKind: 'http' })
      // Immediate flush — written without advancing the debounce timer.
      expect(readPersisted()).toHaveLength(1)
    })
  })

  it('records a rejected fetch as a network error and re-throws', async () => {
    const err = new TypeError('Failed to fetch')
    window.fetch = vi.fn().mockRejectedValue(err)
    installConsoleCapture()

    await expect(window.fetch('http://localhost:8000/api/x')).rejects.toBe(err)
    const nets = netEntries()
    expect(nets).toHaveLength(1)
    expect(nets[0].net).toMatchObject({ status: 0, ok: false, errorKind: 'network' })
    // Network failures flush immediately so they survive a reload.
    expect(readPersisted()).toHaveLength(1)
  })

  it('records an abort as non-immediate and re-throws', async () => {
    const err = new DOMException('aborted', 'AbortError')
    window.fetch = vi.fn().mockRejectedValue(err)
    installConsoleCapture()

    await expect(window.fetch('http://localhost:8000/api/x')).rejects.toBe(err)
    const nets = netEntries()
    expect(nets).toHaveLength(1)
    expect(nets[0].net).toMatchObject({ status: 0, ok: false, errorKind: 'abort' })
    // Aborts are routine UI churn — debounced, not flushed immediately.
    expect(readPersisted()).toHaveLength(0)
    vi.advanceTimersByTime(500)
    expect(readPersisted()).toHaveLength(1)
  })

  it('classifies a TimeoutError as timeout', async () => {
    const err = new DOMException('timed out', 'TimeoutError')
    window.fetch = vi.fn().mockRejectedValue(err)
    installConsoleCapture()

    await expect(window.fetch('http://localhost:8000/api/x')).rejects.toBe(err)
    expect(netEntries()[0].net).toMatchObject({ status: 0, errorKind: 'timeout' })
  })

  it('wraps window.fetch only once on double install', async () => {
    window.fetch = vi.fn().mockResolvedValue(new Response('{}'))
    installConsoleCapture()
    installConsoleCapture()

    await window.fetch('http://localhost:8000/api/x')
    expect(netEntries()).toHaveLength(1)
  })

  it('restores the exact original fetch reference on uninstall', () => {
    const stub = vi.fn().mockResolvedValue(new Response('{}'))
    window.fetch = stub
    const ref = window.fetch
    installConsoleCapture()
    expect(window.fetch).not.toBe(ref)
    uninstallConsoleCapture()
    expect(window.fetch).toBe(ref)
  })

  it('logs each physical attempt on retry (3 rows for 500/500/200)', async () => {
    // requestJson sleeps via setTimeout between retries; real timers keep it simple.
    vi.useRealTimers()
    window.fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response('{}', { status: 500 }))
      .mockResolvedValueOnce(new Response('{}', { status: 500 }))
      .mockResolvedValueOnce(new Response('{}', { status: 200 }))
    installConsoleCapture()

    await getNextOpponentMove('session-1', 'fen')

    const statuses = netEntries().map((e) => e.net!.status)
    expect(statuses).toEqual([500, 500, 200])
  })

  it('keeps a 200 with invalid JSON as a net success', async () => {
    // The wrapper never reads the body, so a downstream parse failure cannot
    // demote the net row — the HTTP response WAS 200.
    window.fetch = vi.fn().mockResolvedValue(new Response('not json', { status: 200 }))
    installConsoleCapture()

    const res = await window.fetch('http://localhost:8000/api/x')
    await expect(res.json()).rejects.toBeDefined() // caller's parse fails…
    expect(netEntries()[0].net).toMatchObject({ status: 200, ok: true }) // …net stays a success
  })

  it('preserves the host for third-party calls, strips our own origins', async () => {
    window.fetch = vi.fn().mockResolvedValue(new Response('{}'))
    installConsoleCapture()

    await window.fetch('https://us.i.posthog.com/e')
    await window.fetch('/api/foo')
    await window.fetch('http://localhost:8000/api/bar')

    const urls = netEntries().map((e) => e.net!.url)
    expect(urls[0]).toBe('us.i.posthog.com/e') // host kept — third-party stays distinguishable
    expect(urls[1]).toBe('/api/foo') // same-origin stripped
    expect(urls[2]).toBe('/api/bar') // dev API base stripped
  })
})

describe('uninstall', () => {
  it('restores console and stops capturing', () => {
    const original = console.log
    installConsoleCapture()
    expect(console.log).not.toBe(original)
    uninstallConsoleCapture()
    expect(console.log).toBe(original)
    console.log('after')
    expect(getEntries()).toHaveLength(0)
  })
})
