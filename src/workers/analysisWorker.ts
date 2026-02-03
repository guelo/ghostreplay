/// <reference lib="webworker" />

import Stockfish from 'stockfish.wasm'
import stockfishWasmUrl from 'stockfish.wasm/stockfish.wasm?url'
import stockfishWorkerUrl from 'stockfish.wasm/stockfish.worker.js?url'
import stockfishMainUrl from 'stockfish.wasm/stockfish.js?url'
import type {
  AnalysisWorkerRequest,
  AnalysisWorkerResponse,
  AnalyzeMoveMessage,
} from './analysisMessages'
import type { EngineInfo, EngineScore } from './stockfishMessages'

const ctx = self as DedicatedWorkerGlobalScope

let engineReady = false
let engine: Awaited<ReturnType<typeof Stockfish>> | null = null
let activeSearch:
  | {
      resolve: (value: { bestmove: string; score: EngineScore | null }) => void
      reject: (error: Error) => void
      lastScore: EngineScore | null
    }
  | null = null

const pendingAnalyses: AnalyzeMoveMessage[] = []
let analysisInFlight = false

const ensureEngine = async () => {
  if (engine) {
    return engine
  }

  try {
    engine = await Stockfish({
      locateFile: (file) => {
        if (file.endsWith('.wasm')) {
          return stockfishWasmUrl
        }

        if (file.endsWith('.worker.js')) {
          return stockfishWorkerUrl
        }

        return file
      },
      mainScriptUrlOrBlob: stockfishMainUrl,
    })

    engine.addMessageListener(handleEngineLine)
    engine.postMessage('uci')
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Failed to initialize Stockfish'
    ctx.postMessage({ type: 'error', error: message } satisfies AnalysisWorkerResponse)
  }

  return engine
}

type ParsedInfo = Pick<EngineInfo, 'score'>

const parseInfo = (line: string): ParsedInfo | null => {
  if (!line.startsWith('info')) {
    return null
  }

  const tokens = line.split(' ')
  const scoreIndex = tokens.indexOf('score')

  if (scoreIndex === -1) {
    return null
  }

  const scoreType = tokens[scoreIndex + 1]
  const scoreValue = Number(tokens[scoreIndex + 2])

  if (Number.isNaN(scoreValue) || (scoreType !== 'cp' && scoreType !== 'mate')) {
    return null
  }

  return {
    score: {
      type: scoreType,
      value: scoreValue,
    },
  }
}

const mateToCp = (movesToMate: number) => {
  const mateBase = 10000
  const mateDecay = 10
  const sign = movesToMate >= 0 ? 1 : -1
  return sign * (mateBase - Math.abs(movesToMate) * mateDecay)
}

const normalizeScore = (score: EngineScore | null, sideToMove: 'w' | 'b') => {
  if (!score) {
    return null
  }

  const raw = score.type === 'cp' ? score.value : mateToCp(score.value)
  const sign = sideToMove === 'w' ? 1 : -1
  return raw * sign
}

const scoreForPlayer = (
  score: EngineScore | null,
  sideToMove: 'w' | 'b',
  playerColor: 'white' | 'black',
) => {
  const whitePerspective = normalizeScore(score, sideToMove)
  if (whitePerspective === null) {
    return null
  }
  return playerColor === 'white' ? whitePerspective : -whitePerspective
}

const getSideToMove = (fen: string) => {
  const parts = fen.split(' ')
  const active = parts[1]
  if (active === 'w' || active === 'b') {
    return active
  }
  return null
}

const runSearch = async (fen: string, moves: string[], movetime: number) => {
  const pendingEngine = await ensureEngine()

  if (!pendingEngine) {
    throw new Error('Stockfish engine unavailable')
  }

  if (activeSearch) {
    pendingEngine.postMessage('stop')
  }

  return new Promise<{ bestmove: string; score: EngineScore | null }>(
    (resolve, reject) => {
      activeSearch = { resolve, reject, lastScore: null }
      const movesSegment = moves.length > 0 ? ` moves ${moves.join(' ')}` : ''
      pendingEngine.postMessage(`position fen ${fen}${movesSegment}`)
      pendingEngine.postMessage(`go movetime ${movetime}`)
    },
  )
}

const handleEngineLine = (line: string) => {
  if (line === 'uciok') {
    engine?.postMessage('isready')
    return
  }

  if (line === 'readyok') {
    engineReady = true
    ctx.postMessage({ type: 'ready' } satisfies AnalysisWorkerResponse)
    drainQueue()
    return
  }

  if (line.startsWith('bestmove')) {
    const current = activeSearch
    activeSearch = null

    if (!current) {
      return
    }

    const parts = line.split(' ')
    const move = parts[1] ?? ''
    current.resolve({ bestmove: move, score: current.lastScore })
    return
  }

  const info = parseInfo(line)
  if (info?.score && activeSearch) {
    activeSearch.lastScore = info.score
  }
}

const enqueueAnalysis = (message: AnalyzeMoveMessage) => {
  pendingAnalyses.push(message)
  drainQueue()
}

const drainQueue = () => {
  if (!engineReady || analysisInFlight) {
    return
  }

  const next = pendingAnalyses.shift()
  if (!next) {
    return
  }

  analysisInFlight = true

  void analyzeMove(next)
    .catch((error) => {
      const message =
        error instanceof Error ? error.message : 'Failed to analyze move'
      ctx.postMessage({ type: 'error', error: message } satisfies AnalysisWorkerResponse)
    })
    .finally(() => {
      analysisInFlight = false
      drainQueue()
    })
}

const analyzeMove = async (request: AnalyzeMoveMessage) => {
  ctx.postMessage({
    type: 'analysis-started',
    id: request.id,
    move: request.move,
  } satisfies AnalysisWorkerResponse)

  const sideToMove = getSideToMove(request.fen)

  if (!sideToMove) {
    throw new Error('Invalid FEN supplied for analysis')
  }

  const movetime = request.movetime ?? 1200

  const bestSearch = await runSearch(request.fen, [], movetime)
  const bestMove = bestSearch.bestmove

  if (!bestMove || bestMove === '(none)') {
    ctx.postMessage({
      type: 'analysis',
      id: request.id,
      bestMove: bestMove || '(none)',
      bestEval: null,
      playedEval: null,
      delta: null,
      blunder: false,
    } satisfies AnalysisWorkerResponse)
    return
  }

  const opponentToMove = sideToMove === 'w' ? 'b' : 'w'

  const bestEvalSearch = await runSearch(request.fen, [bestMove], movetime)
  const bestEval = scoreForPlayer(
    bestEvalSearch.score,
    opponentToMove,
    request.playerColor,
  )

  const playedEvalSearch = await runSearch(request.fen, [request.move], movetime)
  const playedEval = scoreForPlayer(
    playedEvalSearch.score,
    opponentToMove,
    request.playerColor,
  )

  const delta =
    bestEval !== null && playedEval !== null ? bestEval - playedEval : null
  const blunder = delta !== null && delta >= 150

  ctx.postMessage({
    type: 'analysis',
    id: request.id,
    move: request.move,
    bestMove,
    bestEval,
    playedEval,
    delta,
    blunder,
  } satisfies AnalysisWorkerResponse)
}

ensureEngine()

ctx.addEventListener('message', (event: MessageEvent<AnalysisWorkerRequest>) => {
  const message = event.data

  switch (message.type) {
    case 'analyze-move': {
      if (!engineReady) {
        enqueueAnalysis(message)
        return
      }

      enqueueAnalysis(message)
      break
    }
    case 'terminate': {
      engine?.terminate()
      engine = null
      engineReady = false
      activeSearch = null
      pendingAnalyses.length = 0
      break
    }
    default:
      message satisfies never
  }
})
