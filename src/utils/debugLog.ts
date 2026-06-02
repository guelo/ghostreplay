// Persistent debug log store (g-0oow).
//
// Standalone module (no React) so it can initialize before app mount and be used
// anywhere. Captures console.* and uncaught errors into an in-memory ring buffer
// that is mirrored to localStorage, so logs are inspectable on mobile / when
// devtools is closed via the in-app DebugOverlay.
//
// >>> RELEASE BLOCKER (decision #2): capture is ALWAYS on during the dev phase.
// Before public release, revisit: persistent localStorage capture of all logs is a
// privacy/security risk. Required follow-up: token/header/PII redaction in the
// serializer, clear-on-logout, and a decision on PROD gating (flag/?debug=1).

export type LogLevel = 'log' | 'warn' | 'error' | 'info' | 'debug'

export interface LogEntry {
  id: number
  ts: number
  level: LogLevel
  args: string
}

const MAX_ENTRIES = 500
const PERSIST_ENTRIES = 200
const PERSIST_DEBOUNCE_MS = 500
const ARG_TRUNCATE = 2000
const STORAGE_KEY = 'gr.debugLog'

const CONSOLE_METHODS: LogLevel[] = ['log', 'warn', 'error', 'info', 'debug']

let entries: LogEntry[] = []
let nextId = 1
const listeners = new Set<() => void>()

let debounceTimer: ReturnType<typeof setTimeout> | null = null

// --- Serialization ---------------------------------------------------------

function truncate(s: string): string {
  return s.length > ARG_TRUNCATE ? s.slice(0, ARG_TRUNCATE) + '…[truncated]' : s
}

function serializeError(err: Error | DOMException): string {
  const stack = 'stack' in err && err.stack ? `\n${err.stack}` : ''
  return `${err.name}: ${err.message}${stack}`
}

function serializeArg(arg: unknown): string {
  try {
    if (arg === null) return 'null'
    if (arg === undefined) return 'undefined'
    if (typeof arg === 'string') return truncate(arg)
    if (typeof arg !== 'object') return truncate(String(arg))

    if (arg instanceof Error || arg instanceof DOMException) {
      return truncate(serializeError(arg))
    }

    // PromiseRejectionEvent: unwrap reason through the Error branch.
    if (typeof PromiseRejectionEvent !== 'undefined' && arg instanceof PromiseRejectionEvent) {
      return `PromiseRejectionEvent: ${serializeArg(arg.reason)}`
    }

    // ErrorEvent: capture message/filename/lineno, not the live DOM object.
    if (typeof ErrorEvent !== 'undefined' && arg instanceof ErrorEvent) {
      const inner = arg.error ? `\n${serializeArg(arg.error)}` : ''
      return truncate(
        `ErrorEvent: ${arg.message} (${arg.filename}:${arg.lineno}:${arg.colno})${inner}`,
      )
    }

    // Generic Event: capture type, not the live object.
    if (typeof Event !== 'undefined' && arg instanceof Event) {
      return `Event<${arg.type}>`
    }

    // Plain object: JSON with circular-ref guard.
    const seen = new WeakSet<object>()
    return truncate(
      JSON.stringify(arg, (_key, value) => {
        if (typeof value === 'object' && value !== null) {
          if (seen.has(value)) return '[Circular]'
          seen.add(value)
        }
        if (value instanceof Error) return serializeError(value)
        return value
      }),
    )
  } catch {
    try {
      return truncate(String(arg))
    } catch {
      return '[unserializable]'
    }
  }
}

function serializeArgs(args: unknown[]): string {
  return args.map(serializeArg).join(' ')
}

// --- Store core ------------------------------------------------------------

function notify(): void {
  for (const listener of listeners) listener()
}

function push(level: LogLevel, args: string, immediate: boolean): void {
  // Publish a NEW array each push: useSyncExternalStore compares snapshot
  // identity, so mutating in place would leave the UI stale.
  const next = entries.concat({ id: nextId++, ts: Date.now(), level, args })
  entries = next.length > MAX_ENTRIES ? next.slice(next.length - MAX_ENTRIES) : next
  notify()
  schedulePersist(immediate)
}

export function getEntries(): readonly LogEntry[] {
  return entries
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function clear(): void {
  entries = []
  notify()
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* ignore */
  }
}

/** Feed a non-console failure path (e.g. worker errors) directly into the store. */
export function recordError(err: unknown, context?: string): void {
  const prefix = context ? `[${context}] ` : ''
  push('error', prefix + serializeArg(err), true)
}

// --- Persistence -----------------------------------------------------------

function writeNow(): void {
  if (debounceTimer !== null) {
    clearTimeout(debounceTimer)
    debounceTimer = null
  }
  try {
    const slice = entries.slice(-PERSIST_ENTRIES)
    localStorage.setItem(STORAGE_KEY, JSON.stringify(slice))
  } catch {
    /* quota / unavailable — ignore */
  }
}

function schedulePersist(immediate: boolean): void {
  if (immediate) {
    writeNow()
    return
  }
  if (debounceTimer !== null) return
  debounceTimer = setTimeout(() => {
    debounceTimer = null
    writeNow()
  }, PERSIST_DEBOUNCE_MS)
}

function hydrate(): void {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw) as LogEntry[]
    if (!Array.isArray(parsed)) return
    entries = parsed.filter(
      (e) => e && typeof e.id === 'number' && typeof e.args === 'string',
    )
    const maxId = entries.reduce((m, e) => Math.max(m, e.id), 0)
    nextId = maxId + 1
  } catch {
    entries = []
  }
}

// --- Console interception --------------------------------------------------

interface PatchableConsole extends Console {
  __grPatched?: boolean
}

const originalConsole: Partial<Record<LogLevel, (...args: unknown[]) => void>> = {}

function isImmediateLevel(level: LogLevel): boolean {
  return level === 'error' || level === 'warn'
}

function onWindowError(event: ErrorEvent): void {
  push('error', serializeArg(event), true)
}

function onUnhandledRejection(event: PromiseRejectionEvent): void {
  push('error', serializeArg(event), true)
}

function onPageHide(): void {
  writeNow()
}

export function installConsoleCapture(): void {
  const target = console as PatchableConsole
  if (target.__grPatched) return
  target.__grPatched = true

  hydrate()

  for (const level of CONSOLE_METHODS) {
    const original = console[level]
    originalConsole[level] = original
    console[level] = (...args: unknown[]) => {
      push(level, serializeArgs(args), isImmediateLevel(level))
      original.apply(console, args)
    }
  }

  if (typeof window !== 'undefined') {
    window.addEventListener('error', onWindowError)
    window.addEventListener('unhandledrejection', onUnhandledRejection)
    window.addEventListener('pagehide', onPageHide)
    window.addEventListener('beforeunload', onPageHide)
  }
}

/** TEST-ONLY: restore console, remove listeners, cancel timers, reset buffer. */
export function uninstallConsoleCapture(): void {
  const target = console as PatchableConsole
  for (const level of CONSOLE_METHODS) {
    const original = originalConsole[level]
    if (original) console[level] = original
    delete originalConsole[level]
  }
  delete target.__grPatched

  if (typeof window !== 'undefined') {
    window.removeEventListener('error', onWindowError)
    window.removeEventListener('unhandledrejection', onUnhandledRejection)
    window.removeEventListener('pagehide', onPageHide)
    window.removeEventListener('beforeunload', onPageHide)
  }

  if (debounceTimer !== null) {
    clearTimeout(debounceTimer)
    debounceTimer = null
  }
  entries = []
  nextId = 1
  listeners.clear()
}
