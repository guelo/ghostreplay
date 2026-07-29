import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock the analytics singleton so `reportApiRequest` -> `captureEvent` is an
// observable spy and never touches the real PostHog client / network.
vi.mock('../analytics/posthog', () => ({
  captureEvent: vi.fn(),
  initAnalytics: vi.fn(),
  identifyUser: vi.fn(),
  resetAnalytics: vi.fn(),
  isAnalyticsEnabled: vi.fn(() => false),
  posthog: { capture: vi.fn(), identify: vi.fn(), reset: vi.fn() },
}))

import { captureEvent } from '../analytics/posthog'
import {
  normalizeApiPath,
  startGame,
  getNextOpponentMove,
  uploadSessionMoves,
} from './api'
import type { SessionMoveUpload } from './api'

const captureMock = vi.mocked(captureEvent)

const fetchMock = vi.fn()
vi.stubGlobal('fetch', fetchMock)

// Minimal localStorage stub (jsdom's may not be a full Storage here).
let mockStore: Record<string, string> = {}
Object.defineProperty(window, 'localStorage', {
  value: {
    getItem: (key: string) => mockStore[key] ?? null,
    setItem: (key: string, value: string) => {
      mockStore[key] = value
    },
    removeItem: (key: string) => {
      delete mockStore[key]
    },
    clear: () => {
      mockStore = {}
    },
    length: 0,
    key: () => null,
  },
  writable: true,
})

// Mimics fetch's case-insensitive Headers.get, echoing a server request id.
const headersWith = (requestId: string) => ({
  get: (name: string) =>
    name.toLowerCase() === 'x-request-id' ? requestId : null,
})

const okResponse = (data: Record<string, unknown>, status = 200) =>
  fetchMock.mockResolvedValueOnce({
    ok: true,
    status,
    statusText: 'OK',
    headers: headersWith('req-ok'),
    json: () => Promise.resolve(data),
  })

const errorResponse = (
  data: Record<string, unknown>,
  status: number,
  statusText = 'Error',
) =>
  fetchMock.mockResolvedValueOnce({
    ok: false,
    status,
    statusText,
    headers: headersWith('req-err'),
    json: () => Promise.resolve(data),
  })

/** The single `api_request_client` event captured during this call. */
const onlyClientEvent = () => {
  const calls = captureMock.mock.calls.filter(
    ([event]) => event === 'api_request_client',
  )
  expect(calls).toHaveLength(1)
  return calls[0][1] as Record<string, unknown>
}

describe('normalizeApiPath', () => {
  const uuid = '7f3e4d2a-1b2c-4d5e-8f90-abcdef123456'

  it('maps a dynamic drill route to the exact backend template', () => {
    expect(normalizeApiPath(`http://localhost:8000/api/drills/${uuid}/fail`)).toBe(
      '/api/drills/{session_id}/fail',
    )
  })

  it('maps a bare drill-session route to the backend template', () => {
    expect(normalizeApiPath(`/api/drills/${uuid}`)).toBe('/api/drills/{session_id}')
  })

  it('maps the session moves route to the backend template', () => {
    expect(normalizeApiPath(`/api/session/${uuid}/moves`)).toBe(
      '/api/session/{session_id}/moves',
    )
  })

  it('maps the dedicated eval-repair route to the exact backend template', () => {
    expect(normalizeApiPath(`/api/session/${uuid}/moves/eval-repair`)).toBe(
      '/api/session/{session_id}/moves/eval-repair',
    )
  })

  it('maps the analysis-evidence route to the exact template (not {id} fallback)', () => {
    expect(normalizeApiPath(`/api/session/${uuid}/analysis-evidence`)).toBe(
      '/api/session/{session_id}/analysis-evidence',
    )
  })

  it('still maps the sibling session analysis route to its own template', () => {
    expect(normalizeApiPath(`/api/session/${uuid}/analysis`)).toBe(
      '/api/session/{session_id}/analysis',
    )
  })

  it('maps the opening score-delta poll route to the backend template', () => {
    expect(normalizeApiPath(`/api/openings/score-delta/${uuid}`)).toBe(
      '/api/openings/score-delta/{session_id}',
    )
  })

  it('does not mislabel a static openings sibling like /api/openings/tree', () => {
    expect(normalizeApiPath('/api/openings/tree/status')).toBe(
      '/api/openings/tree/status',
    )
  })

  it('does not mislabel a static sibling like /api/drills/start', () => {
    expect(normalizeApiPath('/api/drills/start')).toBe('/api/drills/start')
  })

  it('falls back to {id} for unknown UUID/numeric segments', () => {
    expect(normalizeApiPath('/api/blunder/12345')).toBe('/api/blunder/{id}')
    expect(normalizeApiPath(`/api/widgets/${uuid}/edit`)).toBe('/api/widgets/{id}/edit')
  })

  it('strips the query string', () => {
    expect(normalizeApiPath('/api/stats/rating-history?range=7d')).toBe(
      '/api/stats/rating-history',
    )
  })

  it('strips the origin/base URL down to the pathname', () => {
    expect(
      normalizeApiPath('https://api.example.com/api/openings/tree?player_color=white'),
    ).toBe('/api/openings/tree')
  })

  it('leaves a template-free relative path unchanged', () => {
    expect(normalizeApiPath('/api/game/start')).toBe('/api/game/start')
  })
})

describe('requestJson client timing capture', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    captureMock.mockClear()
    mockStore = {}
  })

  it('emits exactly one success event per logical request', async () => {
    okResponse({ session_id: 'sess-1', engine_elo: 1500, player_color: 'white' })

    await startGame()

    const props = onlyClientEvent()
    expect(props).toMatchObject({
      route: '/api/game/start',
      method: 'POST',
      status: 200,
      ok: true,
      attempts: 1,
      error_kind: null,
      request_id: 'req-ok',
    })
    expect(props.duration_ms).toEqual(expect.any(Number))
  })

  it('emits one http-error event on a non-retryable failure', async () => {
    errorResponse({ detail: 'boom' }, 500, 'Internal Server Error')

    await expect(startGame()).rejects.toThrow()

    expect(onlyClientEvent()).toMatchObject({
      route: '/api/game/start',
      status: 500,
      ok: false,
      attempts: 1,
      error_kind: 'http',
      request_id: 'req-err',
    })
  })

  it('emits a single final event (not per attempt) across retries', async () => {
    errorResponse(
      { error: { code: 'internal_error', message: 'down', retryable: true } },
      503,
      'Service Unavailable',
    )
    okResponse({
      mode: 'ghost',
      move: { uci: 'e7e5', san: 'e5' },
      target_blunder_id: null,
      decision_source: 'ghost_path',
    })

    await getNextOpponentMove('sess-1', 'fen')

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(onlyClientEvent()).toMatchObject({
      route: '/api/game/next-opponent-move',
      status: 200,
      ok: true,
      attempts: 2,
      error_kind: null,
    })
  })

  it('reports a parse failure as error_kind "parse", not a success', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: 'OK',
      headers: headersWith('req-parse'),
      json: () => Promise.reject(new SyntaxError('Unexpected token')),
    })

    await expect(startGame()).rejects.toThrow(SyntaxError)

    expect(onlyClientEvent()).toMatchObject({
      status: 200,
      ok: false,
      attempts: 1,
      error_kind: 'parse',
      request_id: 'req-parse',
    })
  })

  it('reports a network failure with status 0, error_kind "network", null request_id', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))

    await expect(startGame()).rejects.toThrow(TypeError)

    expect(onlyClientEvent()).toMatchObject({
      route: '/api/game/start',
      status: 0,
      ok: false,
      attempts: 1,
      error_kind: 'network',
      request_id: null,
    })
  })

  it('classifies an aborted-by-deadline failure as error_kind "timeout"', async () => {
    const timeout = new DOMException('The operation timed out', 'TimeoutError')
    fetchMock.mockRejectedValueOnce(timeout)

    await expect(startGame()).rejects.toThrow()

    expect(onlyClientEvent()).toMatchObject({
      status: 0,
      ok: false,
      error_kind: 'timeout',
    })
  })

  it('does not emit for a deliberately aborted (superseded) request', async () => {
    fetchMock.mockRejectedValueOnce(
      new DOMException('The user aborted a request.', 'AbortError'),
    )

    await expect(startGame()).rejects.toThrow()

    const clientEvents = captureMock.mock.calls.filter(
      ([event]) => event === 'api_request_client',
    )
    expect(clientEvents).toHaveLength(0)
  })
})

describe('uploadSessionMoves client correlation + upload telemetry', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    captureMock.mockClear()
    mockStore = {}
  })

  const sampleMove: SessionMoveUpload = {
    move_number: 1,
    color: 'white',
    move_san: 'e4',
    fen_after: 'fen-1',
    eval_cp: 20,
    eval_mate: null,
    best_move_san: 'e4',
    best_move_eval_cp: 20,
    eval_delta: 0,
    classification: 'best',
    fen_before: 'fen-0',
    move_uci: 'e2e4',
    best_move_uci: 'e2e4',
    decision_source: null,
    target_blunder_id: null,
  }

  const uploadOk = (movesInserted = 1) =>
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: 'OK',
      headers: headersWith('req-upload'),
      json: () => Promise.resolve({ moves_inserted: movesInserted }),
    })

  it('carries upload_kind + client_request_id on a successful final_full upload, and sends the matching X-Client-Request-ID header', async () => {
    uploadOk()

    await uploadSessionMoves('sess-1', [sampleMove], {
      uploadKind: 'final_full',
      terminalAction: 'game_end',
      deadlineMs: 4000,
    })

    const props = onlyClientEvent()
    expect(props.upload_kind).toBe('final_full')
    expect(props.terminal_action).toBe('game_end')
    expect(props.deadline_ms).toBe(4000)
    expect(props.move_count).toBe(1)
    expect(props.client_request_id).toEqual(expect.any(String))

    // The header the server keys its receipt on == the event's client id.
    const headers = (fetchMock.mock.calls[0][1] as RequestInit).headers as Record<
      string,
      string
    >
    expect(headers['X-Client-Request-ID']).toBe(props.client_request_id)
  })

  it('carries client_request_id + upload_kind on a TIMEOUT with no response (the load-bearing branch)', async () => {
    fetchMock.mockRejectedValueOnce(
      new DOMException('The operation timed out', 'TimeoutError'),
    )

    await expect(
      uploadSessionMoves('sess-1', [sampleMove], {
        uploadKind: 'final_full',
        terminalAction: 'resign',
        deadlineMs: 1234,
      }),
    ).rejects.toThrow()

    const props = onlyClientEvent()
    expect(props.error_kind).toBe('timeout')
    expect(props.upload_kind).toBe('final_full')
    expect(props.terminal_action).toBe('resign')
    expect(props.deadline_ms).toBe(1234)
    // No response ⇒ no server id, but the client id is still present to join on.
    expect(props.request_id).toBeNull()
    expect(props.client_request_id).toEqual(expect.any(String))
  })

  it('carries upload telemetry on an http-error upload', async () => {
    errorResponse({ detail: 'bad' }, 422, 'Unprocessable Entity')

    await expect(
      uploadSessionMoves('sess-1', [sampleMove], { uploadKind: 'incremental' }),
    ).rejects.toThrow()

    const props = onlyClientEvent()
    expect(props.error_kind).toBe('http')
    expect(props.upload_kind).toBe('incremental')
    expect(props.terminal_action).toBeNull()
    expect(props.client_request_id).toEqual(expect.any(String))
  })

  it('carries upload telemetry on a parse-error upload', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: 'OK',
      headers: headersWith('req-parse'),
      json: () => Promise.reject(new SyntaxError('Unexpected token')),
    })

    await expect(
      uploadSessionMoves('sess-1', [sampleMove], { uploadKind: 'revert' }),
    ).rejects.toThrow(SyntaxError)

    const props = onlyClientEvent()
    expect(props.error_kind).toBe('parse')
    expect(props.upload_kind).toBe('revert')
    expect(props.client_request_id).toEqual(expect.any(String))
  })

  it('isolates a successful late evaluation repair from final_full receipts', async () => {
    uploadOk()

    await uploadSessionMoves('sess-1', [sampleMove], {
      uploadKind: 'late_eval_repair',
      finalClientRequestId: 'final-request-123',
      recomputeOpportunity: false,
    })

    const props = onlyClientEvent()
    expect(props.upload_kind).toBe('late_eval_repair')
    expect(props.terminal_action).toBeNull()
    expect(props.deadline_ms).toBeNull()
  })

  it('records payload_bytes as the transmitted UTF-8 byte length (TextEncoder), not JSON.stringify length', async () => {
    uploadOk()

    // A multi-byte char makes UTF-8 byte length differ from UTF-16 code units.
    const multiByteMove: SessionMoveUpload = {
      ...sampleMove,
      best_move_san: 'e4♞', // '♞' is 3 UTF-8 bytes / 1 code unit
    }

    await uploadSessionMoves('sess-1', [multiByteMove], { uploadKind: 'incremental' })

    const body = (fetchMock.mock.calls[0][1] as RequestInit).body as string
    const utf8Bytes = new TextEncoder().encode(body).byteLength
    // Sanity: the multi-byte char really makes the two measures diverge.
    expect(utf8Bytes).toBeGreaterThan(body.length)

    const props = onlyClientEvent()
    expect(props.payload_bytes).toBe(utf8Bytes)
    expect(props.payload_bytes).not.toBe(body.length)
  })
})

describe('deadline expiring while the response BODY streams', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    captureMock.mockClear()
    mockStore = {}
  })

  /**
   * fetch() resolves as soon as headers arrive, so a body still streaming when
   * the deadline fires rejects at `response.json()`, NOT at `fetch()`. These
   * overruns must land in the timeout cohort — counting them as `parse` would
   * hide exactly the late-deadline population g-upload-observe measures.
   */
  const respondThenStallBody = (
    rejectWith?: unknown,
    response: { ok: boolean; status: number; statusText: string } = {
      ok: true,
      status: 200,
      statusText: 'OK',
    },
  ) =>
    fetchMock.mockImplementationOnce((_url: string, init: RequestInit) => {
      const signal = init.signal as AbortSignal
      return Promise.resolve({
        ...response,
        headers: headersWith('req-late-body'),
        json: () =>
          new Promise((_resolve, reject) => {
            signal.addEventListener('abort', () =>
              reject(rejectWith ?? signal.reason),
            )
          }),
      })
    })

  const errorStatus = { ok: false, status: 503, statusText: 'Service Unavailable' }

  it('classifies a body read aborted by the deadline as timeout, not parse', async () => {
    respondThenStallBody()

    await expect(
      uploadSessionMoves('sess-1', [], {
        uploadKind: 'final_full',
        terminalAction: 'game_end',
        deadlineMs: 5,
      }),
    ).rejects.toThrow()

    const props = onlyClientEvent()
    expect(props.error_kind).toBe('timeout')
    // Headers DID arrive: the status and server request id are real, and are the
    // strongest client-side evidence that the server answered (likely committed).
    expect(props.status).toBe(200)
    expect(props.request_id).toBe('req-late-body')
    expect(props.upload_kind).toBe('final_full')
    expect(props.client_request_id).toEqual(expect.any(String))
  })

  it('still reports timeout when the body stream surfaces a bare AbortError', async () => {
    // Some runtimes error the body stream with a generic AbortError even though
    // the signal timed out; `signal.reason` is the authority.
    respondThenStallBody(new DOMException('aborted', 'AbortError'))

    await expect(
      uploadSessionMoves('sess-1', [], {
        uploadKind: 'final_full',
        terminalAction: 'resign',
        deadlineMs: 5,
      }),
    ).rejects.toThrow()

    expect(onlyClientEvent().error_kind).toBe('timeout')
  })

  it('classifies a NON-2xx whose error body stalls past the deadline as timeout, not http', async () => {
    // The error-envelope read tolerates malformed JSON but must not swallow an
    // abort: the status is not the outcome when the deadline is what ended the
    // request, and reporting `http` would hide it from the timeout cohort.
    respondThenStallBody(undefined, errorStatus)

    await expect(
      uploadSessionMoves('sess-1', [], {
        uploadKind: 'final_full',
        terminalAction: 'accuracy_fail',
        deadlineMs: 5,
      }),
    ).rejects.toThrow()

    const props = onlyClientEvent()
    expect(props.error_kind).toBe('timeout')
    expect(props.status).toBe(503)
    expect(props.request_id).toBe('req-late-body')
    expect(props.client_request_id).toEqual(expect.any(String))
  })

  it('does not report a NON-2xx error body cut short by a deliberate cancellation', async () => {
    const controller = new AbortController()
    respondThenStallBody(undefined, errorStatus)
    setTimeout(() => controller.abort(), 5)

    await expect(
      uploadSessionMoves('sess-1', [], {
        uploadKind: 'incremental',
        signal: controller.signal,
      }),
    ).rejects.toThrow()

    expect(
      captureMock.mock.calls.filter(([event]) => event === 'api_request_client'),
    ).toHaveLength(0)
  })

  it('still reports http when a NON-2xx body is merely unparseable', async () => {
    // Guards the rethrow rule from over-firing: only aborts escape the envelope
    // parser — a 500 with a malformed body is an ordinary `http` outcome.
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      headers: headersWith('req-bad-body'),
      json: () => Promise.reject(new SyntaxError('Unexpected token')),
    })

    await expect(
      uploadSessionMoves('sess-1', [], { uploadKind: 'revert' }),
    ).rejects.toThrow()

    expect(onlyClientEvent().error_kind).toBe('http')
  })

  it('does not report a body read cut short by a deliberate cancellation', async () => {
    const controller = new AbortController()
    respondThenStallBody()
    setTimeout(() => controller.abort(), 5)

    await expect(
      uploadSessionMoves('sess-1', [], {
        uploadKind: 'incremental',
        signal: controller.signal,
      }),
    ).rejects.toThrow()

    // A superseded request is not a round-trip outcome — same rule the
    // network-level branch applies.
    expect(
      captureMock.mock.calls.filter(([event]) => event === 'api_request_client'),
    ).toHaveLength(0)
  })
})
