/// <reference lib="webworker" />

import Stockfish from 'stockfish.wasm'
import stockfishWasmUrl from 'stockfish.wasm/stockfish.wasm?url'
import stockfishWorkerUrl from 'stockfish.wasm/stockfish.worker.js?url'
import stockfishMainUrl from 'stockfish.wasm/stockfish.js?url'
import type {
  EvaluatePositionMessage,
  WorkerRequest,
  WorkerResponse,
} from './stockfishMessages'
import { parseUciInfoLine } from './parseInfo'

const ctx = self as DedicatedWorkerGlobalScope

let engineReady = false
let engine: Awaited<ReturnType<typeof Stockfish>> | null = null
let runningSearch: EvaluatePositionMessage | null = null
const queuedOperations: Array<() => void> = []
const queuedEvaluations: EvaluatePositionMessage[] = []

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
    ctx.postMessage({ type: 'booted' } satisfies WorkerResponse)
    engine.postMessage('uci')
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Failed to initialize Stockfish'
    ctx.postMessage({ type: 'error', error: message })
  }

  return engine
}

ensureEngine()

function enqueueOrRun(action: () => void) {
  if (!engineReady) {
    queuedOperations.push(action)
    return
  }

  action()
}

function flushQueuedOperations() {
  while (queuedOperations.length > 0 && engineReady) {
    const operation = queuedOperations.shift()
    operation?.()
  }
}

// parseUciInfoLine is imported from ./parseInfo

let engineConfigured = false

function startEvaluation(request: EvaluatePositionMessage) {
  const pendingEngine = engine

  if (!pendingEngine) {
    return
  }

  // Configure threads/hash on first deep analysis request only
  if (!engineConfigured && (request.depth || (request.multipv && request.multipv > 1))) {
    const threads = Math.min(
      Math.max(1, Math.floor(navigator.hardwareConcurrency / 2)),
      4,
    )
    pendingEngine.postMessage(`setoption name Threads value ${threads}`)
    pendingEngine.postMessage('setoption name Hash value 64')
    engineConfigured = true
  }

  runningSearch = request
  ctx.postMessage({ type: 'thinking', id: request.id, fen: request.fen })

  const movesSegment =
    request.moves && request.moves.length > 0
      ? ` moves ${request.moves.join(' ')}`
      : ''

  const multipv = request.multipv ?? 1
  pendingEngine.postMessage(`setoption name MultiPV value ${multipv}`)
  pendingEngine.postMessage(`position fen ${request.fen}${movesSegment}`)

  const searchmovesSuffix =
    request.searchmoves && request.searchmoves.length > 0
      ? ` searchmoves ${request.searchmoves.join(' ')}`
      : ''

  if (request.depth) {
    pendingEngine.postMessage(`go depth ${request.depth}${searchmovesSuffix}`)
  } else {
    const movetime = request.movetime ?? 1500
    pendingEngine.postMessage(`go movetime ${movetime}${searchmovesSuffix}`)
  }
}

function handleEngineLine(line: string) {
  ctx.postMessage({ type: 'log', line })

  if (line === 'uciok') {
    engine?.postMessage('isready')
    return
  }

  if (line === 'readyok') {
    engineReady = true
    ctx.postMessage({ type: 'ready' })
    flushQueuedOperations()

    if (queuedEvaluations.length > 0 && !runningSearch) {
      const nextEvaluation = queuedEvaluations.shift()
      if (nextEvaluation) {
        startEvaluation(nextEvaluation)
      }
    }

    return
  }

  if (line.startsWith('bestmove')) {
    if (runningSearch) {
      const parts = line.split(' ')
      const move = parts[1] ?? ''
      ctx.postMessage({
        type: 'bestmove',
        id: runningSearch.id,
        move,
        raw: line,
      })
    }

    runningSearch = null

    const nextRequest = queuedEvaluations.shift()
    if (nextRequest) {
      startEvaluation(nextRequest)
    }

    return
  }

  const info = parseUciInfoLine(line)

  if (info && runningSearch) {
    ctx.postMessage({ type: 'info', id: runningSearch.id, info, raw: line })
  }
}

ctx.addEventListener('message', (event: MessageEvent<WorkerRequest>) => {
  const message = event.data

  switch (message.type) {
    case 'command': {
      enqueueOrRun(() => engine?.postMessage(message.command))
      break
    }
    case 'newgame': {
      enqueueOrRun(() => {
        engine?.postMessage('stop')
        engine?.postMessage('ucinewgame')
      })
      runningSearch = null
      queuedEvaluations.length = 0
      break
    }
    case 'evaluate-position': {
      const boundedAction = () => {
        if (runningSearch) {
          queuedEvaluations.length = 0
          queuedEvaluations.push(message)
          engine?.postMessage('stop')
          return
        }

        startEvaluation(message)
      }

      enqueueOrRun(boundedAction)
      break
    }
    case 'terminate': {
      engine?.terminate()
      runningSearch = null
      queuedEvaluations.length = 0
      engine = null
      engineReady = false
      break
    }
    default:
      message satisfies never
  }
})
