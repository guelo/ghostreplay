/**
 * API client for Ghost Replay backend
 */

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'
const RETRY_BASE_DELAY_MS = 200

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

  constructor(
    message: string,
    options: {
      status: number
      code?: string
      details?: unknown
      retryable?: boolean
    },
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = options.status
    this.code = options.code ?? `http_${options.status}`
    this.details = options.details
    this.retryable =
      options.retryable ?? (options.status === 429 || options.status >= 500)
  }
}

const delay = async (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms))

const parseJsonSafely = async (
  response: Response,
): Promise<ApiErrorEnvelope | null> => {
  try {
    return (await response.json()) as ApiErrorEnvelope
  } catch {
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
  return new ApiError(message, {
    status: response.status,
    code: payload?.error?.code,
    details: payload?.error?.details,
    retryable: payload?.error?.retryable,
  })
}

const requestJson = async <T>(
  url: string,
  init: RequestInit,
  options?: { retries?: number; fallbackMessage?: string },
): Promise<T> => {
  const retries = options?.retries ?? 0
  const method = init.method ?? 'GET'
  const fallbackMessage =
    options?.fallbackMessage ?? `Request failed: ${method} ${url}`

  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      const response = await fetch(url, init)
      if (response.ok) {
        return response.json() as Promise<T>
      }

      const apiError = await createApiError(response, fallbackMessage)
      if (attempt < retries && apiError.retryable) {
        await delay(RETRY_BASE_DELAY_MS * 2 ** attempt)
        continue
      }
      throw apiError
    } catch (error) {
      if (error instanceof ApiError) {
        throw error
      }

      const isNetworkRetryable = attempt < retries
      if (isNetworkRetryable) {
        await delay(RETRY_BASE_DELAY_MS * 2 ** attempt)
        continue
      }
      throw error
    }
  }

  throw new Error('Unexpected retry loop exit')
}

interface StartGameRequest {
  engine_elo: number
  player_color: 'white' | 'black'
}

interface StartGameResponse {
  session_id: string
  engine_elo: number
  player_color?: 'white' | 'black'
}

interface EndGameRequest {
  session_id: string
  result: 'checkmate_win' | 'checkmate_loss' | 'resign' | 'draw' | 'abandon'
  pgn: string
}

interface EndGameResponse {
  session_id: string
  blunders_recorded: number
  blunders_reviewed: number
}

export type SessionMoveColor = 'white' | 'black'

export type SessionMoveClassification =
  | 'best'
  | 'excellent'
  | 'good'
  | 'inaccuracy'
  | 'mistake'
  | 'blunder'

export interface SessionMoveUpload {
  move_number: number
  color: SessionMoveColor
  move_san: string
  fen_after: string
  eval_cp: number | null
  eval_mate: number | null
  best_move_san: string | null
  best_move_eval_cp: number | null
  eval_delta: number | null
  classification: SessionMoveClassification | null
}

interface SessionMovesRequest {
  moves: SessionMoveUpload[]
}

interface SessionMovesResponse {
  moves_inserted: number
}

interface BlunderRequest {
  session_id: string
  pgn: string
  fen: string
  user_move: string
  best_move: string
  eval_before: number
  eval_after: number
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

interface GhostMoveResponse {
  mode: 'ghost' | 'engine'
  move: string | null
  target_blunder_id: number | null
}

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
  pgn: string
): Promise<EndGameResponse> => {
  return requestJson<EndGameResponse>(`${API_BASE_URL}/api/game/end`, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify({ session_id: sessionId, result, pgn } satisfies EndGameRequest),
  }, { fallbackMessage: 'Failed to end game' })
}

/**
 * Upload analyzed session moves in a single batch.
 */
export const uploadSessionMoves = async (
  sessionId: string,
  moves: SessionMoveUpload[],
): Promise<SessionMovesResponse> => {
  return requestJson<SessionMovesResponse>(
    `${API_BASE_URL}/api/session/${sessionId}/moves`,
    {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({ moves } satisfies SessionMovesRequest),
    },
    { fallbackMessage: 'Failed to upload session moves' },
  )
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
): Promise<BlunderResponse> => {
  return requestJson<BlunderResponse>(`${API_BASE_URL}/api/blunder`, {
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
    } satisfies BlunderRequest),
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
 * Get ghost move for current position
 */
export const getGhostMove = async (
  sessionId: string,
  fen: string,
): Promise<GhostMoveResponse> => {
  const params = new URLSearchParams({
    session_id: sessionId,
    fen,
  })

  return requestJson<GhostMoveResponse>(`${API_BASE_URL}/api/game/ghost-move?${params}`, {
    method: 'GET',
    headers: getAuthHeaders(),
  }, { retries: 2, fallbackMessage: 'Failed to get ghost move' })
}
