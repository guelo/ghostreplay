import { Chess } from 'chess.js'
import type { EngineScore } from '../../workers/stockfishMessages'

export const parseUciMove = (uci: string) => {
  if (!/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(uci)) return null
  return {
    from: uci.slice(0, 2),
    to: uci.slice(2, 4),
    ...(uci.length > 4 ? { promotion: uci.slice(4, 5) } : {}),
  }
}
export type TerminalMoveScore = {
  /** Exact score in the searched post-move, opponent-to-move frame. */
  postMove: EngineScore
  /** The same exact result in the pre-move root, mover-relative frame. */
  root: EngineScore
  outcome: 'checkmate' | 'stalemate' | 'insufficient-material' | 'draw'
}

/**
 * Complete terminal table for one legal move.
 *
 * A delivered checkmate is post-move mate 0 (the new side to move is mated) and
 * root mate +1 (the mover mates in one). Every non-checkmate terminal is a draw
 * in both frames, including stalemate and insufficient material.
 */
export const terminalScoreAfterMove = (
  fen: string,
  moveUci: string,
): TerminalMoveScore | null => {
  const move = parseUciMove(moveUci)
  if (!move) return null

  const chess = new Chess(fen)
  const played = chess.move(move)
  if (!played || !chess.isGameOver()) return null

  if (chess.isCheckmate()) {
    return {
      postMove: { type: 'mate', value: 0 },
      root: { type: 'mate', value: 1 },
      outcome: 'checkmate',
    }
  }

  const outcome = chess.isStalemate()
    ? 'stalemate'
    : chess.isInsufficientMaterial()
      ? 'insufficient-material'
      : 'draw'
  return {
    postMove: { type: 'cp', value: 0 },
    root: { type: 'cp', value: 0 },
    outcome,
  }
}

/** Convert an opponent-to-move search score into the mover's post-move frame. */
export const opponentToMoverScore = (score: EngineScore): EngineScore => ({
  type: score.type,
  value: score.value === 0 ? 0 : -score.value,
})

/** §5.2: mover-relative post-move score → mover-relative root score. */
export const postToRootScore = (post: EngineScore): EngineScore => {
  if (post.type === 'cp') return post
  return {
    type: 'mate',
    value: post.value >= 0 ? post.value + 1 : post.value,
  }
}

/** §5.2: mover-relative root score → mover-relative post-move score. */
export const rootToPostScore = (root: EngineScore): EngineScore => {
  if (root.type === 'cp') return root
  if (root.value === 0) {
    throw new Error('root mate 0 is invalid while a legal move exists')
  }
  return {
    type: 'mate',
    value: root.value > 0 ? root.value - 1 : root.value,
  }
}

export const positionAfterMoves = (fen: string, moves: readonly string[]): string => {
  const chess = new Chess(fen)
  for (const uci of moves) {
    const move = parseUciMove(uci)
    if (!move || !chess.move(move)) {
      throw new Error(`illegal UCI ${uci} after ${fen}`)
    }
  }
  return chess.fen()
}

export const moveEndsGame = (fen: string, moves: readonly string[], uci: string): boolean => {
  try {
    return terminalScoreAfterMove(positionAfterMoves(fen, moves), uci) !== null
  } catch {
    return false
  }
}
