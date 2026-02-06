import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Chess } from 'chess.js'
import {
  getOpeningBook,
  lookupOpeningByFen,
  resetOpeningBookCacheForTests,
} from './openingBook'

const fetchMock = vi.fn()
vi.stubGlobal('fetch', fetchMock)

describe('getOpeningBook', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    resetOpeningBookCacheForTests()
  })

  it('loads and indexes entries by EPD', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        dataset: 'lichess-chess-openings',
        source_commit: 'abc123',
        entry_count: 1,
        entries: [
          {
            eco: 'C20',
            name: "King's Pawn Game",
            pgn: '1. e4',
            uci: 'e2e4',
            epd: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -',
          },
        ],
      }),
    })

    const book = await getOpeningBook()

    expect(book.dataset).toBe('lichess-chess-openings')
    expect(book.sourceCommit).toBe('abc123')
    expect(book.entries).toHaveLength(1)
    expect(book.byEpd.get('rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -')?.eco).toBe(
      'C20',
    )
    expect(
      book.byPosition.get('rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -')?.eco,
    ).toBe('C20')
  })

  it('caches the fetch result', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        dataset: 'lichess-chess-openings',
        source_commit: 'abc123',
        entry_count: 0,
        entries: [],
      }),
    })

    await getOpeningBook()
    await getOpeningBook()

    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('resets cache on failed fetch so callers can retry', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: 'Server Error',
    })
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        dataset: 'lichess-chess-openings',
        source_commit: 'abc123',
        entry_count: 0,
        entries: [],
      }),
    })

    await expect(getOpeningBook()).rejects.toThrow('Failed to load opening book (500 Server Error)')
    await expect(getOpeningBook()).resolves.toMatchObject({ dataset: 'lichess-chess-openings' })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})

describe('lookupOpeningByFen', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    resetOpeningBookCacheForTests()
  })

  it('prefers deeper lines for the same position and includes variation/source', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        dataset: 'lichess-chess-openings',
        source_commit: 'abc123',
        entry_count: 2,
        entries: [
          {
            eco: 'C20',
            name: "King's Pawn Game",
            pgn: '1. e4',
            uci: 'e2e4',
            epd: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -',
          },
          {
            eco: 'C50',
            name: 'Italian Game: Giuoco Piano',
            pgn: '1. e4 e5 2. Nf3 Nc6 3. Bc4',
            uci: 'e2e4 e7e5 g1f3 b8c6 f1c4',
            epd: 'r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq -',
          },
        ],
      }),
    })

    const opening = await lookupOpeningByFen(
      'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1',
    )

    expect(opening).toEqual({
      eco: 'C50',
      name: 'Italian Game: Giuoco Piano',
      variation: 'Giuoco Piano',
      source: 'eco',
    })
  })

  it('matches transposed positions and uses deterministic tie-breaks', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        dataset: 'lichess-chess-openings',
        source_commit: 'abc123',
        entry_count: 2,
        entries: [
          {
            eco: 'A46',
            name: 'Transposition Sample Beta',
            pgn: '1. d4 Nf6 2. c4 e6 3. Nc3 Bb4',
            uci: 'd2d4 g8f6 c2c4 e7e6 b1c3 f8b4',
            epd: 'rnbqk2r/pppp1ppp/4pn2/8/2PP4/2N5/PP2PPPP/R1BQKBNR w KQkq -',
          },
          {
            eco: 'A45',
            name: 'Transposition Sample Alpha',
            pgn: '1. c4 e6 2. Nc3 Nf6 3. d4 Bb4',
            uci: 'c2c4 e7e6 b1c3 g8f6 d2d4 f8b4',
            epd: 'rnbqk2r/pppp1ppp/4pn2/8/2PP4/2N5/PP2PPPP/R1BQKBNR w KQkq -',
          },
        ],
      }),
    })

    const board = new Chess()
    board.move('d4')
    board.move('Nf6')
    board.move('c4')
    board.move('e6')
    board.move('Nc3')
    board.move('Bb4')

    const opening = await lookupOpeningByFen(board.fen())

    expect(opening).toEqual({
      eco: 'A45',
      name: 'Transposition Sample Alpha',
      source: 'eco',
    })
  })

  it('returns null when no opening matches and memoizes lookups', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        dataset: 'lichess-chess-openings',
        source_commit: 'abc123',
        entry_count: 1,
        entries: [
          {
            eco: 'C20',
            name: "King's Pawn Game",
            pgn: '1. e4',
            uci: 'e2e4',
            epd: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -',
          },
        ],
      }),
    })

    const noMatchFen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
    await expect(lookupOpeningByFen(noMatchFen)).resolves.toBeNull()
    await expect(lookupOpeningByFen(noMatchFen)).resolves.toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
