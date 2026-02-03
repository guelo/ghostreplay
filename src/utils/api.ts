/**
 * API client for Ghost Replay backend
 */

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

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

interface BlunderRequest {
  session_id: string
  pgn: string
  fen: string
  user_move: string
  best_move: string
  eval_before: number
  eval_after: number
}

interface BlunderResponse {
  blunder_id: number | null
  position_id: number
  positions_created: number
  is_new: boolean
}

/**
 * Start a new game session
 */
export const startGame = async (
  engineElo: number = 1500,
  playerColor: StartGameRequest['player_color'] = 'white',
): Promise<StartGameResponse> => {
  const response = await fetch(`${API_BASE_URL}/api/game/start`, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify({
      engine_elo: engineElo,
      player_color: playerColor,
    } satisfies StartGameRequest),
  })

  if (!response.ok) {
    throw new Error(`Failed to start game: ${response.statusText}`)
  }

  return response.json()
}

/**
 * End a game session
 */
export const endGame = async (
  sessionId: string,
  result: EndGameRequest['result'],
  pgn: string
): Promise<EndGameResponse> => {
  const response = await fetch(`${API_BASE_URL}/api/game/end`, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify({ session_id: sessionId, result, pgn } satisfies EndGameRequest),
  })

  if (!response.ok) {
    throw new Error(`Failed to end game: ${response.statusText}`)
  }

  return response.json()
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
  const response = await fetch(`${API_BASE_URL}/api/blunder`, {
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
  })

  if (!response.ok) {
    throw new Error(`Failed to record blunder: ${response.statusText}`)
  }

  return response.json()
}
