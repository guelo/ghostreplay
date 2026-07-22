/**
 * API client for Ghost Replay backend
 */
import { captureEvent } from '../analytics/posthog'

const isLocalHostname = (hostname: string): boolean =>
  hostname === 'localhost' || hostname === '127.0.0.1'

const isVercelHostname = (hostname: string): boolean =>
  hostname === 'vercel.app' || hostname.endsWith('.vercel.app')

export const resolveApiBaseUrl = (
  configuredUrl?: string,
  locationOverride?: { hostname: string; origin: string },
): string => {
  const currentLocation =
    locationOverride ??
    (typeof window === 'undefined'
      ? null
      : {
          hostname: window.location.hostname,
          origin: window.location.origin,
        })

  if (!currentLocation) {
    return configuredUrl || 'http://localhost:8000'
  }

  if (configuredUrl) {
    if (isVercelHostname(currentLocation.hostname)) {
      try {
        const parsed = new URL(configuredUrl, currentLocation.origin)
        if (parsed.origin !== currentLocation.origin) {
          return '/api'
        }
      } catch {
        // Non-URL values such as "/api" are already safe to use as-is.
      }
    }

    return configuredUrl
  }

  return isLocalHostname(currentLocation.hostname)
    ? 'http://localhost:8000'
    : '/api'
}

export const resolveApiEndpointBaseUrl = (
  configuredUrl?: string,
  locationOverride?: { hostname: string; origin: string },
): string => {
  const baseUrl = resolveApiBaseUrl(configuredUrl, locationOverride).replace(/\/+$/, '')
  return baseUrl.endsWith('/api') ? baseUrl.slice(0, -4) : baseUrl
}

const API_BASE_URL = resolveApiEndpointBaseUrl(import.meta.env.VITE_API_URL)
const RETRY_BASE_DELAY_MS = 200

// ---- Client request timing (analytics) ----------------------------
const UUID_SOURCE = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
const UUID_SEGMENT = new RegExp(`^${UUID_SOURCE}$`, 'i')
const NUMERIC_SEGMENT = /^\d+$/

/**
 * Concrete dynamic API paths → the EXACT backend route template (FastAPI param
 * names from Starlette's `route.path_format`, e.g. `{session_id}`). Matching the
 * server's own labels — not a generic `{id}` — is what lets the client
 * `api_request_client.route` join the server `api_request.route`
 * (see docs/posthog-latency-dashboard.md and backend/app/http_logging.py).
 * The dynamic segment is matched as a UUID so static siblings like
 * `/api/drills/start` fall through to the generic rule below rather than being
 * mislabeled `/api/drills/{session_id}`.
 */
const API_ROUTE_TEMPLATES: ReadonlyArray<readonly [RegExp, string]> = [
  [new RegExp(`^/api/drills/${UUID_SOURCE}/fail$`, 'i'), '/api/drills/{session_id}/fail'],
  [new RegExp(`^/api/drills/${UUID_SOURCE}/continue$`, 'i'), '/api/drills/{session_id}/continue'],
  [new RegExp(`^/api/drills/${UUID_SOURCE}/route-check$`, 'i'), '/api/drills/{session_id}/route-check'],
  [new RegExp(`^/api/drills/${UUID_SOURCE}/natural-end$`, 'i'), '/api/drills/{session_id}/natural-end'],
  [new RegExp(`^/api/drills/${UUID_SOURCE}/abandon$`, 'i'), '/api/drills/{session_id}/abandon'],
  [new RegExp(`^/api/drills/${UUID_SOURCE}$`, 'i'), '/api/drills/{session_id}'],
  [new RegExp(`^/api/session/${UUID_SOURCE}/moves$`, 'i'), '/api/session/{session_id}/moves'],
  [new RegExp(`^/api/session/${UUID_SOURCE}/analysis-evidence$`, 'i'), '/api/session/{session_id}/analysis-evidence'],
  [new RegExp(`^/api/session/${UUID_SOURCE}/analysis$`, 'i'), '/api/session/{session_id}/analysis'],
  [new RegExp(`^/api/session/${UUID_SOURCE}/openings$`, 'i'), '/api/session/{session_id}/openings'],
  [new RegExp(`^/api/openings/score-delta/${UUID_SOURCE}$`, 'i'), '/api/openings/score-delta/{session_id}'],
]

/**
 * Reduce a request URL to a low-cardinality route template for analytics: strip
 * the origin/base URL and query string, map known dynamic routes to their exact
 * backend template, then fall back to replacing UUID/bare-numeric segments with
 * `{id}` for anything unrecognized (bounds cardinality for new/future routes).
 */
export const normalizeApiPath = (rawUrl: string): string => {
  let path: string
  try {
    // A dummy base lets relative (`/api/...`) and absolute URLs both parse; the
    // base is ignored when `rawUrl` is already absolute.
    path = new URL(rawUrl, 'http://_').pathname
  } catch {
    path = rawUrl.split('?')[0].split('#')[0]
  }
  const known = API_ROUTE_TEMPLATES.find(([pattern]) => pattern.test(path))
  if (known) return known[1]
  return path
    .split('/')
    .map((segment) =>
      UUID_SEGMENT.test(segment) || NUMERIC_SEGMENT.test(segment) ? '{id}' : segment,
    )
    .join('/')
}

/**
 * Read the server-echoed request id (`X-Request-ID`) off a response so the
 * client event can be correlated 1:1 with the server's `api_request` + logs.
 * Guards the accessor like `createApiError` does, since test doubles omit it.
 */
export const readRequestId = (response: Response): string | null =>
  typeof response.headers?.get === 'function'
    ? response.headers.get('x-request-id')
    : null

export type ApiRequestErrorKind = 'http' | 'network' | 'timeout' | 'parse'

/**
 * Classify a thrown fetch (network-level) error for the `error_kind` property.
 * A request the server never answered surfaces as a `TimeoutError` (deadline)
 * or anything else (offline, DNS, CORS, abort) which we bucket as `network`.
 * Keys off `.name` (not `instanceof Error`) because `AbortSignal.timeout()`
 * rejects with a `DOMException`, which is not an `Error` in every runtime.
 */
export const classifyNetworkError = (error: unknown): ApiRequestErrorKind =>
  (error as { name?: unknown } | null)?.name === 'TimeoutError'
    ? 'timeout'
    : 'network'

/**
 * A deliberately-cancelled request (superseded fetch via `AbortSignal`) rejects
 * with an `AbortError`. That is not a round-trip outcome, so we skip reporting
 * it to keep the client error rate free of self-inflicted "network" failures.
 */
export const isAbortError = (error: unknown): boolean =>
  (error as { name?: unknown } | null)?.name === 'AbortError'

/**
 * Classify a rejection raised while reading the response BODY.
 *
 * `fetch()` resolves as soon as the headers arrive, so a deadline that expires
 * mid-body rejects HERE, not at `fetch()`. Blanket-labelling those `parse` would
 * silently drop late deadline overruns out of the timeout cohort — exactly the
 * population g-upload-observe exists to measure. The body stream errors with the
 * signal's abort reason (`TimeoutError` for `AbortSignal.timeout()`), but some
 * runtimes surface a bare `AbortError` on the stream instead, so we read
 * `signal.reason` first and fall back to the thrown value.
 *
 * Returns `null` for a deliberate cancellation — not a round-trip outcome, so it
 * goes unreported exactly as in the network branch.
 */
export const classifyBodyReadError = (
  error: unknown,
  signal?: AbortSignal | null,
): ApiRequestErrorKind | null => {
  const cause = signal?.aborted ? (signal.reason ?? error) : error
  const name = (cause as { name?: unknown } | null)?.name
  if (name === 'TimeoutError') return 'timeout'
  if (name === 'AbortError') return null
  return 'parse'
}

/**
 * The four end-of-session terminal actions that trigger a `final_full` upload.
 * Carried on the CLIENT `api_request_client` event (available even when no
 * receipt exists), so it — not the server-only `session_mode` — classifies the
 * drill-vs-game path for the unmatched (noncommit) cohort:
 * `drill_natural_end`/`accuracy_fail` ⇒ drill, `game_end`/`resign` ⇒ normal game.
 */
export type TerminalAction =
  | 'game_end'
  | 'resign'
  | 'drill_natural_end'
  | 'accuracy_fail'

/** Which of the three `/moves` callers emitted an upload (g-upload-observe). */
export type UploadKind = 'final_full' | 'incremental' | 'revert'

/**
 * Mint a client-generated correlation id for a request. Unlike the server id
 * (read off the response, absent on a timeout), this is attached to EVERY
 * outcome — including a timed-out request that never got a response — so a
 * client timeout can still be joined to the server's durable upload receipt.
 */
export const newClientRequestId = (): string => crypto.randomUUID()

/** `document.visibilityState` when defined (guards non-DOM/test runtimes). */
const readVisibilityState = (): string | undefined =>
  typeof document !== 'undefined' ? document.visibilityState : undefined

/**
 * Optional per-request telemetry threaded through `requestJson` and attached to
 * the `api_request_client` event on ALL outcome branches. Every field is
 * optional; an absent field is emitted as null. `clientRequestId` additionally
 * injects the `X-Client-Request-ID` request header so the server can key its
 * durable receipt on the same id.
 */
export interface ApiRequestTelemetry {
  uploadKind?: UploadKind
  clientRequestId?: string
  terminalAction?: TerminalAction | null
  moveCount?: number
  /** Actual transmitted UTF-8 byte length of the serialized body. */
  payloadBytes?: number
  /** The deadline that constructed the request's timeout (final_full only). */
  deadlineMs?: number
  /** `document.visibilityState` captured at call time (before the fetch). */
  visibilityStateStart?: string
}

/**
 * Emit exactly one `api_request_client` event for the FINAL outcome of one
 * logical request (never per retry attempt). No-ops when analytics is disabled.
 * Client-side timing complements the server's `api_request`: it also sees
 * retries, timeouts, offline, and CORS failures the server never records.
 */
export const reportApiRequest = (params: {
  url: string
  method: string
  /** HTTP status, or 0 when the request never reached a response. */
  status: number
  ok: boolean
  durationMs: number
  attempts: number
  errorKind?: ApiRequestErrorKind
  /** Server-echoed request id, or null when no response was received. */
  requestId?: string | null
  /** Correlation/upload telemetry; absent for most requests. */
  telemetry?: ApiRequestTelemetry
}): void => {
  const t = params.telemetry
  // Read visibility at EMIT for the end state; only meaningful alongside a start.
  const visibilityEnd = t ? readVisibilityState() : undefined
  captureEvent('api_request_client', {
    route: normalizeApiPath(params.url),
    method: params.method,
    status: params.status,
    ok: params.ok,
    duration_ms: Math.round(params.durationMs),
    attempts: params.attempts,
    error_kind: params.errorKind ?? null,
    request_id: params.requestId ?? null,
    upload_kind: t?.uploadKind ?? null,
    client_request_id: t?.clientRequestId ?? null,
    terminal_action: t?.terminalAction ?? null,
    move_count: t?.moveCount ?? null,
    payload_bytes: t?.payloadBytes ?? null,
    deadline_ms: t?.deadlineMs ?? null,
    visibility_state_start: t?.visibilityStateStart ?? null,
    visibility_state_end: visibilityEnd ?? null,
    // A tab hidden mid-request: start !== end. Null unless both are known.
    visibility_changed:
      t?.visibilityStateStart != null && visibilityEnd != null
        ? t.visibilityStateStart !== visibilityEnd
        : null,
  })
}
// -------------------------------------------------------------------

/**
 * Get headers for authenticated API requests, including JWT token if available.
 */
const getAuthHeaders = (): Record<string, string> => {
  const token = localStorage.getItem('ghost_replay_token')
  return {
    'Content-Type': 'application/json',
    ...(token && { Authorization: `Bearer ${token}` }),
  }
}

type ApiErrorEnvelope = {
  detail?: string
  error?: {
    code?: string
    message?: string
    details?: unknown
    retryable?: boolean
  }
}

export class ApiError extends Error {
  status: number
  code: string
  details: unknown
  retryable: boolean
  /** Parsed `Retry-After` header in milliseconds, when present and parseable. */
  retryAfterMs?: number

  constructor(
    message: string,
    options: {
      status: number
      code?: string
      details?: unknown
      retryable?: boolean
      retryAfterMs?: number
    },
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = options.status
    this.code = options.code ?? `http_${options.status}`
    this.details = options.details
    this.retryable =
      options.retryable ?? (options.status === 429 || options.status >= 500)
    this.retryAfterMs = options.retryAfterMs
  }
}

/**
 * Type guard to read a backend `error_code` off an unknown error.
 * Backend conflict responses carry `{ error: { details: { error_code } } }`,
 * surfaced here as `ApiError.details`.
 */
export const errorCodeOf = (err: unknown): string | undefined => {
  if (
    err instanceof ApiError &&
    err.details &&
    typeof err.details === 'object' &&
    'error_code' in err.details
  ) {
    return (err.details as { error_code?: string }).error_code
  }
  return undefined
}

/**
 * Parse a `Retry-After` header value into milliseconds.
 * Supports both delta-seconds (e.g. "120") and an HTTP-date (RFC 1123).
 * Returns undefined when absent or unparseable.
 */
const parseRetryAfterMs = (headerValue: string | null): number | undefined => {
  if (!headerValue) return undefined
  const trimmed = headerValue.trim()
  if (/^\d+$/.test(trimmed)) {
    return Number(trimmed) * 1000
  }
  const dateMs = Date.parse(trimmed)
  if (!Number.isNaN(dateMs)) {
    return Math.max(0, dateMs - Date.now())
  }
  return undefined
}

const delay = async (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms))

/**
 * Read an error response's body, tolerating a malformed/empty one (a non-2xx with
 * unparseable JSON is still an `http` outcome, so the envelope is simply absent).
 *
 * An ABORT rejection is deliberately NOT swallowed: a 4xx/5xx whose body stalls
 * past the deadline is a `timeout`, and burying it here would report it as `http`
 * and drop it out of the measured cohort exactly as the success path once did.
 * The caller classifies the rethrown reason against the live signal.
 */
const parseJsonSafely = async (
  response: Response,
): Promise<ApiErrorEnvelope | null> => {
  try {
    return (await response.json()) as ApiErrorEnvelope
  } catch (error) {
    const name = (error as { name?: unknown } | null)?.name
    if (name === 'AbortError' || name === 'TimeoutError') throw error
    return null
  }
}

const getErrorMessage = (
  payload: ApiErrorEnvelope | null,
  fallback: string,
  statusText: string,
): string => {
  if (payload?.error?.message) return payload.error.message
  if (payload?.detail) return payload.detail
  return `${fallback}: ${statusText}`
}

const createApiError = async (
  response: Response,
  fallbackMessage: string,
): Promise<ApiError> => {
  const payload = await parseJsonSafely(response)
  const message = getErrorMessage(payload, fallbackMessage, response.statusText)
  const retryAfterHeader =
    typeof response.headers?.get === 'function'
      ? response.headers.get('Retry-After')
      : null
  return new ApiError(message, {
    status: response.status,
    code: payload?.error?.code,
    details: payload?.error?.details,
    retryable: payload?.error?.retryable,
    retryAfterMs: parseRetryAfterMs(retryAfterHeader),
  })
}

const requestJson = async <T>(
  url: string,
  init: RequestInit,
  options?: {
    retries?: number
    fallbackMessage?: string
    telemetry?: ApiRequestTelemetry
  },
): Promise<T> => {
  const retries = options?.retries ?? 0
  const method = init.method ?? 'GET'
  const fallbackMessage =
    options?.fallbackMessage ?? `Request failed: ${method} ${url}`
  const telemetry = options?.telemetry
  // Inject the client correlation id as a request header so the server keys its
  // durable receipt on the SAME id the client event carries. Merged as a plain
  // object (all callers pass object headers); a set id is a load-bearing join key.
  const requestInit: RequestInit = telemetry?.clientRequestId
    ? {
        ...init,
        headers: {
          ...(init.headers as Record<string, string> | undefined),
          'X-Client-Request-ID': telemetry.clientRequestId,
        },
      }
    : init
  // Span the whole logical request (across retries) so duration/attempts reflect
  // what the caller actually waited for.
  const start = performance.now()

  for (let attempt = 0; attempt <= retries; attempt += 1) {
    const attempts = attempt + 1

    let response: Response
    try {
      response = await fetch(url, requestInit)
    } catch (error) {
      // Network-level failure (offline, DNS, CORS, timeout) — the server never
      // answered. Retry transient ones; report only the final outcome. A
      // deliberate cancellation is not an outcome, so it is never reported.
      if (attempt < retries) {
        await delay(RETRY_BASE_DELAY_MS * 2 ** attempt)
        continue
      }
      if (!isAbortError(error)) {
        reportApiRequest({
          url,
          method,
          status: 0,
          ok: false,
          durationMs: performance.now() - start,
          attempts,
          errorKind: classifyNetworkError(error),
          telemetry,
        })
      }
      throw error
    }

    if (response.ok) {
      // Capture success only AFTER the body parses — a parse failure is NOT a
      // successful request, so report it as `parse` rather than swallowing it.
      try {
        const parsed = (await response.json()) as T
        reportApiRequest({
          url,
          method,
          status: response.status,
          ok: true,
          durationMs: performance.now() - start,
          attempts,
          requestId: readRequestId(response),
          telemetry,
        })
        return parsed
      } catch (bodyError) {
        // fetch() already resolved at the headers, so a deadline expiring while
        // the body streams rejects HERE — classify by the abort reason so it
        // lands in the timeout cohort rather than masquerading as `parse`.
        const errorKind = classifyBodyReadError(bodyError, requestInit.signal)
        if (errorKind) {
          reportApiRequest({
            url,
            method,
            status: response.status,
            ok: false,
            durationMs: performance.now() - start,
            attempts,
            errorKind,
            // Kept even for a late timeout: a real status plus a server request
            // id prove the server answered, which is the strongest client-side
            // evidence the write committed.
            requestId: readRequestId(response),
            telemetry,
          })
        }
        throw bodyError
      }
    }

    let apiError: ApiError
    try {
      apiError = await createApiError(response, fallbackMessage)
    } catch (bodyError) {
      // The error body stalled past the deadline. Same rule as the success path:
      // the abort — not the status — is the outcome, so it belongs in the timeout
      // cohort. No retry either; the signal is already spent.
      const errorKind = classifyBodyReadError(bodyError, requestInit.signal)
      if (errorKind) {
        reportApiRequest({
          url,
          method,
          status: response.status,
          ok: false,
          durationMs: performance.now() - start,
          attempts,
          errorKind,
          requestId: readRequestId(response),
          telemetry,
        })
      }
      throw bodyError
    }
    if (attempt < retries && apiError.retryable) {
      await delay(RETRY_BASE_DELAY_MS * 2 ** attempt)
      continue
    }
    reportApiRequest({
      url,
      method,
      status: response.status,
      ok: false,
      durationMs: performance.now() - start,
      attempts,
      errorKind: 'http',
      requestId: readRequestId(response),
      telemetry,
    })
    throw apiError
  }

  throw new Error('Unexpected retry loop exit')
}

interface StartGameRequest {
  engine_elo: number
  player_color: 'white' | 'black'
}

// ---- Drill types --------------------------------------------------
export type DrillStrictness = 'lenient' | 'standard' | 'strict'
export type DrillSessionState =
  | 'active'
  | 'root_reached'
  | 'failed'
  | 'abandoned'
  | 'converted'

export interface DrillStartRequest {
  opening_key: string
  player_color: 'white' | 'black'
  engine_elo: number
  strictness: DrillStrictness
  // Always required on new drills — the slider always produces a value.
  // The response type has strictness_cp? because legacy sessions may lack it.
  strictness_cp: number
  // Ad-hoc card drills: the full UCI line from the start to the target FEN
  // (opening_key). Omitted for registered-root drills.
  line?: string[]
}

export interface DrillSessionContract {
  session_id: string
  mode: string
  drill_state: DrillSessionState
  opening_key: string
  opening_name: string
  opening_family: string
  eco: string | null
  depth: number
  player_color: string
  engine_elo: number
  strictness: string
  strictness_cp: number | null
  is_rated: boolean
  rated_start_ply: number | null
  normal_started_at: string | null
  converted_at: string | null
  terminal_reason?: 'off_route' | 'accuracy' | 'natural_end' | null
  opening_score_changes?: OpeningScoreDeltaItem[] | null
}

export type DrillRouteStatus = 'on_route' | 'root_reached' | 'failed'

export interface DrillRouteSuggestion {
  uci: string
  san: string
  resulting_fen: string
  plies_to_target: number
}

export interface DrillRouteFailure {
  played_move_uci: string | null
  played_move_san: string | null
  correction_fen: string
  reason?: 'off_route' | 'accuracy'
}

export interface DrillRouteCheckResponse {
  status: DrillRouteStatus
  current_fen: string
  target_fen: string
  suggestions: DrillRouteSuggestion[]
  failure: DrillRouteFailure | null
}

export interface DrillRouteMetadata {
  status: Exclude<DrillRouteStatus, 'failed'>
  target_fen: string
  resulting_fen: string
  plies_to_target: number
}

export interface OpeningRootItem {
  opening_key: string
  opening_name: string
  opening_family: string
  eco: string | null
  depth: number
}

export interface OpeningRootsListResponse {
  families: Array<{ family_name: string; roots: OpeningRootItem[] }>
  total_roots: number
  total_families: number
}
// ---- end drill types ----------------------------------------------

interface StartGameResponse {
  session_id: string
  engine_elo: number
  player_color?: 'white' | 'black'
}

interface EndGameRequest {
  session_id: string
  result: 'checkmate_win' | 'checkmate_loss' | 'resign' | 'draw' | 'abandon'
  pgn: string
  is_rated: boolean
}

export interface RatingChange {
  rating_before: number
  rating_after: number
  is_provisional: boolean
}

export type RatingScoreKey = 'elo' | 'chesscom' | 'lichess'

export interface RatingScore {
  rating: number
  rd?: number
  volatility?: number
  is_provisional: boolean
}

export type RatingScores = {
  elo: RatingScore
  chesscom: RatingScore | null
  lichess: RatingScore | null
}

/**
 * One played opening's score change over a just-ended game or drill (g-xanz).
 * `before`/`after` are raw opening scores (same units the /openings cards show);
 * `delta` is `after - before` when both are known. `is_new` is true when the
 * opening was crossed for the first time this session (no baseline entry) — then
 * `before`/`delta` are null and only the after-score is shown.
 */
export interface OpeningScoreDeltaItem {
  opening_key: string
  opening_name: string
  opening_family: string
  eco: string | null
  depth: number
  before: number | null
  after: number | null
  delta: number | null
  is_new: boolean
}

interface EndGameResponse {
  session_id: string
  result: string
  ended_at: string
  rating: RatingChange | null
  scores?: RatingScores | null
  score_changes?: RatingScores | null
  scores_after?: RatingScores | null
  opening_score_changes?: OpeningScoreDeltaItem[] | null
}

export interface CurrentRatingResponse {
  current_rating: number
  is_provisional: boolean
  games_played: number
  scores?: RatingScores
}

export interface RatingPoint {
  timestamp: string
  rating: number
  is_provisional: boolean
  game_session_id: string
  scores?: RatingScores
}

export interface RatingHistoryResponse {
  ratings: RatingPoint[]
  current_rating: number
  games_played: number
  scores?: RatingScores
}

export type SessionMoveColor = 'white' | 'black'
export type SessionDecisionSource =
  | 'ghost_path'
  | 'backend_engine'
  | 'local_fallback'

import type { MoveClassification } from '../workers/analysisUtils'
// Re-export MoveClassification so existing imports from api.ts keep working
export type { MoveClassification }

export interface SessionMoveUpload {
  move_number: number
  color: SessionMoveColor
  move_san: string
  fen_after: string
  eval_cp: number | null
  eval_mate: number | null
  best_move_san: string | null
  best_move_eval_cp: number | null
  // RAW mover-relative loss (uncapped). Backend projects it two ways: RAW into
  // analysis_cache (clamp_delta_nonneg, session.py) and NORMALIZED 0..1000 into
  // session_moves (centipawn_loss, session.py) — must stay raw.
  eval_delta: number | null
  classification: MoveClassification | null
  fen_before: string | null
  move_uci: string | null
  best_move_uci: string | null
  /** Root best-move principal variation (UCI). Starts with best_move_uci. */
  best_line_uci?: string[] | null
  decision_source: SessionDecisionSource | null
  target_blunder_id: number | null
  /** True only for a deterministic terminal score synthesized by the client. */
  synthetic_terminal_eval?: boolean
}

interface SessionMovesRequest {
  moves: SessionMoveUpload[]
  /**
   * When false, the backend SKIPS the expensive blunder-opportunity recompute
   * for this upload (g-y90g). Mid-game incremental uploads send false; only the
   * final, complete upload sends true (or omits it — backend defaults to true).
   * Omitted from the body when undefined so callers that don't care get the
   * backend default.
   */
  recompute_opportunity?: boolean
  /**
   * Sent ONLY on the end-of-session `final_full` upload (g-upload-observe). Its
   * PRESENCE is how the server identifies a final_full upload and gates the
   * durable receipt write; the incremental and revert uploads omit it. Omitted
   * from the body when undefined so those uploads send the exact shape they always
   * have.
   */
  terminal_action?: TerminalAction
}

interface SessionMovesResponse {
  moves_inserted: number
  drill_state?: DrillSessionState | null
  drill_terminal_reason?: 'off_route' | 'accuracy' | 'natural_end' | null
}

/**
 * Exact POST body for `POST /api/blunder`. Exported so the DecisionOwner,
 * `evaluateBlunderCandidate`, and the retry outbox can share one shape; the
 * outbox sends the same `idempotency_key` on every attempt so retries dedupe.
 */
export interface RecordBlunderRequest {
  session_id: string
  pgn: string
  fen: string
  user_move: string
  best_move: string
  eval_before: number
  eval_after: number
  idempotency_key?: string
}

interface ManualBlunderRequest {
  session_id: string
  pgn: string
  fen: string
  user_move: string
  best_move: string | null
  eval_before: number | null
  eval_after: number | null
}

interface BlunderResponse {
  blunder_id: number | null
  position_id: number
  positions_created: number
  is_new: boolean
}

export interface TargetBlunderSrs {
  last_reviewed_at: string | null
  created_at: string | null
  pass_count: number
  fail_count: number
  pass_streak: number
}

interface NextOpponentMoveResponse {
  mode: 'ghost' | 'engine'
  move: { uci: string; san: string }
  target_blunder_id: number | null
  target_blunder_srs: TargetBlunderSrs | null
  target_fen: string | null
  decision_source: Exclude<SessionDecisionSource, 'local_fallback'>
  drill_route: DrillRouteMetadata | null
}

interface SrsReviewRequest {
  session_id: string
  blunder_id: number
  passed: boolean
  user_move: string
  eval_delta: number
  idempotency_key?: string
}

interface SrsReviewResponse {
  blunder_id: number
  pass_streak: number
  priority: number
  next_expected_review: string
}

/**
 * Fetch opening root families for drill selection.
 */
export const getOpeningRoots = async (): Promise<OpeningRootsListResponse> => {
  return requestJson<OpeningRootsListResponse>(
    `${API_BASE_URL}/api/openings/roots`,
    { method: 'GET', headers: getAuthHeaders() },
    { fallbackMessage: 'Failed to load opening roots' },
  )
}

/**
 * Start a new drill session.
 */
export const startDrill = async (
  req: DrillStartRequest,
): Promise<DrillSessionContract> => {
  return requestJson<DrillSessionContract>(
    `${API_BASE_URL}/api/drills/start`,
    {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify(req),
    },
    { fallbackMessage: 'Failed to start drill' },
  )
}

/**
 * Fetch an existing drill session contract.
 */
export const getDrill = async (sessionId: string): Promise<DrillSessionContract> => {
  return requestJson<DrillSessionContract>(
    `${API_BASE_URL}/api/drills/${sessionId}`,
    { method: 'GET', headers: getAuthHeaders() },
    { fallbackMessage: 'Failed to load drill session' },
  )
}

/**
 * Continue the current drill from the given ply.
 */
export const continueDrill = async (
  sessionId: string,
  currentPly: number,
): Promise<DrillSessionContract> => {
  return requestJson<DrillSessionContract>(
    `${API_BASE_URL}/api/drills/${sessionId}/continue`,
    {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({ current_ply: currentPly }),
    },
    { fallbackMessage: 'Failed to continue drill' },
  )
}

/**
 * Mark a post-root drill as failed after an accuracy mistake.
 */
export const failDrill = async (
  sessionId: string,
  terminalReason: 'accuracy' = 'accuracy',
): Promise<DrillSessionContract> => {
  return requestJson<DrillSessionContract>(
    `${API_BASE_URL}/api/drills/${sessionId}/fail`,
    {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({ terminal_reason: terminalReason }),
    },
    { fallbackMessage: 'Failed to record drill failure' },
  )
}

export const checkDrillRoute = async (
  sessionId: string,
  request: {
    current_fen: string
    previous_fen?: string
    played_uci?: string
  },
): Promise<DrillRouteCheckResponse> => {
  return requestJson<DrillRouteCheckResponse>(
    `${API_BASE_URL}/api/drills/${sessionId}/route-check`,
    {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify(request),
    },
    { fallbackMessage: 'Failed to check drill route' },
  )
}

/**
 * End drill via natural game-over (checkmate/stalemate/draw).
 */
export const naturalEndDrill = async (
  sessionId: string,
  result: 'checkmate_win' | 'checkmate_loss' | 'draw',
  pgn: string,
): Promise<DrillSessionContract> => {
  return requestJson<DrillSessionContract>(
    `${API_BASE_URL}/api/drills/${sessionId}/natural-end`,
    {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({ result, pgn }),
    },
    { fallbackMessage: 'Failed to end drill' },
  )
}

/**
 * Abandon the current drill.
 */
export const abandonDrill = async (sessionId: string): Promise<DrillSessionContract> => {
  return requestJson<DrillSessionContract>(
    `${API_BASE_URL}/api/drills/${sessionId}/abandon`,
    { method: 'POST', headers: getAuthHeaders() },
    { fallbackMessage: 'Failed to abandon drill' },
  )
}
// -------------------------------------------------------------------

/**
 * Start a new game session
 */
export const startGame = async (
  engineElo: number = 1500,
  playerColor: StartGameRequest['player_color'] = 'white',
): Promise<StartGameResponse> => {
  return requestJson<StartGameResponse>(`${API_BASE_URL}/api/game/start`, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify({
      engine_elo: engineElo,
      player_color: playerColor,
    } satisfies StartGameRequest),
  }, { fallbackMessage: 'Failed to start game' })
}

/**
 * End a game session
 */
export const endGame = async (
  sessionId: string,
  result: EndGameRequest['result'],
  pgn: string,
  isRated: boolean = true,
): Promise<EndGameResponse> => {
  return requestJson<EndGameResponse>(`${API_BASE_URL}/api/game/end`, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify({ session_id: sessionId, result, pgn, is_rated: isRated } satisfies EndGameRequest),
  }, { fallbackMessage: 'Failed to end game' })
}

/**
 * Fetch the user's current Elo rating.
 */
export const fetchCurrentRating = async (): Promise<CurrentRatingResponse> => {
  return requestJson<CurrentRatingResponse>(`${API_BASE_URL}/api/stats/current-rating`, {
    method: 'GET',
    headers: getAuthHeaders(),
  }, { fallbackMessage: 'Failed to fetch rating' })
}

/**
 * Fetch the user's rating history for a given time range.
 */
export const fetchRatingHistory = async (
  range: '7d' | '30d' | '90d' | 'all' = 'all',
): Promise<RatingHistoryResponse> => {
  return requestJson<RatingHistoryResponse>(
    `${API_BASE_URL}/api/stats/rating-history?range=${range}`,
    { method: 'GET', headers: getAuthHeaders() },
    { fallbackMessage: 'Failed to fetch rating history' },
  )
}

/**
 * Options for `uploadSessionMoves`, a discriminated union on `uploadKind` so the
 * three callers can never mix the wrong fields (g-upload-observe):
 *
 *  - `final_full` REQUIRES `terminalAction` + `deadlineMs`. The timeout is
 *    constructed HERE from `deadlineMs` (`AbortSignal.timeout`), so the recorded
 *    `deadline_ms` and the live signal cannot drift; the call site no longer
 *    builds its own signal.
 *  - `incremental` passes an external cancellation `signal` straight through.
 *  - `revert` has neither.
 *
 * The union guarantees `deadlineMs`/`terminalAction` and `signal` are never both
 * present, so no signal-combining is ever needed.
 */
export type UploadSessionMovesOptions =
  | {
      uploadKind: 'final_full'
      terminalAction: TerminalAction
      deadlineMs: number
      recomputeOpportunity?: boolean
    }
  | { uploadKind: 'incremental'; signal?: AbortSignal; recomputeOpportunity?: boolean }
  | { uploadKind: 'revert'; recomputeOpportunity?: boolean }

/**
 * Upload analyzed session moves in a single batch.
 */
export const uploadSessionMoves = async (
  sessionId: string,
  moves: SessionMoveUpload[],
  options: UploadSessionMovesOptions,
): Promise<SessionMovesResponse> => {
  // Only include recompute_opportunity in the body when the caller specifies it,
  // so omitting it falls through to the backend default (true). Mid-game
  // incremental uploads pass false to skip the opportunity recompute (g-y90g).
  // terminal_action is sent ONLY on final_full — its presence is the server's
  // final_full discriminator and the receipt-write gate.
  const body: SessionMovesRequest = {
    moves,
    ...(options.recomputeOpportunity === undefined
      ? {}
      : { recompute_opportunity: options.recomputeOpportunity }),
    ...(options.uploadKind === 'final_full'
      ? { terminal_action: options.terminalAction }
      : {}),
  }
  // Serialize ONCE: reuse the exact bytes for the fetch body AND the measured
  // payload size. payloadBytes is the transmitted UTF-8 byte length (TextEncoder),
  // NOT JSON.stringify(body).length (UTF-16 code units — wrong for multi-byte).
  const bodyJson = JSON.stringify(body satisfies SessionMovesRequest)
  const payloadBytes = new TextEncoder().encode(bodyJson).byteLength
  const clientRequestId = newClientRequestId()

  // Exactly one signal source per case (no combining): final_full constructs its
  // own deadline here; incremental forwards the external signal; revert has none.
  const signal =
    options.uploadKind === 'final_full'
      ? AbortSignal.timeout(options.deadlineMs)
      : options.uploadKind === 'incremental'
        ? options.signal
        : undefined

  const telemetry: ApiRequestTelemetry = {
    uploadKind: options.uploadKind,
    clientRequestId,
    moveCount: moves.length,
    payloadBytes,
    terminalAction:
      options.uploadKind === 'final_full' ? options.terminalAction : null,
    deadlineMs:
      options.uploadKind === 'final_full' ? options.deadlineMs : undefined,
    visibilityStateStart: readVisibilityState(),
  }

  return requestJson<SessionMovesResponse>(
    `${API_BASE_URL}/api/session/${sessionId}/moves`,
    {
      method: 'POST',
      headers: getAuthHeaders(),
      body: bodyJson,
      signal,
    },
    { fallbackMessage: 'Failed to upload session moves', telemetry },
  )
}

/**
 * Cached analysis result from the backend analysis cache (see backend
 * CachedAnalysisResult). Carries TWO grains that must never be conflated:
 *
 *  - POSITION grain — resolver-derived for the NORMALIZED position and gated by
 *    `position_trusted`. The flattened best-* fields (`best_move_uci`,
 *    `best_move_san`, `best_line_uci`, `best_eval`, `best_eval_mate`) are this
 *    grain; they are `null` when no trusted position resolved and are NEVER
 *    copied from the move row.
 *  - MOVE grain — the exact `(fen, move_uci)` row and gated by `move_trusted`:
 *    `move_san`, `played_eval`, `played_eval_mate`, `eval_delta`,
 *    `classification`.
 *
 * Evals are white-relative centipawns; mate counts are white-relative.
 */
export interface CachedAnalysis {
  // ── MOVE grain (exact (fen, move_uci) row) ──
  /**
   * Null on a POSITION-ONLY hit (trusted position resolved, no exact
   * (fen, move_uci) move row): there is no played move, so no SAN.
   */
  move_san: string | null
  played_eval: number | null
  /** White-relative mate count for the played move, null when not a mate. */
  played_eval_mate: number | null
  /** RAW (uncapped, may be mate pseudo-cp) canonical-run cache snapshot; any
   * blunder/SRS/display consumer normalizes it to 0..1000 via evalLoss before use. */
  eval_delta: number | null
  classification: MoveClassification | null
  /**
   * CROSS-GRAIN drill threshold loss (mover-relative CP, clamped >= 0),
   * derived on the BACKEND from the trusted position `best_eval` and the trusted
   * move `played_eval`. Non-null ONLY when both grains are trusted, both pure CP
   * (no mate field on either), and their profiles are search-strength EQUAL.
   * This — NOT `eval_delta` — is the trusted loss the drill grader reads;
   * `eval_delta` is the RAW (uncapped, may be mate pseudo-cp) canonical-run cache
   * snapshot; any blunder/SRS/display consumer normalizes it to 0..1000 via
   * evalLoss before use. The frontend does no eval arithmetic on it.
   */
  position_eval_loss_cp: number | null

  // ── POSITION grain (resolver-derived; null when no trusted position) ──
  best_move_uci: string | null
  best_move_san: string | null
  best_line_uci: string[] | null
  best_eval: number | null
  /** White-relative mate count for the best move, null when not a mate. */
  best_eval_mate: number | null

  // ── Grain-specific trust (backend-decided; independent of one another) ──
  /** A trusted position resolved → drives the POSITION-grain best_* fields. */
  position_trusted: boolean
  /** The move row's played evidence passes the move-grain (move-complete-v1) gate. */
  move_trusted: boolean

  /**
   * Quality metadata from the backend (see backend CachedAnalysisResult).
   * `authoritative` is true only when the row's identity fields match an active
   * authoritative profile; a structurally-complete but non-authoritative row
   * (e.g. a browser-game upload) must NOT override local worker analysis.
   */
  source?: string | null
  analysis_profile_id?: string | null
  engine_version?: string | null
  engine_build?: string | null
  evidence_contract_id?: string | null
  authoritative?: boolean
  /**
   * Backend-computed: the row's evidence passes its declared contract's
   * semantic validation. Diagnostics only — does not by itself confer trust.
   */
  contract_satisfied?: boolean
}

interface AnalysisLookupRequest {
  positions: { fen: string; move_uci: string }[]
}

interface AnalysisLookupResponse {
  results: Record<string, CachedAnalysis>
}

/**
 * Batch-lookup cached analysis results for position+move pairs.
 * Returns a Map keyed by "fen::move_uci" with only cache hits.
 */
export const lookupAnalysisCache = async (
  positions: { fen: string; move_uci: string }[],
): Promise<Map<string, CachedAnalysis>> => {
  if (positions.length === 0) {
    return new Map()
  }

  const data = await requestJson<AnalysisLookupResponse>(
    `${API_BASE_URL}/api/analysis/lookup`,
    {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({ positions } satisfies AnalysisLookupRequest),
    },
    { fallbackMessage: 'Failed to lookup analysis cache' },
  )

  return new Map(Object.entries(data.results))
}

/**
 * One approved analysis-board evidence row for cache persistence
 * (g-cache-stronger-evals). White-relative evals; `eval_delta` is recomputed
 * client-side from those white-relative evals. Carries no SAN, profile, authority,
 * source, or contract fields — the backend derives SAN, stamps the profile, and
 * validates the contract.
 */
export interface AnalysisEvidenceRow {
  fen: string
  move_uci: string
  best_move_uci: string
  best_line_uci: string[]
  played_eval: number | null
  played_eval_mate: number | null
  best_eval: number | null
  best_eval_mate: number | null
  eval_delta: number | null
  classification: string | null
}

/**
 * A stronger re-annotation of one played move (g-xox0). Built ONCE on the backend
 * from a stored analysis_cache row in the SAME mover-relative representation as
 * `AnalysisMove`, so the immediate patch (Part B) and the durable fetch-time overlay
 * (Part C) cannot diverge. `classification` is the only required field; every
 * eval/SAN field is nullable. `authoritative` (backend-stamped, NOT re-derived from
 * profile ids) drives display precedence: a non-authoritative overlay must not
 * override a trusted position.
 */
export interface MoveUpgrade {
  classification: MoveClassification
  eval_cp: number | null // mover-relative (matches AnalysisMove.eval_cp)
  eval_mate: number | null // mover-relative
  best_move_san: string | null
  best_move_eval_cp: number | null // side-to-move-relative
  eval_delta: number | null // side-to-move-relative loss, normalized 0..1000 (display/decision CPL; raw evidence stays in analysis_cache)
  authoritative: boolean
  analysis_profile_id?: string | null
  depth?: number | null
}

export interface AnalysisEvidenceResult {
  fen: string
  move_uci: string
  reason: string
  /** Present (non-null) only when the write was accepted (g-xox0 Part B). */
  upgrade?: MoveUpgrade | null
}

interface AnalysisEvidenceResponse {
  results: AnalysisEvidenceResult[]
}

/**
 * Persist approved depth-21 analysis-board evidence for an owned session's exact
 * mainline moves. Best-effort like lookup: the caller treats a rejection/throw as
 * a missed upgrade, never a hard error.
 */
export const submitAnalysisEvidence = async (
  sessionId: string,
  rows: AnalysisEvidenceRow[],
): Promise<AnalysisEvidenceResult[]> => {
  if (rows.length === 0) {
    return []
  }

  const data = await requestJson<AnalysisEvidenceResponse>(
    `${API_BASE_URL}/api/session/${sessionId}/analysis-evidence`,
    {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({ rows }),
    },
    { fallbackMessage: 'Failed to submit analysis evidence' },
  )

  return data.results
}

/**
 * Record a blunder from a game session
 */
export const recordBlunder = async (
  sessionId: string,
  pgn: string,
  fen: string,
  userMove: string,
  bestMove: string,
  evalBefore: number,
  evalAfter: number,
  idempotencyKey?: string,
): Promise<BlunderResponse> => {
  return requestJson<BlunderResponse>(`${API_BASE_URL}/api/blunder`, {
    method: 'POST',
    headers: getAuthHeaders(),
    // JSON.stringify drops `idempotency_key` when undefined, so legacy callers
    // that omit the key send the exact body they always have.
    body: JSON.stringify({
      session_id: sessionId,
      pgn,
      fen,
      user_move: userMove,
      best_move: bestMove,
      eval_before: evalBefore,
      eval_after: evalAfter,
      idempotency_key: idempotencyKey,
    } satisfies RecordBlunderRequest),
  }, { fallbackMessage: 'Failed to record blunder' })
}

/**
 * Manually add a selected move to ghost library
 */
export const recordManualBlunder = async (
  sessionId: string,
  pgn: string,
  fen: string,
  userMove: string,
  bestMove: string | null,
  evalBefore: number | null,
  evalAfter: number | null,
): Promise<BlunderResponse> => {
  return requestJson<BlunderResponse>(`${API_BASE_URL}/api/blunder/manual`, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify({
      session_id: sessionId,
      pgn,
      fen,
      user_move: userMove,
      best_move: bestMove,
      eval_before: evalBefore,
      eval_after: evalAfter,
    } satisfies ManualBlunderRequest),
  }, { fallbackMessage: 'Failed to add move to ghost library' })
}

/**
 * History types
 */
/**
 * `average_centipawn_loss` is null IFF no player move has an eval_delta. A partially
 * analyzed game reports the average over the plies that resolved; 0 means perfect
 * play, not missing data — never gate it on truthiness.
 */
export interface HistoryGameSummary {
  total_moves: number
  blunders: number
  mistakes: number
  inaccuracies: number
  average_centipawn_loss: number | null
  accuracy: number | null
}

/** /session/{id}/analysis sends no `total_moves` — the move list carries it. */
export interface SessionAnalysisSummary {
  blunders: number
  mistakes: number
  inaccuracies: number
  average_centipawn_loss: number | null
  accuracy: number | null
}

export interface HistoryGame {
  session_id: string
  started_at: string
  ended_at: string | null
  result: string | null
  engine_elo: number
  player_color: string
  opening_name: string | null
  summary: HistoryGameSummary
}

interface HistoryResponse {
  games: HistoryGame[]
}

/**
 * Fetch game history for the current user
 */
export const fetchHistory = async (
  limit: number = 50,
): Promise<HistoryGame[]> => {
  const params = new URLSearchParams({ limit: String(limit) })
  const resp = await requestJson<HistoryResponse>(
    `${API_BASE_URL}/api/history?${params}`,
    { method: 'GET', headers: getAuthHeaders() },
    { fallbackMessage: 'Failed to load game history' },
  )
  return resp.games
}

/**
 * Session analysis types
 */
export interface AnalysisMove {
  move_number: number
  color: SessionMoveColor
  move_san: string
  fen_after: string
  /**
   * Exact evidence keys (g-cache-stronger-evals). The backend populates these from
   * the stored `SessionMove.fen_before` plus a python-chess SAN->UCI derivation;
   * they are null only for legacy moves with a null/unparseable `fen_before`.
   * Consumers (analysis board, exact-best projection, evidence driver) prefer these
   * directly and never reconstruct `fen_before` from the previous move's
   * `fen_after` nor derive played UCI from SAN. Optional so frontend-local snapshots
   * (drill review) may omit them.
   */
  fen_before?: string | null
  move_uci?: string | null
  eval_cp: number | null
  eval_mate: number | null
  best_move_san: string | null
  best_move_eval_cp: number | null
  // normalized 0..1000 (display/decision CPL); backend session-analysis echo
  // (centipawn_loss). Distinct from the RAW analysis_cache eval_delta.
  eval_delta: number | null
  classification: MoveClassification | null
  /**
   * Read-time re-annotation overlay (g-xox0 Part C): a stronger label joined from
   * analysis_cache for this exact played move, attached ALONGSIDE the base fields
   * (which stay on original game-time evidence so the BACKEND summary and accuracy
   * keep aggregates on original). The review pages' DISPLAYED counts/Avg CPL do not:
   * both project the base fields through `projectExactBest` before computing stats
   * (g-22t8.2), while board-only overlays never reach page-level stats. Null when no
   * display-upgrade-eligible cache row exists. Optional so frontend-local snapshots
   * (drill review) may omit it; `projectExactBest` passes it through untouched.
   */
  upgraded?: MoveUpgrade | null
}

export interface PositionAnalysis {
  best_move_uci: string
  best_move_san: string | null
  best_move_eval_cp: number | null  // side-to-move-relative
  best_move_eval_mate?: number | null  // side-to-move-relative
  /** Root best-move principal variation (UCI). Starts with best_move_uci. */
  best_line_uci?: string[] | null
  /**
   * Whether the backend resolved this as a TRUSTED position (matches the backend
   * pydantic model — required, never defaulted). Locally-built seeds (drill
   * snapshots from worker results) set this `false`. Whether AnalysisBoard gates
   * its restricted-search skip on this flag is g-54h5's call; this field only
   * delivers the honest signal.
   */
  position_trusted: boolean
}

export interface SessionAnalysis {
  session_id: string
  pgn: string | null
  result: string | null
  moves: AnalysisMove[]
  summary: SessionAnalysisSummary
  position_analysis?: Record<string, PositionAnalysis>
  expected_total_moves: number | null
  analyzed_moves: number
  is_complete: boolean
  player_color: 'white' | 'black'
}

/**
 * Fetch analysis data for a specific game session
 */
export const fetchAnalysis = async (
  sessionId: string,
): Promise<SessionAnalysis> => {
  return requestJson<SessionAnalysis>(
    `${API_BASE_URL}/api/session/${sessionId}/analysis`,
    { method: 'GET', headers: getAuthHeaders() },
    { fallbackMessage: 'Failed to load game analysis' },
  )
}

export interface OpeningLineageItem {
  opening_key: string
  opening_name: string
  opening_family: string
  eco: string | null
  depth: number
  score: number | null
  confidence: number | null
  coverage: number | null
  sample_size: number | null
  game_count: number | null
  path: string[]
  /** The player's actual SAN moves up to and including the move that crossed
   *  into this opening (e.g. ["e4", "c6", "Bc4"]). Numbered from `start_ply`. */
  moves: string[]
}

/** Whether the server's opening scores are available yet. `pending` means the
 *  score cache was cold: the lineage is complete but every score field is null
 *  and a background recompute is running, so the client should show a loading
 *  affordance and re-poll rather than render the cards as unscored. */
export type OpeningScoreStatus = 'ready' | 'pending'

export interface SessionOpeningsResponse {
  player_color: OpeningPlayerColor
  lineage: OpeningLineageItem[]
  /** Ply of `moves[0]` (1 = White's move 1). Constant across all lineage items;
   *  anchors move numbering so a drill starting mid-game still numbers right. */
  start_ply: number
  score_status: OpeningScoreStatus
}

/** Wire shape: `score_status` is optional so a client running against an older
 *  backend still type-checks; it is defaulted at the boundary below. */
interface SessionOpeningsWire extends Omit<SessionOpeningsResponse, 'score_status'> {
  score_status?: OpeningScoreStatus
}

/**
 * Fetch the opening lineage (broadest -> deepest) actually played in a session.
 */
export const fetchSessionOpenings = async (
  sessionId: string,
  options?: { signal?: AbortSignal },
): Promise<SessionOpeningsResponse> => {
  const raw = await requestJson<SessionOpeningsWire>(
    `${API_BASE_URL}/api/session/${sessionId}/openings`,
    { method: 'GET', headers: getAuthHeaders(), signal: options?.signal },
    { fallbackMessage: 'Failed to load session openings' },
  )
  // requestJson only CASTS the payload — it does not default absent fields, so
  // normalize explicitly. An older backend omitting the field degrades to
  // 'ready', i.e. exactly the pre-g-a5v3 behavior (render whatever scores came
  // back, never show a spinner).
  return { ...raw, score_status: raw.score_status ?? 'ready' }
}

/**
 * Get next opponent move via unified backend pipeline (ghost + engine).
 */
export const getNextOpponentMove = async (
  sessionId: string,
  fen: string,
  moves: string[] = [],
): Promise<NextOpponentMoveResponse> => {
  return requestJson<NextOpponentMoveResponse>(`${API_BASE_URL}/api/game/next-opponent-move`, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify({ session_id: sessionId, fen, moves }),
  }, { retries: 2, fallbackMessage: 'Failed to get opponent move' })
}

/**
 * Record pass/fail review outcome for a targeted blunder.
 */
export const reviewSrsBlunder = async (
  sessionId: string,
  blunderId: number,
  passed: boolean,
  userMove: string,
  evalDelta: number,
  idempotencyKey?: string,
): Promise<SrsReviewResponse> => {
  return requestJson<SrsReviewResponse>(`${API_BASE_URL}/api/srs/review`, {
    method: 'POST',
    headers: getAuthHeaders(),
    // JSON.stringify drops `idempotency_key` when undefined, so legacy callers
    // that omit the key send the exact body they always have.
    body: JSON.stringify({
      session_id: sessionId,
      blunder_id: blunderId,
      passed,
      user_move: userMove,
      eval_delta: evalDelta,
      idempotency_key: idempotencyKey,
    } satisfies SrsReviewRequest),
  }, { fallbackMessage: 'Failed to record SRS review' })
}

export interface BlunderListItem {
  id: number
  fen: string
  bad_move: string
  best_move: string
  eval_loss_cp: number
  opening_family: string | null
  pass_streak: number
  last_reviewed_at: string | null
  created_at: string
  srs_priority: number
  srs_due: boolean
  ghost_eligible: boolean
  practice_priority_score: number
  review_count: number
  pass_count: number
  fail_count: number
  last_result: boolean | null
  source_session_id?: string | null
  last_session_id: string | null
  last_played_at: string | null
  opportunities_since_review: number
  opportunities_30d: number
  reached_30d: number
  reached_since_review: number
  p_reach: number
}

export interface BlunderListResponse {
  items: BlunderListItem[]
  total: number
  due_total: number | null
  practice_ready_total: number | null
  limit: number
  offset: number
  due: boolean
  practice_ready: boolean
}

export interface FetchBlundersParams {
  due?: boolean
  practiceReady?: boolean
  limit?: number
  offset?: number
}

/**
 * Fetch the user's blunder library, optionally filtered to only due items.
 */
export const fetchBlunders = async (
  params: FetchBlundersParams = {},
): Promise<BlunderListResponse> => {
  const searchParams = new URLSearchParams()
  if (params.due) searchParams.set('due', 'true')
  if (params.practiceReady) searchParams.set('practice_ready', 'true')
  if (params.limit !== undefined) searchParams.set('limit', String(params.limit))
  if (params.offset !== undefined) searchParams.set('offset', String(params.offset))
  const qs = searchParams.toString()
  return requestJson<BlunderListResponse>(
    `${API_BASE_URL}/api/blunder${qs ? `?${qs}` : ''}`,
    { method: 'GET', headers: getAuthHeaders() },
    { fallbackMessage: 'Failed to load blunders' },
  )
}

export type OpeningPlayerColor = 'white' | 'black'

export interface FamilyScoreItem {
  family_name: string
  root_count: number
  family_score: number
  family_confidence: number
  family_coverage: number
  root_sample_size_sum: number
  last_practiced_at: string | null
  weakest_root_name: string
  weakest_root_score: number
}

export interface FamilyScoresResponse {
  player_color: OpeningPlayerColor
  families: FamilyScoreItem[]
  total_families: number
  computed_at: string | null
}

/**
 * Fetch opening family scores for one player color.
 */
export const getOpeningFamilyScores = async (
  playerColor: OpeningPlayerColor,
): Promise<FamilyScoresResponse> => {
  const params = new URLSearchParams({
    player_color: playerColor,
  })

  return requestJson<FamilyScoresResponse>(
    `${API_BASE_URL}/api/openings/families/scores?${params}`,
    { method: 'GET', headers: getAuthHeaders() },
    { fallbackMessage: 'Failed to load opening families' },
  )
}

// ---- Opening tree (horizontal move-graph) read model (epic g-d5cu) -----------
// Mirrors the pydantic models in backend/app/api/openings.py. Evals are
// WHITE-RELATIVE centipawns/mate and are rendered as-is on the card (standard
// +white / −black convention); the column sort applies the column's
// side-to-move favorability on the backend.

export interface TreeNode {
  parent_fen: string
  child_fen: string
  uci: string
  san: string
  ply: number
  opening_name: string | null
  eco: string | null
  in_book: boolean
  /** uci is in parent's structural child set OR is this column's user-selected
   *  move (g-obh5) → gates node clicks. Board drops are NOT gated: any legal
   *  move can be played, becoming a user-selected node. */
  is_navigable: boolean
  is_observed: boolean
  /** A legal move chosen on the board that is not in book/observed — the third
   *  move type. Line-scoped: only emitted as the selected move of its column. */
  is_user_selected: boolean
  /** The edge comes from the validated routing overlay: it reaches a named book
   *  position through a different move order (g-openings-transpose). Not
   *  mutually exclusive with the other flags — a selected overlay edge outside
   *  the navigable set carries this AND is_user_selected. Optional so an older
   *  backend response still parses. */
  is_transposition?: boolean
  is_prepared: boolean
  user_choice_count: number
  encounter_count: number
  opening_score: number | null
  confidence: number | null
  coverage: number | null
  sample_size: number | null
  game_count: number | null
  last_practiced_at: string | null
  eval_cp: number | null
  eval_mate: number | null
  terminal_reason: string | null
  drill_opening_key: string | null
  /** Backend-baked selection flag — the page derives selection from the URL
   *  line instead, so a cached superset can be clipped/re-rendered. */
  is_selected: boolean
}

export interface TreeColumn {
  position_fen: string
  ply: number
  selected_uci: string | null
  nodes: TreeNode[]
}

export interface TreeResponse {
  player_color: OpeningPlayerColor
  /** Resolved/normalized UCI line; invalid input lines truncate to this. */
  canonical_line: string[]
  selected_fen: string
  selected_ply: number
  selected_is_terminal: boolean
  selected_terminal_reason: string | null
  drill_opening_key: string | null
  /** Start-position eval (white-relative); line-independent. */
  root_eval_cp: number | null
  root_eval_mate: number | null
  root_opening_score: number | null
  root_coverage: number | null
  root_game_count: number | null
  root_confidence: number | null
  columns: TreeColumn[]
  batch_computed_at: string | null
  model_version: string
  /** Diagnostic cache signal (warm_fresh / bootstrapped / book_only /
   *  bootstrap_timeout) for a DIRECT caller that did not gate on /tree/status —
   *  lets it detect a degraded book-only/timeout tree. Optional for backward
   *  compatibility with older responses (g-k4z2). */
  cache_state?: string
}

/** Cheap cache-state of the opening tree for one (color): `warm` => load /tree
 *  now; `building` => a one-time bootstrap is running; `cold` => this poll just
 *  kicked it off. `building`/`cold` both mean "show the setup UI and keep
 *  polling". (g-k4z2) */
export type TreeCacheState = "warm" | "building" | "cold"

export interface TreeStatusResponse {
  player_color: OpeningPlayerColor
  state: TreeCacheState
}

/**
 * Fetch the hydrated opening tree for one canonical move line in a single
 * request. The line is built from repeated `move=<uci>`; a legacy `opening=<fen>`
 * deep-link entry is honored only when no `move` is present. `options.signal`
 * threads an AbortController so a superseded in-flight request can be cancelled.
 */
export const getOpeningTree = async (
  params: {
    playerColor: OpeningPlayerColor
    moves?: string[]
    opening?: string | null
  },
  options?: { signal?: AbortSignal },
): Promise<TreeResponse> => {
  const { playerColor, moves, opening } = params
  const search = new URLSearchParams({ player_color: playerColor })
  for (const move of moves ?? []) {
    search.append('move', move)
  }
  if (opening && !moves?.length) {
    search.set('opening', opening)
  }

  return requestJson<TreeResponse>(
    `${API_BASE_URL}/api/openings/tree?${search}`,
    { method: 'GET', headers: getAuthHeaders(), signal: options?.signal },
    { fallbackMessage: 'Failed to load openings' },
  )
}

/**
 * Cheap, non-blocking cache-state probe for one (color). The page polls this
 * before/while loading the tree so a cold (user, color) — whose tree needs a
 * one-time ~22s bootstrap — shows an explicit "Setting up…" state instead of a
 * silent long spinner. Warm reads return immediately. (g-k4z2)
 */
export const getOpeningTreeStatus = async (
  playerColor: OpeningPlayerColor,
  options?: { signal?: AbortSignal },
): Promise<TreeStatusResponse> => {
  const search = new URLSearchParams({ player_color: playerColor })
  return requestJson<TreeStatusResponse>(
    `${API_BASE_URL}/api/openings/tree/status?${search}`,
    { method: 'GET', headers: getAuthHeaders(), signal: options?.signal },
    { fallbackMessage: 'Failed to load openings' },
  )
}

export interface OpeningScoreDeltaPollResponse {
  opening_score_changes: OpeningScoreDeltaItem[] | null
  is_fresh: boolean
}

/**
 * Reconcile-poll for the end-of-session opening-score delta (g-fix-end-latency).
 * The terminal endpoints now return a warm (possibly stale) delta immediately and
 * recompute in the background; this GET is polled until `is_fresh` so the banner
 * self-corrects in place. Shared by game and drill sessions. `options.signal`
 * threads a per-request timeout so a hung GET can't stall the poll loop.
 */
export const getOpeningScoreDelta = async (
  sessionId: string,
  options?: { signal?: AbortSignal },
): Promise<OpeningScoreDeltaPollResponse> => {
  return requestJson<OpeningScoreDeltaPollResponse>(
    `${API_BASE_URL}/api/openings/score-delta/${sessionId}`,
    { method: 'GET', headers: getAuthHeaders(), signal: options?.signal },
    { fallbackMessage: 'Failed to load opening score delta' },
  )
}

export type StatsWindowDays = 0 | 7 | 30 | 90 | 365

export interface StatsGamesSummary {
  played: number
  score_pct: number | null
  wins: number
  losses: number
  draws: number
  avg_moves: number
}

export interface StatsColorSummary {
  games: number
  score_pct: number | null
  accuracy_pct: number | null
}

export interface StatsColorSplitSummary {
  white: StatsColorSummary
  black: StatsColorSummary
}

export interface StatsMoveQualityDistribution {
  inaccuracy: number
  mistake: number
  blunder: number
}

export interface StatsMoveSummary {
  accuracy_pct: number | null
  mistake_free_game_rate: number | null
  // null when there are zero classified player moves.
  quality_distribution: StatsMoveQualityDistribution | null
}

export interface StatsTrainingSummary {
  retention_pct: number | null
  reviewed_blunders: number
  retained_blunders: number
  review_pass_rate: number | null
  reviews_total: number
  reviews_passed: number
  conversions_in_window: number
  mastery_threshold: number
}

export interface StatsTopCostlyBlunder {
  blunder_id: number
  eval_loss_cp: number
  bad_move_san: string
  best_move_san: string
  created_at: string
}

export interface StatsLibrarySummary {
  blunders_total: number
  new_blunders_in_window: number
  avg_blunder_eval_loss_cp: number
  top_costly_blunders: StatsTopCostlyBlunder[]
}

export interface StatsOpeningStat {
  opening_name: string
  opening_family: string
  player_color: string
  opening_score: number
  sample_size: number
  game_count: number
}

export interface StatsOpeningsSummary {
  strongest: StatsOpeningStat[]
  weakest: StatsOpeningStat[]
}

export interface StatsSummaryResponse {
  window_days: number
  generated_at: string
  games: StatsGamesSummary
  moves: StatsMoveSummary
  colors: StatsColorSplitSummary
  training: StatsTrainingSummary
  library: StatsLibrarySummary
  openings: StatsOpeningsSummary
}

// Perfect Streak is served only by the standalone /achievements endpoint (used
// by the in-game streak toast); it is no longer part of the stats summary.
export interface PerfectStreakSummary {
  personal_best: number
}

export interface StatsAchievementsSummary {
  perfect_streak: PerfectStreakSummary
}

/**
 * Fetch account-level stats summary for a selected time window.
 */
export const getStatsSummary = async (
  windowDays: StatsWindowDays = 30,
): Promise<StatsSummaryResponse> => {
  const params = new URLSearchParams({
    window_days: String(windowDays),
  })

  return requestJson<StatsSummaryResponse>(
    `${API_BASE_URL}/api/stats/summary?${params}`,
    { method: 'GET', headers: getAuthHeaders() },
    { fallbackMessage: 'Failed to load stats summary' },
  )
}

export const getStatsAchievements = async (): Promise<StatsAchievementsSummary> => {
  return requestJson<StatsAchievementsSummary>(
    `${API_BASE_URL}/api/stats/achievements`,
    { method: 'GET', headers: getAuthHeaders() },
    { fallbackMessage: 'Failed to load achievements' },
  )
}
