export type EvaluatePositionMessage = {
  type: 'evaluate-position'
  id: string
  fen: string
  moves?: string[]
  movetime?: number
}

export type WorkerRequest =
  | EvaluatePositionMessage
  | { type: 'command'; command: string }
  | { type: 'newgame' }
  | { type: 'terminate' }

export type EngineScore =
  | { type: 'cp'; value: number }
  | { type: 'mate'; value: number }

export type EngineInfo = {
  depth?: number
  score?: EngineScore
  pv?: string[]
}

export type WorkerResponse =
  | { type: 'booted' }
  | { type: 'ready' }
  | { type: 'thinking'; id: string; fen: string }
  | { type: 'bestmove'; id: string; move: string; raw: string }
  | { type: 'info'; id: string; info: EngineInfo; raw: string }
  | { type: 'log'; line: string }
  | { type: 'error'; error: string }
