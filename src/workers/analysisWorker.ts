/// <reference lib="webworker" />

import stockfishEngineUrl from 'stockfish/bin/stockfish-18-lite-single.js?url'
import stockfishWasmUrl from 'stockfish/bin/stockfish-18-lite-single.wasm?url'
import type {
  AnalysisWorkerRequest,
  AnalysisWorkerResponse,
  AnalyzeMoveMessage,
} from './analysisMessages'
import type { EngineScore } from './stockfishMessages'
import {
  parseScoreInfo,
  getSideToMove,
  computeAnalysisResult,
  scoreForPlayer,
  classifyMove,
  classifyMoveAdvanced,
} from './analysisUtils'
import type { MoveClassification } from './analysisUtils'

const ctx = self as DedicatedWorkerGlobalScope

let engineReady = false
let engine: Worker | null = null
let activeSearch:
  | {
      resolve: (value: { bestmove: string; score: EngineScore | null }) => void
      reject: (error: Error) => void
      lastScore: EngineScore | null
      onInfo?: (score: EngineScore, depth: number) => void
    }
  | null = null

const pendingAnalyses: AnalyzeMoveMessage[] = []
let analysisInFlight = false

// Stockfish's browser worker bootstrap reads the wasm asset from location.hash.
// This is a private package contract, so upgrades must be revalidated with the
// real-browser smoke test before changing the pinned stockfish version.
const createEngineWorkerUrl = () =>
  `${stockfishEngineUrl}#${encodeURIComponent(stockfishWasmUrl)}`

const ensureEngine = async () => {
  if (engine) {
    return engine
  }

  try {
    engine = new Worker(createEngineWorkerUrl())
    engine.addEventListener('message', handleEngineMessage)
    engine.addEventListener('error', handleEngineError)
    engine.postMessage('uci')
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Failed to initialize Stockfish'
    ctx.postMessage({ type: 'error', error: message } satisfies AnalysisWorkerResponse)
  }

  return engine
}


const runSearch = async (
  fen: string,
  moves: string[],
  onInfo?: (score: EngineScore, depth: number) => void,
) => {
  const pendingEngine = await ensureEngine()

  if (!pendingEngine) {
    throw new Error('Stockfish engine unavailable')
  }

  if (activeSearch) {
    pendingEngine.postMessage('stop')
  }

  return new Promise<{ bestmove: string; score: EngineScore | null }>(
    (resolve, reject) => {
      activeSearch = { resolve, reject, lastScore: null, onInfo }
      const movesSegment = moves.length > 0 ? ` moves ${moves.join(' ')}` : ''
      pendingEngine.postMessage(`position fen ${fen}${movesSegment}`)
      pendingEngine.postMessage('go depth 20 movetime 3000')
    },
  )
}

const handleEngineError = (event: ErrorEvent) => {
  const message = event.message || 'Failed to initialize Stockfish'
  ctx.postMessage({ type: 'error', error: message } satisfies AnalysisWorkerResponse)
}

const handleEngineMessage = (event: MessageEvent<string>) => {
  handleEngineLine(event.data)
}

const handleEngineLine = (line: string) => {
  if (line === 'uciok') {
    engine?.postMessage('setoption name Hash value 128')
    engine?.postMessage('setoption name MultiPV value 1')
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

  const info = parseScoreInfo(line)
  if (info?.score && activeSearch) {
    activeSearch.lastScore = info.score
    if (activeSearch.onInfo) {
      const tokens = line.split(' ')
      const depthIdx = tokens.indexOf('depth')
      const depth = depthIdx >= 0 ? Number(tokens[depthIdx + 1]) : 0
      activeSearch.onInfo(info.score, depth)
    }
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

  const bestSearch = await runSearch(request.fen, [])
  const bestMove = bestSearch.bestmove

  if (!bestMove || bestMove === '(none)') {
    ctx.postMessage({
      type: 'analysis',
      id: request.id,
      move: request.move,
      bestMove: bestMove || '(none)',
      bestEval: null,
      playedEval: null,
      delta: null,
      classification: null,
    } satisfies AnalysisWorkerResponse)
    return
  }

  // Evaluate the position after the played move, streaming intermediate evals
  const opponentToMove = sideToMove === 'w' ? 'b' : 'w'
  const playedEvalSearch = await runSearch(request.fen, [request.move], (score, depth) => {
    const cp = scoreForPlayer(score, opponentToMove, request.playerColor)
    if (cp !== null) {
      ctx.postMessage({
        type: 'analysis-streaming',
        id: request.id,
        cp,
        depth,
      } satisfies AnalysisWorkerResponse)
    }
  })

  // When best != played, search after the best move too for an apples-to-apples
  // comparison. The pre-move minimax eval is unreliable in WASM Stockfish because
  // independent searches reach different depths, inflating the delta.
  const postBestScore = request.move === bestMove
    ? playedEvalSearch.score
    : (await runSearch(request.fen, [bestMove])).score

  const { bestEval, playedEval, delta } = computeAnalysisResult({
    bestMove,
    playedMove: request.move,
    postPlayedScore: playedEvalSearch.score,
    postBestScore,
    sideToMove,
    playerColor: request.playerColor,
  })

  const isBestMove = bestMove === request.move
  const mover: 'white' | 'black' = sideToMove === 'w' ? 'white' : 'black'
  const scorePov: 'white' | 'black' = sideToMove === 'w' ? 'black' : 'white'

  let classification: MoveClassification | null = null
  if (postBestScore && playedEvalSearch.score) {
    classification = classifyMoveAdvanced({
      prevScore: postBestScore,
      nextScore: playedEvalSearch.score,
      scorePov,
      mover,
      isBestMove,
    })
  } else {
    classification = classifyMove(delta)
  }

  ctx.postMessage({
    type: 'analysis',
    id: request.id,
    move: request.move,
    bestMove,
    bestEval,
    playedEval,
    delta,
    classification,
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
      engine?.removeEventListener('message', handleEngineMessage)
      engine?.removeEventListener('error', handleEngineError)
      engine?.terminate()
      engine = null
      engineReady = false
      activeSearch = null
      analysisInFlight = false
      pendingAnalyses.length = 0
      break
    }
    default:
      message satisfies never
  }
})
