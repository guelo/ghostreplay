import { describe, it, expect } from 'vitest'
import {
  parseInfo,
  mateToCp,
  normalizeScore,
  toWhitePerspective,
  scoreForPlayer,
  getSideToMove,
  computeAnalysisResult,
  isBlunder,
  isWithinRecordingMoveCap,
  classifyMove,
  classifySessionMove,
  ANNOTATION_SYMBOL,
  BLUNDER_THRESHOLD,
  MOVE_LIST_BLUNDER_THRESHOLD,
} from './analysisUtils'
import type { EngineScore } from './stockfishMessages'

describe('parseInfo', () => {
  it('parses centipawn score from info line', () => {
    const result = parseInfo('info depth 18 score cp 45 nodes 123456 pv e2e4')

    expect(result).toEqual({ score: { type: 'cp', value: 45 } })
  })

  it('parses mate score from info line', () => {
    const result = parseInfo('info depth 20 score mate 3 pv e1g1')

    expect(result).toEqual({ score: { type: 'mate', value: 3 } })
  })

  it('parses negative centipawn score', () => {
    const result = parseInfo('info depth 15 score cp -120 nodes 50000')

    expect(result).toEqual({ score: { type: 'cp', value: -120 } })
  })

  it('parses negative mate score', () => {
    const result = parseInfo('info depth 20 score mate -2 pv e8d8')

    expect(result).toEqual({ score: { type: 'mate', value: -2 } })
  })

  it('returns null for non-info lines', () => {
    expect(parseInfo('bestmove e2e4')).toBeNull()
    expect(parseInfo('readyok')).toBeNull()
    expect(parseInfo('uciok')).toBeNull()
  })

  it('returns null when score keyword is missing', () => {
    const result = parseInfo('info depth 18 nodes 123456 pv e2e4')

    expect(result).toBeNull()
  })

  it('returns null for invalid score type', () => {
    const result = parseInfo('info depth 18 score invalid 45')

    expect(result).toBeNull()
  })

  it('returns null for non-numeric score value', () => {
    const result = parseInfo('info depth 18 score cp abc')

    expect(result).toBeNull()
  })

  it('parses zero centipawn score', () => {
    const result = parseInfo('info depth 10 score cp 0 pv d2d4')

    expect(result).toEqual({ score: { type: 'cp', value: 0 } })
  })

  it('parses mate in 1', () => {
    const result = parseInfo('info depth 25 score mate 1 pv d1h5')

    expect(result).toEqual({ score: { type: 'mate', value: 1 } })
  })
})

describe('mateToCp', () => {
  it('converts mate in 1 to high positive value', () => {
    // MATE_BASE(10000) - 1 * MATE_DECAY(10) = 9990
    expect(mateToCp(1)).toBe(9990)
  })

  it('converts mate in 3 to positive value', () => {
    // 10000 - 3 * 10 = 9970
    expect(mateToCp(3)).toBe(9970)
  })

  it('converts negative mate (being mated) to negative value', () => {
    // -(10000 - 1 * 10) = -9990
    expect(mateToCp(-1)).toBe(-9990)
  })

  it('converts being mated in 3 to negative value', () => {
    // -(10000 - 3 * 10) = -9970
    expect(mateToCp(-3)).toBe(-9970)
  })

  it('decays further mates less aggressively', () => {
    const mateIn1 = mateToCp(1) // 9990
    const mateIn5 = mateToCp(5) // 9950
    const mateIn10 = mateToCp(10) // 9900

    expect(mateIn1).toBeGreaterThan(mateIn5)
    expect(mateIn5).toBeGreaterThan(mateIn10)
    expect(mateIn1 - mateIn5).toBe(40) // 4 moves * 10 decay
  })

  it('handles mate in 0 (checkmate position — side to move is mated)', () => {
    // mate 0 means the position is checkmate; the side to move lost
    expect(mateToCp(0)).toBe(-10000)
  })

  it('preserves symmetry for positive and negative mates', () => {
    expect(mateToCp(5)).toBe(-mateToCp(-5))
  })

  it('always exceeds blunder threshold for any mate', () => {
    // Even mate in 100: 10000 - 100*10 = 9000 >> 50
    expect(Math.abs(mateToCp(100))).toBeGreaterThan(BLUNDER_THRESHOLD)
  })
})

describe('normalizeScore', () => {
  it('returns positive cp as-is when white to move', () => {
    const score: EngineScore = { type: 'cp', value: 50 }

    expect(normalizeScore(score, 'w')).toBe(50)
  })

  it('negates cp when black to move', () => {
    const score: EngineScore = { type: 'cp', value: 50 }

    // Stockfish scores are from side-to-move perspective.
    // When black to move and score is +50, it means black is better by 50cp.
    // From white's perspective that is -50.
    expect(normalizeScore(score, 'b')).toBe(-50)
  })

  it('converts mate score using mateToCp before normalizing', () => {
    const score: EngineScore = { type: 'mate', value: 3 }

    // mateToCp(3) = 9970, white to move → 9970
    expect(normalizeScore(score, 'w')).toBe(9970)
  })

  it('converts and negates mate score for black to move', () => {
    const score: EngineScore = { type: 'mate', value: 3 }

    // mateToCp(3) = 9970, black to move → -9970
    expect(normalizeScore(score, 'b')).toBe(-9970)
  })

  it('returns null for null score', () => {
    expect(normalizeScore(null, 'w')).toBeNull()
    expect(normalizeScore(null, 'b')).toBeNull()
  })

  it('handles zero centipawn score', () => {
    const score: EngineScore = { type: 'cp', value: 0 }

    expect(normalizeScore(score, 'w')).toBe(0)
    expect(normalizeScore(score, 'b')).toBe(-0)
  })
})

describe('scoreForPlayer', () => {
  it('returns white perspective for white player', () => {
    const score: EngineScore = { type: 'cp', value: 100 }

    // White to move, +100cp from engine = +100 for white player
    expect(scoreForPlayer(score, 'w', 'white')).toBe(100)
  })

  it('negates for black player', () => {
    const score: EngineScore = { type: 'cp', value: 100 }

    // White to move, +100cp = white is better. For black player, that's -100.
    expect(scoreForPlayer(score, 'w', 'black')).toBe(-100)
  })

  it('handles black to move with white player', () => {
    const score: EngineScore = { type: 'cp', value: 50 }

    // Black to move, +50cp from engine = black is better.
    // normalizeScore(50, 'b') = -50 (white perspective)
    // For white player: -50
    expect(scoreForPlayer(score, 'b', 'white')).toBe(-50)
  })

  it('handles black to move with black player', () => {
    const score: EngineScore = { type: 'cp', value: 50 }

    // Black to move, +50cp from engine = black is better.
    // normalizeScore(50, 'b') = -50 (white perspective)
    // For black player: -(-50) = 50
    expect(scoreForPlayer(score, 'b', 'black')).toBe(50)
  })

  it('returns null for null score', () => {
    expect(scoreForPlayer(null, 'w', 'white')).toBeNull()
    expect(scoreForPlayer(null, 'b', 'black')).toBeNull()
  })

  it('converts mate scores from player perspective', () => {
    const mateIn2: EngineScore = { type: 'mate', value: 2 }

    // White to move, mate in 2 = great for side to move (white)
    // mateToCp(2) = 9980, normalizeScore = 9980
    // For white player: 9980
    expect(scoreForPlayer(mateIn2, 'w', 'white')).toBe(9980)
    // For black player: -9980
    expect(scoreForPlayer(mateIn2, 'w', 'black')).toBe(-9980)
  })
})

describe('toWhitePerspective', () => {
  it('keeps eval unchanged for white move indices', () => {
    expect(toWhitePerspective(120, 0)).toBe(120)
    expect(toWhitePerspective(-80, 2)).toBe(-80)
  })

  it('flips eval sign for black move indices', () => {
    expect(toWhitePerspective(120, 1)).toBe(-120)
    expect(toWhitePerspective(-80, 3)).toBe(80)
  })

  it('returns input unchanged for null or unknown move index', () => {
    expect(toWhitePerspective(45, null)).toBe(45)
    expect(toWhitePerspective(45, undefined)).toBe(45)
    expect(toWhitePerspective(null, 1)).toBeNull()
  })
})

describe('getSideToMove', () => {
  it('returns w for white to move', () => {
    const fen = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1'

    // This FEN has 'b' as the active color
    expect(getSideToMove(fen)).toBe('b')
  })

  it('returns w for starting position', () => {
    const fen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'

    expect(getSideToMove(fen)).toBe('w')
  })

  it('returns null for invalid FEN without active color', () => {
    expect(getSideToMove('invalid-fen')).toBeNull()
  })

  it('returns null for FEN with invalid active color', () => {
    expect(getSideToMove('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR x KQkq - 0 1')).toBeNull()
  })

  it('handles FEN with only board part', () => {
    expect(getSideToMove('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR')).toBeNull()
  })
})

describe('computeAnalysisResult', () => {
  it('returns delta zero when played move is the best move despite eval disagreement', () => {
    // Reproduces the 1.e4 false-blunder bug: the pre-move minimax search
    // returned +97cp but the post-move search returned cp:-29 from black's
    // POV (= +29 for white). The old code compared these two independent
    // search results directly: 97 - 29 = 68 >= 50 threshold → false blunder.
    //
    // The fix: when bestMove === playedMove, postBestScore IS postPlayedScore
    // (same search), so delta is always 0.
    const postPlayedScore: EngineScore = { type: 'cp', value: -29 }

    const result = computeAnalysisResult({
      bestMove: 'e2e4',
      playedMove: 'e2e4',
      postPlayedScore,
      postBestScore: postPlayedScore,
      sideToMove: 'w',
      playerColor: 'white',
    })

    expect(result.playedEval).toBe(29)
    expect(result.bestEval).toBe(29)
    expect(result.delta).toBe(0)
  })

  it('computes delta from post-move evals, not pre-move minimax', () => {
    // White played e4, but best move was d4.
    // Old code used the pre-move minimax eval (+90cp) as bestEval — a search
    // artifact from depth/timing variance in WASM. The post-d4 search returns
    // cp:-30 from black's POV (= +30 for white), and the post-e4 search
    // returns cp:-20 (= +20 for white).
    //
    // Old (buggy): delta = 90 - 20 = 70 → false blunder
    // New (fixed): delta = 30 - 20 = 10 → correct small inaccuracy
    const postBestScore: EngineScore = { type: 'cp', value: -30 }
    const postPlayedScore: EngineScore = { type: 'cp', value: -20 }

    const result = computeAnalysisResult({
      bestMove: 'd2d4',
      playedMove: 'e2e4',
      postPlayedScore,
      postBestScore,
      sideToMove: 'w',
      playerColor: 'white',
    })

    expect(result.bestEval).toBe(30)
    expect(result.playedEval).toBe(20)
    expect(result.delta).toBe(10)
  })

  it('converts scores from opponent perspective for black player', () => {
    // Black played e5, best was d5. After d5 white to move: cp:10.
    // After e5 white to move: cp:40.
    const postBestScore: EngineScore = { type: 'cp', value: 10 }
    const postPlayedScore: EngineScore = { type: 'cp', value: 40 }

    const result = computeAnalysisResult({
      bestMove: 'd7d5',
      playedMove: 'e7e5',
      postPlayedScore,
      postBestScore,
      sideToMove: 'b',
      playerColor: 'black',
    })

    // normalizeScore(10, 'w') = 10, black player → -10
    // normalizeScore(40, 'w') = 40, black player → -40
    // delta = -10 - (-40) = 30
    expect(result.bestEval).toBe(-10)
    expect(result.playedEval).toBe(-40)
    expect(result.delta).toBe(30)
  })

  it('returns null delta when score is missing', () => {
    const result = computeAnalysisResult({
      bestMove: 'd2d4',
      playedMove: 'e2e4',
      postPlayedScore: null,
      postBestScore: { type: 'cp', value: -30 },
      sideToMove: 'w',
      playerColor: 'white',
    })

    expect(result.delta).toBeNull()
  })
})

describe('isBlunder', () => {
  it('returns true when delta equals threshold', () => {
    expect(isBlunder(50)).toBe(true)
  })

  it('returns true when delta exceeds threshold', () => {
    expect(isBlunder(51)).toBe(true)
    expect(isBlunder(500)).toBe(true)
    expect(isBlunder(9990)).toBe(true)
  })

  it('returns false when delta is below threshold', () => {
    expect(isBlunder(49)).toBe(false)
    expect(isBlunder(10)).toBe(false)
    expect(isBlunder(0)).toBe(false)
  })

  it('returns false for negative delta', () => {
    expect(isBlunder(-50)).toBe(false)
    expect(isBlunder(-200)).toBe(false)
  })

  it('returns false for null delta', () => {
    expect(isBlunder(null)).toBe(false)
  })

  it('threshold constant is 50', () => {
    expect(BLUNDER_THRESHOLD).toBe(50)
  })

  describe('context-aware with preEval', () => {
    it('uses base threshold in equal positions', () => {
      expect(isBlunder(50, 0)).toBe(true)
      expect(isBlunder(49, 0)).toBe(false)
    })

    it('uses base threshold when player is winning', () => {
      expect(isBlunder(50, 300)).toBe(true)
      expect(isBlunder(49, 300)).toBe(false)
    })

    it('raises threshold when player is losing', () => {
      // At preEval -500: threshold = 50 + 500*0.1 = 100
      expect(isBlunder(99, -500)).toBe(false)
      expect(isBlunder(100, -500)).toBe(true)
    })

    it('raises threshold further when deeply losing', () => {
      // At preEval -1000: threshold = 50 + 1000*0.1 = 150
      expect(isBlunder(149, -1000)).toBe(false)
      expect(isBlunder(150, -1000)).toBe(true)
    })

    it('still rejects deltas below base threshold regardless of preEval', () => {
      expect(isBlunder(30, 0)).toBe(false)
      expect(isBlunder(30, -1000)).toBe(false)
    })

    it('falls back to base threshold when preEval is null', () => {
      expect(isBlunder(50, null)).toBe(true)
      expect(isBlunder(49, null)).toBe(false)
    })

    it('falls back to base threshold when preEval is undefined', () => {
      expect(isBlunder(50, undefined)).toBe(true)
      expect(isBlunder(49, undefined)).toBe(false)
    })
  })
})

describe('isWithinRecordingMoveCap', () => {
  it('includes all moves through full move 10', () => {
    expect(isWithinRecordingMoveCap(0)).toBe(true)
    expect(isWithinRecordingMoveCap(19)).toBe(true)
  })

  it('excludes moves after full move 10', () => {
    expect(isWithinRecordingMoveCap(20)).toBe(false)
  })

  it('returns false for null/undefined indices', () => {
    expect(isWithinRecordingMoveCap(null)).toBe(false)
    expect(isWithinRecordingMoveCap(undefined)).toBe(false)
  })
})

describe('classifyMove', () => {
  it('returns null for null delta', () => {
    expect(classifyMove(null)).toBeNull()
  })

  it('classifies blunder at threshold', () => {
    expect(classifyMove(MOVE_LIST_BLUNDER_THRESHOLD)).toBe('blunder')
  })

  it('classifies blunder above threshold', () => {
    expect(classifyMove(300)).toBe('blunder')
  })

  it('classifies inaccuracy between 50 and threshold', () => {
    expect(classifyMove(50)).toBe('inaccuracy')
    expect(classifyMove(100)).toBe('inaccuracy')
    expect(classifyMove(149)).toBe('inaccuracy')
  })

  it('classifies best move at zero delta', () => {
    expect(classifyMove(0)).toBe('best')
  })

  it('classifies good move between 0 and 50', () => {
    expect(classifyMove(1)).toBe('good')
    expect(classifyMove(25)).toBe('good')
    expect(classifyMove(49)).toBe('good')
  })

  it('classifies great move for negative delta', () => {
    expect(classifyMove(-1)).toBe('great')
    expect(classifyMove(-50)).toBe('great')
    expect(classifyMove(-200)).toBe('great')
  })
})

describe('ANNOTATION_SYMBOL', () => {
  it('maps blunder to ??', () => {
    expect(ANNOTATION_SYMBOL.blunder).toBe('??')
  })

  it('maps inaccuracy to ?!', () => {
    expect(ANNOTATION_SYMBOL.inaccuracy).toBe('?!')
  })

  it('maps good to empty string', () => {
    expect(ANNOTATION_SYMBOL.good).toBe('')
  })

  it('maps great to !', () => {
    expect(ANNOTATION_SYMBOL.great).toBe('!')
  })

  it('maps best to !', () => {
    expect(ANNOTATION_SYMBOL.best).toBe('!')
  })
})

describe('classifySessionMove', () => {
  it('returns null for null delta', () => {
    expect(classifySessionMove(null)).toBeNull()
  })

  it('classifies best at zero and negative deltas', () => {
    expect(classifySessionMove(0)).toBe('best')
    expect(classifySessionMove(-1)).toBe('best')
    expect(classifySessionMove(-250)).toBe('best')
  })

  it('classifies excellent between 1 and 10', () => {
    expect(classifySessionMove(1)).toBe('excellent')
    expect(classifySessionMove(10)).toBe('excellent')
  })

  it('classifies good between 11 and 50', () => {
    expect(classifySessionMove(11)).toBe('good')
    expect(classifySessionMove(50)).toBe('good')
  })

  it('classifies inaccuracy between 51 and 100', () => {
    expect(classifySessionMove(51)).toBe('inaccuracy')
    expect(classifySessionMove(100)).toBe('inaccuracy')
  })

  it('classifies mistake between 101 and 149', () => {
    expect(classifySessionMove(101)).toBe('mistake')
    expect(classifySessionMove(149)).toBe('mistake')
  })

  it('classifies blunder at and above 150', () => {
    expect(classifySessionMove(150)).toBe('blunder')
    expect(classifySessionMove(400)).toBe('blunder')
  })
})
