/**
 * Checked-in position sets for the device runner (g-two-search-grade §10.4).
 *
 * NOT the §10.3 corpus — the 200-position corpus, its threshold/terminal/mate
 * coverage, and the 1.e4 and g-kgiq regressions belong to g-grade-corpus-harness.
 * These three sets exist so a device baseline is reproducible: a cooled thermal
 * sequence long enough for §10.4's latency-by-move-index graph, a short
 * instrument check for a new device, and the `P === B` cohort the mobile kill
 * gate (g-grade-kill-gate) is decided on.
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

/**
 * The engine's own depth-17 best move at each kept ply of the thermal game.
 *
 * Read out of the three committed thermal-40 baselines rather than re-derived:
 * all 40 positions agree across iPhone XR, Pixel 7 Pro and desktop Chromium with
 * zero errors, which is what single-threaded WASM at a fixed depth with a
 * `ucinewgame` per analyze-move gives you. `positions.test.ts` cross-checks
 * every entry back against `result.bestMove` in those files, so the corpus
 * cannot drift from the evidence that produced it.
 *
 * Every 4th ply is dropped, landing on 30 positions spread evenly from opening
 * to endgame — the kill gate needs about 30, and taking the first 30 would have
 * measured only the opening.
 */
const BEST_MOVE_DROP_STRIDE = 4

const RECORDED_BEST_MOVES: Readonly<Record<number, string>> = {
  1: 'e2e4', 2: 'e7e5', 3: 'd2d4', 5: 'b1c3', 6: 'e7e5',
  7: 'h2h3', 9: 'f2f3', 10: 'e8g8', 11: 'e3h6', 13: 'h2h4',
  14: 'b8d7', 15: 'a2a3', 17: 'd2h6', 18: 'a7a5', 19: 'g2g4',
  21: 'g2g4', 22: 'a7a5', 23: 'g2g4', 25: 'g2g4', 26: 'e5d4',
  27: 'h6e3', 29: 'd1d4', 30: 'd7b6', 31: 'd4d1', 33: 'g2g3',
  34: 'c8b8', 35: 'h6f4', 37: 'h6f4', 38: 'd6d5', 39: 'h1e1',
}

/** Which plies of the thermal game the best-move set covers. */
export const bestMovePlies = (): number[] =>
  Array.from({ length: DEFAULT_THERMAL_PLIES }, (_, index) => index + 1).filter(
    (ply) => ply % BEST_MOVE_DROP_STRIDE !== 0,
  )

/**
 * The `P === B` cohort, by construction under the CURRENT protocol
 * (g-grade-kill-gate §4).
 *
 * Same positions as the thermal sequence, with the played move REPLACED by the
 * engine's own recorded best move — so `pEqualsB` is true for every row of a
 * current-protocol run, which is the cohort §3.4 says Variant A can only lose
 * on. The gate is read off this fixed corpus, never off each arm's own
 * `p-equals-b` cell.
 *
 * Two identity fields are load-bearing and deliberately NOT copied from the
 * thermal rows:
 *
 * - `positionId` is `best30:`, never the `thermal:` id. These rows carry a
 *   DIFFERENT played move at the same FEN, and reusing the id would give two
 *   different measurements the same join key across files.
 * - `thermalIndex` is null and the set declares `isThermalSequence: false`.
 *   Retaining indices would let a set that is not a sequence be graphed as a
 *   thermal curve, and would fire the 40-ply method warning on it.
 */
export const buildBestMovePositions = (): BenchPosition[] => {
  const chess = new Chess()
  const positions: BenchPosition[] = []

  for (let index = 0; index < DEFAULT_THERMAL_PLIES; index += 1) {
    const ply = index + 1
    const bestMove = RECORDED_BEST_MOVES[ply]
    if (bestMove !== undefined) {
      const fen = chess.fen()
      const playerColor = chess.turn() === 'w' ? 'white' : 'black'
      // A throwaway copy: the game replay must continue down the GAME's moves,
      // not the engine's. An illegal entry throws at load, exactly as a wrong
      // thermal move does, rather than silently measuring another position.
      const probe = new Chess(fen)
      const played = probe.move({
        from: bestMove.slice(0, 2),
        to: bestMove.slice(2, 4),
        ...(bestMove.length > 4 ? { promotion: bestMove.slice(4, 5) } : {}),
      })
      if (!played) {
        throw new Error(`best-move set: illegal move ${bestMove} at ply ${ply}`)
      }
      positions.push({
        positionId: `best30:ply-${String(ply).padStart(3, '0')}`,
        fen,
        playedMove: bestMove,
        playerColor,
        thermalIndex: null,
        label: `${Math.floor(index / 2) + 1}${playerColor === 'white' ? '.' : '...'} ${played.san} (engine best)`,
      })
    }

    const san = THERMAL_GAME_SAN[index]
    if (!chess.move(san)) {
      throw new Error(`thermal game: illegal move ${san} at ply ${ply}`)
    }
  }

  return positions
}

export type BenchPositionSetId = 'smoke-6' | 'thermal-40' | 'best-30'

export const buildPositionSet = (
  id: BenchPositionSetId,
  thermalPlies = DEFAULT_THERMAL_PLIES,
): BenchPositionSet => {
  if (id === 'smoke-6') {
    return {
      id,
      label: 'Smoke (6 positions)',
      positions: SMOKE_POSITIONS,
      isThermalSequence: false,
    }
  }
  if (id === 'best-30') {
    const positions = buildBestMovePositions()
    return {
      id,
      label: `Best-move cohort (${positions.length} positions)`,
      positions,
      isThermalSequence: false,
    }
  }
  return {
    id,
    label: `Thermal sequence (${Math.min(thermalPlies, THERMAL_GAME_SAN.length)} plies)`,
    positions: buildThermalPositions(thermalPlies),
    isThermalSequence: true,
  }
}
