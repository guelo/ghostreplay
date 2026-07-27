/**
 * Checked-in position sets for the device runner (g-two-search-grade §10.4).
 *
 * NOT the §10.3 corpus — the 200-position corpus, its threshold/terminal/mate
 * coverage, and the 1.e4 and g-kgiq regressions belong to g-grade-corpus-harness.
 * These two sets exist so a device baseline is reproducible: a cooled thermal
 * sequence long enough for §10.4's latency-by-move-index graph, and a short
 * instrument check for a new device.
 *
 * Positions are stored as MOVES, not FENs, and expanded with chess.js. A wrong
 * FEN is a silent measurement of the wrong position; a wrong move list throws at
 * load and is caught by `positions.test.ts`.
 */

import { Chess } from 'chess.js'

export type BenchPosition = {
  /** Stable across runs and devices, so results join by position. */
  positionId: string
  fen: string
  /** UCI, as production sends it. */
  playedMove: string
  playerColor: 'white' | 'black'
  /** 1-based index within a thermal sequence; null for unordered sets. */
  thermalIndex: number | null
  label: string
}

export type BenchPositionSet = {
  id: string
  label: string
  positions: BenchPosition[]
  /**
   * Whether this set is consecutive plies of one game — §10.4's thermal sequence,
   * which is the only set the 40-move minimum and the by-move-index graph apply
   * to. A set of unrelated positions has neither.
   */
  isThermalSequence: boolean
}

/**
 * Kasparov–Topalov, Wijk aan Zee 1999, first 60 plies.
 *
 * One real game in move order: §10.4 wants a cooled sequence of at least 40
 * moves graphed by move index, and a real game gives the natural mix of quiet
 * and tactical positions plus a realistic P===B share (§3.4's `m`), which a set
 * of hand-picked positions would distort.
 */
const THERMAL_GAME_SAN = [
  'e4', 'd6', 'd4', 'Nf6', 'Nc3', 'g6', 'Be3', 'Bg7', 'Qd2', 'c6',
  'f3', 'b5', 'Nge2', 'Nbd7', 'Bh6', 'Bxh6', 'Qxh6', 'Bb7', 'a3', 'e5',
  'O-O-O', 'Qe7', 'Kb1', 'a6', 'Nc1', 'O-O-O', 'Nb3', 'exd4', 'Rxd4', 'c5',
  'Rd1', 'Nb6', 'g3', 'Kb8', 'Na5', 'Ba8', 'Bh3', 'd5', 'Qf4+', 'Ka7',
  'Rhe1', 'd4', 'Nd5', 'Nbxd5', 'exd5', 'Qd6', 'Rxd4', 'cxd4', 'Re7+', 'Kb6',
  'Qxd4+', 'Kxa5', 'b4+', 'Ka4', 'Qc3', 'Qxd5', 'Ra7', 'Bb7', 'Rxb7', 'Qc4',
] as const

/** §10.4's minimum thermal length. The stored game is longer; runs cap to this. */
export const DEFAULT_THERMAL_PLIES = 40

/**
 * The longest thermal sequence this set can produce.
 *
 * `buildThermalPositions` silently caps a longer request, so a run asking for
 * more would quietly measure this instead — the configuration guard bounds the
 * request by this number so the substitution cannot happen unnoticed.
 */
export const MAX_THERMAL_PLIES = THERMAL_GAME_SAN.length

/**
 * Replay the game into (position before the move, move played) pairs.
 *
 * Every ply is a measurement, so 40 plies is 40 analyze-moves — the graph's
 * x-axis is measurement order, which is what a thermal curve needs.
 */
export const buildThermalPositions = (plies = DEFAULT_THERMAL_PLIES): BenchPosition[] => {
  const chess = new Chess()
  const positions: BenchPosition[] = []
  const limit = Math.min(plies, THERMAL_GAME_SAN.length)

  for (let index = 0; index < limit; index += 1) {
    const san = THERMAL_GAME_SAN[index]
    const fen = chess.fen()
    const playerColor = chess.turn() === 'w' ? 'white' : 'black'
    const move = chess.move(san)
    if (!move) {
      throw new Error(`thermal game: illegal move ${san} at ply ${index + 1}`)
    }
    positions.push({
      positionId: `thermal:ply-${String(index + 1).padStart(3, '0')}`,
      fen,
      playedMove: `${move.from}${move.to}${move.promotion ?? ''}`,
      playerColor,
      thermalIndex: index + 1,
      label: `${Math.floor(index / 2) + 1}${playerColor === 'white' ? '.' : '...'} ${san}`,
    })
  }

  return positions
}

/**
 * Six positions covering the shapes that cost different amounts of search:
 * a best move (two searches), non-best moves (three searches), an endgame, and
 * a clear blunder. Enough to prove the instrument works on a new device in about
 * a minute, before committing to a thermal run.
 */
export const SMOKE_POSITIONS: BenchPosition[] = [
  {
    positionId: 'smoke:start-e4',
    fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
    playedMove: 'e2e4',
    playerColor: 'white',
    thermalIndex: null,
    label: 'start position, 1.e4',
  },
  {
    positionId: 'smoke:opening-best',
    fen: 'rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2',
    playedMove: 'b8c6',
    playerColor: 'black',
    thermalIndex: null,
    label: 'after 1.e4 e5 2.Nf3, 2...Nc6 (expected P===B)',
  },
  {
    positionId: 'smoke:midgame-quiet',
    fen: 'r1bq1rk1/ppp2ppp/2np1n2/2b1p3/2B1P3/2PP1N2/PP3PPP/RNBQ1RK1 w - - 2 7',
    playedMove: 'a2a3',
    playerColor: 'white',
    thermalIndex: null,
    label: 'quiet Italian middlegame, 7.a3',
  },
  {
    positionId: 'smoke:midgame-tactical',
    fen: 'r1bqk1nr/pppp1ppp/2n5/b7/2BpP3/2P2N2/P4PPP/RNBQ1RK1 b kq - 1 7',
    playedMove: 'd4c3',
    playerColor: 'black',
    thermalIndex: null,
    label: 'Evans Gambit tactics, 7...dxc3',
  },
  {
    positionId: 'smoke:endgame',
    fen: 'r3kbnr/1pp3pp/p1p1p3/8/3P4/8/PPP2PPP/RNB3K1 w kq - 0 11',
    playedMove: 'b1c3',
    playerColor: 'white',
    thermalIndex: null,
    label: 'Exchange Ruy endgame, 11.Nc3',
  },
  {
    positionId: 'smoke:blunder',
    fen: 'r1bq1rk1/ppp2ppp/2np1n2/2b1p3/2B1P3/2PP1N2/PP3PPP/RNBQ1RK1 w - - 2 7',
    playedMove: 'c4f7',
    playerColor: 'white',
    thermalIndex: null,
    label: 'quiet Italian middlegame, 7.Bxf7+?? (hangs the bishop)',
  },
]

export type BenchPositionSetId = 'smoke-6' | 'thermal-40'

export const buildPositionSet = (
  id: BenchPositionSetId,
  thermalPlies = DEFAULT_THERMAL_PLIES,
): BenchPositionSet =>
  id === 'smoke-6'
    ? {
        id,
        label: 'Smoke (6 positions)',
        positions: SMOKE_POSITIONS,
        isThermalSequence: false,
      }
    : {
        id,
        label: `Thermal sequence (${Math.min(thermalPlies, THERMAL_GAME_SAN.length)} plies)`,
        positions: buildThermalPositions(thermalPlies),
        isThermalSequence: true,
      }
