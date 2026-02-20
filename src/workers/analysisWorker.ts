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
import type { EngineScore } from './stockfishMessages'
import {
  parseInfo,
  getSideToMove,
  computeAnalysisResult,
  isBlunder,
  isWithinRecordingMoveCap,
} from './analysisUtils'

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


const runSearch = async (fen: string, moves: string[]) => {
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
      pendingEngine.postMessage('go depth 20 movetime 3000')
    },
  )
}

const handleEngineLine = (line: string) => {
  if (line === 'uciok') {
    engine?.postMessage('setoption name Hash value 128')
    const threads = Math.min(Math.max(Math.floor(navigator.hardwareConcurrency / 2), 1), 4)
    engine?.postMessage(`setoption name Threads value ${threads}`)
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
      blunder: false,
    } satisfies AnalysisWorkerResponse)
    return
  }

  // Evaluate the position after the played move
  const playedEvalSearch = await runSearch(request.fen, [request.move])

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

  const forced = request.legalMoveCount !== undefined && request.legalMoveCount <= 2
  const blunder =
    !forced &&
    isBlunder(delta, bestEval) &&
    (request.moveIndex === undefined || isWithinRecordingMoveCap(request.moveIndex))

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
