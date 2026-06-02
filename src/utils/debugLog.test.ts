import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  clear,
  getEntries,
  installConsoleCapture,
  recordError,
  uninstallConsoleCapture,
} from './debugLog'

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
