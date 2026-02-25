import { describe, expect, it } from 'vitest'
import { render, screen } from '../test/utils'
import MaterialDisplay, { parseMaterial } from './MaterialDisplay'

// Standard starting position
const STARTING_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'

// After 1. e4 e5 2. Qh5 Nc6 3. Qxf7# (scholar's mate-ish, white captured f7 pawn)
const WHITE_UP_PAWN = 'r1bqkbnr/pppp1Qpp/2n5/4p3/4P3/8/PPPP1PPP/RNB1KBNR b KQkq - 0 3'

// White captured a queen, black captured a bishop: white ahead by +6 net
const WHITE_UP_QUEEN_DOWN_BISHOP =
  'rnb1kbnr/pppppppp/8/8/8/8/PPPPPPPP/RN1QKBNR w KQkq - 0 1'
// ^ black queen missing, white bishop missing

// Both sides traded a knight — equal
const EQUAL_TRADE = 'r1bqkbnr/pppppppp/8/8/8/8/PPPPPPPP/R1BQKBNR w KQkq - 0 1'
// ^ both lost one knight

// Black up a rook (+5)
const BLACK_UP_ROOK = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/1NBQKBNR w Kkq - 0 1'
// ^ white rook on a1 missing

describe('parseMaterial', () => {
  it('returns zero captures at starting position', () => {
    const { capturedByWhite, capturedByBlack } = parseMaterial(STARTING_FEN)
    for (const piece of ['p', 'n', 'b', 'r', 'q']) {
      expect(capturedByWhite[piece]).toBe(0)
      expect(capturedByBlack[piece]).toBe(0)
    }
  })

  it('detects white captured a pawn', () => {
    const { capturedByWhite } = parseMaterial(WHITE_UP_PAWN)
    expect(capturedByWhite.p).toBe(1)
  })

  it('detects missing pieces on both sides', () => {
    const { capturedByWhite, capturedByBlack } = parseMaterial(WHITE_UP_QUEEN_DOWN_BISHOP)
    expect(capturedByWhite.q).toBe(1) // white captured black's queen
    expect(capturedByBlack.b).toBe(1) // black captured white's bishop
  })

  it('detects equal knight trade', () => {
    const { capturedByWhite, capturedByBlack } = parseMaterial(EQUAL_TRADE)
    expect(capturedByWhite.n).toBe(1)
    expect(capturedByBlack.n).toBe(1)
  })
})

describe('MaterialDisplay', () => {
  it('renders nothing at starting position', () => {
    const { container } = render(
      <MaterialDisplay fen={STARTING_FEN} perspective="white" />,
    )
    expect(container.querySelector('.material-icons')).toBeNull()
    expect(container.querySelector('.material-score')).toBeNull()
  })

  it('renders no score for the losing side', () => {
    const { container } = render(
      <MaterialDisplay fen={WHITE_UP_PAWN} perspective="black" />,
    )
    expect(container.querySelector('.material-score')).toBeNull()
  })

  it('shows +1 when white is up a pawn', () => {
    render(<MaterialDisplay fen={WHITE_UP_PAWN} perspective="white" />)
    expect(screen.getByText('+1')).toBeInTheDocument()
  })

  it('shows icons as black pieces when white is ahead (captured black pieces)', () => {
    render(<MaterialDisplay fen={WHITE_UP_PAWN} perspective="white" />)
    // ♟ is the black pawn unicode char
    expect(screen.getByText('♟')).toBeInTheDocument()
  })

  it('shows +6 when white is up a queen but down a bishop (9-3=6)', () => {
    render(
      <MaterialDisplay fen={WHITE_UP_QUEEN_DOWN_BISHOP} perspective="white" />,
    )
    expect(screen.getByText('+6')).toBeInTheDocument()
  })

  it('renders nothing for equal trades', () => {
    const { container } = render(
      <MaterialDisplay fen={EQUAL_TRADE} perspective="white" />,
    )
    expect(container.querySelector('.material-score')).toBeNull()
  })

  it('renders nothing for equal trades (black perspective)', () => {
    const { container } = render(
      <MaterialDisplay fen={EQUAL_TRADE} perspective="black" />,
    )
    expect(container.querySelector('.material-score')).toBeNull()
  })

  it('shows +5 for black when black is up a rook', () => {
    render(<MaterialDisplay fen={BLACK_UP_ROOK} perspective="black" />)
    expect(screen.getByText('+5')).toBeInTheDocument()
  })

  it('shows white piece icon when black is ahead (captured white pieces)', () => {
    render(<MaterialDisplay fen={BLACK_UP_ROOK} perspective="black" />)
    // ♖ is the white rook unicode char
    expect(screen.getByText('♖')).toBeInTheDocument()
  })

  it('only the winning side shows score, but both show icons', () => {
    const { container: whiteContainer } = render(
      <MaterialDisplay fen={WHITE_UP_QUEEN_DOWN_BISHOP} perspective="white" />,
    )
    const { container: blackContainer } = render(
      <MaterialDisplay fen={WHITE_UP_QUEEN_DOWN_BISHOP} perspective="black" />,
    )
    // White should show score
    expect(whiteContainer.querySelector('.material-score')).not.toBeNull()
    // Black should show icons (captured bishop) but no score
    expect(blackContainer.querySelector('.material-icons')).not.toBeNull()
    expect(blackContainer.querySelector('.material-score')).toBeNull()
  })
})
