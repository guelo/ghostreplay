import { describe, expect, it } from 'vitest'
import {
  createTranscriptCollector,
  feedLog,
  finishMove,
  parseGoDepth,
  parsePositionCommand,
  parseWorkerLogLine,
} from './transcript'
import type { WorkerLogEntry } from './transcript'

const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'

/**
 * Replay a worker log transcript through the collector on a synthetic clock, so
 * phase wall times are exact and the tests never depend on real timing.
 */
const replay = (
  lines: string[],
  context: { playedMove: string; bestMove: string | null },
  msPerLine = 10,
) => {
  const collector = createTranscriptCollector()
  let clock = 0
  for (const line of lines) {
    clock += msPerLine
    const entry = parseWorkerLogLine(line)
    if (entry) {
      feedLog(collector, entry, clock)
    }
  }
  return finishMove(collector, context, clock + msPerLine)
}

const out = (command: string) => `[analysisWorker ->] ${command}`
const inbound = (line: string) => `[analysisWorker <-] ${line}`

/** A depth-3 search that iterates cleanly and ends on `bestmove`. */
const search = (
  fen: string,
  moves: string[],
  depth: number,
  pv: string[],
  opts: { nodesBase?: number; score?: string } = {},
) => {
  const nodesBase = opts.nodesBase ?? 1000
  const score = opts.score ?? 'cp 20'
  const movesSegment = moves.length > 0 ? ` moves ${moves.join(' ')}` : ''
  const lines = [out(`position fen ${fen}${movesSegment}`), out(`go depth ${depth}`)]
  for (let d = 1; d <= depth; d += 1) {
    lines.push(
      inbound(
        `info depth ${d} seldepth ${d + 2} multipv 1 score ${score} nodes ${nodesBase * d} nps 50000 time ${d * 100} pv ${pv.join(' ')}`,
      ),
    )
  }
  lines.push(inbound(`bestmove ${pv[0]}`))
  return lines
}

describe('parseWorkerLogLine', () => {
  it('splits the worker log prefixes by direction', () => {
    expect(parseWorkerLogLine(out('go depth 17'))).toEqual({
      direction: 'out',
      text: 'go depth 17',
    })
    expect(parseWorkerLogLine(inbound('readyok'))).toEqual({
      direction: 'in',
      text: 'readyok',
    })
    expect(parseWorkerLogLine('[analysisWorker] something else')).toBeNull()
  })
})

describe('parsePositionCommand', () => {
  it('separates the FEN from the moves segment', () => {
    expect(parsePositionCommand(`position fen ${START_FEN}`)).toEqual({
      fen: START_FEN,
      moves: [],
    })
    expect(parsePositionCommand(`position fen ${START_FEN} moves e2e4 e7e5`)).toEqual({
      fen: START_FEN,
      moves: ['e2e4', 'e7e5'],
    })
    expect(parsePositionCommand('position startpos')).toBeNull()
  })

  it('reads the requested depth off the go command', () => {
    expect(parseGoDepth('go depth 17')).toBe(17)
    expect(parseGoDepth('go movetime 500')).toBeNull()
  })
})

describe('phase segmentation', () => {
  it('names the three searches of a non-best move and totals their cost', () => {
    const lines = [
      out('ucinewgame'),
      out('isready'),
      inbound('readyok'),
      ...search(START_FEN, [], 3, ['d2d4', 'd7d5', 'g1f3']),
      ...search(START_FEN, ['e2e4'], 3, ['e7e5', 'g1f3', 'b8c6'], { nodesBase: 2000 }),
      ...search(START_FEN, ['d2d4'], 3, ['d7d5', 'c2c4', 'e7e6'], { nodesBase: 3000 }),
    ]

    const { phases, resetMs } = replay(lines, { playedMove: 'e2e4', bestMove: 'd2d4' })

    expect(phases.map((phase) => phase.name)).toEqual(['root', 'post-played', 'post-best'])
    expect(resetMs).toBe(20)
    expect(phases[0].nodes).toBe(3000)
    expect(phases[1].nodes).toBe(6000)
    expect(phases[2].nodes).toBe(9000)
    expect(phases.map((phase) => phase.timeMs)).toEqual([300, 300, 300])
    expect(phases.map((phase) => phase.requestedDepth)).toEqual([3, 3, 3])
    expect(phases.map((phase) => phase.reachedDepth)).toEqual([3, 3, 3])
    expect(phases.map((phase) => phase.bestmove)).toEqual(['d2d4', 'e7e5', 'd7d5'])
    // wall time spans `go` -> `bestmove`: four lines at 10ms each.
    expect(phases[0].wallMs).toBe(40)
  })

  it('records two phases when the played move is the best move', () => {
    const lines = [
      out('ucinewgame'),
      out('isready'),
      inbound('readyok'),
      ...search(START_FEN, [], 3, ['e2e4', 'e7e5', 'g1f3']),
      ...search(START_FEN, ['e2e4'], 3, ['e7e5', 'g1f3', 'b8c6']),
    ]

    const { phases } = replay(lines, { playedMove: 'e2e4', bestMove: 'e2e4' })

    expect(phases.map((phase) => phase.name)).toEqual(['root', 'post-played'])
  })

  it('records one phase when the played move is terminal', () => {
    const lines = [
      out('ucinewgame'),
      out('isready'),
      inbound('readyok'),
      ...search(START_FEN, [], 3, ['d2d4', 'd7d5', 'g1f3']),
    ]

    const { phases } = replay(lines, { playedMove: 'e2e4', bestMove: 'd2d4' })

    expect(phases).toHaveLength(1)
    expect(phases[0].name).toBe('root')
  })

  it('ignores the init handshake readyok that precedes any ucinewgame', () => {
    const lines = [
      inbound('readyok'),
      ...search(START_FEN, [], 3, ['d2d4', 'd7d5', 'g1f3']),
    ]

    expect(replay(lines, { playedMove: 'd2d4', bestMove: 'd2d4' }).resetMs).toBeNull()
  })

  it('leaves §4 acceptance undefined for a phase that never answered bestmove', () => {
    // A worker that dies mid-search, or a harness timeout. Judging the truncated
    // search against the full requested depth would record a `stale-depth`
    // rejection the worker never made — and those counters decide §12 step 9's
    // adoption verdict, so an unterminated phase reports NO acceptance at all.
    // Its cost is real and kept.
    const lines = [
      out('ucinewgame'),
      out('isready'),
      inbound('readyok'),
      out(`position fen ${START_FEN}`),
      out('go depth 17'),
      inbound('info depth 5 multipv 1 score cp 12 nodes 900 time 40 pv e2e4 e7e5'),
    ]

    const { phases } = replay(lines, { playedMove: 'e2e4', bestMove: null })

    expect(phases).toHaveLength(1)
    expect(phases[0].bestmove).toBeNull()
    expect(phases[0].terminated).toBe(false)
    expect(phases[0].snapshot).toBeNull()
    expect(phases[0].legacyDivergence).toBeNull()
    expect(phases[0].nodes).toBe(900)
    expect(phases[0].reachedDepth).toBe(5)
  })

  it('still reports acceptance for a search that did answer bestmove', () => {
    const lines = [
      out('ucinewgame'),
      out('isready'),
      inbound('readyok'),
      ...search(START_FEN, [], 3, ['e2e4', 'e7e5', 'g1f3']),
    ]

    const { phases } = replay(lines, { playedMove: 'e2e4', bestMove: 'e2e4' })

    expect(phases[0].terminated).toBe(true)
    expect(phases[0].snapshot).not.toBeNull()
  })

  it('records a stop issued while a search is open', () => {
    const lines = [
      out('ucinewgame'),
      out('isready'),
      inbound('readyok'),
      out(`position fen ${START_FEN}`),
      out('go depth 17'),
      inbound('info depth 5 multipv 1 score cp 12 nodes 900 time 40 pv e2e4 e7e5'),
      out('stop'),
      inbound('bestmove e2e4'),
    ]

    const { phases } = replay(lines, { playedMove: 'e2e4', bestMove: 'e2e4' })

    expect(phases[0].stopObserved).toBe(true)
    // §4.2 accepts a below-target depth only under the forced-mate exemption,
    // which requires `capFired` false. A stopped search is therefore honestly
    // `stale-depth` — recording the stop is what makes that attribution correct
    // rather than blaming the engine for a depth the harness cut short.
    expect(phases[0].snapshot).toEqual({ accepted: false, reason: 'stale-depth' })
  })
})

describe('§4 acceptance and §4.3 divergence over the transcript', () => {
  it('accepts a clean search and reports no divergence', () => {
    const lines = [...search(START_FEN, [], 3, ['e2e4', 'e7e5', 'g1f3'])]
    const { phases } = replay(lines, { playedMove: 'e2e4', bestMove: 'e2e4' })

    expect(phases[0].snapshot).toEqual({ accepted: true, depth: 3 })
    expect(phases[0].legacyDivergence).toBeNull()
    expect(phases[0].admittedLines).toBe(3)
  })

  it('does not admit bounded lines, and reports the divergence they cause', () => {
    // The legacy accumulators take the aspiration fail-high score (they do not
    // filter on bound); the atomic selector never admits it. That gap is exactly
    // what §4.3 measures.
    const lines = [
      out(`position fen ${START_FEN}`),
      out('go depth 2'),
      inbound('info depth 1 multipv 1 score cp 10 nodes 100 time 10 pv e2e4 e7e5'),
      inbound('info depth 2 multipv 1 score cp 20 nodes 200 time 20 pv e2e4 e7e5'),
      inbound('info depth 2 multipv 1 score cp 88 lowerbound nodes 260 time 24 pv e2e4'),
      inbound('bestmove e2e4'),
    ]

    const { phases } = replay(lines, { playedMove: 'e2e4', bestMove: 'e2e4' })

    expect(phases[0].infoLines).toBe(3)
    expect(phases[0].admittedLines).toBe(2)
    expect(phases[0].snapshot).toEqual({ accepted: true, depth: 2 })
    expect(phases[0].legacyDivergence).toBe('accepted')
  })

  it('treats a same-depth re-search as a new batch rather than merging it', () => {
    const lines = [
      out(`position fen ${START_FEN}`),
      out('go depth 2'),
      inbound('info depth 2 multipv 1 score cp 20 nodes 200 time 20 pv e2e4 e7e5'),
      // Aspiration re-search at the same depth: a later, better line wins.
      inbound('info depth 2 multipv 1 score cp 35 nodes 400 time 30 pv e2e4 c7c5'),
      inbound('bestmove e2e4'),
    ]

    const { phases } = replay(lines, { playedMove: 'e2e4', bestMove: 'e2e4' })

    expect(phases[0].snapshot).toEqual({ accepted: true, depth: 2 })
    // The re-search supersedes the pass it re-searched, and the legacy
    // accumulators land on the same line, so the selectors agree.
    expect(phases[0].legacyDivergence).toBeNull()
  })

  it('rejects a PV that does not start with the engine bestmove', () => {
    const lines = [
      out(`position fen ${START_FEN}`),
      out('go depth 2'),
      inbound('info depth 2 multipv 1 score cp 20 nodes 200 time 20 pv e2e4 e7e5'),
      inbound('bestmove d2d4'),
    ]

    const { phases } = replay(lines, { playedMove: 'd2d4', bestMove: 'd2d4' })

    expect(phases[0].snapshot).toEqual({ accepted: false, reason: 'pv-mismatch' })
    expect(phases[0].legacyDivergence).toBe('pv-mismatch')
  })

  it('exempts a one-move PV whose move ends the game', () => {
    // Fool's mate position: d1h5 is mate in one, so a single-move PV is correct.
    const fen = 'rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 2'
    const lines = [
      out(`position fen ${fen}`),
      out('go depth 2'),
      inbound('info depth 2 multipv 1 score mate 1 nodes 200 time 20 pv d8h4'),
      inbound('bestmove d8h4'),
    ]

    const { phases } = replay(lines, { playedMove: 'd8h4', bestMove: 'd8h4' })

    expect(phases[0].snapshot).toEqual({ accepted: true, depth: 2 })
  })
})

describe('log-only entries', () => {
  it('ignores engine lines that arrive with no search open', () => {
    const collector = createTranscriptCollector()
    const entries: WorkerLogEntry[] = [
      { direction: 'in', text: 'uciok' },
      { direction: 'in', text: 'info depth 1 score cp 5 pv e2e4' },
    ]
    entries.forEach((entry, index) => feedLog(collector, entry, index))

    expect(collector.phases).toHaveLength(0)
    expect(collector.open).toBeNull()
  })
})

describe('engine construction', () => {
  it('sees a mid-move engine rebuild that the worker only reports as a scoped error', () => {
    // `ensureEngine` sends `uci` exactly once per engine, so a `uci` here is the
    // worker's deadline-grace path tearing Stockfish down and rebuilding it. The
    // failure it posts is request-scoped, so this is the only signal a harness has
    // that the NEXT measurement runs on a cold engine.
    const collector = createTranscriptCollector()
    const entries: WorkerLogEntry[] = [
      { direction: 'out', text: 'ucinewgame' },
      { direction: 'out', text: 'isready' },
      { direction: 'in', text: 'readyok' },
      { direction: 'out', text: `position fen ${START_FEN}` },
      { direction: 'out', text: 'go depth 3' },
      { direction: 'out', text: 'stop' },
      { direction: 'out', text: 'uci' },
    ]
    entries.forEach((entry, index) => feedLog(collector, entry, index * 10))

    expect(collector.engineBoots).toBe(1)
    expect(collector.engineBootStartMs).toBe(60)
    // `uci` is not a search command: the open phase is untouched by it.
    expect(collector.open?.stopObserved).toBe(true)
  })

  it('counts no engine construction for an ordinary move', () => {
    const collector = createTranscriptCollector()
    for (const line of search(START_FEN, [], 3, ['e2e4', 'e7e5'])) {
      const entry = parseWorkerLogLine(line)
      if (entry) feedLog(collector, entry, 0)
    }

    expect(collector.engineBoots).toBe(0)
  })
})
