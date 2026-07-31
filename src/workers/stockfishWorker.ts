/// <reference lib="webworker" />

import stockfishEngineUrl from 'stockfish/bin/stockfish-18-lite-single.js?url'
import stockfishWasmUrl from 'stockfish/bin/stockfish-18-lite-single.wasm?url'
import type {
  CompletedRootAnalysis,
  EngineInfo,
  EvaluatePositionMessage,
  SearchLimit,
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
// PV-bearing info slots of the CURRENT running search, keyed by one-based UCI
// multipv index. Reset per search start; the latest line for each slot wins.
// Used to build the atomic completed snapshot at bestmove (g-reuse-d21-search §3).
let runningSlots = new Map<number, EngineInfo>()

// Build one self-consistent copy from the accumulated slots — no references to the
// still-mutating accumulator survive. Slots are emitted in ascending multipv order.
function buildCompletedSnapshot(
  request: EvaluatePositionMessage,
  slots: Map<number, EngineInfo>,
  bestMove: string,
): CompletedRootAnalysis {
  const lines: EngineInfo[] = Array.from(slots.keys())
    .sort((a, b) => a - b)
    .map((multipv) => {
      const info = slots.get(multipv) as EngineInfo
      return {
        depth: info.depth,
        score: info.score ? { ...info.score } : undefined,
        pv: info.pv ? [...info.pv] : undefined,
        multipv: info.multipv,
      }
    })

  const limit: SearchLimit = request.depth
    ? { type: 'depth', value: request.depth }
    : { type: 'movetime', value: request.movetime ?? 1500 }

  return {
    requestId: request.id,
    fen: request.fen,
    bestMove,
    lines,
    limit,
    multipv: request.multipv ?? 1,
    searchmoves:
      request.searchmoves && request.searchmoves.length > 0
        ? [...request.searchmoves]
        : null,
  }
}

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

  // Visible depth searches are durable evidence inputs, so each one must start
  // from the same empty-TT state regardless of which positions were browsed
  // earlier. Keep movetime searches warm: the in-game fallback is timer-bound
  // and does not feed the visible depth-21 evidence contract.
  if (request.depth) {
    sendEngineCommand('ucinewgame')
  }

  runningSearch = request
  runningSlots = new Map()
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
      // Build the atomic completed snapshot from the accumulated slots BEFORE
      // clearing per-search state, associating every line with this request id.
      const snapshot = buildCompletedSnapshot(runningSearch, runningSlots, move)
      ctx.postMessage({
        type: 'bestmove',
        id: runningSearch.id,
        move,
        raw: line,
        snapshot,
      } satisfies WorkerResponse)
    }

    runningSearch = null
    runningSlots = new Map()

    const nextRequest = queuedEvaluations.shift()
    if (nextRequest) {
      startEvaluation(nextRequest)
    }

    return
  }

  const info = parseUciInfoLine(line)

  if (info && runningSearch) {
    // Accumulate only PV-bearing lines by one-based multipv index for the snapshot;
    // the latest line for each slot wins (deeper iterations overwrite shallower).
    if (info.pv && info.pv.length > 0) {
      runningSlots.set(info.multipv ?? 1, info)
    }
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
      runningSlots = new Map()
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
      runningSlots = new Map()
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
