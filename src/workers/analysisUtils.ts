/**
 * Pure utility functions for move analysis.
 * Extracted from analysisWorker for testability.
 */

import type { EngineScore } from './stockfishMessages'
import { parseUciInfoLine } from './parseInfo'
// MoveClassification and AnalysisResult live in the neutral types module so this
// low-level module never imports from the hook layer (which would form a
// workers -> hooks cycle). MoveClassification is re-exported below so existing
// consumers that import it from analysisUtils keep working unchanged.
import { MATE_BASE_CP } from '../types/analysis'
import type { MoveClassification, AnalysisResult } from '../types/analysis'

export type { MoveClassification }

export const RECORDABLE_FAILURE_THRESHOLD_CP = 50
export const RECORDING_MOVE_CAP_FULL_MOVES = 10
const RECORDING_MOVE_CAP_PLY = RECORDING_MOVE_CAP_FULL_MOVES * 2

export type ParsedInfo = {
  score: EngineScore
}

/** Thin wrapper: only returns info lines that carry a score. */
export const parseScoreInfo = (line: string): ParsedInfo | null => {
  const info = parseUciInfoLine(line)
  if (!info?.score) return null
  return { score: info.score }
}

export const mateToCp = (movesToMate: number) => {
  const mateDecay = 10
  // mate 0 means the side to move is checkmated (lost)
  if (movesToMate === 0) return -MATE_BASE_CP
  const sign = movesToMate >= 0 ? 1 : -1
  return sign * (MATE_BASE_CP - Math.abs(movesToMate) * mateDecay)
}

export const normalizeScore = (score: EngineScore | null, sideToMove: 'w' | 'b') => {
  if (!score) {
    return null
  }

  const raw = score.type === 'cp' ? score.value : mateToCp(score.value)
  const sign = sideToMove === 'w' ? 1 : -1
  return raw * sign
}

export const toWhitePerspective = (
  moverPerspectiveEval: number | null,
  moveIndex: number | null | undefined,
) => {
  if (moverPerspectiveEval === null || moveIndex === null || moveIndex === undefined) {
    return moverPerspectiveEval
  }

  return moveIndex % 2 === 0 ? moverPerspectiveEval : -moverPerspectiveEval
}

/** Convert a player-perspective eval to white perspective. */
export const playerToWhite = (
  playerPerspectiveEval: number | null,
  playerColor: 'white' | 'black',
): number | null => {
  if (playerPerspectiveEval === null) return null
  return playerColor === 'white' ? playerPerspectiveEval : -playerPerspectiveEval
}

/** Convert a player-perspective mate count to white perspective (sign-flip for black). */
export const playerToWhiteMate = (
  playerPerspectiveMate: number | null,
  playerColor: 'white' | 'black',
): number | null => {
  if (playerPerspectiveMate === null) return null
  const whiteRelative = playerColor === 'white' ? playerPerspectiveMate : -playerPerspectiveMate
  // Normalize -0 to 0 so equality checks and serialized output stay stable.
  return whiteRelative === 0 ? 0 : whiteRelative
}

export const scoreForPlayer = (
  score: EngineScore | null,
  sideToMove: 'w' | 'b',
  playerColor: 'white' | 'black',
) => {
  const whitePerspective = normalizeScore(score, sideToMove)
  if (whitePerspective === null) {
    return null
  }
  return playerColor === 'white' ? whitePerspective : -whitePerspective
}

/**
 * Player-relative mate count for a score, or null when the score is not a mate.
 * Mirrors `scoreForPlayer` but flips perspective by sign-negating the move count
 * (a positive count means the player delivers mate).
 */
export const mateForPlayer = (
  score: EngineScore | null,
  sideToMove: 'w' | 'b',
  playerColor: 'white' | 'black',
): number | null => {
  if (!score || score.type !== 'mate') {
    return null
  }
  const whiteRelative = sideToMove === 'w' ? score.value : -score.value
  const playerRelative = playerColor === 'white' ? whiteRelative : -whiteRelative
  // Normalize -0 to 0 so equality checks and serialized output stay stable.
  return playerRelative === 0 ? 0 : playerRelative
}

/**
 * Convert a mover-perspective mate count to white perspective, mirroring
 * `toWhitePerspective` (parity-based, NOT playerColor) but sign-negating the
 * count rather than the eval value.
 */
export const toWhitePerspectiveMate = (
  moverPerspectiveMate: number | null,
  moveIndex: number | null | undefined,
): number | null => {
  if (moverPerspectiveMate === null || moveIndex === null || moveIndex === undefined) {
    return moverPerspectiveMate
  }
  if (moveIndex % 2 === 0 || moverPerspectiveMate === 0) {
    return moverPerspectiveMate
  }
  return -moverPerspectiveMate
}

/**
 * White-perspective centipawn value for a mover-perspective mate count at a
 * given ply. `mateToCp` is side-to-move (post-move opponent) perspective, so we
 * feed it the negated count and negate the result to land back in mover
 * perspective, then convert mover→white by ply parity. This resolves the winner
 * even for mate-0 (checkmate) where the count alone is sign-ambiguous.
 */
export const moverMateToWhiteCp = (
  moverPerspectiveMate: number | null,
  moveIndex: number | null | undefined,
): number | null => {
  if (moverPerspectiveMate === null) return null
  return toWhitePerspective(-mateToCp(-moverPerspectiveMate), moveIndex)
}

export const getSideToMove = (fen: string) => {
  const parts = fen.split(' ')
  const active = parts[1]
  if (active === 'w' || active === 'b') {
    return active
  }
  return null
}

/**
 * Computes bestEval, playedEval, and delta from post-move search scores.
 *
 * Both scores must come from positions AFTER their respective moves (i.e. from
 * the opponent-to-move perspective). Using the pre-move minimax eval as bestEval
 * is unreliable because independent WASM Stockfish searches reach different
 * depths, inflating the delta (e.g. the 1.e4 false-blunder: minimax +97cp vs
 * post-move +29cp for the same resulting position).
 */
export const computeAnalysisResult = (input: {
  bestMove: string
  playedMove: string
  postPlayedScore: EngineScore | null
  postBestScore: EngineScore | null
  sideToMove: 'w' | 'b'
  playerColor: 'white' | 'black'
}): { bestEval: number | null; playedEval: number | null; delta: number | null } => {
  const opponentToMove = input.sideToMove === 'w' ? 'b' : 'w'
  const playedEval = scoreForPlayer(input.postPlayedScore, opponentToMove, input.playerColor)
  const bestEval = input.bestMove === input.playedMove
    ? playedEval
    : scoreForPlayer(input.postBestScore, opponentToMove, input.playerColor)

  const delta = bestEval !== null && playedEval !== null ? bestEval - playedEval : null
  return { bestEval, playedEval, delta }
}

/**
 * Tri-state grade for a played move. `unavailable` means the eval is missing or
 * non-finite (NOT a pass): the move could not be graded and callers must route
 * to a recovery/no-op path rather than treating it as correct.
 */
export type MoveGrade = 'pass' | 'fail' | 'unavailable'

/**
 * The authoritative centipawn loss for threshold decisions: the played move's
 * delta floored to >= 0 and capped at `EVAL_LOSS_CAP_CP`, or `null` when the delta
 * is missing/non-finite. Every threshold comparator below derives from this single
 * helper so drill accuracy, regular-game recording, and SRS pass/fail read the same
 * eval surface. Mirrors the backend `centipawn_loss` normalizer (0..1000).
 */
export const evalLoss = (delta: number | null | undefined): number | null => {
  if (delta === null || delta === undefined || !Number.isFinite(delta)) {
    return null
  }
  // References EVAL_LOSS_CAP_CP (declared below) at call time — safe forward ref.
  return Math.min(Math.max(delta, 0), EVAL_LOSS_CAP_CP)
}

/**
 * Drill-accuracy comparator for nonzero strictness: a move FAILS only when its
 * eval loss strictly EXCEEDS the configured strictness (the boundary value
 * PASSES). Distinct from the recording/SRS comparator on purpose — do not merge
 * the two thresholds.
 */
export const failsDrill = (
  delta: number | null | undefined,
  strictnessCp: number,
): boolean => {
  const loss = evalLoss(delta)
  return loss !== null && loss > strictnessCp
}

/**
 * Determines if a move is a recordable failure (for blunder recording and SRS
 * review pass/fail). Inclusive boundary (>= 50) matching the backend contract
 * — the boundary value FAILS. No context-aware scaling.
 */
export const isRecordableFailure = (delta: number | null | undefined): boolean => {
  const loss = evalLoss(delta)
  return loss !== null && loss >= RECORDABLE_FAILURE_THRESHOLD_CP
}

/**
 * Tri-state drill grade. A 0cp strictness means exact-best ONLY; above 0cp the
 * grade is eval-loss based and the boundary value passes (`unavailable` when the
 * eval is missing/non-finite at that tier).
 *
 * ORDERING IS INTENTIONAL (g-position-analysis Phase 6, epic g-l02q): the
 * strictness-0 branch runs FIRST, BEFORE the `evalLoss(delta) === null` gate, so
 * exact-best depends only on `isBestMove` and never on move-eval availability. A
 * trusted position with no move-eval row (`gradeDrillMove(null, 0, true)`) now
 * PASSES instead of returning `unavailable`. `isBestMove` is the caller's trust
 * contract: `waitForDrillGrade` computes it from trusted position
 * `best_move_uci` (see `isTrustedExactBestHit`) or an honest worker best move —
 * never `bestMove ?? playedMove`. Do NOT restore the old ordering (eval-loss gate
 * first); that would re-block strictness-0 on missing move evidence.
 */
export const gradeDrillMove = (
  delta: number | null | undefined,
  strictnessCp: number,
  isBestMove: boolean,
): MoveGrade => {
  if (strictnessCp <= 0) return isBestMove ? 'pass' : 'fail'
  if (evalLoss(delta) === null) return 'unavailable'
  return failsDrill(delta, strictnessCp) ? 'fail' : 'pass'
}

/**
 * Tri-state recordable grade (regular-game recording + SRS). `unavailable` when
 * the eval is missing/non-finite; otherwise `fail` when isRecordableFailure
 * (>= 50), else `pass`.
 */
export const gradeRecordableMove = (
  delta: number | null | undefined,
): MoveGrade => {
  if (evalLoss(delta) === null) return 'unavailable'
  return isRecordableFailure(delta) ? 'fail' : 'pass'
}

export const isWithinRecordingMoveCap = (
  moveIndex: number | null | undefined,
): boolean => {
  if (moveIndex === null || moveIndex === undefined || moveIndex < 0) {
    return false
  }
  return moveIndex < RECORDING_MOVE_CAP_PLY
}

/**
 * @deprecated Use classifyMoveAdvanced for new code. Kept as fallback for
 * legacy cache entries that lack a `classification` value.
 */
export const classifyMove = (
  delta: number | null,
): MoveClassification | null => {
  if (delta === null) return null

  const normalizedDelta = Math.max(delta, 0)
  if (normalizedDelta === 0) return 'best'
  if (normalizedDelta <= 10) return 'excellent'
  if (normalizedDelta <= 50) return 'good'
  if (normalizedDelta <= 100) return 'inaccuracy'
  if (normalizedDelta <= 149) return 'mistake'
  return 'blunder'
}

const MOVE_CLASSIFICATIONS: ReadonlySet<MoveClassification> = new Set([
  'best',
  'excellent',
  'good',
  'inaccuracy',
  'mistake',
  'blunder',
])

/** Lightweight runtime membership guard over the MoveClassification union. */
export const isMoveClassification = (
  value: unknown,
): value is MoveClassification =>
  typeof value === 'string' &&
  MOVE_CLASSIFICATIONS.has(value as MoveClassification)

/**
 * POSITION-grain structure guard: the cached row carries a renderable best-move
 * principal variation. Requires a best move plus a multi-move PV that begins
 * with it. The PV belongs to the POSITION grain, not the move row. This guards
 * local rendering, not trust — trust is decided by the backend (see
 * isTrustedPositionHit).
 */
export const canResolvePositionAnalysis = (input: {
  best_move_uci?: string | null | undefined
  best_line_uci?: string[] | null | undefined
}): boolean => {
  if (!input.best_move_uci) return false

  return (
    Array.isArray(input.best_line_uci) &&
    input.best_line_uci.length > 1 &&
    input.best_line_uci[0] === input.best_move_uci
  )
}

/**
 * MOVE-grain structure guard: the cached row carries renderable played-move
 * evidence. Mirrors the backend `move-complete-v1` contract — an enum-valid
 * classification AND a usable played eval that may be CP (`played_eval`) OR mate
 * (`played_eval_mate`). It DELIBERATELY does NOT require `eval_delta`: a
 * move-trusted mate-only row has none. This guards local rendering, not trust —
 * trust is decided by the backend (see isTrustedMoveHit).
 */
export const canResolveMoveAnalysis = (input: {
  classification: MoveClassification | string | null | undefined
  played_eval?: number | null | undefined
  played_eval_mate?: number | null | undefined
}): boolean => {
  if (!isMoveClassification(input.classification)) return false

  return (
    Number.isFinite(input.played_eval) || Number.isFinite(input.played_eval_mate)
  )
}

/**
 * POSITION-grain trust: the backend resolved a trusted position
 * (`position_trusted === true`) AND the row carries a renderable best-move PV.
 * The frontend does NOT re-derive trust — it only re-checks structure so a
 * trusted row that somehow lacks renderable structure falls back to the worker.
 */
export const isTrustedPositionHit = (input: {
  position_trusted?: boolean
  best_move_uci?: string | null | undefined
  best_line_uci?: string[] | null | undefined
}): boolean => input.position_trusted === true && canResolvePositionAnalysis(input)

/**
 * DRILL exact-best trust: a backend-trusted position (`position_trusted === true`)
 * carrying a `best_move_uci` to compare the played move against. Deliberately
 * LOOSER than `isTrustedPositionHit` — it does NOT re-check the renderable PV,
 * because drill strictness-0 only needs to know the canonical winning move, not
 * render a line. This is a FRONTEND-only relaxation, NOT a backend trust change:
 * `position_trusted` is gated server-side on the position-complete-v1 contract,
 * which already REQUIRES a multi-move PV beginning with the best move
 * (`_validate_position_complete` -> `_pv_first_equals_best`). So a
 * `position_trusted` row is guaranteed a valid PV upstream. Do NOT "tighten" this
 * to `isTrustedPositionHit` (it would needlessly block exact-best when the PV did
 * not survive the wire) and do NOT weaken the backend contract to match it.
 */
export const isTrustedExactBestHit = (input: {
  position_trusted?: boolean
  best_move_uci?: string | null | undefined
}): boolean => input.position_trusted === true && input.best_move_uci != null

/**
 * MOVE-grain trust: the backend trusts the move row (`move_trusted === true`)
 * AND the row carries renderable played evidence. NOTE: this is correctly TRUE
 * for a move-trusted mate-only row (no `eval_delta`) — that satisfies the
 * move-complete-v1 contract, which does not require a CP delta.
 */
export const isTrustedMoveHit = (input: {
  move_trusted?: boolean
  classification: MoveClassification | string | null | undefined
  played_eval?: number | null | undefined
  played_eval_mate?: number | null | undefined
}): boolean => input.move_trusted === true && canResolveMoveAnalysis(input)

/**
 * TRANSITIONAL CP-only grading usability gate — NOT a trust decision. The
 * current live grader (`gradeDrillMove` / `evalLoss`) needs a finite,
 * non-negative CP `eval_delta`; a move-trusted mate-only row lacks one and must
 * fall back to the worker until Phase 6 adds mate-aware eval-loss grading (epic
 * g-l02q). Preserves the old guard's finite-non-negative-delta rejection that
 * `evalLoss` alone does not — `evalLoss(-1)` returns 0, not null.
 */
export const hasCpEvalLoss = (input: {
  eval_delta?: number | null | undefined
}): boolean => {
  const delta = input.eval_delta
  return delta != null && Number.isFinite(delta) && delta >= 0
}

/**
 * STRUCTURAL re-check of the backend's atomic reuse payload (g-v21l).
 *
 * The backend already proved the payload is a coherent tuple — matching settings,
 * agreeing facts, a rederived classification — so this does NOT re-decide trust.
 * It only confirms the payload is renderable and gradeable HERE, so a wire-level
 * loss (a dropped PV, a non-finite delta) releases the worker fallback instead of
 * publishing a half-built result. Mirrors what `isTrustedPositionHit` +
 * `isTrustedMoveHit` + `hasCpEvalLoss` used to check across the generic fields,
 * against the one payload that is actually being published.
 *
 * The CAPABILITY half is the caller's: each consumer must independently require
 * its OWN flag (`interactive_analysis_reuse` / `game_analysis_reuse`) — a payload
 * approved for one consumer must never be published by the other.
 */
export const canResolveReusableAnalysis = (payload: {
  best_move_uci?: string | null | undefined
  best_line_uci?: string[] | null | undefined
  classification: MoveClassification | string | null | undefined
  played_eval?: number | null | undefined
  played_eval_mate?: number | null | undefined
  eval_delta?: number | null | undefined
}): boolean =>
  payload.best_move_uci != null &&
  canResolvePositionAnalysis(payload) &&
  canResolveMoveAnalysis(payload) &&
  hasCpEvalLoss(payload)

/**
 * Grain-split best reconciliation (g-move-best-icon, g-jfdj). The TRUSTED position
 * grain names `trustedBestUci` as the position's best move. "Is the played move
 * best?" is a POSITION-grain question, so the trusted position grain's answer wins
 * over a stale move-grain/worker classification (the same grain the drill grade
 * already trusts). This reconciles best-ness in BOTH directions:
 *
 * - PROMOTION (played == trustedBest): the played move IS best — even when the
 *   published `result` came from a move-untrusted cache row or shallower worker
 *   fallback that called it merely 'excellent'. Normalize to a coherent loss-0 best
 *   move so the MoveList renders the best-move star (and best-move bling / perfect
 *   streak fire) and uploaded evidence is internally consistent. Only best-ness
 *   facts are rewritten; eval MAGNITUDES (playedEval/mate) stay the move/worker
 *   grain. `delta: 0` is exact by definition (playing the trusted best loses 0).
 *
 * - DEMOTION (played != trustedBest but a fallback wrongly graded it 'best'): the
 *   trusted position says best is elsewhere, so a published 'best' is wrong. Demote
 *   to the 'excellent' floor and point bestMove/bestLine at the trusted best. No
 *   trusted eval loss exists (move grain is untrusted; mixing it with the position
 *   eval would be invented), so delta and bestEval are nulled rather than
 *   fabricated. playedEval magnitude is kept.
 *
 * No-op for an already-non-best result whose played move is not the trusted best
 * (the Bf4 case) — nothing the position grain can correct without inventing a loss.
 *
 * Shared single source between the live MoveList path (GameAnalysisCoordinator)
 * and the post-game AnalysisBoard path (useMoveAnalysis) — see g-49e2.
 */
export const reconcileTrustedBest = (
  result: AnalysisResult,
  trustedBestUci: string,
): AnalysisResult => {
  // Direction 1 — PROMOTION: played IS the trusted best → it is best (loss 0).
  if (result.move === trustedBestUci) {
    if (result.classification === 'best') return result
    return {
      ...result,
      classification: 'best',
      bestMove: trustedBestUci,
      bestLine: [trustedBestUci],
      bestEval: result.playedEval,
      delta: 0,
      blunder: false,
      recordable: false,
      // The tuple is now part canonical POSITION truth, not purely this device's
      // search, so the browser-search provenance claim would be false (g-mk1d
      // §2.5). Cleared here, in the single shared reconciler, so every caller —
      // live MoveList and post-game alike — is covered in one place. The row
      // uploads as provenance-less browser-game-v1, which is correct: its
      // best-ness came from the position grain, not from a stronger search.
      provenance: null,
    }
  }
  // played is NOT the trusted best.
  // Direction 2 — already non-best: nothing the position grain can correct without
  // inventing an eval loss; leave the move/worker grain untouched (the Bf4 case).
  if (result.classification !== 'best') return result
  // Direction 3 — DEMOTION: a fallback wrongly called a non-best move 'best'. Demote
  // to the 'excellent' floor and point bestMove/bestLine at the trusted best.
  return {
    ...result,
    classification: 'excellent',
    bestMove: trustedBestUci,
    bestLine: [trustedBestUci],
    bestEval: null,
    delta: null,
    blunder: false,
    recordable: false,
    // Same rule as the promotion branch: a canonically corrected tuple no longer
    // describes this device's search, so it carries no depth claim (g-mk1d §2.5).
    provenance: null,
  }
}

// ── Win-chance classifier (Lichess logistic model) ──────────────────

export const WIN_CHANCE_MULTIPLIER = -0.00368208
export const CP_CEILING = 1000

// The per-move CPL / severity decisive-mistake ceiling (g-no51): the max a single
// move contributes to Avg CPL and the max severity a single blunder contributes to
// practice scheduling. Mirrors the backend CENTIPAWN_LOSS_CAP_CP. This is a DISTINCT
// product control from the win-chance CP_CEILING above; they are currently equal by
// the shared ±1000 clip convention, but decoupling them is a future explicit product
// decision — do NOT alias this to CP_CEILING (aliasing would silently re-couple them,
// so a future change to the win-chance clip would move the CPL cap as an invisible
// side effect). Their current equality is PINNED by a unit test
// (EVAL_LOSS_CAP_CP === CP_CEILING), so an intentional divergence is a deliberate,
// visible edit to that assertion rather than a silent drift.
export const EVAL_LOSS_CAP_CP = 1000

/**
 * Converts an engine score to a win chance between -1.0 and 1.0,
 * normalized to white's perspective.
 */
export const calculateWinChance = (
  score: EngineScore,
  pov: 'white' | 'black',
): number => {
  const whiteValue = pov === 'white' ? score.value : -score.value

  const cp =
    score.type === 'mate'
      ? score.value === 0
        ? (pov === 'white' ? -CP_CEILING : CP_CEILING)
        : (whiteValue > 0 ? CP_CEILING : -CP_CEILING)
      : Math.max(-CP_CEILING, Math.min(CP_CEILING, whiteValue))

  return 2 / (1 + Math.exp(WIN_CHANCE_MULTIPLIER * cp)) - 1
}

/**
 * Detects mate transitions: blundering into being mated (MateCreated)
 * or throwing away a winning mate (MateLost). Returns a severity-adjusted
 * classification or null if no mate event occurred.
 *
 * Both scores share the same `scorePov` (the perspective they were reported from).
 * `mover` is the color that played the move being classified.
 */
export const checkMateEvents = (
  prevScore: EngineScore,
  nextScore: EngineScore,
  scorePov: 'white' | 'black',
  mover: 'white' | 'black',
): MoveClassification | null => {
  // Convert to mover POV
  const flipPrev = mover === scorePov ? 1 : -1
  const mPv = prevScore.value * flipPrev
  const mNv = nextScore.value * flipPrev

  // MateCreated: cp → losing mate (blundered into being mated)
  if (prevScore.type === 'cp' && nextScore.type === 'mate' && mNv < 0) {
    if (mPv < -999) return 'inaccuracy'
    if (mPv < -700) return 'mistake'
    return 'blunder'
  }

  // MateLost: winning mate → cp or losing mate
  if (
    prevScore.type === 'mate' &&
    mPv > 0 &&
    (nextScore.type === 'cp' || (nextScore.type === 'mate' && mNv < 0))
  ) {
    const resCp = nextScore.type === 'cp' ? mNv : -1000
    if (resCp > 999) return 'inaccuracy'
    if (resCp > 700) return 'mistake'
    return 'blunder'
  }

  return null
}

/**
 * Advanced move classifier using the Lichess logistic win-chance model.
 *
 * Both `prevScore` and `nextScore` are from post-move positions where the
 * opponent is to move, sharing the same `scorePov`.
 */
/**
 * Maps a mover-relative win-chance loss to a classification label. Shared by
 * `classifyMoveAdvanced` (post-move opponent-to-move scores) and
 * `classifyRootAlternative` (root same-search alternatives) so the two classifiers
 * cannot drift on the threshold ladder. Mirrors `_classify_win_chance_drop` in
 * `backend/app/move_classification.py`.
 */
export const classifyWinChanceDrop = (drop: number): MoveClassification => {
  if (drop >= 0.30) return 'blunder'
  if (drop >= 0.20) return 'mistake'
  if (drop >= 0.10) return 'inaccuracy'
  if (drop >= 0.02) return 'good'
  return 'excellent'
}

export const classifyMoveAdvanced = (input: {
  prevScore: EngineScore
  nextScore: EngineScore
  scorePov: 'white' | 'black'
  mover: 'white' | 'black'
  isBestMove: boolean
}): MoveClassification => {
  const { prevScore, nextScore, scorePov, mover, isBestMove } = input

  if (isBestMove) return 'best'

  const mateResult = checkMateEvents(prevScore, nextScore, scorePov, mover)
  if (mateResult) return mateResult

  const prevWc = calculateWinChance(prevScore, scorePov)
  const nextWc = calculateWinChance(nextScore, scorePov)

  const drop = mover === 'white' ? -(nextWc - prevWc) : (nextWc - prevWc)

  return classifyWinChanceDrop(drop)
}

/**
 * Classifies a played root move against the best root move using two lines of the
 * SAME completed search (g-reuse-d21-search §5.1).
 *
 * `bestScore` and `playedScore` are both ROOT side-to-move (`mover`)-relative
 * EngineScores from one completed request — NOT the post-move opponent-to-move
 * scores `classifyMoveAdvanced` takes. Routing root scores through that
 * classifier's argument order would misclassify, so this uses a truthful root
 * contract while sharing `calculateWinChance` / `checkMateEvents` /
 * `classifyWinChanceDrop`. `isBestMove` (played UCI === best UCI) short-circuits to
 * `best`. Pinned to the Python `classify_root_alternative` by the shared golden
 * fixture `backend/tests/fixtures/root_classification_vectors.json`.
 */
export const classifyRootAlternative = (input: {
  bestScore: EngineScore
  playedScore: EngineScore
  mover: 'white' | 'black'
  isBestMove: boolean
}): MoveClassification => {
  const { bestScore, playedScore, mover, isBestMove } = input

  if (isBestMove) return 'best'

  // Both scores are already mover-relative (root, `mover` to move), so mate
  // transitions use scorePov === mover (no opponent-frame flip): best plays the
  // "prev/better" role, played the "next/worse" role.
  const mateResult = checkMateEvents(bestScore, playedScore, mover, mover)
  if (mateResult) return mateResult

  const bestWc = calculateWinChance(bestScore, mover)
  const playedWc = calculateWinChance(playedScore, mover)

  // Mover-relative loss from best to played (best is >= played for the mover).
  const drop = mover === 'white' ? bestWc - playedWc : playedWc - bestWc

  return classifyWinChanceDrop(drop)
}
