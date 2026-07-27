export type EvaluatePositionMessage = {
  type: 'evaluate-position'
  id: string
  fen: string
  moves?: string[]
  movetime?: number
  depth?: number
  multipv?: number
  searchmoves?: string[]
}

export type WorkerRequest =
  | EvaluatePositionMessage
  | { type: 'command'; command: string }
  | { type: 'newgame' }
  | { type: 'terminate' }

export type EngineScore =
  | { type: 'cp'; value: number }
  | { type: 'mate'; value: number }

/**
 * Whether an `info` line's score is a settled evaluation or an aspiration-window
 * failure. Stockfish marks the latter with `lowerbound`/`upperbound`; only `exact`
 * lines may enter atomic snapshot assembly (g-atomic-snapshots §4).
 */
export type EngineScoreBound = 'exact' | 'lower' | 'upper'

export type EngineInfo = {
  depth?: number
  seldepth?: number
  score?: EngineScore
  /** Set only alongside `score`; a score-less line has no bound to report. */
  bound?: EngineScoreBound
  pv?: string[]
  multipv?: number
  nodes?: number
  nps?: number
  /** UCI `time`, in milliseconds. */
  time?: number
  hashfull?: number
}

// The ACTUAL search limit issued for a request (never a hardcoded constant), so a
// snapshot truthfully records whether it was a depth or movetime search.
export type SearchLimit =
  | { type: 'depth'; value: number }
  | { type: 'movetime'; value: number }

// An immutable, self-consistent snapshot of ONE completed root search, built
// atomically at the worker's bestmove boundary (g-reuse-d21-search §3). The
// visible display and the evidence-reuse layer share this single value so there is
// exactly one root opinion. ``lines`` are the final PV-bearing slots in one-based
// multipv order; ``limit`` / ``multipv`` / ``searchmoves`` describe the request
// actually posted, NOT a constant, so local eligibility can reject anything that is
// not the unrestricted depth-21 MultiPV-3 shape.
export type CompletedRootAnalysis = {
  requestId: string
  fen: string
  bestMove: string
  lines: EngineInfo[]
  limit: SearchLimit
  multipv: number
  searchmoves: string[] | null
}

export type WorkerResponse =
  | { type: 'booted' }
  | { type: 'ready' }
  | { type: 'thinking'; id: string; fen: string }
  | { type: 'bestmove'; id: string; move: string; raw: string; snapshot: CompletedRootAnalysis }
  | { type: 'info'; id: string; info: EngineInfo; raw: string }
  | { type: 'log'; line: string }
  | { type: 'error'; error: string }
