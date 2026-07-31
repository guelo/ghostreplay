import { Chess } from 'chess.js'
import { describe, expect, it } from 'vitest'
import corpusJson from './corpus.json'
import {
  corpusProblems,
  corpusSha256,
  loadCorpus,
  MIN_CORPUS_POSITIONS,
} from './corpus'

const play = (fen: string, uci: string): Chess => {
  const chess = new Chess(fen)
  chess.move({
    from: uci.slice(0, 2),
    to: uci.slice(2, 4),
    ...(uci.length > 4 ? { promotion: uci.slice(4, 5) } : {}),
  })
  return chess
}

describe('Node grading corpus', () => {
  it('is a legal, unique, coverage-complete checked-in corpus', () => {
    expect(corpusProblems(corpusJson)).toEqual([])

    const corpus = loadCorpus()
    expect(corpus.positions).toHaveLength(224)
    expect(corpus.positions.length).toBeGreaterThanOrEqual(MIN_CORPUS_POSITIONS)
    expect(corpusSha256()).toBe(
      '492396187f4f8aae926fca4af1942b96cef2f4024918c4b5809081279dfb24a4',
    )
  })

  it('pins the 1.e4 and g-kgiq regression inputs exactly', () => {
    const corpus = loadCorpus()
    const e4 = corpus.positions.find((row) => row.id === 'regression-1e4')
    const nb6 = corpus.positions.find((row) => row.id === 'regression-g-kgiq-nb6')

    expect(e4).toMatchObject({
      fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
      playedMove: 'e2e4',
      playerColor: 'white',
    })
    expect(nb6).toMatchObject({
      fen: '2kr1b1r/pp1q4/2p5/P1P2p2/2NP2pp/8/1B3PPP/R2QR1K1 w - - 0 22',
      playedMove: 'c4b6',
      playerColor: 'white',
    })
    expect(new Chess(nb6?.fen).moves()).toContain('Nb6+')
  })

  it('proves the terminal tags against chess.js outcomes', () => {
    const corpus = loadCorpus()
    const byId = (id: string) => {
      const row = corpus.positions.find((candidate) => candidate.id === id)
      if (!row) throw new Error(`missing fixture ${id}`)
      return row
    }

    const mate = byId('terminal-mate-in-one')
    expect(play(mate.fen, mate.playedMove).isCheckmate()).toBe(true)

    const stalemate = byId('terminal-stalemate')
    expect(play(stalemate.fen, stalemate.playedMove).isStalemate()).toBe(true)

    const insufficient = byId('terminal-insufficient-material')
    expect(play(insufficient.fen, insufficient.playedMove).isInsufficientMaterial()).toBe(true)

    const only = byId('single-legal-move-block')
    const onlyChess = new Chess(only.fen)
    expect(onlyChess.moves({ verbose: true }).map((move) => `${move.from}${move.to}`)).toEqual([
      only.playedMove,
    ])
  })

  it('keeps coverage intent separate from measured truth', () => {
    const corpus = loadCorpus()
    const targeted = corpus.positions.filter((row) =>
      row.tags.some((tag) => tag.startsWith('target-')))

    // The tags make every intended bucket discoverable; no expected engine score
    // or classification is stored in the corpus. Those belong to adjudication.
    expect(targeted.length).toBe(corpus.positions.length)
    for (const row of targeted) {
      expect(row).not.toHaveProperty('expectedClassification')
      expect(row).not.toHaveProperty('expectedDelta')
    }
  })
})
