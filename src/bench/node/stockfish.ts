import { createRequire } from 'node:module'
import { performance } from 'node:perf_hooks'
import type { BenchPhaseName, BenchPhaseRecord } from '../benchRecord'
import { parseUciInfoLine } from '../../workers/parseInfo'
import {
  admitInfoLine,
  createSnapshotAssembler,
  legacyDivergenceReason,
  selectAtomicSnapshot,
} from '../../workers/pvSnapshots'
import type {
  AtomicSelection,
  LegacySelection,
  SnapshotDivergenceReason,
} from '../../workers/pvSnapshots'
import type { EngineScore } from '../../workers/stockfishMessages'
import { moveEndsGame } from './terminal'

const require = createRequire(import.meta.url)

type PackageEngine = {
  listener?: (line: string) => void
  sendCommand: (command: string) => void
  terminate?: () => void
}

type InitStockfish = (flavor: 'lite-single') => Promise<PackageEngine>

export type UciTransport = {
  onLine: (listener: (line: string) => void) => () => void
  send: (command: string) => void
  close: () => void
}

const packageTransport = async (): Promise<UciTransport> => {
  // The keyword resolves through stockfish@18.0.7's package manifest to the exact
  // JS/WASM pair the browser worker imports.
  const initStockfish = require('stockfish') as InitStockfish
  const engine = await initStockfish('lite-single')
  const listeners = new Set<(line: string) => void>()
  engine.listener = (line) => {
    for (const listener of listeners) listener(line)
  }
  return {
    onLine: (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    send: (command) => engine.sendCommand(command),
    close: () => {
      engine.sendCommand('quit')
      engine.terminate?.()
      listeners.clear()
    },
  }
}

export type NodeSearchRequest = {
  fen: string
  moves?: string[]
  depth: number
  searchmoves?: string[]
  multipv?: number
  phase: BenchPhaseName
  timeoutMs?: number
}

export type NodeSearchResult = {
  bestmove: string
  score: EngineScore | null
  pv: string[] | null
  reachedDepth: number | null
  selection: AtomicSelection
  phase: BenchPhaseRecord
  rawLines: string[]
}

type StatName = 'nodes' | 'time' | 'nps' | 'hashfull' | 'seldepth'

const readStat = (line: string, name: StatName): number | null => {
  const tokens = line.split(/\s+/)
  const index = tokens.indexOf(name)
  if (index === -1) return null
  const value = Number(tokens[index + 1])
  return Number.isFinite(value) ? value : null
}

const waitForLine = (
  transport: UciTransport,
  predicate: (line: string) => boolean,
  timeoutMs: number,
  description: string,
): Promise<string> => new Promise((resolve, reject) => {
  let done = false
  const unsubscribe = transport.onLine((line) => {
    if (done || !predicate(line)) return
    done = true
    clearTimeout(timer)
    unsubscribe()
    resolve(line)
  })
  const timer = setTimeout(() => {
    if (done) return
    done = true
    unsubscribe()
    reject(new Error(`timed out waiting for Stockfish ${description}`))
  }, timeoutMs)
})

export class NodeStockfish {
  readonly commands: string[] = []
  private searchActive = false
  private readonly transport: UciTransport

  private constructor(transport: UciTransport) {
    this.transport = transport
  }

  static async create(options: { transport?: UciTransport; timeoutMs?: number } = {}) {
    const transport = options.transport ?? await packageTransport()
    const engine = new NodeStockfish(transport)
    await engine.initialize(options.timeoutMs ?? 30_000)
    return engine
  }

  private send(command: string) {
    this.commands.push(command)
    this.transport.send(command)
  }

  private async initialize(timeoutMs: number) {
    const uciok = waitForLine(this.transport, (line) => line === 'uciok', timeoutMs, 'uciok')
    this.send('uci')
    await uciok
    // Byte-for-byte the production analysisWorker initialization policy.
    this.send('setoption name Hash value 128')
    this.send('setoption name MultiPV value 1')
    const ready = waitForLine(this.transport, (line) => line === 'readyok', timeoutMs, 'readyok')
    this.send('isready')
    await ready
  }

  /** Exactly one reset barrier per independent corpus row. */
  async reset(timeoutMs = 30_000): Promise<number> {
    if (this.searchActive) throw new Error('cannot reset during a search')
    const started = performance.now()
    const ready = waitForLine(this.transport, (line) => line === 'readyok', timeoutMs, 'reset readyok')
    this.send('ucinewgame')
    this.send('isready')
    await ready
    return performance.now() - started
  }

  async search(request: NodeSearchRequest): Promise<NodeSearchResult> {
    if (this.searchActive) throw new Error('NodeStockfish permits only one active search')
    this.searchActive = true

    const moves = request.moves ?? []
    const multipv = request.multipv ?? 1
    const raisedMultiPv = multipv !== 1
    const assembler = createSnapshotAssembler(multipv)
    const legacy: LegacySelection = { score: null, pv: null, reachedDepth: null }
    const rawLines: string[] = []
    let infoLines = 0
    let admittedLines = 0
    let nodes: number | null = null
    let timeMs: number | null = null
    let nps: number | null = null
    let hashfull: number | null = null
    let seldepth: number | null = null
    const started = performance.now()

    const unsubscribe = this.transport.onLine((line) => {
      rawLines.push(line)
      if (!line.startsWith('info')) return
      infoLines += 1
      nodes = readStat(line, 'nodes') ?? nodes
      timeMs = readStat(line, 'time') ?? timeMs
      nps = readStat(line, 'nps') ?? nps
      hashfull = readStat(line, 'hashfull') ?? hashfull
      seldepth = readStat(line, 'seldepth') ?? seldepth

      const info = parseUciInfoLine(line)
      if (!info) return
      if (info.depth !== undefined) legacy.reachedDepth = info.depth
      if (info.score) legacy.score = info.score
      if (info.pv && (info.multipv === undefined || info.multipv === 1)) {
        legacy.pv = info.pv
      }
      if (admitInfoLine(assembler, info)) admittedLines += 1
    })

    let bestmove = ''
    try {
      const bestmoveLine = waitForLine(
        this.transport,
        (line) => line.startsWith('bestmove'),
        request.timeoutMs ?? 10 * 60_000,
        'bestmove',
      )
      if (raisedMultiPv) this.send(`setoption name MultiPV value ${multipv}`)
      const movesSegment = moves.length > 0 ? ` moves ${moves.join(' ')}` : ''
      const searchmovesSegment =
        request.searchmoves && request.searchmoves.length > 0
          ? ` searchmoves ${request.searchmoves.join(' ')}`
          : ''
      this.send(`position fen ${request.fen}${movesSegment}`)
      this.send(`go depth ${request.depth}${searchmovesSegment}`)
      try {
        bestmove = (await bestmoveLine).split(/\s+/)[1] ?? ''
      } catch (error) {
        // Leave the engine synchronized for the next row. `stop` is only useful
        // if its eventual bestmove is consumed before any reset/search begins.
        const stopped = waitForLine(
          this.transport,
          (line) => line.startsWith('bestmove'),
          5_000,
          'bestmove after stop',
        )
        this.send('stop')
        await stopped.catch(() => undefined)
        throw error
      }
    } finally {
      unsubscribe()
      if (raisedMultiPv) this.send('setoption name MultiPV value 1')
      this.searchActive = false
    }

    const selection = selectAtomicSnapshot({
      assembler,
      requestedDepth: request.depth,
      bestMove: bestmove,
      capFired: false,
      stopReason: 'bestmove',
      requiredMoves: request.searchmoves ?? null,
      endsGame: (uci) => moveEndsGame(request.fen, moves, uci),
    })
    const legacyDivergence: SnapshotDivergenceReason | null =
      legacyDivergenceReason(selection, legacy)
    const wallMs = performance.now() - started

    return {
      bestmove,
      score: legacy.score,
      pv: legacy.pv,
      reachedDepth: legacy.reachedDepth,
      selection,
      rawLines,
      phase: {
        index: 0,
        name: request.phase,
        moves,
        requestedDepth: request.depth,
        bestmove,
        nodes,
        timeMs,
        nps,
        hashfull,
        reachedDepth: legacy.reachedDepth,
        seldepth,
        wallMs,
        infoLines,
        admittedLines,
        terminated: true,
        snapshot: selection.accepted
          ? { accepted: true, depth: selection.depth }
          : { accepted: false, reason: selection.reason },
        legacyDivergence,
        stopObserved: false,
      },
    }
  }

  close() {
    this.transport.close()
  }
}
