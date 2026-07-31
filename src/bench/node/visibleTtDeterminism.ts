import { createRequire } from 'node:module'
import { buildThermalPositions } from '../device/positions'
import { parseUciInfoLine } from '../../workers/parseInfo'
import type { EngineInfo, EngineScore } from '../../workers/stockfishMessages'

const require = createRequire(import.meta.url)

const TARGET_FEN =
  '2kr1b1r/pp1q4/2p5/P1P2p2/2NP2pp/8/1B3PPP/R2QR1K1 w - - 0 22'
const BROWSE_PLIES = [24, 30, 36, 42] as const
const VISIBLE_DEPTH = 21
const VISIBLE_MULTIPV = 3
const DEFAULT_TIMEOUT_MS = 10 * 60_000

type PackageEngine = {
  listener?: (line: string) => void
  sendCommand: (command: string) => void
  terminate?: () => void
}

type InitStockfish = (flavor: 'lite-single') => Promise<PackageEngine>

export type VisibleTtTransport = {
  onLine: (listener: (line: string) => void) => () => void
  send: (command: string) => void
  close: () => void
}

export type VisibleTtSearchPosition = {
  id: string
  label: string
  fen: string
}

export type VisibleTtLine = {
  depth: number
  multipv: number
  score: EngineScore
  pv: string[]
}

export type VisibleTtSnapshot = {
  bestmove: string
  lines: VisibleTtLine[]
}

export type VisibleTtCheckResult = {
  positions: VisibleTtSearchPosition[]
  snapshots: VisibleTtSnapshot[]
}

const createPackageTransport = async (): Promise<VisibleTtTransport> => {
  // `lite-single` resolves through stockfish@18.0.7 to the exact JS/WASM pair
  // imported by stockfishWorker.ts.
  const initStockfish = require('stockfish') as InitStockfish
  const engine = await initStockfish('lite-single')
  const listeners = new Set<(line: string) => void>()
  let closed = false

  engine.listener = (line) => {
    for (const listener of listeners) {
      listener(line)
    }
  }

  return {
    onLine: (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    send: (command) => engine.sendCommand(command),
    close: () => {
      if (closed) {
        return
      }
      closed = true
      engine.sendCommand('quit')
      engine.terminate?.()
      listeners.clear()
    },
  }
}

const waitForLine = (
  transport: VisibleTtTransport,
  predicate: (line: string) => boolean,
  timeoutMs: number,
  description: string,
): Promise<string> => new Promise((resolve, reject) => {
  let done = false
  const unsubscribe = transport.onLine((line) => {
    if (done || !predicate(line)) {
      return
    }
    done = true
    clearTimeout(timer)
    unsubscribe()
    resolve(line)
  })
  const timer = setTimeout(() => {
    if (done) {
      return
    }
    done = true
    unsubscribe()
    reject(new Error(`timed out waiting for Stockfish ${description}`))
  }, timeoutMs)
})

const normalizeSnapshot = (
  label: string,
  bestmove: string,
  slots: Map<number, EngineInfo>,
): VisibleTtSnapshot => {
  if (!bestmove) {
    throw new Error(`${label}: Stockfish returned an empty bestmove`)
  }

  const lines: VisibleTtLine[] = []
  for (let multipv = 1; multipv <= VISIBLE_MULTIPV; multipv += 1) {
    const info = slots.get(multipv)
    if (
      !info
      || info.depth !== VISIBLE_DEPTH
      || !info.score
      || !info.pv
      || info.pv.length === 0
    ) {
      throw new Error(
        `${label}: missing complete depth-${VISIBLE_DEPTH} MultiPV slot ${multipv}`,
      )
    }
    lines.push({
      depth: info.depth,
      multipv,
      score: { ...info.score },
      pv: [...info.pv],
    })
  }

  return { bestmove, lines }
}

export const buildVisibleTtSearchPositions = (): VisibleTtSearchPosition[] => {
  const thermalPositions = new Map(
    buildThermalPositions(Math.max(...BROWSE_PLIES)).map((position) => [
      position.thermalIndex,
      position,
    ]),
  )
  const browsePositions = BROWSE_PLIES.map((ply) => {
    const position = thermalPositions.get(ply)
    if (!position) {
      throw new Error(`missing Kasparov–Topalov thermal position at ply ${ply}`)
    }
    return {
      id: position.positionId,
      label: `Kasparov–Topalov ply ${ply}`,
      fen: position.fen,
    }
  })

  return [
    { id: 'target:cold', label: 'g-kgiq target (cold)', fen: TARGET_FEN },
    ...browsePositions,
    {
      id: 'target:after-browse',
      label: 'g-kgiq target (after browse)',
      fen: TARGET_FEN,
    },
  ]
}

export class VisibleTtEngine {
  readonly commands: string[] = []
  private readonly transport: VisibleTtTransport
  private configured = false
  private searchActive = false

  private constructor(transport: VisibleTtTransport) {
    this.transport = transport
  }

  static async create(
    options: { transport?: VisibleTtTransport; timeoutMs?: number } = {},
  ): Promise<VisibleTtEngine> {
    const transport = options.transport ?? await createPackageTransport()
    const engine = new VisibleTtEngine(transport)
    try {
      await engine.initialize(options.timeoutMs ?? 30_000)
      return engine
    } catch (error) {
      engine.close()
      throw error
    }
  }

  private send(command: string) {
    this.commands.push(command)
    this.transport.send(command)
  }

  private async initialize(timeoutMs: number) {
    const uciok = waitForLine(
      this.transport,
      (line) => line === 'uciok',
      timeoutMs,
      'uciok',
    )
    this.send('uci')
    await uciok

    const readyok = waitForLine(
      this.transport,
      (line) => line === 'readyok',
      timeoutMs,
      'readyok',
    )
    this.send('isready')
    await readyok
  }

  async search(
    position: VisibleTtSearchPosition,
    timeoutMs = DEFAULT_TIMEOUT_MS,
  ): Promise<VisibleTtSnapshot> {
    if (this.searchActive) {
      throw new Error('visible TT checker permits only one active search')
    }
    this.searchActive = true

    const slots = new Map<number, EngineInfo>()
    const unsubscribe = this.transport.onLine((line) => {
      const info = parseUciInfoLine(line)
      if (info?.pv && info.pv.length > 0) {
        slots.set(info.multipv ?? 1, info)
      }
    })

    try {
      const bestmoveLine = waitForLine(
        this.transport,
        (line) => line.startsWith('bestmove'),
        timeoutMs,
        `bestmove for ${position.label}`,
      )

      if (!this.configured) {
        this.send('setoption name Hash value 64')
        this.configured = true
      }
      // This is intentionally unbarriered: it is the production visible-worker
      // command order whose real-engine behavior this check exists to prove.
      this.send('ucinewgame')
      this.send(`setoption name MultiPV value ${VISIBLE_MULTIPV}`)
      this.send(`position fen ${position.fen}`)
      this.send(`go depth ${VISIBLE_DEPTH}`)

      const bestmove = (await bestmoveLine).split(/\s+/)[1] ?? ''
      return normalizeSnapshot(position.label, bestmove, slots)
    } finally {
      unsubscribe()
      this.searchActive = false
    }
  }

  close() {
    this.transport.close()
  }
}

const sameScore = (left: EngineScore, right: EngineScore) =>
  left.type === right.type && left.value === right.value

export const assertVisibleTtSnapshotsEqual = (
  cold: VisibleTtSnapshot,
  afterBrowse: VisibleTtSnapshot,
) => {
  if (cold.bestmove !== afterBrowse.bestmove) {
    throw new Error(
      `target bestmove changed: ${cold.bestmove} -> ${afterBrowse.bestmove}`,
    )
  }
  if (cold.lines.length !== VISIBLE_MULTIPV || afterBrowse.lines.length !== VISIBLE_MULTIPV) {
    throw new Error(`target snapshots must each contain ${VISIBLE_MULTIPV} lines`)
  }

  cold.lines.forEach((coldLine, index) => {
    const afterLine = afterBrowse.lines[index]
    if (
      coldLine.depth !== afterLine.depth
      || coldLine.multipv !== afterLine.multipv
    ) {
      throw new Error(`target line ${index + 1} depth/MultiPV changed`)
    }
    if (!sameScore(coldLine.score, afterLine.score)) {
      throw new Error(`target line ${index + 1} score changed`)
    }
    if (
      coldLine.pv.length !== afterLine.pv.length
      || coldLine.pv.some((move, moveIndex) => move !== afterLine.pv[moveIndex])
    ) {
      throw new Error(`target line ${index + 1} PV changed`)
    }
  })
}

export const runVisibleTtDeterminismCheck = async (
  engine: VisibleTtEngine,
  positions = buildVisibleTtSearchPositions(),
): Promise<VisibleTtCheckResult> => {
  if (
    positions.length < 2
    || positions[0].fen !== TARGET_FEN
    || positions[positions.length - 1].fen !== TARGET_FEN
  ) {
    throw new Error('visible TT check must begin and end with the g-kgiq target FEN')
  }

  const snapshots: VisibleTtSnapshot[] = []
  for (const position of positions) {
    snapshots.push(await engine.search(position))
  }

  assertVisibleTtSnapshotsEqual(snapshots[0], snapshots[snapshots.length - 1])
  return { positions, snapshots }
}

export const runVisibleTtDeterminismCli = async () => {
  const positions = buildVisibleTtSearchPositions()
  console.log('Visible TT determinism search order:')
  positions.forEach((position, index) => {
    console.log(`${index + 1}. ${position.label}`)
  })

  let engine: VisibleTtEngine | null = null
  try {
    engine = await VisibleTtEngine.create()
    const result = await runVisibleTtDeterminismCheck(engine, positions)
    const target = result.snapshots[0]
    console.log(
      `PASS: bestmove ${target.bestmove} and all ${target.lines.length} depth-${VISIBLE_DEPTH} lines matched after browsing.`,
    )
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    console.error(`FAIL: visible TT determinism check: ${message}`)
    process.exitCode = 1
  } finally {
    engine?.close()
  }
}
