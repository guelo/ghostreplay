/// <reference lib="webworker" />

import stockfishEngineUrl from 'stockfish/bin/stockfish-18-lite-single.js?url'
import stockfishWasmUrl from 'stockfish/bin/stockfish-18-lite-single.wasm?url'
import type {
  EvaluatePositionMessage,
  WorkerRequest,
  WorkerResponse,
} from './stockfishMessages'
import { parseUciInfoLine } from './parseInfo'

const ctx = self as DedicatedWorkerGlobalScope

let engineReady = false
let engine: Worker | null = null
let runningSearch: EvaluatePositionMessage | null = null
let engineConfigured = false
const queuedOperations: Array<() => void> = []
const queuedEvaluations: EvaluatePositionMessage[] = []

// Stockfish's browser worker bootstrap reads the wasm asset from location.hash.
// This is a private package contract, so upgrades must be revalidated with the
// real-browser smoke test before changing the pinned stockfish version.
const createEngineWorkerUrl = () =>
  `${stockfishEngineUrl}#${encodeURIComponent(stockfishWasmUrl)}`

function postLog(line: string) {
  ctx.postMessage({ type: 'log', line } satisfies WorkerResponse)
}

function sendEngineCommand(command: string) {
  postLog(`[stockfishWorker ->] ${command}`)
  engine?.postMessage(command)
}

const ensureEngine = async () => {
  if (engine) {
    return engine
  }

  try {
    engine = new Worker(createEngineWorkerUrl())
    engine.addEventListener('message', handleEngineMessage)
    engine.addEventListener('error', handleEngineError)
    ctx.postMessage({ type: 'booted' } satisfies WorkerResponse)
    sendEngineCommand('uci')
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Failed to initialize Stockfish'
    ctx.postMessage({ type: 'error', error: message } satisfies WorkerResponse)
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

function startEvaluation(request: EvaluatePositionMessage) {
  const pendingEngine = engine

  if (!pendingEngine) {
    return
  }

  if (!engineConfigured && (request.depth || (request.multipv && request.multipv > 1))) {
    sendEngineCommand('setoption name Hash value 64')
    engineConfigured = true
  }

  runningSearch = request
  ctx.postMessage({ type: 'thinking', id: request.id, fen: request.fen } satisfies WorkerResponse)

  const movesSegment =
    request.moves && request.moves.length > 0
      ? ` moves ${request.moves.join(' ')}`
      : ''

  const multipv = request.multipv ?? 1
  sendEngineCommand(`setoption name MultiPV value ${multipv}`)
  sendEngineCommand(`position fen ${request.fen}${movesSegment}`)

  const searchmovesSuffix =
    request.searchmoves && request.searchmoves.length > 0
      ? ` searchmoves ${request.searchmoves.join(' ')}`
      : ''

  if (request.depth) {
    sendEngineCommand(`go depth ${request.depth}${searchmovesSuffix}`)
  } else {
    const movetime = request.movetime ?? 1500
    sendEngineCommand(`go movetime ${movetime}${searchmovesSuffix}`)
  }
}

function handleEngineError(event: ErrorEvent) {
  const message = event.message || 'Failed to initialize Stockfish'
  ctx.postMessage({ type: 'error', error: message } satisfies WorkerResponse)
}

function handleEngineMessage(event: MessageEvent<string>) {
  handleEngineLine(event.data)
}

function handleEngineLine(line: string) {
  postLog(`[stockfishWorker <-] ${line}`)

  if (line === 'uciok') {
    sendEngineCommand('isready')
    return
  }

  if (line === 'readyok') {
    engineReady = true
    ctx.postMessage({ type: 'ready' } satisfies WorkerResponse)
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
      } satisfies WorkerResponse)
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
    ctx.postMessage({ type: 'info', id: runningSearch.id, info, raw: line } satisfies WorkerResponse)
  }
}

ctx.addEventListener('message', (event: MessageEvent<WorkerRequest>) => {
  const message = event.data

  switch (message.type) {
    case 'command': {
      enqueueOrRun(() => sendEngineCommand(message.command))
      break
    }
    case 'newgame': {
      enqueueOrRun(() => {
        sendEngineCommand('stop')
        sendEngineCommand('ucinewgame')
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
          sendEngineCommand('stop')
          return
        }

        startEvaluation(message)
      }

      enqueueOrRun(boundedAction)
      break
    }
    case 'terminate': {
      engine?.removeEventListener('message', handleEngineMessage)
      engine?.removeEventListener('error', handleEngineError)
      engine?.terminate()
      runningSearch = null
      queuedEvaluations.length = 0
      engine = null
      engineReady = false
      engineConfigured = false
      break
    }
    default:
      message satisfies never
  }
})
