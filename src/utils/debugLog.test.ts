import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  clear,
  getEntries,
  installConsoleCapture,
  getNetworkCaptureMode,
  recordError,
  setNetworkCaptureMode,
  uninstallConsoleCapture,
  type NetMeta,
  type NetworkCaptureMode,
} from './debugLog'
import { getNextOpponentMove } from './api'

const STORAGE_KEY = 'gr.debugLog'
const BODY_STORAGE_KEY = 'gr.debugBody'
const CAPTURE_MODE_STORAGE_KEY = 'gr.debugCaptureMode'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function readPersisted(): Array<{ level: string; args: string; net?: NetMeta }> {
  const raw = localStorage.getItem(STORAGE_KEY)
  return raw ? JSON.parse(raw) : []
}

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.removeItem(STORAGE_KEY)
  localStorage.removeItem(BODY_STORAGE_KEY)
  localStorage.removeItem(CAPTURE_MODE_STORAGE_KEY)
})

afterEach(() => {
  uninstallConsoleCapture()
  vi.unstubAllGlobals()
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
    // Capture reads a clone; the original remains readable by the caller.
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
    // A downstream parse failure cannot demote the captured HTTP 200 status.
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

describe('body capture', () => {
  let originalWindowFetch: typeof window.fetch | undefined

  beforeEach(() => {
    originalWindowFetch = window.fetch
  })

  afterEach(() => {
    uninstallConsoleCapture()
    vi.unstubAllGlobals()
    localStorage.removeItem(BODY_STORAGE_KEY)
    localStorage.removeItem(CAPTURE_MODE_STORAGE_KEY)
    window.history.replaceState({}, '', '/')
    if (typeof originalWindowFetch === 'function') {
      window.fetch = originalWindowFetch
    } else {
      delete (window as { fetch?: typeof fetch }).fetch
    }
  })

  function netEntries() {
    return getEntries().filter((e) => e.level === 'net')
  }

  /** The response-body attach rides on microtasks only (fake timers stay valid). */
  async function flushMicrotasks(rounds = 50): Promise<void> {
    for (let i = 0; i < rounds; i++) await Promise.resolve()
  }

  /** For ReadableStream-bodied responses: full event-loop turns (real timers). */
  async function settle(): Promise<void> {
    for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0))
  }

  function install(body: string, init?: ResponseInit): void {
    window.fetch = vi.fn().mockResolvedValue(new Response(body, init))
    installConsoleCapture()
  }

  it('defaults to redacted responses without request bodies and preserves the original', async () => {
    install('{"detail":"ready","token":"response-secret"}')
    expect(getNetworkCaptureMode()).toBe('responses')

    const res = await window.fetch('/api/x', {
      method: 'POST', body: '{"move":"e4","password":"request-secret"}',
    })
    await expect(res.json()).resolves.toEqual({ detail: 'ready', token: 'response-secret' })
    await flushMicrotasks()

    expect(netEntries()).toHaveLength(1)
    expect(netEntries()[0].net!.reqBody).toBeUndefined()
    expect(netEntries()[0].net!.resBody).toBe('{"detail":"ready","token":"[redacted]"}')
  })

  it.each<NetworkCaptureMode>(['metadata', 'responses', 'bodies'])(
    'restores stored %s mode ahead of the legacy flag', (mode) => {
      localStorage.setItem(CAPTURE_MODE_STORAGE_KEY, mode)
      localStorage.setItem(BODY_STORAGE_KEY, '1')
      install('{}')
      expect(getNetworkCaptureMode()).toBe(mode)
    },
  )

  it.each<NetworkCaptureMode>(['metadata', 'responses', 'bodies'])(
    'persists explicit %s mode across reinstall', (mode) => {
      install('{}')
      setNetworkCaptureMode(mode)
      expect(getNetworkCaptureMode()).toBe(mode)
      expect(localStorage.getItem(CAPTURE_MODE_STORAGE_KEY)).toBe(mode)
      uninstallConsoleCapture()
      // Overlay visibility must not override a capture preference.
      window.history.replaceState({}, '', '/?debug=1')
      install('{}')
      expect(getNetworkCaptureMode()).toBe(mode)
    },
  )

  it.each([
    ['?debugcapture=metadata&debugbody=1', 'bodies', 'metadata'],
    ['?debugcapture=responses&debugbody=0', 'metadata', 'responses'],
    ['?debugcapture=bodies&debugbody=0', 'metadata', 'bodies'],
    ['?debugbody=0', 'bodies', 'metadata'],
    ['?debugbody=1', 'metadata', 'bodies'],
    ['?debugcapture=invalid&debugbody=0', 'bodies', 'metadata'],
    ['?debugcapture=invalid&debugbody=1', 'metadata', 'bodies'],
    ['?debugcapture=invalid&debugbody=invalid', 'metadata', 'metadata'],
  ])('resolves URL %s before stored %s', (query, stored, expected) => {
    localStorage.setItem(CAPTURE_MODE_STORAGE_KEY, stored)
    window.history.replaceState({}, '', `/${query}`)
    install('{}')
    expect(getNetworkCaptureMode()).toBe(expected)
    expect(localStorage.getItem(CAPTURE_MODE_STORAGE_KEY)).toBe(expected)
  })

  it.each([null, 'invalid'])('migrates legacy opt-in with new key %s', (stored) => {
    if (stored) localStorage.setItem(CAPTURE_MODE_STORAGE_KEY, stored)
    localStorage.setItem(BODY_STORAGE_KEY, '1')
    install('{}')
    expect(getNetworkCaptureMode()).toBe('bodies')
    expect(localStorage.getItem(CAPTURE_MODE_STORAGE_KEY)).toBe('bodies')
    expect(localStorage.getItem(BODY_STORAGE_KEY)).toBeNull()
  })

  it.each([null, '0', 'invalid'])('uses responses for unrecognized or absent legacy value %s', (legacy) => {
    localStorage.setItem(CAPTURE_MODE_STORAGE_KEY, 'invalid')
    if (legacy) localStorage.setItem(BODY_STORAGE_KEY, legacy)
    window.history.replaceState({}, '', '/?debugcapture=invalid&debugbody=invalid')
    install('{}')
    expect(getNetworkCaptureMode()).toBe('responses')
  })

  it.each([
    ['/', 'responses'],
    ['/?debugcapture=metadata', 'metadata'],
    ['/?debugbody=1', 'bodies'],
  ])('keeps fetch working without storage at %s', async (url, expected) => {
    const fail = () => { throw new Error('storage unavailable') }
    vi.stubGlobal('localStorage', { getItem: fail, setItem: fail, removeItem: fail })
    window.history.replaceState({}, '', url)
    install('{}')
    expect(getNetworkCaptureMode()).toBe(expected)
    const res = await window.fetch('/api/x')
    await expect(res.json()).resolves.toEqual({})
    setNetworkCaptureMode('metadata')
    expect(getNetworkCaptureMode()).toBe('metadata')
  })

  it.each(['/', '/?debugcapture=metadata'])(
    'retains legacy preference if migration cannot persist at %s', (url) => {
      const storage = localStorage
      storage.setItem(BODY_STORAGE_KEY, '1')
      vi.stubGlobal('localStorage', {
        getItem: (key: string) => storage.getItem(key),
        setItem: () => { throw new Error('quota exceeded') },
        removeItem: (key: string) => storage.removeItem(key),
      })
      window.history.replaceState({}, '', url)
      install('{}')
      expect(getNetworkCaptureMode()).toBe(url === '/' ? 'bodies' : 'metadata')
      expect(storage.getItem(BODY_STORAGE_KEY)).toBe('1')
      expect(storage.getItem(CAPTURE_MODE_STORAGE_KEY)).toBeNull()
    },
  )

  it('removes the legacy preference after a URL choice is persisted', () => {
    localStorage.setItem(BODY_STORAGE_KEY, '1')
    window.history.replaceState({}, '', '/?debugbody=0')
    install('{}')
    expect(getNetworkCaptureMode()).toBe('metadata')
    expect(localStorage.getItem(CAPTURE_MODE_STORAGE_KEY)).toBe('metadata')
    expect(localStorage.getItem(BODY_STORAGE_KEY)).toBeNull()
  })

  it.each<[NetworkCaptureMode, NetworkCaptureMode]>([
    ['responses', 'metadata'], ['metadata', 'responses'], ['bodies', 'responses'],
  ])('keeps request-start %s mode after switching to %s', async (start, next) => {
    const pending = deferred<Response>()
    window.fetch = vi.fn().mockReturnValue(pending.promise)
    installConsoleCapture()
    setNetworkCaptureMode(start)
    const request = window.fetch('/api/x', { method: 'POST', body: '{"move":"e4"}' })
    setNetworkCaptureMode(next)
    pending.resolve(new Response('{"token":"response-secret"}'))
    await request
    await flushMicrotasks()
    expect(netEntries()).toHaveLength(1)
    expect(netEntries()[0].net!.reqBody).toBe(start === 'bodies' ? '{"move":"e4"}' : undefined)
    expect(netEntries()[0].net!.resBody).toBe(
      start === 'metadata' ? undefined : '{"token":"[redacted]"}',
    )
  })

  it.each(['network', 'abort', 'timeout'])('omits request bodies on response-only %s failures', async (kind) => {
    const err = kind === 'network'
      ? new TypeError('Failed to fetch')
      : new DOMException('stopped', kind === 'abort' ? 'AbortError' : 'TimeoutError')
    const pending = deferred<Response>()
    const stub = vi.fn().mockReturnValue(pending.promise)
    window.fetch = stub
    installConsoleCapture()
    const init = { method: 'POST', body: '{"move":"e4"}', signal: new AbortController().signal }
    const request = window.fetch('/api/x', init)
    setNetworkCaptureMode('bodies')
    pending.reject(err)
    await expect(request).rejects.toBe(err)
    expect(stub).toHaveBeenCalledWith('/api/x', init)
    expect(netEntries()[0].net).toMatchObject({ status: 0, errorKind: kind })
    expect(netEntries()[0].net!.reqBody).toBeUndefined()
    expect(netEntries()[0].net!.resBody).toBeUndefined()
  })

  it.each([200, 400, 500])('captures response-only HTTP %s without consuming caller content', async (status) => {
    install('{"detail":"test-result","token":"private-value"}', { status })
    const res = await window.fetch('/api/x', { method: 'POST', body: '{"move":"e4"}' })
    await expect(res.json()).resolves.toEqual({ detail: 'test-result', token: 'private-value' })
    await flushMicrotasks()
    expect(netEntries()[0].net!.reqBody).toBeUndefined()
    expect(netEntries()[0].net!.resBody).toContain('test-result')
    if (status !== 200) {
      expect(readPersisted()[0].net!.resBody).toContain('test-result')
      expect(localStorage.getItem(STORAGE_KEY)).not.toContain('private-value')
    }
  })

  it('captures an empty response', async () => {
    window.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    installConsoleCapture()
    const res = await window.fetch('/api/x')
    await flushMicrotasks()
    expect(await res.text()).toBe('')
    expect(netEntries()[0].net!.resBody).toBe('')
  })

  it('captures responses for Request inputs without reading their body', async () => {
    install('{"move":"d5"}')
    const request = new Request('http://localhost:8000/api/x', { method: 'POST', body: 'e4' })
    await window.fetch(request)
    await flushMicrotasks()
    expect(netEntries()[0].net).toMatchObject({ method: 'POST', resBody: '{"move":"d5"}' })
    expect(netEntries()[0].net!.reqBody).toBeUndefined()
    expect(await request.text()).toBe('e4')
  })

  it.each(['clear', 'eviction'])('drops delayed response completion after %s', async (removal) => {
    const body = deferred<string>()
    const delayed = new Response(null)
    vi.spyOn(delayed, 'clone').mockReturnValue({ body: null, text: () => body.promise } as Response)
    window.fetch = vi.fn().mockResolvedValueOnce(delayed)
      .mockImplementation(() => Promise.resolve(new Response('later')))
    installConsoleCapture()
    await window.fetch('/api/old')
    const before = netEntries()[0]
    if (removal === 'clear') clear()
    setNetworkCaptureMode('metadata')
    for (let i = 0; i < (removal === 'clear' ? 1 : 500); i++) await window.fetch('/api/new')
    const snapshot = getEntries()
    body.resolve('late-content')
    await flushMicrotasks()
    expect(getEntries()).toBe(snapshot)
    expect(getEntries().some((entry) => entry.id === before.id)).toBe(false)
    expect(getEntries().every((entry) => entry.net?.resBody === undefined)).toBe(true)
    expect(before.net!.resBody).toBeUndefined()
  })

  it('publishes a new snapshot and entry when a response body attaches', async () => {
    const body = deferred<string>()
    const res = new Response(null)
    vi.spyOn(res, 'clone').mockReturnValue({ body: null, text: () => body.promise } as Response)
    window.fetch = vi.fn().mockResolvedValue(res)
    installConsoleCapture()
    await window.fetch('/api/x')
    const before = getEntries()
    body.resolve('{"move":"e4"}')
    await flushMicrotasks()
    expect(getEntries()).not.toBe(before)
    expect(getEntries()[0]).not.toBe(before[0])
    expect(getEntries()[0].id).toBe(before[0].id)
    expect(before[0].net!.resBody).toBeUndefined()
    expect(getEntries()[0].args).toContain('e4')
  })

  it('metadata mode never reads request bodies or clones responses', async () => {
    const res = new Response('{"token":"eyJa.eyJb.cccc"}')
    const clone = vi.spyOn(res, 'clone')
    window.fetch = vi.fn().mockResolvedValue(res)
    installConsoleCapture()
    setNetworkCaptureMode('metadata')
    const readBody = vi.fn(() => JSON.stringify({ password: 'secret' }))

    await window.fetch('http://localhost:8000/api/auth/login', {
      method: 'POST',
      get body() { return readBody() },
    })
    await flushMicrotasks()
    expect(readBody).not.toHaveBeenCalled()
    expect(clone).not.toHaveBeenCalled()

    const entry = netEntries()[0]
    expect(entry.net!.reqBody).toBeUndefined()
    expect(entry.net!.resBody).toBeUndefined()
    expect(entry.args).not.toContain('→')
    expect(entry.args).not.toContain('←')
  })

  it('redacts sensitive keys in a JSON request body, keeps username', async () => {
    install('{}')
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: 'a', password: 'secret' }),
    })

    const reqBody = netEntries()[0].net!.reqBody!
    expect(reqBody).toContain('username')
    expect(reqBody).toContain('[redacted]')
    expect(reqBody).not.toContain('secret')
  })

  it('redacts secret-ish keys beyond an exact denylist, keeps debugging identifiers', async () => {
    install('{}')
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/x', {
      method: 'POST',
      body: JSON.stringify({
        client_secret: 'cs-leak',
        secretKey: 'sk-leak',
        'X-Api-Key': 'ak-leak',
        apiToken: 'at-leak',
        userCredentials: 'uc-leak',
        session_id: 'sess-keep',
        username: 'name-keep',
      }),
    })

    const reqBody = netEntries()[0].net!.reqBody!
    for (const leaked of ['cs-leak', 'sk-leak', 'ak-leak', 'at-leak', 'uc-leak']) {
      expect(reqBody).not.toContain(leaked)
    }
    expect(reqBody).toContain('sess-keep')
    expect(reqBody).toContain('name-keep')
  })

  it('suppresses assignment-shaped secrets inside JSON string values, keeps bare mentions', async () => {
    install('{}')
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/x', {
      method: 'POST',
      body: JSON.stringify({
        data: 'password=hunter2', // JSON.parse succeeds → old code showed this raw
        list: ['token: abc123'],
        detail: 'Invalid username or password', // error envelopes must stay visible
      }),
    })

    const reqBody = netEntries()[0].net!.reqBody!
    expect(reqBody).not.toContain('hunter2')
    expect(reqBody).not.toContain('abc123')
    expect(reqBody).toContain('Invalid username or password')
  })

  it('suppresses a top-level JSON string body carrying an embedded secret', async () => {
    install('{}')
    setNetworkCaptureMode('bodies')

    // '"password=hunter2"' IS valid JSON (a string scalar), so it used to skip
    // the sensitive-text fallback entirely.
    await window.fetch('http://localhost:8000/api/x', {
      method: 'POST',
      body: JSON.stringify('password=hunter2'),
    })

    const reqBody = netEntries()[0].net!.reqBody!
    expect(reqBody).not.toContain('hunter2')
    expect(reqBody).toContain('[redacted]')
  })

  it('scrubs email addresses out of any captured text', async () => {
    install('{"message":"sent to bob@example.com"}')
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/x')
    await flushMicrotasks()

    const resBody = netEntries()[0].net!.resBody!
    expect(resBody).not.toContain('bob@example.com')
    expect(resBody).toContain('[redacted-email]')
  })

  it('reads the response on a clone — the caller stream stays intact', async () => {
    install('{"token":"eyJa.eyJb.cccc"}')
    setNetworkCaptureMode('bodies')

    const res = await window.fetch('http://localhost:8000/api/auth/login')
    // The wrapper only read its clone; the original body is still consumable.
    await expect(res.text()).resolves.toBe('{"token":"eyJa.eyJb.cccc"}')
    await flushMicrotasks()

    const net = netEntries()[0].net!
    expect(net.resBody).toContain('[redacted]')
    expect(net.resBody).not.toContain('eyJa')
  })

  it('attaches the response body asynchronously and rebuilds args', async () => {
    install('{"b":2}')
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/x', { method: 'POST', body: '{"a":1}' })
    await flushMicrotasks()

    const entry = netEntries()[0]
    expect(entry.net!.resBody).toBe('{"b":2}')
    expect(entry.args).toContain('\n  → {"a":1}')
    expect(entry.args).toContain('\n  ← {"b":2}')
  })

  it('never persists a raw password or JWT from a login-shaped exchange', async () => {
    const jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl'
    install(`{"token":"${jwt}","user":{"username":"a"}}`)
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: 'a', password: 'hunter2' }),
    })
    await flushMicrotasks()
    vi.advanceTimersByTime(500)

    const raw = localStorage.getItem(STORAGE_KEY) ?? ''
    expect(raw).toContain('[redacted]')
    expect(raw).not.toContain('hunter2')
    expect(raw).not.toContain('eyJhbGci')
  })

  it('truncates long bodies at the display cap, after redaction', async () => {
    const big = JSON.stringify({ data: 'x'.repeat(5000) })
    install(big)
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/x', { method: 'POST', body: big })
    await flushMicrotasks()

    const net = netEntries()[0].net!
    expect(net.reqBody).toContain('[truncated]')
    expect(net.reqBody!.length).toBeLessThan(2100)
    expect(net.resBody).toContain('[truncated]')
    expect(net.resBody!.length).toBeLessThan(2100)
  })

  it('never clones a response whose content-length exceeds the cap', async () => {
    const res = new Response('x', { headers: { 'content-length': '999999' } })
    // An unread tee branch buffers everything the app reads from the original
    // stream, so the large-body fast path must skip clone() entirely.
    const cloneSpy = vi.spyOn(res, 'clone')
    window.fetch = vi.fn().mockResolvedValue(res)
    installConsoleCapture()
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/big')
    await flushMicrotasks()

    expect(netEntries()[0].net!.resBody).toBe('[large body 999999 bytes, not captured]')
    expect(cloneSpy).not.toHaveBeenCalled()
  })

  it('bounds the read when content-length is missing and the stream exceeds the cap', async () => {
    vi.useRealTimers()
    let pulls = 0
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls++
        if (pulls <= 100) controller.enqueue(new TextEncoder().encode('x'.repeat(1000)))
        else controller.close()
      },
    })
    window.fetch = vi.fn().mockResolvedValue(new Response(stream))
    installConsoleCapture()
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/big')
    await settle()

    // Not JSON once cut → safe note, never the raw prefix.
    expect(netEntries()[0].net!.resBody).toBe('[large body truncated before safe redaction]')
    // Reader cancelled at the cap (~8 chunks) — the stream was not drained.
    expect(pulls).toBeLessThan(50)
  })

  it('replaces a body whose secret straddles the read cap with a safe note', async () => {
    vi.useRealTimers()
    const secret = 'A'.repeat(20000) // value crosses BODY_READ_CAP → cut mid-value
    const enc = new TextEncoder().encode(`{"token":"${secret}"}`)
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let i = 0; i < enc.length; i += 1000) controller.enqueue(enc.slice(i, i + 1000))
        controller.close()
      },
    })
    window.fetch = vi.fn().mockResolvedValue(new Response(stream))
    installConsoleCapture()
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/x')
    await settle()

    expect(netEntries()[0].net!.resBody).toBe('[large body truncated before safe redaction]')
    // The key assertion: no fragment of the secret was persisted.
    window.dispatchEvent(new Event('pagehide'))
    expect(localStorage.getItem(STORAGE_KEY) ?? '').not.toContain('AAAAAAAA')
  })

  it('handles a single chunk far larger than the cap without crashing or leaking', async () => {
    vi.useRealTimers()
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('x'.repeat(80000)))
        controller.close()
      },
    })
    window.fetch = vi.fn().mockResolvedValue(new Response(stream))
    installConsoleCapture()
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/x')
    await settle()

    expect(netEntries()[0].net!.resBody).toBe('[large body truncated before safe redaction]')
  })

  it('hides a raw form-encoded string body carrying sensitive keys', async () => {
    install('{}')
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/x', {
      method: 'POST',
      body: 'password=secret&x=1',
    })
    await flushMicrotasks()
    vi.advanceTimersByTime(500)

    expect(netEntries()[0].net!.reqBody).toBe('[body with sensitive keys, not shown]')
    expect(localStorage.getItem(STORAGE_KEY) ?? '').not.toContain('secret')
  })

  it('structurally redacts URLSearchParams bodies, keeping benign params readable', async () => {
    install('{}')
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/x', {
      method: 'POST',
      body: new URLSearchParams({ username: 'a', password: 'secret', access_token: 'tok123' }),
    })

    expect(netEntries()[0].net!.reqBody).toBe(
      'username=a&password=[redacted]&access_token=[redacted]',
    )
  })

  it('passes a benign non-JSON string body through, scrubbed', async () => {
    install('{}')
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/x', { method: 'POST', body: 'ping' })

    expect(netEntries()[0].net!.reqBody).toBe('ping')
  })

  it('scrubs token shapes out of benign non-JSON response text', async () => {
    install('auth Bearer abc123xyz done')
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/x')
    await flushMicrotasks()

    const resBody = netEntries()[0].net!.resBody!
    expect(resBody).toBe('auth Bearer [redacted] done')
  })

  it('flushes a failed row response body immediately, without the debounce timer', async () => {
    install('{"detail":"boom"}', { status: 500 })
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/game/end')
    await flushMicrotasks()

    // Written WITHOUT advancing timers — the error envelope survives a page close.
    expect(readPersisted()[0]?.net?.resBody).toContain('boom')
  })

  it('keeps a successful row response body on the debounced path', async () => {
    install('{"a":1}')
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/x')
    await flushMicrotasks()

    expect(readPersisted()).toHaveLength(0)
    vi.advanceTimersByTime(500)
    expect(readPersisted()[0]?.net?.resBody).toBe('{"a":1}')
  })

  it('tolerates a response double without clone()', async () => {
    const double = { status: 200, ok: true, headers: { get: () => null } }
    window.fetch = vi.fn().mockResolvedValue(double as unknown as Response)
    installConsoleCapture()
    setNetworkCaptureMode('bodies')

    await window.fetch('http://localhost:8000/api/x')
    await flushMicrotasks()

    expect(netEntries()[0].net!.resBody).toBeUndefined()
  })

  it('represents a non-string request body as a placeholder without consuming it', async () => {
    install('{}')
    setNetworkCaptureMode('bodies')
    const blob = new Blob(['x'])

    await window.fetch('http://localhost:8000/api/x', { method: 'POST', body: blob })

    expect(netEntries()[0].net!.reqBody).toBe('[Blob]')
    expect(blob.size).toBe(1) // untouched (jsdom Blob has no .text() to re-read)
  })

  it('records reqBody on the error path too', async () => {
    const err = new TypeError('Failed to fetch')
    window.fetch = vi.fn().mockRejectedValue(err)
    installConsoleCapture()
    setNetworkCaptureMode('bodies')

    await expect(
      window.fetch('http://localhost:8000/api/x', {
        method: 'POST',
        body: JSON.stringify({ username: 'a', password: 'secret' }),
      }),
    ).rejects.toBe(err)

    const net = netEntries()[0].net!
    expect(net.status).toBe(0)
    expect(net.reqBody).toContain('[redacted]')
    expect(net.reqBody).not.toContain('secret')
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
