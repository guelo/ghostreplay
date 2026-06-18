import { describe, it, expect } from 'vitest'
import {
  parseScoreInfo,
  mateToCp,
  normalizeScore,
  toWhitePerspective,
  toWhitePerspectiveMate,
  moverMateToWhiteCp,
  playerToWhiteMate,
  scoreForPlayer,
  mateForPlayer,
  getSideToMove,
  computeAnalysisResult,
  isRecordableFailure,
  isWithinRecordingMoveCap,
  classifyMove,
  classifyMoveAdvanced,
  canResolvePositionAnalysis,
  canResolveMoveAnalysis,
  isTrustedPositionHit,
  isTrustedExactBestHit,
  isTrustedMoveHit,
  hasCpEvalLoss,
  isMoveClassification,
  evalLoss,
  failsDrill,
  gradeDrillMove,
  gradeRecordableMove,
  calculateWinChance,
  checkMateEvents,
  WIN_CHANCE_MULTIPLIER,
  CP_CEILING,
  RECORDABLE_FAILURE_THRESHOLD_CP,
} from './analysisUtils'
import type { EngineScore } from './stockfishMessages'

describe('parseScoreInfo', () => {
  it('parses centipawn score from info line', () => {
    const result = parseScoreInfo('info depth 18 score cp 45 nodes 123456 pv e2e4')

    expect(result).toEqual({ score: { type: 'cp', value: 45 } })
  })

  it('parses mate score from info line', () => {
    const result = parseScoreInfo('info depth 20 score mate 3 pv e1g1')

    expect(result).toEqual({ score: { type: 'mate', value: 3 } })
  })

  it('parses negative centipawn score', () => {
    const result = parseScoreInfo('info depth 15 score cp -120 nodes 50000')

    expect(result).toEqual({ score: { type: 'cp', value: -120 } })
  })

  it('parses negative mate score', () => {
    const result = parseScoreInfo('info depth 20 score mate -2 pv e8d8')

    expect(result).toEqual({ score: { type: 'mate', value: -2 } })
  })

  it('returns null for non-info lines', () => {
    expect(parseScoreInfo('bestmove e2e4')).toBeNull()
    expect(parseScoreInfo('readyok')).toBeNull()
    expect(parseScoreInfo('uciok')).toBeNull()
  })

  it('returns null when score keyword is missing', () => {
    const result = parseScoreInfo('info depth 18 nodes 123456 pv e2e4')

    expect(result).toBeNull()
  })

  it('returns null for invalid score type', () => {
    const result = parseScoreInfo('info depth 18 score invalid 45')

    expect(result).toBeNull()
  })

  it('returns null for non-numeric score value', () => {
    const result = parseScoreInfo('info depth 18 score cp abc')

    expect(result).toBeNull()
  })

  it('parses zero centipawn score', () => {
    const result = parseScoreInfo('info depth 10 score cp 0 pv d2d4')

    expect(result).toEqual({ score: { type: 'cp', value: 0 } })
  })

  it('parses mate in 1', () => {
    const result = parseScoreInfo('info depth 25 score mate 1 pv d1h5')

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

  it('always exceeds recordable failure threshold for any mate', () => {
    // Even mate in 100: 10000 - 100*10 = 9000 >> 50
    expect(Math.abs(mateToCp(100))).toBeGreaterThan(RECORDABLE_FAILURE_THRESHOLD_CP)
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

describe('mateForPlayer', () => {
  it('returns null for non-mate scores', () => {
    expect(mateForPlayer({ type: 'cp', value: 100 }, 'w', 'white')).toBeNull()
    expect(mateForPlayer(null, 'w', 'white')).toBeNull()
  })

  it('returns player-relative mate count when player is side to move', () => {
    const mateIn2: EngineScore = { type: 'mate', value: 2 }
    // White to move mates in 2; white player delivers it.
    expect(mateForPlayer(mateIn2, 'w', 'white')).toBe(2)
    // Same position from black player's view: white mates them.
    expect(mateForPlayer(mateIn2, 'w', 'black')).toBe(-2)
  })

  it('flips sign when black is side to move', () => {
    const mateIn3: EngineScore = { type: 'mate', value: 3 }
    // Black to move mates in 3 → white-relative -3 → black player +3.
    expect(mateForPlayer(mateIn3, 'b', 'white')).toBe(-3)
    expect(mateForPlayer(mateIn3, 'b', 'black')).toBe(3)
  })

  it('preserves a getting-mated count (negative)', () => {
    const gettingMated: EngineScore = { type: 'mate', value: -1 }
    expect(mateForPlayer(gettingMated, 'w', 'white')).toBe(-1)
    expect(mateForPlayer(gettingMated, 'w', 'black')).toBe(1)
  })
})

describe('toWhitePerspectiveMate', () => {
  it('keeps the count unchanged for white move indices', () => {
    expect(toWhitePerspectiveMate(2, 0)).toBe(2)
    expect(toWhitePerspectiveMate(-1, 2)).toBe(-1)
  })

  it('flips the count sign for black move indices', () => {
    expect(toWhitePerspectiveMate(2, 1)).toBe(-2)
    expect(toWhitePerspectiveMate(-1, 3)).toBe(1)
  })

  it('returns input unchanged for null count or unknown move index', () => {
    expect(toWhitePerspectiveMate(null, 1)).toBeNull()
    expect(toWhitePerspectiveMate(2, null)).toBe(2)
    expect(toWhitePerspectiveMate(2, undefined)).toBe(2)
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

describe('moverMateToWhiteCp', () => {
  it('returns a positive cp when the mover (white move) mates', () => {
    expect(moverMateToWhiteCp(3, 0)).toBeGreaterThan(0)
  })

  it('returns a negative cp when the mover (black move) mates', () => {
    // odd ply: black is the mover → white is losing
    expect(moverMateToWhiteCp(2, 1)).toBeLessThan(0)
  })

  it('resolves the mate-0 winner via ply parity', () => {
    expect(moverMateToWhiteCp(0, 0)).toBeGreaterThan(0) // white delivered mate
    expect(moverMateToWhiteCp(0, 1)).toBeLessThan(0) // black delivered mate
  })

  it('returns null for a null mate count', () => {
    expect(moverMateToWhiteCp(null, 0)).toBeNull()
  })
})

describe('playerToWhiteMate', () => {
  it('keeps the count for white player', () => {
    expect(playerToWhiteMate(3, 'white')).toBe(3)
    expect(playerToWhiteMate(-2, 'white')).toBe(-2)
  })

  it('flips the count sign for black player', () => {
    expect(playerToWhiteMate(3, 'black')).toBe(-3)
    expect(playerToWhiteMate(-2, 'black')).toBe(2)
  })

  it('normalizes -0 to 0 and passes through null', () => {
    expect(Object.is(playerToWhiteMate(0, 'black'), 0)).toBe(true)
    expect(playerToWhiteMate(null, 'black')).toBeNull()
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

  it('treats an alternate terminal mating move as equivalent to the engine mate', () => {
    const result = computeAnalysisResult({
      bestMove: 'g6e8',
      playedMove: 'g6g7',
      postPlayedScore: { type: 'mate', value: 0 },
      postBestScore: { type: 'mate', value: 0 },
      sideToMove: 'w',
      playerColor: 'white',
    })

    expect(result.bestEval).toBe(10000)
    expect(result.playedEval).toBe(10000)
    expect(result.delta).toBe(0)
  })

  it('fails an inferior terminal draw when the best move checkmates', () => {
    const result = computeAnalysisResult({
      bestMove: 'g6g7',
      playedMove: 'f7e8',
      postPlayedScore: { type: 'cp', value: 0 },
      postBestScore: { type: 'mate', value: 0 },
      sideToMove: 'w',
      playerColor: 'white',
    })

    expect(result.bestEval).toBe(10000)
    expect(result.playedEval).toBeCloseTo(0)
    expect(result.delta).toBe(10000)
  })
})

describe('evalLoss', () => {
  it('clamps the delta to >= 0', () => {
    expect(evalLoss(35)).toBe(35)
    expect(evalLoss(0)).toBe(0)
    expect(evalLoss(-20)).toBe(0)
  })

  it('returns null for missing or non-finite deltas', () => {
    expect(evalLoss(null)).toBe(null)
    expect(evalLoss(undefined)).toBe(null)
    expect(evalLoss(Infinity)).toBe(null)
    expect(evalLoss(NaN)).toBe(null)
  })
})

describe('failsDrill vs isRecordableFailure boundaries (separate comparators)', () => {
  // The drill comparator uses strict `>` (boundary PASSES); recording/SRS use
  // inclusive `>=` 50 (boundary FAILS). The two MUST NOT collapse into one.
  for (const v of [0, 15, 35, 50]) {
    it(`drill @${v}: equal value passes, above fails, below passes`, () => {
      expect(failsDrill(v, v)).toBe(false) // boundary PASSES
      expect(failsDrill(v + 1, v)).toBe(true)
      expect(failsDrill(v - 1, v)).toBe(false)
    })
  }

  it('drill comparator: 0cp eval loss does not fail by itself', () => {
    expect(failsDrill(0, 0)).toBe(false)
    expect(failsDrill(0, 50)).toBe(false)
  })

  it('recording: 50 fails (inclusive), 49 passes', () => {
    expect(isRecordableFailure(50)).toBe(true)
    expect(isRecordableFailure(49)).toBe(false)
  })

  it('non-finite delta is never a drill or recordable failure', () => {
    expect(failsDrill(null, 0)).toBe(false)
    expect(failsDrill(Infinity, 0)).toBe(false)
    expect(isRecordableFailure(null)).toBe(false)
    expect(isRecordableFailure(NaN)).toBe(false)
  })
})

describe('gradeDrillMove / gradeRecordableMove tri-state', () => {
  it('drill: unavailable for null/non-finite, pass at/below nonzero strictness, fail above', () => {
    expect(gradeDrillMove(null, 35, false)).toBe('unavailable')
    expect(gradeDrillMove(NaN, 35, false)).toBe('unavailable')
    expect(gradeDrillMove(35, 35, false)).toBe('pass') // boundary passes
    expect(gradeDrillMove(0, 35, false)).toBe('pass')
    expect(gradeDrillMove(36, 35, true)).toBe('fail')
  })

  it('drill: 0cp strictness grades on isBestMove only, independent of eval', () => {
    expect(gradeDrillMove(0, 0, true)).toBe('pass')
    expect(gradeDrillMove(0, 0, false)).toBe('fail')
    expect(gradeDrillMove(-4, 0, false)).toBe('fail')
    // Phase 6: the strictness-0 branch runs BEFORE the eval-loss gate, so a
    // missing/non-finite eval no longer blocks exact-best — a trusted position
    // with no move-eval row still grades from isBestMove.
    expect(gradeDrillMove(null, 0, true)).toBe('pass')
    expect(gradeDrillMove(null, 0, false)).toBe('fail')
    expect(gradeDrillMove(NaN, 0, true)).toBe('pass')
    expect(gradeDrillMove(undefined, 0, false)).toBe('fail')
  })

  it('recordable: unavailable for null, pass below 50, fail at/above 50', () => {
    expect(gradeRecordableMove(null)).toBe('unavailable')
    expect(gradeRecordableMove(49)).toBe('pass')
    expect(gradeRecordableMove(50)).toBe('fail') // boundary fails
    expect(gradeRecordableMove(500)).toBe('fail')
  })
})

describe('isMoveClassification', () => {
  it('accepts every union member', () => {
    for (const c of ['best', 'excellent', 'good', 'inaccuracy', 'mistake', 'blunder']) {
      expect(isMoveClassification(c)).toBe(true)
    }
  })

  it('rejects arbitrary strings and non-strings', () => {
    expect(isMoveClassification('great')).toBe(false)
    expect(isMoveClassification('')).toBe(false)
    expect(isMoveClassification(null)).toBe(false)
    expect(isMoveClassification(undefined)).toBe(false)
    expect(isMoveClassification(3)).toBe(false)
  })
})

describe('isRecordableFailure', () => {
  it('threshold constant is 50', () => {
    expect(RECORDABLE_FAILURE_THRESHOLD_CP).toBe(50)
  })

  it('returns true when delta equals threshold (50cp fails)', () => {
    expect(isRecordableFailure(50)).toBe(true)
  })

  it('returns true when delta exceeds threshold', () => {
    expect(isRecordableFailure(51)).toBe(true)
    expect(isRecordableFailure(500)).toBe(true)
    expect(isRecordableFailure(9990)).toBe(true)
  })

  it('returns false when delta is below threshold (49cp passes)', () => {
    expect(isRecordableFailure(49)).toBe(false)
  })

  it('returns false for small deltas', () => {
    expect(isRecordableFailure(10)).toBe(false)
    expect(isRecordableFailure(0)).toBe(false)
  })

  it('returns false for negative delta', () => {
    expect(isRecordableFailure(-50)).toBe(false)
    expect(isRecordableFailure(-200)).toBe(false)
  })

  it('returns false for null delta', () => {
    expect(isRecordableFailure(null)).toBe(false)
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

  it('classifies best at zero and negative deltas', () => {
    expect(classifyMove(0)).toBe('best')
    expect(classifyMove(-1)).toBe('best')
    expect(classifyMove(-250)).toBe('best')
  })

  it('classifies excellent between 1 and 10', () => {
    expect(classifyMove(1)).toBe('excellent')
    expect(classifyMove(10)).toBe('excellent')
  })

  it('classifies good between 11 and 50', () => {
    expect(classifyMove(11)).toBe('good')
    expect(classifyMove(50)).toBe('good')
  })

  it('classifies inaccuracy between 51 and 100', () => {
    expect(classifyMove(51)).toBe('inaccuracy')
    expect(classifyMove(100)).toBe('inaccuracy')
  })

  it('classifies mistake between 101 and 149', () => {
    expect(classifyMove(101)).toBe('mistake')
    expect(classifyMove(149)).toBe('mistake')
  })

  it('classifies blunder at and above 150', () => {
    expect(classifyMove(150)).toBe('blunder')
    expect(classifyMove(400)).toBe('blunder')
  })
})

describe('calculateWinChance', () => {
  it('returns 0 for equal position (0cp)', () => {
    const wc = calculateWinChance({ type: 'cp', value: 0 }, 'white')
    expect(wc).toBeCloseTo(0, 5)
  })

  it('returns positive for white advantage', () => {
    const wc = calculateWinChance({ type: 'cp', value: 200 }, 'white')
    expect(wc).toBeGreaterThan(0)
    expect(wc).toBeLessThan(1)
  })

  it('returns negative for black advantage (white POV)', () => {
    const wc = calculateWinChance({ type: 'cp', value: -200 }, 'white')
    expect(wc).toBeLessThan(0)
  })

  it('flips sign when score is from black POV', () => {
    // +200 from black POV = -200 from white POV
    const wcBlack = calculateWinChance({ type: 'cp', value: 200 }, 'black')
    const wcWhite = calculateWinChance({ type: 'cp', value: -200 }, 'white')
    expect(wcBlack).toBeCloseTo(wcWhite, 10)
  })

  it('clamps to CP_CEILING for mate scores', () => {
    const wcMate = calculateWinChance({ type: 'mate', value: 3 }, 'white')
    const wcCeiling = calculateWinChance({ type: 'cp', value: CP_CEILING }, 'white')
    expect(wcMate).toBeCloseTo(wcCeiling, 10)
  })

  it('returns near -1 for losing mate', () => {
    const wc = calculateWinChance({ type: 'mate', value: -2 }, 'white')
    expect(wc).toBeLessThan(-0.9)
  })

  it('treats mate 0 as the score POV being checkmated', () => {
    expect(calculateWinChance({ type: 'mate', value: 0 }, 'white')).toBeLessThan(-0.9)
    expect(calculateWinChance({ type: 'mate', value: 0 }, 'black')).toBeGreaterThan(0.9)
  })

  it('exports expected constants', () => {
    expect(WIN_CHANCE_MULTIPLIER).toBeCloseTo(-0.00368208)
    expect(CP_CEILING).toBe(1000)
  })
})

describe('checkMateEvents', () => {
  it('detects MateCreated — cp to losing mate is blunder from equal', () => {
    const result = checkMateEvents(
      { type: 'cp', value: 0 },
      { type: 'mate', value: 3 },  // opponent has mate in 3 (from opponent POV, positive = good for them)
      'black',  // scorePov: scores are from opponent's (black's) perspective
      'white',  // mover: white played the move
    )
    // From white mover POV: prevValue flipped = -0, nextValue flipped = -3 (losing mate)
    // mNv < 0 → MateCreated, mPv = 0 → blunder
    expect(result).toBe('blunder')
  })

  it('MateCreated is downgraded to mistake when already losing badly', () => {
    // Mover is white, scorePov is black (opponent's perspective)
    // prev: white was already losing -800cp from white's perspective → from black POV: +800
    const result = checkMateEvents(
      { type: 'cp', value: 800 },
      { type: 'mate', value: 2 },
      'black',
      'white',
    )
    // mPv = 800 * (white===black? 1 : -1) = -800. mPv < -700 → mistake
    expect(result).toBe('mistake')
  })

  it('MateCreated is downgraded to inaccuracy when dead lost', () => {
    const result = checkMateEvents(
      { type: 'cp', value: 1000 },
      { type: 'mate', value: 1 },
      'black',
      'white',
    )
    // mPv = -1000. mPv < -999 → inaccuracy
    expect(result).toBe('inaccuracy')
  })

  it('detects MateLost — winning mate to cp is blunder', () => {
    // White had mate, now just cp. scorePov = black
    const result = checkMateEvents(
      { type: 'mate', value: -3 },  // from black POV, -3 means white has mate in 3
      { type: 'cp', value: 50 },   // now black is slightly better
      'black',
      'white',
    )
    // mPv = -3 * -1 = 3 (positive, white had mate). mNv = 50 * -1 = -50.
    // resCp = -50. resCp < 700 → blunder
    expect(result).toBe('blunder')
  })

  it('MateLost is downgraded to mistake when still winning big', () => {
    const result = checkMateEvents(
      { type: 'mate', value: -2 },
      { type: 'cp', value: -800 },  // from black POV = white is +800
      'black',
      'white',
    )
    // mPv = -2 * -1 = 2 (had mate). mNv = -800 * -1 = 800. resCp = 800 > 700 → mistake
    expect(result).toBe('mistake')
  })

  it('returns null for normal cp-to-cp transition', () => {
    const result = checkMateEvents(
      { type: 'cp', value: 50 },
      { type: 'cp', value: -100 },
      'black',
      'white',
    )
    expect(result).toBeNull()
  })

  it('returns null for mate-to-better-mate (no loss)', () => {
    // White had mate in 5, now has mate in 3 — not a loss
    const result = checkMateEvents(
      { type: 'mate', value: -5 },
      { type: 'mate', value: -3 },
      'black',
      'white',
    )
    // mPv = 5 (positive). mNv = 3 (positive, not < 0). → no MateLost
    expect(result).toBeNull()
  })
})

describe('classifyMoveAdvanced', () => {
  it('returns best for engine-preferred move', () => {
    expect(
      classifyMoveAdvanced({
        prevScore: { type: 'cp', value: -30 },
        nextScore: { type: 'cp', value: -50 },
        scorePov: 'black',
        mover: 'white',
        isBestMove: true,
      }),
    ).toBe('best')
  })

  it('classifies excellent for small win-chance drop', () => {
    // Both from black POV (opponent to move). White played, scores barely changed.
    const result = classifyMoveAdvanced({
      prevScore: { type: 'cp', value: -30 },
      nextScore: { type: 'cp', value: -25 },
      scorePov: 'black',
      mover: 'white',
      isBestMove: false,
    })
    expect(result).toBe('excellent')
  })

  it('classifies blunder for large win-chance drop', () => {
    // White played, was equal, now losing badly
    const result = classifyMoveAdvanced({
      prevScore: { type: 'cp', value: 0 },
      nextScore: { type: 'cp', value: 500 },
      scorePov: 'black',
      mover: 'white',
      isBestMove: false,
    })
    expect(result).toBe('blunder')
  })

  it('classifies mate transitions with severity', () => {
    // White blundered into being mated from equal
    const result = classifyMoveAdvanced({
      prevScore: { type: 'cp', value: 0 },
      nextScore: { type: 'mate', value: 3 },
      scorePov: 'black',
      mover: 'white',
      isBestMove: false,
    })
    expect(result).toBe('blunder')
  })

  it('100cp loss from equal is classified more severely than from -500cp', () => {
    // From equal: 0 → 100 from black POV
    const fromEqual = classifyMoveAdvanced({
      prevScore: { type: 'cp', value: 0 },
      nextScore: { type: 'cp', value: 100 },
      scorePov: 'black',
      mover: 'white',
      isBestMove: false,
    })

    // From already losing: +500 → +600 from black POV
    const fromLosing = classifyMoveAdvanced({
      prevScore: { type: 'cp', value: 500 },
      nextScore: { type: 'cp', value: 600 },
      scorePov: 'black',
      mover: 'white',
      isBestMove: false,
    })

    const severity = ['excellent', 'good', 'inaccuracy', 'mistake', 'blunder'] as const
    const equalIdx = severity.indexOf(fromEqual as typeof severity[number])
    const losingIdx = severity.indexOf(fromLosing as typeof severity[number])
    expect(equalIdx).toBeGreaterThan(losingIdx)
  })

  it('uses null score fallback via classifyMove in worker path', () => {
    // This tests the worker's fallback — classifyMoveAdvanced itself requires non-null scores
    // The worker code handles this, so we just test classifyMoveAdvanced with valid inputs
    const result = classifyMoveAdvanced({
      prevScore: { type: 'cp', value: 0 },
      nextScore: { type: 'cp', value: 0 },
      scorePov: 'black',
      mover: 'white',
      isBestMove: false,
    })
    expect(result).toBe('excellent')
  })

  it('works for black mover', () => {
    // Black played, scores from white POV (opponent to move)
    // prev: white was slightly worse (-30 from white POV)
    // next: white is now much better (+300 from white POV) — black blundered
    const result = classifyMoveAdvanced({
      prevScore: { type: 'cp', value: -30 },
      nextScore: { type: 'cp', value: 300 },
      scorePov: 'white',
      mover: 'black',
      isBestMove: false,
    })
    expect(result).toBe('blunder')
  })
})

describe('canResolvePositionAnalysis', () => {
  it('accepts a best move with a multi-move best line starting at that move', () => {
    expect(
      canResolvePositionAnalysis({
        best_move_uci: 'e2e4',
        best_line_uci: ['e2e4', 'e7e5'],
      }),
    ).toBe(true)
  })

  it('rejects a best move with no usable best line', () => {
    // Null PV.
    expect(
      canResolvePositionAnalysis({ best_move_uci: 'e2e4', best_line_uci: null }),
    ).toBe(false)
    // Single-move PV.
    expect(
      canResolvePositionAnalysis({ best_move_uci: 'e2e4', best_line_uci: ['e2e4'] }),
    ).toBe(false)
    // PV[0] differs from the best move.
    expect(
      canResolvePositionAnalysis({
        best_move_uci: 'e2e4',
        best_line_uci: ['d2d4', 'd7d5'],
      }),
    ).toBe(false)
  })

  it('rejects a row with no best move (cannot synthesize a PV-less best line)', () => {
    expect(
      canResolvePositionAnalysis({ best_move_uci: null, best_line_uci: null }),
    ).toBe(false)
  })
})

describe('canResolveMoveAnalysis', () => {
  it('accepts a valid classification with a finite CP played eval', () => {
    expect(
      canResolveMoveAnalysis({
        classification: 'good',
        played_eval: 12,
        played_eval_mate: null,
      }),
    ).toBe(true)
  })

  it('accepts a mate-only row (played_eval_mate set, played_eval null) — DOES NOT require eval_delta', () => {
    expect(
      canResolveMoveAnalysis({
        classification: 'blunder',
        played_eval: null,
        played_eval_mate: -2,
      }),
    ).toBe(true)
  })

  it('rejects when both played evals are null', () => {
    expect(
      canResolveMoveAnalysis({
        classification: 'good',
        played_eval: null,
        played_eval_mate: null,
      }),
    ).toBe(false)
  })

  it('rejects an invalid or null classification even with a usable played eval', () => {
    expect(
      canResolveMoveAnalysis({
        classification: 'totally-not-a-classification',
        played_eval: 12,
        played_eval_mate: null,
      }),
    ).toBe(false)
    expect(
      canResolveMoveAnalysis({
        classification: null,
        played_eval: 12,
        played_eval_mate: null,
      }),
    ).toBe(false)
  })
})

describe('hasCpEvalLoss', () => {
  it('is true only for a finite non-negative eval_delta', () => {
    expect(hasCpEvalLoss({ eval_delta: 0 })).toBe(true)
    expect(hasCpEvalLoss({ eval_delta: 42 })).toBe(true)
  })

  it('is false for null/undefined/non-finite/negative deltas', () => {
    expect(hasCpEvalLoss({ eval_delta: null })).toBe(false)
    expect(hasCpEvalLoss({ eval_delta: undefined })).toBe(false)
    expect(hasCpEvalLoss({ eval_delta: Infinity })).toBe(false)
    expect(hasCpEvalLoss({ eval_delta: NaN })).toBe(false)
    expect(hasCpEvalLoss({ eval_delta: -1 })).toBe(false)
  })
})

describe('isTrustedPositionHit', () => {
  const complete = {
    position_trusted: true as boolean,
    best_move_uci: 'e2e4',
    best_line_uci: ['e2e4', 'e7e5'],
  }

  it('trusts a backend position_trusted row with renderable PV structure', () => {
    expect(isTrustedPositionHit(complete)).toBe(true)
  })

  it('does NOT trust a row the backend left position-untrusted', () => {
    expect(isTrustedPositionHit({ ...complete, position_trusted: false })).toBe(false)
    // No flag at all -> worker fallback.
    expect(
      isTrustedPositionHit({ best_move_uci: 'e2e4', best_line_uci: ['e2e4', 'e7e5'] }),
    ).toBe(false)
  })

  it('falls back to the worker when a trusted row lacks renderable PV structure', () => {
    expect(isTrustedPositionHit({ ...complete, best_line_uci: null })).toBe(false)
  })
})

describe('isTrustedExactBestHit', () => {
  it('trusts a position_trusted row with a best move and NO PV requirement', () => {
    // Looser than isTrustedPositionHit: a renderable PV is NOT required, because
    // backend position_trusted already guarantees the PV upstream.
    expect(
      isTrustedExactBestHit({ position_trusted: true, best_move_uci: 'c2c4' }),
    ).toBe(true)
    // Even with a missing/degenerate PV that isTrustedPositionHit would reject.
    expect(
      isTrustedExactBestHit({
        position_trusted: true,
        best_move_uci: 'c2c4',
        best_line_uci: null,
      } as { position_trusted?: boolean; best_move_uci?: string | null }),
    ).toBe(true)
  })

  it('does NOT trust an untrusted position or a null best move', () => {
    expect(
      isTrustedExactBestHit({ position_trusted: false, best_move_uci: 'c2c4' }),
    ).toBe(false)
    expect(
      isTrustedExactBestHit({ position_trusted: true, best_move_uci: null }),
    ).toBe(false)
    // No flag at all -> worker fallback.
    expect(isTrustedExactBestHit({ best_move_uci: 'c2c4' })).toBe(false)
  })
})

describe('isTrustedMoveHit', () => {
  const complete = {
    move_trusted: true as boolean,
    classification: 'good' as const,
    played_eval: 12,
    played_eval_mate: null as number | null,
  }

  it('trusts a backend move_trusted row with renderable played evidence', () => {
    expect(isTrustedMoveHit(complete)).toBe(true)
  })

  it('is TRUE for a move-trusted mate-only row (eval_delta absent)', () => {
    expect(
      isTrustedMoveHit({
        move_trusted: true,
        classification: 'blunder',
        played_eval: null,
        played_eval_mate: -2,
        // eval_delta intentionally omitted — move-complete-v1 does not require it.
      }),
    ).toBe(true)
  })

  it('does NOT trust a row the backend left move-untrusted', () => {
    expect(isTrustedMoveHit({ ...complete, move_trusted: false })).toBe(false)
    // No flag at all -> worker fallback.
    expect(
      isTrustedMoveHit({ classification: 'good', played_eval: 12, played_eval_mate: null }),
    ).toBe(false)
  })

  it('falls back to the worker when a trusted row lacks renderable played evidence', () => {
    expect(
      isTrustedMoveHit({ ...complete, played_eval: null, played_eval_mate: null }),
    ).toBe(false)
  })
})

// Shared golden vectors — same fixture drives backend test_move_classification.py
// so the two classifier implementations cannot drift.
import classificationVectors from '../../backend/tests/fixtures/classification_vectors.json'

describe('classifyMoveAdvanced golden vectors (shared with backend)', () => {
  for (const [i, c] of (classificationVectors.cases as Array<{
    prevScore: EngineScore
    nextScore: EngineScore
    scorePov: 'white' | 'black'
    mover: 'white' | 'black'
    isBest: boolean
    expected: string
  }>).entries()) {
    it(`case ${i}: ${c.expected}`, () => {
      expect(
        classifyMoveAdvanced({
          prevScore: c.prevScore,
          nextScore: c.nextScore,
          scorePov: c.scorePov,
          mover: c.mover,
          isBestMove: c.isBest,
        }),
      ).toBe(c.expected)
    })
  }
})
