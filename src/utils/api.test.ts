import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  ApiError,
  errorCodeOf,
  resolveApiBaseUrl,
  resolveApiEndpointBaseUrl,
  startGame,
  endGame,
  uploadSessionMoves,
  recordBlunder,
  recordManualBlunder,
  fetchBlunders,
  getNextOpponentMove,
  reviewSrsBlunder,
  getOpeningFamilyScores,
  getStatsSummary,
  getStatsAchievements,
  submitAnalysisEvidence,
  fetchSessionOpenings,
  checkDrillRoute,
} from './api'

const fetchMock = vi.fn()
vi.stubGlobal('fetch', fetchMock)

// Mock localStorage (jsdom's built-in one may not be available)
let mockStore: Record<string, string> = {}
const localStorageMock = {
  getItem: (key: string) => mockStore[key] ?? null,
  setItem: (key: string, value: string) => { mockStore[key] = value },
  removeItem: (key: string) => { delete mockStore[key] },
  clear: () => { mockStore = {} },
  length: 0,
  key: () => null,
}
Object.defineProperty(window, 'localStorage', { value: localStorageMock, writable: true })

const mockResponse = (
  data: Record<string, unknown>,
  ok = true,
  statusText = 'OK',
  status = ok ? 200 : 500,
) => {
  fetchMock.mockResolvedValueOnce({
    ok,
    status,
    statusText,
    json: () => Promise.resolve(data),
  })
}

describe('resolveApiBaseUrl', () => {
  it('uses localhost backend by default during local development', () => {
    expect(
      resolveApiBaseUrl(undefined, {
        hostname: 'localhost',
        origin: 'http://localhost:5173',
      }),
    ).toBe('http://localhost:8000')
  })

  it('uses same-origin api path by default on deployed frontend origins', () => {
    expect(
      resolveApiBaseUrl(undefined, {
        hostname: 'ghostreplay.vercel.app',
        origin: 'https://ghostreplay.vercel.app',
      }),
    ).toBe('/api')
  })

  it('rewrites absolute cross-origin config to same-origin api path on Vercel', () => {
    expect(
      resolveApiBaseUrl('https://ghostreplay-production.up.railway.app', {
        hostname: 'ghostreplay.vercel.app',
        origin: 'https://ghostreplay.vercel.app',
      }),
    ).toBe('/api')
  })

  it('keeps explicit same-origin-relative config intact', () => {
    expect(
      resolveApiBaseUrl('/api', {
        hostname: 'ghostreplay.vercel.app',
        origin: 'https://ghostreplay.vercel.app',
      }),
    ).toBe('/api')
  })
})

describe('resolveApiEndpointBaseUrl', () => {
  it('strips same-origin api path before endpoint paths are appended', () => {
    expect(
      resolveApiEndpointBaseUrl(undefined, {
        hostname: 'ghostreplay.vercel.app',
        origin: 'https://ghostreplay.vercel.app',
      }),
    ).toBe('')
  })

  it('strips explicit api path configs with optional trailing slash', () => {
    expect(
      resolveApiEndpointBaseUrl('/api/', {
        hostname: 'ghostreplay.vercel.app',
        origin: 'https://ghostreplay.vercel.app',
      }),
    ).toBe('')
    expect(
      resolveApiEndpointBaseUrl('https://api.example.com/api/', {
        hostname: 'localhost',
        origin: 'http://localhost:5173',
      }),
    ).toBe('https://api.example.com')
  })

  it('keeps local backend origin before endpoint paths are appended', () => {
    expect(
      resolveApiEndpointBaseUrl(undefined, {
        hostname: 'localhost',
        origin: 'http://localhost:5173',
      }),
    ).toBe('http://localhost:8000')
  })
})

describe('startGame', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  afterEach(() => {
    mockStore = {}
  })

  it('sends correct request body', async () => {
    mockResponse({ session_id: 'sess-1', engine_elo: 1500, player_color: 'white' })

    await startGame(1500, 'white')

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/game/start'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ engine_elo: 1500, player_color: 'white' }),
      }),
    )
  })

  it('uses default values', async () => {
    mockResponse({ session_id: 'sess-1', engine_elo: 1500, player_color: 'white' })

    await startGame()

    expect(fetchMock).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        body: JSON.stringify({ engine_elo: 1500, player_color: 'white' }),
      }),
    )
  })

  it('returns parsed response', async () => {
    const expected = { session_id: 'sess-1', engine_elo: 1500, player_color: 'black' }
    mockResponse(expected)

    const result = await startGame(1500, 'black')

    expect(result).toEqual(expected)
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Internal Server Error', 500)

    await expect(startGame()).rejects.toThrow(
      'Failed to start game: Internal Server Error',
    )
  })

  it('includes JWT token in headers when available', async () => {
    localStorage.setItem('ghost_replay_token', 'test-jwt-token')
    mockResponse({ session_id: 'sess-1', engine_elo: 1500 })

    await startGame()

    expect(fetchMock).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer test-jwt-token',
          'Content-Type': 'application/json',
        }),
      }),
    )
  })

  it('omits Authorization header when no token', async () => {
    mockResponse({ session_id: 'sess-1', engine_elo: 1500 })

    await startGame()

    const [, options] = fetchMock.mock.calls[0]
    expect(options.headers).not.toHaveProperty('Authorization')
    expect(options.headers['Content-Type']).toBe('application/json')
  })
})

describe('endGame', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('sends correct request body', async () => {
    mockResponse({ session_id: 'sess-1', blunders_recorded: 1, blunders_reviewed: 0 })

    await endGame('sess-1', 'checkmate_win', '1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#')

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/game/end'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          session_id: 'sess-1',
          result: 'checkmate_win',
          pgn: '1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#',
          is_rated: true,
        }),
      }),
    )
  })

  it('returns parsed response', async () => {
    const expected = { session_id: 'sess-1', blunders_recorded: 1, blunders_reviewed: 0 }
    mockResponse(expected)

    const result = await endGame('sess-1', 'resign', '1. e4')

    expect(result).toEqual(expected)
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Not Found', 404)

    await expect(endGame('sess-1', 'resign', '')).rejects.toThrow(
      'Failed to end game: Not Found',
    )
  })

  it('handles all result types', async () => {
    const results = ['checkmate_win', 'checkmate_loss', 'resign', 'draw', 'abandon'] as const

    for (const result of results) {
      fetchMock.mockReset()
      mockResponse({ session_id: 'sess-1', blunders_recorded: 0, blunders_reviewed: 0 })

      await endGame('sess-1', result, '1. e4')

      const body = JSON.parse(fetchMock.mock.calls[0][1].body)
      expect(body.result).toBe(result)
    }
  })
})

describe('uploadSessionMoves', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  const sampleMove = {
    move_number: 1,
    color: 'white' as const,
    move_san: 'e4',
    fen_after: 'fen-1',
    eval_cp: 20,
    eval_mate: null,
    best_move_san: 'e4',
    best_move_eval_cp: 20,
    eval_delta: 0,
    classification: 'best' as const,
    fen_before: 'fen-0',
    move_uci: 'e2e4',
    best_move_uci: 'e2e4',
    decision_source: null,
    target_blunder_id: null,
  }

  it('sends POST request to the session moves endpoint', async () => {
    mockResponse({ moves_inserted: 1 })

    await uploadSessionMoves('sess-1', [sampleMove], { uploadKind: 'incremental' })

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/session/sess-1/moves'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ moves: [sampleMove] }),
      }),
    )
  })

  it('returns parsed response', async () => {
    const expected = { moves_inserted: 4 }
    mockResponse(expected)

    const result = await uploadSessionMoves('sess-1', [], { uploadKind: 'incremental' })

    expect(result).toEqual(expected)
  })

  it('forwards the external cancellation signal for an incremental upload', async () => {
    mockResponse({ moves_inserted: 0 })
    const controller = new AbortController()

    await uploadSessionMoves('sess-1', [], {
      uploadKind: 'incremental',
      signal: controller.signal,
    })

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/session/sess-1/moves'),
      expect.objectContaining({ signal: controller.signal }),
    )
  })

  it('sends a cancellable late repair without terminal receipt fields', async () => {
    mockResponse({ moves_inserted: 1 })
    const controller = new AbortController()

    await uploadSessionMoves('sess-1', [sampleMove], {
      uploadKind: 'late_eval_repair',
      finalClientRequestId: 'final-request-123',
      signal: controller.signal,
      recomputeOpportunity: false,
    })

    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(fetchMock.mock.calls[0][0]).toEqual(
      expect.stringContaining('/api/session/sess-1/moves/eval-repair'),
    )
    const body = JSON.parse(init.body as string)
    expect(init.signal).toBe(controller.signal)
    expect(body.recompute_opportunity).toBe(false)
    expect(body.eval_repair).toBe(true)
    expect(body.final_client_request_id).toBe('final-request-123')
    expect('terminal_action' in body).toBe(false)
  })

  it('constructs the timeout from deadlineMs for a final_full upload', async () => {
    mockResponse({ moves_inserted: 1 })

    await uploadSessionMoves('sess-1', [sampleMove], {
      uploadKind: 'final_full',
      terminalAction: 'game_end',
      deadlineMs: 4000,
    })

    const init = fetchMock.mock.calls[0][1] as RequestInit
    // The union constructs its own AbortSignal.timeout(deadlineMs) internally.
    expect(init.signal).toBeInstanceOf(AbortSignal)
  })

  it('uses a caller-bound client id for the final receipt', async () => {
    mockResponse({ moves_inserted: 1 })
    const clientRequestId = '11111111-1111-4111-8111-111111111111'

    await uploadSessionMoves('sess-1', [sampleMove], {
      uploadKind: 'final_full',
      terminalAction: 'game_end',
      deadlineMs: 4000,
      clientRequestId,
    })

    const headers = (fetchMock.mock.calls[0][1] as RequestInit)
      .headers as Record<string, string>
    expect(headers['X-Client-Request-ID']).toBe(clientRequestId)
  })

  it('sends terminal_action in the body ONLY for a final_full upload', async () => {
    mockResponse({ moves_inserted: 1 })
    await uploadSessionMoves('sess-1', [sampleMove], {
      uploadKind: 'final_full',
      terminalAction: 'resign',
      deadlineMs: 4000,
    })
    const finalBody = JSON.parse(fetchMock.mock.calls[0][1].body as string)
    expect(finalBody.terminal_action).toBe('resign')

    fetchMock.mockReset()
    mockResponse({ moves_inserted: 0 })
    await uploadSessionMoves('sess-1', [], { uploadKind: 'revert' })
    const revertBody = JSON.parse(fetchMock.mock.calls[0][1].body as string)
    expect('terminal_action' in revertBody).toBe(false)

    fetchMock.mockReset()
    mockResponse({ moves_inserted: 0 })
    await uploadSessionMoves('sess-1', [], { uploadKind: 'incremental' })
    const incBody = JSON.parse(fetchMock.mock.calls[0][1].body as string)
    expect('terminal_action' in incBody).toBe(false)
  })

  it('sends the X-Client-Request-ID header keyed to a client-generated id', async () => {
    mockResponse({ moves_inserted: 0 })

    await uploadSessionMoves('sess-1', [], { uploadKind: 'incremental' })

    const headers = (fetchMock.mock.calls[0][1] as RequestInit).headers as Record<
      string,
      string
    >
    expect(headers['X-Client-Request-ID']).toEqual(expect.any(String))
    expect(headers['X-Client-Request-ID']).toMatch(
      /^[0-9a-f-]{36}$/i,
    )
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Unprocessable Entity', 422)

    await expect(
      uploadSessionMoves('sess-1', [], { uploadKind: 'incremental' }),
    ).rejects.toThrow('Failed to upload session moves: Unprocessable Entity')
  })

  it('omits recompute_opportunity from the body when not specified', async () => {
    mockResponse({ moves_inserted: 0 })

    await uploadSessionMoves('sess-1', [], { uploadKind: 'incremental' })

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string)
    expect(body).toEqual({ moves: [] })
    expect('recompute_opportunity' in body).toBe(false)
  })

  it('includes recompute_opportunity: false when opted out (incremental upload)', async () => {
    mockResponse({ moves_inserted: 0 })

    await uploadSessionMoves('sess-1', [], {
      uploadKind: 'incremental',
      recomputeOpportunity: false,
    })

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string)
    expect(body.recompute_opportunity).toBe(false)
  })

  it('includes recompute_opportunity: true when flagged (final upload)', async () => {
    mockResponse({ moves_inserted: 0 })

    await uploadSessionMoves('sess-1', [sampleMove], {
      uploadKind: 'final_full',
      terminalAction: 'game_end',
      deadlineMs: 4000,
      recomputeOpportunity: true,
    })

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string)
    expect(body.recompute_opportunity).toBe(true)
  })

  // Type-level contract (enforced by `tsc -b` in the build): the discriminated
  // union rejects mixing the wrong fields across upload kinds. Never executed.
  it('enforces the discriminated-union option shape at compile time', () => {
    const _typeContracts = async () => {
      // @ts-expect-error final_full REQUIRES terminalAction + deadlineMs
      await uploadSessionMoves('s', [], { uploadKind: 'final_full' })
      await uploadSessionMoves('s', [], {
        uploadKind: 'incremental',
        // @ts-expect-error incremental cannot carry terminalAction
        terminalAction: 'game_end',
      })
      await uploadSessionMoves('s', [], {
        uploadKind: 'revert',
        // @ts-expect-error revert cannot carry deadlineMs
        deadlineMs: 4000,
      })
      // @ts-expect-error repair uploads cannot trigger opportunity recompute
      await uploadSessionMoves('s', [], {
        uploadKind: 'late_eval_repair',
        finalClientRequestId: 'final-request-123',
        recomputeOpportunity: true,
      })
      // @ts-expect-error uploadKind is required
      await uploadSessionMoves('s', [], {})
    }
    expect(typeof _typeContracts).toBe('function')
  })
})

describe('recordBlunder', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('sends correct snake_case request body', async () => {
    mockResponse({ blunder_id: 1, position_id: 10, positions_created: 3, is_new: true })

    await recordBlunder(
      'sess-1',
      '1. e4 d5 2. Bb5+',
      'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1',
      'Bb5+',
      'd2d4',
      50,
      -150,
    )

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/blunder'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          session_id: 'sess-1',
          pgn: '1. e4 d5 2. Bb5+',
          fen: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1',
          user_move: 'Bb5+',
          best_move: 'd2d4',
          eval_before: 50,
          eval_after: -150,
        }),
      }),
    )
  })

  it('includes idempotency_key in the body when provided', async () => {
    mockResponse({ blunder_id: 1, position_id: 10, positions_created: 3, is_new: true })

    await recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100, 'rec-key-1')

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/blunder'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          session_id: 'sess-1',
          pgn: '1. e4',
          fen: 'fen',
          user_move: 'e4',
          best_move: 'd4',
          eval_before: 50,
          eval_after: -100,
          idempotency_key: 'rec-key-1',
        }),
      }),
    )
  })

  it('returns parsed response', async () => {
    const expected = { blunder_id: 1, position_id: 10, positions_created: 3, is_new: true }
    mockResponse(expected)

    const result = await recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100)

    expect(result).toEqual(expected)
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Unprocessable Entity', 422)

    await expect(
      recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100),
    ).rejects.toThrow('Failed to record blunder: Unprocessable Entity')
  })

  it('sends auth headers', async () => {
    localStorage.setItem('ghost_replay_token', 'jwt-123')
    mockResponse({ blunder_id: 1, position_id: 1, positions_created: 1, is_new: true })

    await recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100)

    const [, options] = fetchMock.mock.calls[0]
    expect(options.headers.Authorization).toBe('Bearer jwt-123')
  })
})

describe('fetchBlunders', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('requests paginated blunders and returns the response envelope', async () => {
    const expected = {
      items: [],
      total: 12,
      due_total: null,
      limit: 50,
      offset: 0,
      due: false,
    }
    mockResponse(expected)

    const result = await fetchBlunders({ limit: 50, offset: 0 })

    expect(result).toEqual(expected)
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/blunder?limit=50&offset=0'),
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('sends due, limit, and offset query params', async () => {
    mockResponse({
      items: [],
      total: 2,
      due_total: 2,
      limit: 25,
      offset: 50,
      due: true,
    })

    await fetchBlunders({ due: true, limit: 25, offset: 50 })

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/blunder?due=true&limit=25&offset=50'),
      expect.any(Object),
    )
  })
})

describe('recordManualBlunder', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('sends correct request body to manual endpoint', async () => {
    mockResponse({ blunder_id: 1, position_id: 10, positions_created: 3, is_new: true })

    await recordManualBlunder(
      'sess-1',
      '1. e4',
      'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
      'e4',
      null,
      null,
      null,
    )

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/blunder/manual'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          session_id: 'sess-1',
          pgn: '1. e4',
          fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
          user_move: 'e4',
          best_move: null,
          eval_before: null,
          eval_after: null,
        }),
      }),
    )
  })

  it('returns parsed response', async () => {
    const expected = { blunder_id: 1, position_id: 10, positions_created: 3, is_new: false }
    mockResponse(expected)

    const result = await recordManualBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 20, 5)

    expect(result).toEqual(expected)
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Unauthorized', 401)

    await expect(
      recordManualBlunder('sess-1', '1. e4', 'fen', 'e4', null, null, null),
    ).rejects.toThrow('Failed to add move to ghost library: Unauthorized')
  })
})

describe('getNextOpponentMove', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('sends POST request with JSON body', async () => {
    mockResponse({
      mode: 'ghost',
      move: { uci: 'e7e5', san: 'e5' },
      target_blunder_id: 42,
      decision_source: 'ghost_path',
    })

    const fen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
    await getNextOpponentMove('sess-1', fen, ['e2e4', 'e7e5'])

    const [url, options] = fetchMock.mock.calls[0]
    expect(url).toContain('/api/game/next-opponent-move')
    expect(options.method).toBe('POST')
    expect(options.body).toBe(JSON.stringify({ session_id: 'sess-1', fen, moves: ['e2e4', 'e7e5'] }))
  })

  it('defaults to empty moves array when not provided', async () => {
    mockResponse({
      mode: 'engine',
      move: { uci: 'e7e5', san: 'e5' },
      target_blunder_id: null,
      decision_source: 'backend_engine',
    })

    await getNextOpponentMove('sess-1', 'some-fen')

    const [, options] = fetchMock.mock.calls[0]
    const body = JSON.parse(options.body)
    expect(body.moves).toEqual([])
  })

  it('returns ghost mode response', async () => {
    const expected = {
      mode: 'ghost',
      move: { uci: 'g8f6', san: 'Nf6' },
      target_blunder_id: 7,
      decision_source: 'ghost_path',
    }
    mockResponse(expected)

    const result = await getNextOpponentMove('sess-1', 'some-fen')

    expect(result).toEqual(expected)
  })

  it('returns engine mode response', async () => {
    const expected = {
      mode: 'engine',
      move: { uci: 'e7e5', san: 'e5' },
      target_blunder_id: null,
      decision_source: 'backend_engine',
    }
    mockResponse(expected)

    const result = await getNextOpponentMove('sess-1', 'some-fen')

    expect(result).toEqual(expected)
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Forbidden', 403)

    await expect(getNextOpponentMove('sess-1', 'fen')).rejects.toThrow(
      'Failed to get opponent move: Forbidden',
    )
  })

  it('retries idempotent request on retryable server errors', async () => {
    mockResponse(
      {
        detail: 'Service unavailable',
        error: { code: 'internal_error', message: 'Internal server error', retryable: true },
      },
      false,
      'Service Unavailable',
      503,
    )
    mockResponse(
      {
        mode: 'ghost',
        move: { uci: 'e7e5', san: 'e5' },
        target_blunder_id: 11,
        decision_source: 'ghost_path',
      },
      true,
      'OK',
      200,
    )

    const result = await getNextOpponentMove('sess-1', 'fen')

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(result).toEqual({
      mode: 'ghost',
      move: { uci: 'e7e5', san: 'e5' },
      target_blunder_id: 11,
      decision_source: 'ghost_path',
    })
  })

  it('throws typed ApiError with normalized fields', async () => {
    mockResponse(
      {
        detail: 'Internal server error',
        error: {
          code: 'internal_error',
          message: 'Database unavailable',
          details: { service: 'postgres' },
          retryable: false,
        },
      },
      false,
      'Internal Server Error',
      503,
    )

    try {
      await getNextOpponentMove('sess-1', 'fen')
      throw new Error('Expected getNextOpponentMove to fail')
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError)
      const apiError = error as ApiError
      expect(apiError.status).toBe(503)
      expect(apiError.code).toBe('internal_error')
      expect(apiError.message).toBe('Database unavailable')
      expect(apiError.retryable).toBe(false)
      expect(apiError.details).toEqual({ service: 'postgres' })
    }
  })
})

describe('reviewSrsBlunder', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('sends correct review payload', async () => {
    mockResponse({
      blunder_id: 42,
      pass_streak: 3,
      priority: 1.25,
      next_expected_review: '2026-02-08T12:00:00Z',
    })

    await reviewSrsBlunder('sess-1', 42, false, 'Qh5', 50)

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/srs/review'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          session_id: 'sess-1',
          blunder_id: 42,
          passed: false,
          user_move: 'Qh5',
          eval_delta: 50,
        }),
      }),
    )
  })

  it('includes idempotency_key in the body when provided', async () => {
    mockResponse({
      blunder_id: 42,
      pass_streak: 3,
      priority: 1.25,
      next_expected_review: '2026-02-08T12:00:00Z',
    })

    await reviewSrsBlunder('sess-1', 42, false, 'Qh5', 50, 'srs-key-1')

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/srs/review'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          session_id: 'sess-1',
          blunder_id: 42,
          passed: false,
          user_move: 'Qh5',
          eval_delta: 50,
          idempotency_key: 'srs-key-1',
        }),
      }),
    )
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Unauthorized', 401)

    await expect(
      reviewSrsBlunder('sess-1', 42, true, 'Nf3', 20),
    ).rejects.toThrow('Failed to record SRS review: Unauthorized')
  })
})

describe('ApiError classification, errorCodeOf, and Retry-After', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  const mockErrorResponse = (
    data: Record<string, unknown>,
    status: number,
    headers?: Record<string, string>,
  ) => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status,
      statusText: 'Error',
      json: () => Promise.resolve(data),
      headers: headers
        ? { get: (name: string) => headers[name] ?? headers[name.toLowerCase()] ?? null }
        : undefined,
    })
  }

  it('marks a 429 response as retryable', async () => {
    mockErrorResponse({ detail: 'slow down' }, 429)

    try {
      await recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100)
      throw new Error('expected recordBlunder to throw')
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError)
      expect((error as ApiError).retryable).toBe(true)
    }
  })

  it('reads IDEMPOTENCY_CONFLICT via errorCodeOf on a 409', async () => {
    mockErrorResponse(
      { error: { code: 'http_409', details: { error_code: 'IDEMPOTENCY_CONFLICT' } } },
      409,
    )

    try {
      await recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100)
      throw new Error('expected recordBlunder to throw')
    } catch (error) {
      expect(errorCodeOf(error)).toBe('IDEMPOTENCY_CONFLICT')
    }
  })

  it('reads LEGACY_AMBIGUOUS via errorCodeOf on a 409', async () => {
    mockErrorResponse(
      { error: { code: 'http_409', details: { error_code: 'LEGACY_AMBIGUOUS' } } },
      409,
    )

    try {
      await recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100)
      throw new Error('expected recordBlunder to throw')
    } catch (error) {
      expect(errorCodeOf(error)).toBe('LEGACY_AMBIGUOUS')
    }
  })

  it('returns undefined from errorCodeOf for non-ApiError and detail-less errors', () => {
    expect(errorCodeOf(new Error('boom'))).toBeUndefined()
    expect(errorCodeOf(new ApiError('x', { status: 500 }))).toBeUndefined()
  })

  it('parses Retry-After delta-seconds into milliseconds', async () => {
    mockErrorResponse({ detail: 'slow down' }, 429, { 'Retry-After': '120' })

    try {
      await recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100)
      throw new Error('expected recordBlunder to throw')
    } catch (error) {
      expect((error as ApiError).retryAfterMs).toBe(120000)
    }
  })

  it('parses Retry-After HTTP-date into a clamped non-negative delay', async () => {
    const future = new Date(Date.now() + 60_000).toUTCString()
    mockErrorResponse({ detail: 'slow down' }, 429, { 'Retry-After': future })

    try {
      await recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100)
      throw new Error('expected recordBlunder to throw')
    } catch (error) {
      const { retryAfterMs } = error as ApiError
      expect(retryAfterMs).toBeGreaterThanOrEqual(0)
      expect(retryAfterMs).toBeLessThanOrEqual(60_000)
    }
  })

  it('parses a past Retry-After HTTP-date as 0 (clamped)', async () => {
    const past = new Date(Date.now() - 60_000).toUTCString()
    mockErrorResponse({ detail: 'slow down' }, 429, { 'Retry-After': past })

    try {
      await recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100)
      throw new Error('expected recordBlunder to throw')
    } catch (error) {
      expect((error as ApiError).retryAfterMs).toBe(0)
    }
  })

  it('leaves retryAfterMs undefined when header is absent or unparseable', async () => {
    mockErrorResponse({ detail: 'nope' }, 429)
    try {
      await recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100)
      throw new Error('expected recordBlunder to throw')
    } catch (error) {
      expect((error as ApiError).retryAfterMs).toBeUndefined()
    }

    mockErrorResponse({ detail: 'nope' }, 429, { 'Retry-After': 'not-a-date' })
    try {
      await recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100)
      throw new Error('expected recordBlunder to throw')
    } catch (error) {
      expect((error as ApiError).retryAfterMs).toBeUndefined()
    }
  })
})

describe('getStatsSummary', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('sends default window query parameter', async () => {
    mockResponse({
      window_days: 30,
      generated_at: '2026-01-01T00:00:00Z',
      games: {
        played: 0,
        score_pct: null,
        wins: 0,
        losses: 0,
        draws: 0,
        avg_moves: 0,
      },
      moves: {
        accuracy_pct: null,
        mistake_free_game_rate: null,
        quality_distribution: null,
      },
      colors: {
        white: { games: 0, score_pct: null, accuracy_pct: null },
        black: { games: 0, score_pct: null, accuracy_pct: null },
      },
      training: {
        retention_pct: null,
        reviewed_blunders: 0,
        retained_blunders: 0,
        review_pass_rate: null,
        reviews_total: 0,
        reviews_passed: 0,
        conversions_in_window: 0,
        mastery_threshold: 3,
      },
      library: {
        blunders_total: 0,
        new_blunders_in_window: 0,
        avg_blunder_eval_loss_cp: 0,
        top_costly_blunders: [],
      },
      openings: { strongest: [], weakest: [] },
    })

    await getStatsSummary()

    const [url, options] = fetchMock.mock.calls[0]
    expect(options.method).toBe('GET')
    expect(url).toContain('/api/stats/summary')
    expect(url).toContain('window_days=30')
  })

  it('sends provided window query parameter', async () => {
    mockResponse({
      window_days: 90,
      generated_at: '2026-01-01T00:00:00Z',
      games: {
        played: 0,
        score_pct: null,
        wins: 0,
        losses: 0,
        draws: 0,
        avg_moves: 0,
      },
      moves: {
        accuracy_pct: null,
        mistake_free_game_rate: null,
        quality_distribution: null,
      },
      colors: {
        white: { games: 0, score_pct: null, accuracy_pct: null },
        black: { games: 0, score_pct: null, accuracy_pct: null },
      },
      training: {
        retention_pct: null,
        reviewed_blunders: 0,
        retained_blunders: 0,
        review_pass_rate: null,
        reviews_total: 0,
        reviews_passed: 0,
        conversions_in_window: 0,
        mastery_threshold: 3,
      },
      library: {
        blunders_total: 0,
        new_blunders_in_window: 0,
        avg_blunder_eval_loss_cp: 0,
        top_costly_blunders: [],
      },
      openings: { strongest: [], weakest: [] },
    })

    await getStatsSummary(90)

    const [url] = fetchMock.mock.calls[0]
    expect(url).toContain('window_days=90')
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Bad Request', 400)

    await expect(getStatsSummary(30)).rejects.toThrow(
      'Failed to load stats summary: Bad Request',
    )
  })
})

describe('getStatsAchievements', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('requests the achievements endpoint', async () => {
    mockResponse({
      perfect_streak: { personal_best: 7 },
    })

    await getStatsAchievements()

    const [url, options] = fetchMock.mock.calls[0]
    expect(options.method).toBe('GET')
    expect(url).toContain('/api/stats/achievements')
  })
})

describe('getOpeningFamilyScores', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('requests the family score endpoint with player color query', async () => {
    mockResponse({
      player_color: 'white',
      families: [],
      total_families: 0,
      computed_at: null,
    })

    await getOpeningFamilyScores('white')

    const [url, options] = fetchMock.mock.calls[0]
    expect(options.method).toBe('GET')
    expect(url).toContain('/api/openings/families/scores')
    expect(url).toContain('player_color=white')
  })

  it('includes auth headers', async () => {
    localStorage.setItem('ghost_replay_token', 'opening-token')
    mockResponse({
      player_color: 'black',
      families: [],
      total_families: 0,
      computed_at: null,
    })

    await getOpeningFamilyScores('black')

    const [, options] = fetchMock.mock.calls[0]
    expect(options.headers).toEqual(
      expect.objectContaining({
        Authorization: 'Bearer opening-token',
        'Content-Type': 'application/json',
      }),
    )
  })

  it('surfaces the fallback error message for non-ok responses', async () => {
    mockResponse({}, false, 'Bad Request', 400)

    await expect(getOpeningFamilyScores('white')).rejects.toThrow(
      'Failed to load opening families: Bad Request',
    )
  })
})

describe('submitAnalysisEvidence', () => {
  beforeEach(() => {
    fetchMock.mockReset()
  })

  const row = {
    fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
    move_uci: 'e2e4',
    best_move_uci: 'e2e4',
    best_line_uci: ['e2e4', 'e7e5'],
    played_eval: 30,
    played_eval_mate: null,
    best_eval: 30,
    best_eval_mate: null,
    eval_delta: 0,
    classification: 'best',
  }

  it('posts rows to the session analysis-evidence endpoint with auth headers', async () => {
    mockResponse({ results: [{ fen: row.fen, move_uci: 'e2e4', reason: 'new_key' }] })

    const results = await submitAnalysisEvidence('sess-1', [row])

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/session/sess-1/analysis-evidence'),
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
        // The endpoint-controlled producer discriminator is sent so a stale bundle
        // that omits it fails closed server-side (g-reuse-d21-search §6.3).
        body: JSON.stringify({ rows: [row], producer: 'visible-multipv-v1' }),
      }),
    )
    expect(results).toEqual([{ fen: row.fen, move_uci: 'e2e4', reason: 'new_key' }])
  })

  it('short-circuits an empty batch without a request', async () => {
    const results = await submitAnalysisEvidence('sess-1', [])
    expect(fetchMock).not.toHaveBeenCalled()
    expect(results).toEqual([])
  })
})

describe('fetchSessionOpenings', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  const payload = (extra: Record<string, unknown> = {}) => ({
    player_color: 'white',
    lineage: [],
    start_ply: 1,
    ...extra,
  })

  it('passes through an explicit score_status', async () => {
    mockResponse(payload({ score_status: 'pending' }))

    const result = await fetchSessionOpenings('s1')

    expect(result.score_status).toBe('pending')
  })

  it('defaults an ABSENT score_status to "ready"', async () => {
    // requestJson only CASTS the payload — it does not fill in missing fields —
    // so an older backend would otherwise leave score_status undefined and the
    // cards would compare against a value that is neither 'ready' nor
    // 'pending'. Asserted here, at the normalization boundary, rather than
    // only through the hook.
    mockResponse(payload())

    const result = await fetchSessionOpenings('s1')

    expect(result.score_status).toBe('ready')
  })

  it('preserves the rest of the response while normalizing', async () => {
    mockResponse(payload({ player_color: 'black', start_ply: 9 }))

    const result = await fetchSessionOpenings('s1')

    expect(result.player_color).toBe('black')
    expect(result.start_ply).toBe(9)
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/session/s1/openings'),
      expect.objectContaining({ method: 'GET' }),
    )
  })
})

describe('checkDrillRoute', () => {
  beforeEach(() => {
    fetchMock.mockReset()
  })

  const routePayload = {
    status: 'root_reached',
    current_fen: 'fen',
    target_fen: 'fen',
    suggestions: [],
    failure: null,
    drill_root_reached_ply: 3,
  }

  it('omits current_ply and decision_id when not supplied', async () => {
    // Absent current_ply is the legacy shape and means "no boundary claim". It
    // must not be serialized as null, which the backend would reject.
    mockResponse(routePayload)

    await checkDrillRoute('s1', {
      current_fen: 'after',
      previous_fen: 'before',
      played_uci: 'e2e4',
    })

    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body).toEqual({
      current_fen: 'after',
      previous_fen: 'before',
      played_uci: 'e2e4',
    })
  })

  it('serializes a boundary claim with current_ply and decision_id', async () => {
    mockResponse(routePayload)

    const result = await checkDrillRoute('s1', {
      current_fen: 'after',
      current_ply: 3,
      decision_id: 'decision-1',
    })

    const [url, options] = fetchMock.mock.calls[0]
    expect(url).toContain('/api/drills/s1/route-check')
    expect(options.method).toBe('POST')
    expect(JSON.parse(options.body)).toEqual({
      current_fen: 'after',
      current_ply: 3,
      decision_id: 'decision-1',
    })
    // The mirror was stale before the cutover — the backend has always sent it.
    expect(result.drill_root_reached_ply).toBe(3)
  })

  it('threads an AbortSignal into the request', async () => {
    // The confirmation is a gameplay barrier, so it must be bounded: without a
    // signal a hung server blocks the board forever.
    mockResponse(routePayload)
    const controller = new AbortController()

    await checkDrillRoute(
      's1',
      { current_fen: 'after', current_ply: 1 },
      { signal: controller.signal },
    )

    expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal)
  })
})
