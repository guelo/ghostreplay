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

export type LogLevel = 'log' | 'warn' | 'error' | 'info' | 'debug' | 'net'

// Console interception must stay typed to ONLY the console methods. Adding 'net'
// to LogLevel otherwise breaks `console[level]` and the `originalConsole` map —
// TS can no longer prove `level` is a real console key. Net entries flow through
// pushNet, never through the console patch, so they use the wider LogLevel.
export type ConsoleLogLevel = Exclude<LogLevel, 'net'>

export type NetErrorKind = 'http' | 'network' | 'timeout' | 'abort'

export interface NetMeta {
  method: string // upper-cased
  url: string // path+search, origin stripped for readability (host kept for third-party)
  status: number // 0 when the request never got a response
  ok: boolean
  durationMs: number
  errorKind?: NetErrorKind // present only on failures
  requestId?: string | null // X-Request-ID echo, correlates with server logs
}

export interface LogEntry {
  id: number
  ts: number
  level: LogLevel
  args: string
  net?: NetMeta // present only when level === 'net'
}

const MAX_ENTRIES = 500
const PERSIST_ENTRIES = 200
const PERSIST_DEBOUNCE_MS = 500
const ARG_TRUNCATE = 2000
const STORAGE_KEY = 'gr.debugLog'

const CONSOLE_METHODS: ConsoleLogLevel[] = ['log', 'warn', 'error', 'info', 'debug']

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

function pushNet(meta: NetMeta): void {
  // Genuine failures / never-answered requests survive a reload (same rationale
  // as isImmediateLevel for error/warn). Successes AND aborts take the debounced
  // path: superseded openings/session fetches are routine UI churn, not real
  // failures, so flushing each to localStorage would be wasteful.
  const immediate = !meta.ok && meta.errorKind !== 'abort'
  const next = entries.concat({
    id: nextId++,
    ts: Date.now(),
    level: 'net',
    args: formatNetArgs(meta),
    net: meta,
  })
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

// --- Network interception --------------------------------------------------

/**
 * Origins whose path-only form is more readable in the overlay. We strip OUR
 * origins (same-origin prod `/api`, dev backend) but PRESERVE the host for
 * third-party egress (PostHog) so external calls stay distinguishable.
 *
 * Derived inline — NOT by importing api.ts — because debugLog must stay a
 * standalone module that installs before analytics (main.tsx). This mirrors the
 * localhost default at api.ts:44 without depending on it.
 */
function strippableOrigins(): Set<string> {
  const origins = new Set<string>()
  if (typeof window !== 'undefined') {
    origins.add(window.location.origin)
    const host = window.location.hostname
    if (host === 'localhost' || host === '127.0.0.1') {
      origins.add('http://localhost:8000')
    }
  }
  const configured = import.meta.env.VITE_API_URL
  if (typeof configured === 'string' && configured) {
    try {
      origins.add(new URL(configured).origin)
    } catch {
      // Relative values such as "/api" have no origin to strip — ignore.
    }
  }
  return origins
}

/** Strip our own origin for readability; keep the host for third-party calls. */
function displayUrl(raw: string): string {
  try {
    const base = typeof window !== 'undefined' ? window.location.origin : undefined
    const u = new URL(raw, base)
    return strippableOrigins().has(u.origin)
      ? u.pathname + u.search
      : u.host + u.pathname + u.search
  } catch {
    return raw
  }
}

/** One-line human summary; keeps the overlay text-filter + Copy working unchanged. */
function formatNetArgs(meta: NetMeta): string {
  const parts = [meta.method, meta.url, String(meta.status), `(${Math.round(meta.durationMs)}ms)`]
  if (meta.errorKind && meta.errorKind !== 'http') parts.push(`[${meta.errorKind}]`)
  else if (meta.errorKind === 'http') parts.push('[http]')
  if (meta.requestId) parts.push(`#${meta.requestId}`)
  return parts.join(' ')
}

/** Read the server-echoed request id; guard the accessor (mirrors api.ts readRequestId). */
function readRequestIdHeader(res: Response): string | null {
  return typeof res.headers?.get === 'function' ? res.headers.get('x-request-id') : null
}

/**
 * Classify a thrown fetch error. `TimeoutError` → deadline; `AbortError` →
 * deliberate cancellation (superseded fetch); else network (offline, DNS, CORS).
 * Keys off `.name` (not `instanceof Error`) since `AbortSignal.timeout()` rejects
 * with a `DOMException` that isn't an `Error` in every runtime.
 */
function classifyFetchError(error: unknown): NetErrorKind {
  const name = (error as { name?: unknown } | null)?.name
  if (name === 'TimeoutError') return 'timeout'
  if (name === 'AbortError') return 'abort'
  return 'network'
}

let originalFetch: typeof fetch | null = null

function installFetchCapture(): void {
  if (typeof window === 'undefined' || typeof window.fetch !== 'function') return
  // Store the RAW, UNBOUND ref (NOT .bind()) so uninstall can restore the EXACT
  // original identity. Assigning to window.fetch gives the arrow the `typeof
  // fetch` contextual type, so input/init are typed for free.
  originalFetch = window.fetch
  window.fetch = async (input, init) => {
    const method = (
      init?.method ?? (input instanceof Request ? input.method : 'GET')
    ).toUpperCase()
    const rawUrl =
      typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const start = performance.now()
    try {
      // .call(window) because we stored the raw unbound fn; bare invocation would
      // throw "Illegal invocation" (fetch requires `this === window`).
      const res = await originalFetch!.call(window, input, init)
      pushNet({
        method,
        url: displayUrl(rawUrl),
        status: res.status,
        ok: res.ok,
        durationMs: performance.now() - start,
        errorKind: res.ok ? undefined : 'http',
        requestId: readRequestIdHeader(res),
      })
      return res // pass-through UNTOUCHED — body never read, so no clone() needed
    } catch (err) {
      pushNet({
        method,
        url: displayUrl(rawUrl),
        status: 0,
        ok: false,
        durationMs: performance.now() - start,
        errorKind: classifyFetchError(err),
      })
      throw err
    }
  }
}

function uninstallFetchCapture(): void {
  if (originalFetch) {
    window.fetch = originalFetch
    originalFetch = null
  }
}

// --- Console interception --------------------------------------------------

interface PatchableConsole extends Console {
  __grPatched?: boolean
}

const originalConsole: Partial<Record<ConsoleLogLevel, (...args: unknown[]) => void>> = {}

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

  installFetchCapture()

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
  uninstallFetchCapture()
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
