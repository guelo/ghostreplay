/// <reference lib="webworker" />

import Stockfish from 'stockfish.wasm'
import stockfishWasmUrl from 'stockfish.wasm/stockfish.wasm?url'
import stockfishWorkerUrl from 'stockfish.wasm/stockfish.worker.js?url'
import stockfishMainUrl from 'stockfish.wasm/stockfish.js?url'
import type {
  EngineInfo,
  EvaluatePositionMessage,
  WorkerRequest,
  WorkerResponse,
} from './stockfishMessages'

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

function parseInfo(line: string): EngineInfo | null {
  if (!line.startsWith('info')) {
    return null
  }

  const tokens = line.split(' ')
  const info: EngineInfo = {}
  const depthIndex = tokens.indexOf('depth')

  if (depthIndex !== -1) {
    const depthValue = Number(tokens[depthIndex + 1])
    if (!Number.isNaN(depthValue)) {
      info.depth = depthValue
    }
  }

  const scoreIndex = tokens.indexOf('score')

  if (scoreIndex !== -1) {
    const scoreType = tokens[scoreIndex + 1]
    const scoreValue = Number(tokens[scoreIndex + 2])

    if (!Number.isNaN(scoreValue) && (scoreType === 'cp' || scoreType === 'mate')) {
      info.score = {
        type: scoreType,
        value: scoreValue,
      }
    }
  }

  const pvIndex = tokens.indexOf('pv')

  if (pvIndex !== -1) {
    const pv = tokens.slice(pvIndex + 1)
    if (pv.length > 0) {
      info.pv = pv
    }
  }

  if (info.depth || info.score || info.pv) {
    return info
  }

  return null
}

function startEvaluation(request: EvaluatePositionMessage) {
  const pendingEngine = engine

  if (!pendingEngine) {
    return
  }

  runningSearch = request
  ctx.postMessage({ type: 'thinking', id: request.id, fen: request.fen })

  const movesSegment =
    request.moves && request.moves.length > 0
      ? ` moves ${request.moves.join(' ')}`
      : ''

  pendingEngine.postMessage(`position fen ${request.fen}${movesSegment}`)

  const movetime = request.movetime ?? 1500
  pendingEngine.postMessage(`go movetime ${movetime}`)
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
      const [_, move] = line.split(' ')
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

  const info = parseInfo(line)

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
