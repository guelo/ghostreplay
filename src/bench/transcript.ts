/**
 * Per-phase instrumentation derived from the worker's own UCI transcript
 * (g-two-search-grade §10.1).
 *
 * `analysisWorker.postLog` already mirrors EVERY engine command and EVERY engine
 * line to the host, unconditionally and in production. That transcript is a
 * complete, ordered record of the orchestration — one reset, the MultiPV
 * sequence, the up-to-three searches, every `stop` — so a harness can measure
 * nodes and time per search phase, §4 slot acceptance, and §4.3 selector
 * divergence WITHOUT re-implementing any orchestration and without a single
 * worker change. §10.1 is explicit that a harness which re-implements
 * orchestration benchmarks the harness rather than the shipping worker.
 *
 * Everything here is a pure function over log entries, so both the browser
 * device runner and the Node corpus harness use it and their JSONL agrees by
 * construction. §15.2 keeps this module after a rejection verdict.
 */

import { Chess } from 'chess.js'
import { parseUciInfoLine } from '../workers/parseInfo'
import {
  admitInfoLine,
  createSnapshotAssembler,
  legacyDivergenceReason,
  selectAtomicSnapshot,
} from '../workers/pvSnapshots'
import type {
  LegacySelection,
  SnapshotAssembler,
  SnapshotDivergenceReason,
} from '../workers/pvSnapshots'
import type { EngineScore } from '../workers/stockfishMessages'
import type { BenchPhaseName, BenchPhaseRecord, BenchSnapshotOutcome } from './benchRecord'

/** `postLog`'s two prefixes (analysisWorker.ts:313, :317). */
const OUT_PREFIX = '[analysisWorker ->] '
const IN_PREFIX = '[analysisWorker <-] '

export type WorkerLogEntry = {
  direction: 'out' | 'in'
  text: string
}

/** Split one `{type:'log'}` message into direction + engine text, or null. */
export const parseWorkerLogLine = (message: string): WorkerLogEntry | null => {
  if (message.startsWith(OUT_PREFIX)) {
    return { direction: 'out', text: message.slice(OUT_PREFIX.length) }
  }
  if (message.startsWith(IN_PREFIX)) {
    return { direction: 'in', text: message.slice(IN_PREFIX.length) }
  }
  return null
}

/** `position fen <FEN> [moves m1 m2 ...]` — the only `position` form the worker sends. */
export const parsePositionCommand = (
  command: string,
): { fen: string; moves: string[] } | null => {
  if (!command.startsWith('position fen ')) {
    return null
  }
  const rest = command.slice('position fen '.length)
  const movesAt = rest.indexOf(' moves ')
  if (movesAt === -1) {
    return { fen: rest.trim(), moves: [] }
  }
  return {
    fen: rest.slice(0, movesAt).trim(),
    moves: rest
      .slice(movesAt + ' moves '.length)
      .trim()
      .split(/\s+/)
      .filter((move) => move.length > 0),
  }
}

/** `go depth N` → N. Null for any other limit, which this protocol never sends. */
export const parseGoDepth = (command: string): number | null => {
  const match = /^go\s+depth\s+(\d+)\b/.exec(command)
  return match ? Number(match[1]) : null
}

/**
 * Engine counters read straight off the raw line rather than through
 * `parseUciInfoLine`.
 *
 * Stockfish emits periodic stats-only lines (`info nodes ... nps ... time ...`)
 * that the worker's parser drops, because it returns null unless a line carries
 * depth, score, or pv. Those lines carry the freshest cumulative counters, so
 * phase totals read them directly. This affects reported cost only — snapshot
 * assembly still sees exactly the lines the worker sees.
 */
const readStat = (tokens: string[], token: string): number | null => {
  const index = tokens.indexOf(token)
  if (index === -1) {
    return null
  }
  const value = Number(tokens[index + 1])
  return Number.isFinite(value) ? value : null
}

/**
 * Whether a move ends the game in a given position — §4.2's exemption from the
 * two-move PV minimum. Mirrors `analysisWorker.makeEndsGame`, lazily and
 * defensively: the selector asks only about PVs shorter than two moves.
 */
const makeEndsGame = (fen: string, moves: string[]) => {
  let searchFen: string | null | undefined

  return (uci: string): boolean => {
    if (searchFen === undefined) {
      try {
        const chess = new Chess(fen)
        for (const move of moves) {
          chess.move({
            from: move.slice(0, 2),
            to: move.slice(2, 4),
            ...(move.length > 4 ? { promotion: move.slice(4, 5) } : {}),
          })
        }
        searchFen = chess.fen()
      } catch {
        searchFen = null
      }
    }
    if (searchFen === null) {
      return false
    }
    try {
      const chess = new Chess(searchFen)
      const played = chess.move({
        from: uci.slice(0, 2),
        to: uci.slice(2, 4),
        ...(uci.length > 4 ? { promotion: uci.slice(4, 5) } : {}),
      })
      return Boolean(played) && chess.isGameOver()
    } catch {
      return false
    }
  }
}

type OpenPhase = {
  index: number
  fen: string
  moves: string[]
  requestedDepth: number | null
  startMs: number
  infoLines: number
  admittedLines: number
  assembler: SnapshotAssembler
  /**
   * The legacy selector, mirrored exactly from `handleEngineLine` (:793-807):
   * three independently accumulated fields combined at `bestmove`. Mirrored
   * rather than imported because it lives inside the worker's message handler;
   * `transcript.test.ts` pins the mirror against a recorded transcript.
   */
  legacy: LegacySelection
  stats: {
    nodes: number | null
    timeMs: number | null
    nps: number | null
    hashfull: number | null
    seldepth: number | null
  }
  stopObserved: boolean
}

export type CollectedPhase = Omit<BenchPhaseRecord, 'name'> & {
  /** Resolved to a phase name once the caller knows the played and best moves. */
  moves: string[]
  legacyDivergence: SnapshotDivergenceReason | null
}

export type TranscriptCollector = {
  phases: CollectedPhase[]
  open: OpenPhase | null
  pendingPosition: { fen: string; moves: string[] } | null
  resetStartMs: number | null
  resetMs: number | null
  /** Engine lines seen before the first `ucinewgame` — the boot handshake. */
  bootLines: number
  /**
   * Engine (re)constructions observed while this collector was open.
   *
   * `analysisWorker.ensureEngine` sends `uci` exactly once per engine and nothing
   * else in the protocol sends it, so a `uci` seen DURING a move means the worker
   * tore its Stockfish sub-worker down and rebuilt it — its deadline-grace or
   * reset-timeout path. The worker reports that as a REQUEST-scoped error,
   * indistinguishable from a bad FEN, so the transcript is the only place a
   * harness can see it: the next measurement runs on a cold engine.
   */
  engineBoots: number
  /** Host clock at the last observed engine construction. */
  engineBootStartMs: number | null
}

export const createTranscriptCollector = (): TranscriptCollector => ({
  phases: [],
  open: null,
  pendingPosition: null,
  resetStartMs: null,
  resetMs: null,
  bootLines: 0,
  engineBoots: 0,
  engineBootStartMs: null,
})

const closePhase = (
  collector: TranscriptCollector,
  outcome: { bestmove: string | null; terminated: boolean },
  nowMs: number,
) => {
  const open = collector.open
  if (!open) {
    return
  }
  collector.open = null

  const base = {
    index: open.index,
    moves: open.moves,
    requestedDepth: open.requestedDepth,
    bestmove: outcome.bestmove,
    nodes: open.stats.nodes,
    timeMs: open.stats.timeMs,
    nps: open.stats.nps,
    hashfull: open.stats.hashfull,
    reachedDepth: open.legacy.reachedDepth,
    seldepth: open.stats.seldepth,
    wallMs: nowMs - open.startMs,
    infoLines: open.infoLines,
    admittedLines: open.admittedLines,
    terminated: outcome.terminated,
    stopObserved: open.stopObserved,
  }

  if (!outcome.terminated) {
    // The search was still running when the move ended — a worker error or a
    // harness timeout. Running the §4.2 selector on it would judge a truncated
    // search against the full requested depth and record a rejection the worker
    // never made; those counters decide §12 step 9, so they stay UNDEFINED here
    // rather than being filled in with a guess. The cost fields above are still
    // real and worth keeping.
    collector.phases.push({ ...base, snapshot: null, legacyDivergence: null })
    return
  }

  const requestedDepth = open.requestedDepth ?? 0
  const selection = selectAtomicSnapshot({
    assembler: open.assembler,
    requestedDepth,
    bestMove: outcome.bestmove ?? '',
    // A bench run leaves `MAX_ANALYSIS_MS` dormant and never cancels, so no
    // `stop` should ever appear. If one does, it is recorded and honoured rather
    // than assumed away: a truncated search must not be judged against the full
    // requested depth.
    capFired: open.stopObserved,
    stopReason: open.stopObserved ? 'deadline' : 'bestmove',
    endsGame: makeEndsGame(open.fen, open.moves),
  })

  const snapshot: BenchSnapshotOutcome = selection.accepted
    ? { accepted: true, depth: selection.depth }
    : { accepted: false, reason: selection.reason }

  collector.phases.push({
    ...base,
    snapshot,
    legacyDivergence: legacyDivergenceReason(selection, open.legacy),
  })
}

/**
 * Fold one log entry into the collector.
 *
 * `nowMs` is the HOST clock at receipt. postMessage preserves order, so phase
 * boundaries are exact; the host clock adds message-queue latency to `wallMs`,
 * which is why the engine's own `time` is recorded beside it.
 */
export const feedLog = (
  collector: TranscriptCollector,
  entry: WorkerLogEntry,
  nowMs: number,
): void => {
  if (entry.direction === 'out') {
    if (entry.text === 'uci') {
      // A new engine. During a move this is the worker rebuilding its own engine
      // (see TranscriptCollector.engineBoots).
      collector.engineBoots += 1
      collector.engineBootStartMs = nowMs
      return
    }
    if (entry.text === 'ucinewgame') {
      collector.resetStartMs = nowMs
      return
    }
    if (entry.text === 'stop') {
      if (collector.open) {
        collector.open.stopObserved = true
      }
      return
    }
    const position = parsePositionCommand(entry.text)
    if (position) {
      collector.pendingPosition = position
      return
    }
    if (entry.text.startsWith('go')) {
      // A `go` with no preceding `position` cannot happen against this worker;
      // fall back to an empty position so instrumentation never throws.
      const position = collector.pendingPosition ?? { fen: '', moves: [] }
      collector.pendingPosition = null
      collector.open = {
        index: collector.phases.length,
        fen: position.fen,
        moves: position.moves,
        requestedDepth: parseGoDepth(entry.text),
        startMs: nowMs,
        infoLines: 0,
        admittedLines: 0,
        // K = 1: today's protocol never raises MultiPV on the analyze path.
        assembler: createSnapshotAssembler(1),
        legacy: { score: null, pv: null, reachedDepth: null },
        stats: { nodes: null, timeMs: null, nps: null, hashfull: null, seldepth: null },
        stopObserved: false,
      }
    }
    return
  }

  if (entry.text === 'readyok') {
    // The init handshake also answers `readyok`; only a reset that this request
    // started (`ucinewgame` seen) closes a reset window.
    if (collector.resetStartMs !== null && collector.resetMs === null) {
      collector.resetMs = nowMs - collector.resetStartMs
    }
    return
  }

  if (entry.text.startsWith('bestmove')) {
    closePhase(
      collector,
      { bestmove: entry.text.split(/\s+/)[1] ?? null, terminated: true },
      nowMs,
    )
    return
  }

  if (!entry.text.startsWith('info')) {
    if (!collector.open && collector.resetStartMs === null) {
      collector.bootLines += 1
    }
    return
  }

  const open = collector.open
  if (!open) {
    return
  }
  open.infoLines += 1

  const tokens = entry.text.split(/\s+/)
  const nodes = readStat(tokens, 'nodes')
  if (nodes !== null) open.stats.nodes = nodes
  const timeMs = readStat(tokens, 'time')
  if (timeMs !== null) open.stats.timeMs = timeMs
  const nps = readStat(tokens, 'nps')
  if (nps !== null) open.stats.nps = nps
  const hashfull = readStat(tokens, 'hashfull')
  if (hashfull !== null) open.stats.hashfull = hashfull
  const seldepth = readStat(tokens, 'seldepth')
  if (seldepth !== null) open.stats.seldepth = seldepth

  const info = parseUciInfoLine(entry.text)
  if (!info) {
    return
  }

  // Legacy accumulators, mirroring analysisWorker.handleEngineLine exactly —
  // including that bounded lines DO update them, which is the gap §4.3 measures.
  if (info.depth !== undefined) {
    open.legacy.reachedDepth = info.depth
  }
  if (info.score) {
    open.legacy.score = info.score as EngineScore
  }
  if (info.pv && (info.multipv === undefined || info.multipv === 1)) {
    open.legacy.pv = info.pv
  }

  if (admitInfoLine(open.assembler, info)) {
    open.admittedLines += 1
  }
}

/**
 * Close the move and name its phases.
 *
 * Naming needs the final `bestMove`, which is only known once the worker posts
 * its result — hence a two-stage collector rather than naming at `go` time.
 * `played === best` legitimately produces two phases (production skips the
 * post-best search) and a terminal played move produces one; a short phase list
 * is data, not an error.
 */
export const finishMove = (
  collector: TranscriptCollector,
  context: { playedMove: string; bestMove: string | null },
  nowMs: number,
): { phases: BenchPhaseRecord[]; resetMs: number | null } => {
  // A worker error or a harness timeout can leave a search open with no
  // `bestmove`. It is recorded as an UNTERMINATED phase — its cost is real, its
  // §4 acceptance is undefined — rather than being judged as if it had finished.
  if (collector.open) {
    closePhase(collector, { bestmove: null, terminated: false }, nowMs)
  }

  const phases = collector.phases.map((phase): BenchPhaseRecord => {
    let name: BenchPhaseName = 'other'
    if (phase.moves.length === 0) {
      name = 'root'
    } else if (phase.moves.length === 1 && phase.moves[0] === context.playedMove) {
      name = 'post-played'
    } else if (
      phase.moves.length === 1 &&
      context.bestMove !== null &&
      phase.moves[0] === context.bestMove
    ) {
      name = 'post-best'
    }
    return { ...phase, name }
  })

  return { phases, resetMs: collector.resetMs }
}
