// Persistent debug log store (g-0oow).
//
// Standalone module (no React) so it can initialize before app mount and be used
// anywhere. Captures console.*, uncaught errors, and fetch metadata (g-l8t2) —
// plus opt-in, redacted request/response bodies (g-bsg9) — into an in-memory
// ring buffer that is mirrored to localStorage, so logs are inspectable on
// mobile / when devtools is closed via the in-app DebugOverlay.
//
// >>> RELEASE BLOCKER (decision #2): capture is ALWAYS on during the dev phase.
// Before public release, revisit: persistent localStorage capture of all logs is a
// privacy/security risk. Network BODY capture (g-bsg9) is opt-in (?debugbody=1 /
// overlay "Bodies" toggle) and redacted (sensitive-key denylist + JWT/Bearer
// scrub; the Authorization header is never captured) — but that guarantee covers
// captured fetch bodies ONLY. The console.*/error path (serializeArg below) still
// persists arbitrary args unredacted — a console.log(token) would leak. Required
// follow-up: redaction in serializeArg, clear-on-logout, and a decision on PROD
// gating (flag/?debug=1).

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
  reqBody?: string // redacted + truncated; only when the body gate was on at request start
  resBody?: string // redacted + truncated; attached asynchronously after the response resolves
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
const BODY_STORAGE_KEY = 'gr.debugBody'
// Hard cap on bytes read from a response clone. Only needs to exceed the display
// cap enough to keep redaction reliable (a JWT is short) — we never read
// megabytes just to show 2 KB.
const BODY_READ_CAP = ARG_TRUNCATE * 4

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

function pushNet(meta: NetMeta): number {
  // Genuine failures / never-answered requests survive a reload (same rationale
  // as isImmediateLevel for error/warn). Successes AND aborts take the debounced
  // path: superseded openings/session fetches are routine UI churn, not real
  // failures, so flushing each to localStorage would be wasteful.
  const immediate = !meta.ok && meta.errorKind !== 'abort'
  const id = nextId++
  const next = entries.concat({
    id,
    ts: Date.now(),
    level: 'net',
    args: formatNetArgs(meta),
    net: meta,
  })
  entries = next.length > MAX_ENTRIES ? next.slice(next.length - MAX_ENTRIES) : next
  notify()
  schedulePersist(immediate)
  return id
}

/**
 * Attach the (already-redacted) response body to an existing net row once the
 * async clone read resolves. Failed rows re-flush immediately: their METADATA
 * was written at push time, but the error envelope arrives here later, and
 * losing it to the debounce on page close is exactly the case body capture
 * exists for. Successful/aborted rows stay on the debounced path.
 */
function setNetResBody(id: number, resBody: string): void {
  const idx = entries.findIndex((e) => e.id === id)
  if (idx === -1) return // already evicted from the ring buffer — drop silently
  const entry = entries[idx]
  if (!entry.net) return
  const net: NetMeta = { ...entry.net, resBody }
  // New array + new entry object: useSyncExternalStore snapshot identity.
  entries = entries
    .slice(0, idx)
    .concat({ ...entry, net, args: formatNetArgs(net) }, entries.slice(idx + 1))
  notify()
  schedulePersist(!net.ok && net.errorKind !== 'abort')
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

/**
 * Human summary; keeps the overlay text-filter + Copy working unchanged. Bodies
 * ride along as extra lines in `args` (the overlay renders args in a pre-wrap
 * <pre>), so filtering and copied bug reports include them for free.
 */
function formatNetArgs(meta: NetMeta): string {
  const parts = [meta.method, meta.url, String(meta.status), `(${Math.round(meta.durationMs)}ms)`]
  if (meta.errorKind && meta.errorKind !== 'http') parts.push(`[${meta.errorKind}]`)
  else if (meta.errorKind === 'http') parts.push('[http]')
  if (meta.requestId) parts.push(`#${meta.requestId}`)
  let line = parts.join(' ')
  if (meta.reqBody !== undefined) line += `\n  → ${meta.reqBody}`
  if (meta.resBody !== undefined) line += `\n  ← ${meta.resBody}`
  return line
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

// --- Body capture gate (g-bsg9) ---------------------------------------------

// Body capture is OPT-IN: bodies are heavier and higher-risk than metadata
// (login/register requests carry credentials, their responses carry a JWT), so
// nothing is captured unless ?debugbody=1 (persisted) or the overlay "Bodies"
// toggle turned it on. The fetch wrapper reads this flag PER-REQUEST, so a
// runtime toggle takes effect without reinstalling the capture.
let bodyCaptureOn = false

function initBodyGate(): void {
  try {
    const p =
      typeof window !== 'undefined'
        ? new URLSearchParams(window.location.search).get('debugbody')
        : null
    if (p === '1') {
      bodyCaptureOn = true
      localStorage.setItem(BODY_STORAGE_KEY, '1')
    } else if (p === '0') {
      bodyCaptureOn = false
      localStorage.removeItem(BODY_STORAGE_KEY)
    } else {
      bodyCaptureOn = localStorage.getItem(BODY_STORAGE_KEY) === '1'
    }
  } catch {
    bodyCaptureOn = false
  }
}

export function isBodyCaptureEnabled(): boolean {
  return bodyCaptureOn
}

/** Overlay "Bodies" toggle. Applies to the NEXT request (gate checked per-request). */
export function setBodyCapture(on: boolean): void {
  bodyCaptureOn = on
  try {
    if (on) localStorage.setItem(BODY_STORAGE_KEY, '1')
    else localStorage.removeItem(BODY_STORAGE_KEY)
  } catch {
    /* ignore */
  }
}

// --- Body redaction (the security core) --------------------------------------
//
// Invariant: REDACT FIRST, TRUNCATE LAST — a secret must never survive by
// landing across a truncation boundary. When a body cannot be redacted with
// confidence (non-JSON text carrying sensitive-key markers, or a read-cap cut
// mid-document), the whole payload is replaced with a note, never shown raw.

// Marker SUBSTRINGS matched against a separator-stripped, lowercased key, so
// client_secret / secretKey / X-Api-Key / apiToken all redact without needing
// an exhaustive denylist. 'username' and 'session_id' are intentionally kept —
// they are the primary debugging identifiers. Over-redaction is the acceptable
// failure direction for a debug tool.
const SENSITIVE_KEY_MARKERS = [
  'password',
  'passwd',
  'token',
  'secret',
  'apikey',
  'authorization',
  'credential',
  'privatekey',
  'email',
]

function isSensitiveKey(key: string): boolean {
  const s = key.toLowerCase().replace(/[^a-z0-9]/g, '')
  return SENSITIVE_KEY_MARKERS.some((m) => s.includes(m))
}

// Text-level detector for bodies we CANNOT parse — conservative: a sensitive
// marker anywhere sends the whole body to a note. Chess FEN/PGN never contains
// these words, and over-hiding an unknown-shape blob beats leaking it.
const SENSITIVE_TEXT_RE =
  /password|passwd|token|secret|api[-_ ]?key|authorization|credential|private[-_ ]?key|email/i

function hasSensitiveKeyText(s: string): boolean {
  return SENSITIVE_TEXT_RE.test(s)
}

// A string VALUE inside parsed JSON is suppressed only when it embeds an
// assignment-shaped secret (`password=…`, `"token": …`). A bare mention —
// "Invalid username or password" — stays visible: error envelopes are the
// overlay's main payload, so the value predicate must be precise where the
// unparseable-body one above is conservative.
const SENSITIVE_ASSIGNMENT_RE =
  /(password|passwd|token|secret|api[-_]?key|authorization|credential|private[-_]?key)\s*["']?\s*[=:]/i

const JWT_RE = /\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g
const BEARER_RE = /Bearer\s+[A-Za-z0-9._~+/=-]+/gi
const EMAIL_RE = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g

/** Always-applied token/PII-shape backstop, independent of body structure. */
function scrubText(s: string): string {
  return s
    .replace(JWT_RE, '[redacted-jwt]')
    .replace(BEARER_RE, 'Bearer [redacted]')
    .replace(EMAIL_RE, '[redacted-email]')
}

/** Replace every sensitive-keyed or secret-carrying value in a parsed JSON tree (parse ⇒ acyclic). */
function redactJsonValue(value: unknown): unknown {
  if (typeof value === 'string') {
    return SENSITIVE_ASSIGNMENT_RE.test(value) ? '[redacted]' : value
  }
  if (Array.isArray(value)) return value.map(redactJsonValue)
  if (value !== null && typeof value === 'object') {
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(value)) {
      out[k] = isSensitiveKey(k) ? '[redacted]' : redactJsonValue(v)
    }
    return out
  }
  return value
}

/**
 * Redact any COMPLETE body (request strings, fully-read responses):
 * 1. JSON → structural key redaction, then token scrub, then display truncation.
 * 2. Non-JSON with sensitive-key markers → generic note. We cannot key-redact a
 *    blob we can't parse, and scrubText only knows JWT/Bearer shapes — and the
 *    wrapper patches ALL fetch callers, so string bodies are not assumed safe.
 * 3. Benign non-JSON (PostHog egress, plain text) → scrubbed + truncated.
 */
function redactCompleteText(str: string): string {
  try {
    let parsed: unknown
    try {
      parsed = JSON.parse(str)
    } catch {
      if (hasSensitiveKeyText(str)) return '[body with sensitive keys, not shown]'
      return truncate(scrubText(str))
    }
    return truncate(scrubText(JSON.stringify(redactJsonValue(parsed))))
  } catch {
    return '[unserializable]'
  }
}

/**
 * Redact a response buffer that may have been CUT by the read cap. The cap is
 * itself a truncation BEFORE redaction, so a secret can be sliced mid-value —
 * JSON.parse then fails AND the token regexes miss the incomplete token (no
 * closing segment / boundary), which would persist the raw prefix. If the cut
 * buffer still parses, structural redaction remains safe (the whole parsed doc
 * is walked before display truncation); otherwise never show the raw prefix.
 */
function redactCappedBody(buffer: string, hitCap: boolean): string {
  if (hitCap) {
    try {
      JSON.parse(buffer)
    } catch {
      return '[large body truncated before safe redaction]'
    }
  }
  return redactCompleteText(buffer)
}

/**
 * Extract the request body from init.body, synchronously, without consuming
 * anything. Reads init.body ONLY — never input.body: reading a Request object's
 * stream would consume the app's own copy. This app issues every request as
 * fetch(url, { body }) (api.ts requestJson, AuthContext, openingBook), so the
 * fetch(new Request(...)) form is a documented gap (metadata + response body
 * are still captured for such calls).
 */
function captureRequestBody(init?: RequestInit): string | undefined {
  try {
    const body = init?.body
    if (body === undefined || body === null || body === '') return undefined
    if (typeof body === 'string') return redactCompleteText(body)
    if (typeof URLSearchParams !== 'undefined' && body instanceof URLSearchParams) {
      // Structural redaction (NOT toString() → text path): keys are known, so
      // sensitive params stay readable-but-safe (password=[redacted]). Joined
      // by hand because URLSearchParams.toString() would percent-encode the
      // [redacted] marker.
      const parts: string[] = []
      for (const [k, v] of body.entries()) {
        const hide = isSensitiveKey(k) || SENSITIVE_ASSIGNMENT_RE.test(v)
        parts.push(`${k}=${hide ? '[redacted]' : v}`)
      }
      return truncate(scrubText(parts.join('&')))
    }
    // Blob/FormData/ArrayBuffer/ReadableStream — placeholder, never consumed.
    const name = (body as { constructor?: { name?: string } }).constructor?.name || 'body'
    return `[${name}]`
  } catch {
    return '[unserializable]'
  }
}

/** Test doubles may lack .clone(); a body already in use throws — never propagate. */
function safeClone(res: Response): Response | null {
  try {
    return typeof res.clone === 'function' ? res.clone() : null
  } catch {
    return null
  }
}

/**
 * Kick off async response-body capture for a net row. The content-length check
 * runs BEFORE clone(): a tee branch that is never read buffers everything the
 * app consumes from the original stream, so cloning a known-large response
 * would defeat the cap even though we skip reading it.
 */
function captureResponseBody(res: Response, id: number): void {
  try {
    const lenHeader =
      typeof res.headers?.get === 'function' ? res.headers.get('content-length') : null
    if (lenHeader !== null) {
      const len = Number(lenHeader)
      if (Number.isFinite(len) && len > BODY_READ_CAP) {
        setNetResBody(id, `[large body ${len} bytes, not captured]`)
        return
      }
    }
    const clone = safeClone(res)
    if (clone) {
      void readBodyCapped(clone)
        .then((text) => setNetResBody(id, text))
        .catch(() => {})
    }
  } catch {
    /* capture must never break the fetch path */
  }
}

/**
 * Read a response CLONE with a hard byte cap, independent of content-length (a
 * missing or lying header must not let a chunked body buffer unbounded). Chunks
 * are byte-sliced to the remaining capacity BEFORE decoding, so even a single
 * chunk far larger than the cap only decodes/allocates `remaining` bytes; the
 * reader is then cancelled — safe, the app holds its own copy of the stream,
 * and cancelling the clone branch also stops the tee buffering on our behalf.
 */
async function readBodyCapped(clone: Response): Promise<string> {
  try {
    let buffer = ''
    let hitCap = false
    const reader =
      clone.body && typeof clone.body.getReader === 'function' ? clone.body.getReader() : null
    if (reader) {
      const decoder = new TextDecoder()
      let readBytes = 0
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        if (!value) continue
        const remaining = BODY_READ_CAP - readBytes
        const slice = value.byteLength > remaining ? value.subarray(0, remaining) : value
        buffer += decoder.decode(slice, { stream: true })
        readBytes += slice.byteLength
        if (slice.byteLength < value.byteLength || readBytes >= BODY_READ_CAP) {
          hitCap = true
          try {
            void reader.cancel().catch(() => {})
          } catch {
            /* ignore */
          }
          break
        }
      }
      buffer += decoder.decode()
    } else {
      // Fallback for doubles / exotic responses: complete body, capped by truncate.
      buffer = await clone.text()
    }
    return redactCappedBody(buffer, hitCap)
  } catch {
    return '[unreadable]'
  }
}

let originalFetch: typeof fetch | null = null

function installFetchCapture(): void {
  if (typeof window === 'undefined' || typeof window.fetch !== 'function') return
  // Store the RAW, UNBOUND ref (NOT .bind()) so uninstall can restore the EXACT
  // original identity. Assigning to window.fetch gives the arrow the `typeof
  // fetch` contextual type, so input/init are typed for free.
  originalFetch = window.fetch
  initBodyGate()
  window.fetch = async (input, init) => {
    // Snapshot the gate ONCE at request start so a mid-flight toggle cannot
    // produce a half-captured request (reqBody without resBody or vice versa).
    const captureBodies = bodyCaptureOn
    const method = (
      init?.method ?? (input instanceof Request ? input.method : 'GET')
    ).toUpperCase()
    const rawUrl =
      typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const reqBody = captureBodies ? captureRequestBody(init) : undefined
    const start = performance.now()
    try {
      // .call(window) because we stored the raw unbound fn; bare invocation would
      // throw "Illegal invocation" (fetch requires `this === window`).
      const res = await originalFetch!.call(window, input, init)
      const id = pushNet({
        method,
        url: displayUrl(rawUrl),
        status: res.status,
        ok: res.ok,
        durationMs: performance.now() - start,
        errorKind: res.ok ? undefined : 'http',
        requestId: readRequestIdHeader(res),
        reqBody,
      })
      if (captureBodies) {
        // Clone happens inside (before anyone reads the stream); the clone is
        // read asynchronously so the caller gets its response back immediately
        // and untouched.
        captureResponseBody(res, id)
      }
      return res // pass-through UNTOUCHED — only the clone is ever read
    } catch (err) {
      pushNet({
        method,
        url: displayUrl(rawUrl),
        status: 0,
        ok: false,
        durationMs: performance.now() - start,
        errorKind: classifyFetchError(err),
        reqBody, // a failed request's payload is exactly what you want to see
      })
      throw err
    }
  }
}

function uninstallFetchCapture(): void {
  bodyCaptureOn = false // test isolation — the gate never leaks across installs
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
