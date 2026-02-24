import { useMemo } from 'react'

type MaterialDisplayProps = {
  fen: string
  perspective: 'white' | 'black'
}

const PIECE_VALUES: Record<string, number> = { p: 1, n: 3, b: 3, r: 5, q: 9 }
const STARTING_COUNTS: Record<string, number> = { p: 8, n: 2, b: 2, r: 2, q: 1 }
const PIECE_ORDER = ['p', 'n', 'b', 'r', 'q'] as const

// Unicode chess pieces: white captures black pieces (shown in black), black captures white pieces (shown in white)
const PIECE_CHARS: Record<string, { w: string; b: string }> = {
  p: { w: '♙', b: '♟' },
  n: { w: '♘', b: '♞' },
  b: { w: '♗', b: '♝' },
  r: { w: '♖', b: '♜' },
  q: { w: '♕', b: '♛' },
}

function parseMaterial(fen: string) {
  const placement = fen.split(' ')[0]
  const counts = { w: { ...STARTING_COUNTS }, b: { ...STARTING_COUNTS } }

  // Count pieces remaining on board, then captured = starting - remaining
  const remaining: Record<string, Record<string, number>> = {
    w: { p: 0, n: 0, b: 0, r: 0, q: 0 },
    b: { p: 0, n: 0, b: 0, r: 0, q: 0 },
  }

  for (const ch of placement) {
    if (ch === '/' || (ch >= '1' && ch <= '8')) continue
    const lower = ch.toLowerCase()
    if (!(lower in PIECE_VALUES)) continue
    const color = ch === lower ? 'b' : 'w'
    remaining[color][lower]++
  }

  // Captured by white = black pieces missing from board
  // Captured by black = white pieces missing from board
  const capturedByWhite: Record<string, number> = {}
  const capturedByBlack: Record<string, number> = {}
  for (const piece of PIECE_ORDER) {
    capturedByWhite[piece] = counts.b[piece] - remaining.b[piece]
    capturedByBlack[piece] = counts.w[piece] - remaining.w[piece]
  }

  return { capturedByWhite, capturedByBlack }
}

const MaterialDisplay = ({ fen, perspective }: MaterialDisplayProps) => {
  const { icons, score } = useMemo(() => {
    const { capturedByWhite, capturedByBlack } = parseMaterial(fen)
    const myCaptured = perspective === 'white' ? capturedByWhite : capturedByBlack
    const theirCaptured = perspective === 'white' ? capturedByBlack : capturedByWhite

    // Net advantage: surplus pieces I captured over what they captured, per type
    const iconList: string[] = []
    let totalScore = 0

    for (const piece of PIECE_ORDER) {
      const net = myCaptured[piece] - theirCaptured[piece]
      if (net > 0) {
        // Show icons in the color of the captured pieces (opponent's color)
        const capturedColor = perspective === 'white' ? 'b' : 'w'
        const char = PIECE_CHARS[piece][capturedColor]
        for (let i = 0; i < net; i++) {
          iconList.push(char)
        }
        totalScore += net * PIECE_VALUES[piece]
      }
    }

    return { icons: iconList, score: totalScore }
  }, [fen, perspective])

  return (
    <div className="material-display">
      {score > 0 && (
        <>
          <span className="material-icons">{icons.join('')}</span>
          <span className="material-score">+{score}</span>
        </>
      )}
    </div>
  )
}

export default MaterialDisplay
