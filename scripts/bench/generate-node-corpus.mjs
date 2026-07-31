#!/usr/bin/env node

/**
 * Deterministically rebuild the checked-in Node grading corpus.
 *
 * The generated JSON is reviewed and committed; the benchmark never regenerates
 * it implicitly. Keeping the recipe beside the data makes the 200-position set
 * reproducible without making the benchmark depend on mutable/random selection.
 */

import { readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { Chess } from 'chess.js'

const repoRoot = resolve(import.meta.dirname, '..', '..')
const ecoPath = resolve(repoRoot, 'public/data/openings/eco.json')
const outputPath = resolve(repoRoot, 'src/bench/node/corpus.json')

const toUci = (move) => `${move.from}${move.to}${move.promotion ?? ''}`

const playUci = (chess, uci) => chess.move({
  from: uci.slice(0, 2),
  to: uci.slice(2, 4),
  ...(uci.length > 4 ? { promotion: uci.slice(4, 5) } : {}),
})

const position = ({
  id,
  fen,
  playedMove,
  phase,
  tags,
  label,
  source,
}) => ({
  id,
  fen,
  playedMove,
  playerColor: fen.split(' ')[1] === 'w' ? 'white' : 'black',
  phase,
  tags,
  label,
  source,
})

const SPECIAL_POSITIONS = [
  position({
    id: 'regression-1e4',
    fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
    playedMove: 'e2e4',
    phase: 'opening',
    tags: ['quiet', 'target-best', 'regression-1e4', 'drill-0cp'],
    label: '1.e4 root/post-horizon regression',
    source: 'g-two-search-grade §10.3',
  }),
  position({
    id: 'regression-g-kgiq-nb6',
    fen: '2kr1b1r/pp1q4/2p5/P1P2p2/2NP2pp/8/1B3PPP/R2QR1K1 w - - 0 22',
    playedMove: 'c4b6',
    phase: 'middlegame',
    tags: ['tactical', 'target-excellent', 'regression-g-kgiq', 'depth-best-change'],
    label: 'g-kgiq 22.Nb6+ root/post ordering regression',
    source: 'session aa05b29f-4409-4713-bd9b-719cdefcdb68, move 22 white',
  }),
  position({
    id: 'terminal-mate-in-one',
    fen: '7k/5Q2/6K1/8/8/8/8/8 w - - 0 1',
    playedMove: 'f7f8',
    phase: 'endgame',
    tags: ['tactical', 'target-best', 'mate-winning', 'mate-in-1', 'checkmate', 'forced-mate-below-depth'],
    label: 'Qf8 checkmate',
    source: 'constructed terminal vector',
  }),
  position({
    id: 'terminal-stalemate',
    fen: 'k7/2Q5/2K5/8/8/8/8/8 w - - 0 1',
    playedMove: 'c7b6',
    phase: 'endgame',
    tags: ['quiet', 'target-blunder', 'stalemate', 'mate-losing', 'blunder-boundary-30pct'],
    label: 'Qb6 stalemate throws away a winning position',
    source: 'constructed terminal vector',
  }),
  position({
    id: 'terminal-insufficient-material',
    fen: '8/8/8/8/8/2k5/1b6/K1B5 w - - 0 1',
    playedMove: 'c1b2',
    phase: 'endgame',
    tags: ['quiet', 'target-best', 'insufficient-material'],
    label: 'Bxb2 leaves king and bishop versus king',
    source: 'constructed terminal vector',
  }),
  position({
    id: 'single-legal-move-block',
    fen: '7k/8/8/8/8/8/5RPP/r6K w - - 0 1',
    playedMove: 'f2f1',
    phase: 'endgame',
    tags: ['tactical', 'target-best', 'single-legal-move'],
    label: 'Rf1 is the only legal response to the rook check',
    source: 'constructed single-legal-move vector',
  }),
  position({
    id: 'mate-winning-preserved',
    fen: '6k1/5ppp/8/8/8/5Q2/5PPP/6K1 w - - 0 1',
    playedMove: 'f3a8',
    phase: 'endgame',
    tags: ['tactical', 'mate-winning', 'target-best'],
    label: 'Winning-mate candidate',
    source: 'constructed mate-transition vector',
  }),
  position({
    id: 'mate-winning-lost',
    fen: '6k1/5ppp/8/8/8/5Q2/5PPP/6K1 w - - 0 1',
    playedMove: 'f3f6',
    phase: 'endgame',
    tags: ['tactical', 'mate-losing', 'target-mistake', 'win-chance-20pct'],
    label: 'Winning-mate loss candidate',
    source: 'constructed mate-transition vector',
  }),
  position({
    id: 'mate-sign-change',
    fen: 'r3r1k1/ppp2ppp/2n5/8/8/2P2Q2/PP3PPP/R3R1K1 w - - 0 1',
    playedMove: 'f3f7',
    phase: 'middlegame',
    tags: ['tactical', 'mate-changing', 'target-blunder', 'win-chance-30pct'],
    label: 'Mate-sign-change candidate',
    source: 'constructed mate-transition vector',
  }),
  position({
    id: 'threshold-recording-50cp',
    fen: 'r1bq1rk1/ppp2ppp/2np1n2/2b1p3/2B1P3/2PP1N2/PP3PPP/RNBQ1RK1 w - - 2 7',
    playedMove: 'a2a3',
    phase: 'middlegame',
    tags: ['quiet', 'target-good', 'recording-50cp', 'drill-50cp'],
    label: '50cp recording/drill boundary candidate',
    source: 'Italian middlegame vector',
  }),
  position({
    id: 'threshold-drill-10cp',
    fen: 'r1bq1rk1/ppp2ppp/2np1n2/2b1p3/2B1P3/2PP1N2/PP3PPP/RNBQ1RK1 w - - 2 7',
    playedMove: 'h2h3',
    phase: 'middlegame',
    tags: ['quiet', 'target-excellent', 'drill-10cp', 'win-chance-02pct'],
    label: '10cp drill / 2% win-chance boundary candidate',
    source: 'Italian middlegame vector',
  }),
  position({
    id: 'threshold-drill-25cp',
    fen: 'r1bq1rk1/ppp2ppp/2np1n2/2b1p3/2B1P3/2PP1N2/PP3PPP/RNBQ1RK1 w - - 2 7',
    playedMove: 'b1d2',
    phase: 'middlegame',
    tags: ['quiet', 'target-good', 'drill-25cp', 'win-chance-10pct'],
    label: '25cp drill / 10% win-chance boundary candidate',
    source: 'Italian middlegame vector',
  }),
  position({
    id: 'threshold-tactical-blunder',
    fen: 'r1bq1rk1/ppp2ppp/2np1n2/2b1p3/2B1P3/2PP1N2/PP3PPP/RNBQ1RK1 w - - 2 7',
    playedMove: 'c4f7',
    phase: 'middlegame',
    tags: ['tactical', 'target-blunder', 'win-chance-30pct', 'blunder-boundary-30pct'],
    label: 'Bxf7 tactical blunder candidate',
    source: 'Italian middlegame vector',
  }),
]

const KASPAROV_TOPALOV_SAN = [
  'e4', 'd6', 'd4', 'Nf6', 'Nc3', 'g6', 'Be3', 'Bg7', 'Qd2', 'c6',
  'f3', 'b5', 'Nge2', 'Nbd7', 'Bh6', 'Bxh6', 'Qxh6', 'Bb7', 'a3', 'e5',
  'O-O-O', 'Qe7', 'Kb1', 'a6', 'Nc1', 'O-O-O', 'Nb3', 'exd4', 'Rxd4', 'c5',
  'Rd1', 'Nb6', 'g3', 'Kb8', 'Na5', 'Ba8', 'Bh3', 'd5', 'Qf4+', 'Ka7',
  'Rhe1', 'd4', 'Nd5', 'Nbxd5', 'exd5', 'Qd6', 'Rxd4', 'cxd4', 'Re7+', 'Kb6',
  'Qxd4+', 'Kxa5', 'b4+', 'Ka4', 'Qc3', 'Qxd5', 'Ra7', 'Bb7', 'Rxb7', 'Qc4',
]

const buildGamePositions = () => {
  const chess = new Chess()
  const positions = KASPAROV_TOPALOV_SAN.map((san, index) => {
    const fen = chess.fen()
    const move = chess.move(san)
    if (!move) throw new Error(`Kasparov–Topalov: illegal ${san} at ply ${index + 1}`)
    const phase = index < 15 ? 'opening' : 'middlegame'
    const tags = [
      index % 3 === 0 ? 'tactical' : 'quiet',
      `target-${['best', 'excellent', 'good', 'inaccuracy', 'mistake', 'blunder'][index % 6]}`,
    ]
    if (index % 13 === 0) tags.push('depth-best-change')
    return position({
      id: `kasparov-topalov-1999-ply-${String(index + 1).padStart(3, '0')}`,
      fen,
      playedMove: toUci(move),
      phase,
      tags,
      label: `${Math.floor(index / 2) + 1}${index % 2 === 0 ? '.' : '...'} ${san}`,
      source: 'Kasparov–Topalov, Wijk aan Zee 1999',
    })
  })
  // The first row is the named 1.e4 regression already pinned above.
  return positions.slice(1)
}

const buildEcoPositions = () => {
  const raw = JSON.parse(readFileSync(ecoPath, 'utf8'))
  const entries = raw.entries
  const chosen = []
  const seen = new Set()
  const gradeTargets = ['best', 'excellent', 'good', 'inaccuracy', 'mistake', 'blunder']

  // A prime stride spreads the set across ECO A00-E99 without a random seed.
  for (let cursor = 0; chosen.length < 120 && cursor < entries.length * 2; cursor += 1) {
    const entry = entries[(cursor * 127) % entries.length]
    const moves = entry.uci.split(' ')
    if (moves.length < 4 || moves.length > 24) continue

    const chess = new Chess()
    let valid = true
    for (const uci of moves.slice(0, -1)) {
      if (!playUci(chess, uci)) {
        valid = false
        break
      }
    }
    if (!valid || chess.isGameOver()) continue

    const bookMove = moves[moves.length - 1]
    const legal = chess.moves({ verbose: true })
      .map(toUci)
      .sort()
    if (!legal.includes(bookMove)) continue

    const gradeTarget = gradeTargets[chosen.length % gradeTargets.length]
    const alternatives = legal.filter((uci) => uci !== bookMove)
    const playedMove =
      gradeTarget === 'best' || gradeTarget === 'excellent' || alternatives.length === 0
        ? bookMove
        : alternatives[(chosen.length * 17) % alternatives.length]
    const key = `${chess.fen()}|${playedMove}`
    if (seen.has(key)) continue
    seen.add(key)

    const thresholdTags = [
      'win-chance-02pct',
      'win-chance-10pct',
      'win-chance-20pct',
      'win-chance-30pct',
      'recording-50cp',
      'drill-50cp',
    ]
    chosen.push(position({
      id: `eco-${String(chosen.length + 1).padStart(3, '0')}-${entry.eco.toLowerCase()}`,
      fen: chess.fen(),
      playedMove,
      phase: 'opening',
      tags: [
        chosen.length % 4 === 0 ? 'tactical' : 'quiet',
        `target-${gradeTarget}`,
        thresholdTags[chosen.length % thresholdTags.length],
        ...(chosen.length % 19 === 0 ? ['depth-best-change'] : []),
      ],
      label: `${entry.eco} ${entry.name}`,
      source: `JeffML/eco.json ${raw.source_commit}`,
    }))
  }

  if (chosen.length !== 120) {
    throw new Error(`expected 120 ECO positions, built ${chosen.length}`)
  }
  return chosen
}

const ENDGAME_SEEDS = [
  '8/5pk1/6p1/7p/4P3/5KP1/5P1P/8 w - - 0 1',
  '8/5pk1/6p1/7p/4P3/5KP1/4RP1P/7r w - - 0 1',
  '8/5pk1/4b1p1/7p/3BP3/5KP1/5P1P/8 w - - 0 1',
  '8/5pk1/4n1p1/7p/4P3/5KPN/5P1P/8 w - - 0 1',
]

const buildEndgamePositions = () => {
  const positions = []

  for (let seedIndex = 0; seedIndex < ENDGAME_SEEDS.length && positions.length < 32; seedIndex += 1) {
    const chess = new Chess(ENDGAME_SEEDS[seedIndex])
    for (let ply = 0; ply < 24 && positions.length < 32 && !chess.isGameOver(); ply += 1) {
      const legal = chess.moves({ verbose: true })
      if (legal.length === 0) break
      const move = legal[(seedIndex * 11 + ply * 7) % legal.length]
      const fen = chess.fen()
      positions.push(position({
        id: `constructed-endgame-${String(positions.length + 1).padStart(3, '0')}`,
        fen,
        playedMove: toUci(move),
        phase: 'endgame',
        tags: [
          move.captured || move.promotion ? 'tactical' : 'quiet',
          `target-${['best', 'excellent', 'good', 'inaccuracy', 'mistake', 'blunder'][positions.length % 6]}`,
          ...(legal.length === 1 ? ['single-legal-move'] : []),
        ],
        label: `Constructed endgame seed ${seedIndex + 1}, ply ${ply + 1}`,
        source: 'deterministic constructed endgame',
      }))
      chess.move(move)
    }
  }

  if (positions.length !== 32) {
    throw new Error(`expected 32 endgame positions, built ${positions.length}`)
  }
  return positions
}

const corpus = {
  schemaVersion: 1,
  description: 'GhostReplay two-search grading corpus (g-grade-corpus-harness)',
  positions: [
    ...SPECIAL_POSITIONS,
    ...buildEcoPositions(),
    ...buildGamePositions(),
    ...buildEndgamePositions(),
  ],
}

const duplicate = corpus.positions.find((row, index) =>
  corpus.positions.findIndex((other) => other.id === row.id) !== index)
if (duplicate) throw new Error(`duplicate id ${duplicate.id}`)

writeFileSync(outputPath, `${JSON.stringify(corpus, null, 2)}\n`)
console.log(`wrote ${corpus.positions.length} positions to ${outputPath}`)
