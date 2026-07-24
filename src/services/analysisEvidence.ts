import { useCallback, useEffect, useLayoutEffect, useRef } from 'react'
import { Chess } from 'chess.js'
import type { CompletedRootAnalysis, EngineInfo, EngineScore } from '../workers/stockfishMessages'
import {
  classifyRootAlternative,
  getSideToMove,
  mateForPlayer,
  normalizeScore,
} from '../workers/analysisUtils'
import { submitAnalysisEvidence } from '../utils/api'
import type { AnalysisEvidenceRow, MoveUpgrade } from '../utils/api'

/** Fired when an accepted write returns a stronger re-annotation for a move. */
export type OnAcceptedEvidence = (
  fen: string,
  moveUci: string,
  upgrade: MoveUpgrade,
) => void

/**
 * Analysis-board evidence reuse layer (g-reuse-d21-search).
 *
 * NO private analyzer worker and NO extra Stockfish search. Instead it REUSES the
 * already-completed, unrestricted visible depth-21 MultiPV-3 snapshot that the
 * analysis board runs for arrows/lines: when the exact played mainline move appears
 * in the completed lines it derives a same-search evidence row and submits it
 * through the authenticated endpoint (producer "visible-multipv-v1"). When the
 * played move is absent it does nothing — no targeted search — and the existing
 * depth-17 evidence is retained. Runs only for a saved-game session with visible
 * engine analysis enabled.
 */

/** The required terminal depth for a reusable visible search. */
export const EVIDENCE_SEARCH_DEPTH = 21
const REQUIRED_MULTIPV = 3

/** Context captured from the board at the moment a visible search completes. */
export type EvidenceReuseContext = {
  // The next mainline move's exact wire fields (never reconstructed).
  fenBefore: string | null
  moveUci: string | null
  // True only on the mainline (not inside a user variation).
  isMainline: boolean
  // True only when visible engine analysis is enabled (showEngineArrows).
  engineEnabled: boolean
}

/** Outcome categories for the focused development diagnostic (§11). */
export type ReuseOutcome =
  | 'submitted'
  | 'unrepresented'
  | 'restricted'
  | 'incomplete'
  | 'sparse'
  | 'stale'
  | 'duplicate'
  | 'not-eligible'
  | 'network-failed-retryable'
  | 'endpoint-rejected'
  // A POST that completed after an in-place session change was dropped (§4.2).
  | 'stale-session'

const logReuse = (outcome: ReuseOutcome, detail?: string) => {
  if (import.meta.env.DEV) {
    // Focused: one line per eligibility/outcome decision, never per engine info line.
    console.debug(`[analysisEvidence] ${outcome}${detail ? ` ${detail}` : ''}`)
  }
}

const usableScore = (score: EngineScore | undefined): score is EngineScore =>
  !!score && (score.type === 'cp' || score.type === 'mate')

const isDenseSlot = (line: EngineInfo): boolean =>
  !!line.pv &&
  line.pv.length >= 1 &&
  line.depth === EVIDENCE_SEARCH_DEPTH &&
  usableScore(line.score)

export type DeriveResult =
  | { row: AnalysisEvidenceRow }
  | { skip: Exclude<ReuseOutcome, 'submitted' | 'duplicate' | 'network-failed-retryable' | 'endpoint-rejected' | 'stale-session'> }

/**
 * Pure eligibility gate + row builder over ONE completed snapshot (§4, §5). Returns
 * either the atomic evidence row or a skip reason. No engine search, no async.
 */
export const deriveEvidenceRow = (
  snapshot: CompletedRootAnalysis,
  fenBefore: string,
  moveUci: string,
): DeriveResult => {
  // The snapshot must be exactly the displayed position / next move fen_before.
  if (snapshot.fen !== fenBefore) return { skip: 'stale' }

  // Only the unrestricted depth-21 MultiPV-3 shape is reusable (§4 gate 6).
  if (snapshot.limit.type !== 'depth' || snapshot.limit.value !== EVIDENCE_SEARCH_DEPTH) {
    return { skip: 'restricted' }
  }
  if (snapshot.multipv !== REQUIRED_MULTIPV) return { skip: 'restricted' }
  if (snapshot.searchmoves && snapshot.searchmoves.length > 0) return { skip: 'restricted' }

  const turn = getSideToMove(fenBefore)
  if (turn !== 'w' && turn !== 'b') return { skip: 'incomplete' }
  const moverColor = turn === 'w' ? 'white' : 'black'

  // Slot density relative to legal-move count (§4.1): exactly min(3, legalMoves)
  // dense slots, each PV-bearing, at depth 21, with a usable score.
  let legalMoveCount: number
  try {
    legalMoveCount = new Chess(fenBefore).moves().length
  } catch {
    return { skip: 'incomplete' }
  }
  const requiredDense = Math.min(REQUIRED_MULTIPV, legalMoveCount)
  const denseSlots = snapshot.lines.filter(isDenseSlot)
  if (denseSlots.length !== requiredDense) return { skip: 'sparse' }

  // Line 1 (multipv 1) must have a usable score and a multi-move PV beginning with
  // bestMove (§4 gate 7).
  const bestLine = snapshot.lines.find((l) => l.multipv === 1)
  if (
    !bestLine ||
    !usableScore(bestLine.score) ||
    !bestLine.pv ||
    bestLine.pv.length <= 1 ||
    bestLine.depth !== EVIDENCE_SEARCH_DEPTH ||
    bestLine.pv[0] !== snapshot.bestMove
  ) {
    return { skip: 'incomplete' }
  }

  // The played move must equal pv[0] of one complete line at the terminal depth
  // (§4 gates 8-9). Absent -> retain existing evidence, launch nothing.
  const playedLine = snapshot.lines.find((l) => l.pv?.[0] === moveUci)
  if (!playedLine) return { skip: 'unrepresented' }
  if (
    !usableScore(playedLine.score) ||
    !playedLine.pv ||
    playedLine.depth !== EVIDENCE_SEARCH_DEPTH
  ) {
    return { skip: 'incomplete' }
  }

  const bestScore = bestLine.score
  const playedScore = playedLine.score
  const bestCpWhite = normalizeScore(bestScore, turn)
  const playedCpWhite = normalizeScore(playedScore, turn)
  if (bestCpWhite === null || playedCpWhite === null) return { skip: 'incomplete' }

  const isBest = moveUci === bestLine.pv[0]
  const evalDelta = Math.max(
    turn === 'w' ? bestCpWhite - playedCpWhite : playedCpWhite - bestCpWhite,
    0,
  )
  // Root-alternative classification from the same-search mover-relative scores; the
  // backend independently rederives and enforces it for every row.
  const classification = classifyRootAlternative({
    bestScore,
    playedScore,
    mover: moverColor,
    isBestMove: isBest,
  })

  return {
    row: {
      fen: fenBefore,
      move_uci: moveUci,
      best_move_uci: bestLine.pv[0],
      best_line_uci: bestLine.pv,
      played_eval: playedCpWhite,
      played_eval_mate:
        playedScore.type === 'mate' ? mateForPlayer(playedScore, turn, 'white') : null,
      best_eval: bestCpWhite,
      best_eval_mate:
        bestScore.type === 'mate' ? mateForPlayer(bestScore, turn, 'white') : null,
      eval_delta: evalDelta,
      classification,
    },
  }
}

/**
 * A stable content signature over the evidence-bearing snapshot/row (§4.2). Two
 * completed searches sharing fen/options/bestMove but differing in the played-line
 * score, PVs, or classification produce DISTINCT signatures (a signature that
 * omitted result content would collapse genuinely different evidence).
 */
export const completedSearchSignature = (
  sessionId: string,
  snapshot: CompletedRootAnalysis,
  row: AnalysisEvidenceRow,
): string => {
  const bestLine = snapshot.lines.find((l) => l.multipv === 1)
  const playedLine = snapshot.lines.find((l) => l.pv?.[0] === row.move_uci)
  const lineSig = (l: EngineInfo | undefined) => ({
    st: l?.score?.type ?? null,
    sv: l?.score?.value ?? null,
    d: l?.depth ?? null,
    pv: l?.pv ?? null,
  })
  return JSON.stringify([
    sessionId,
    row.fen,
    row.move_uci,
    snapshot.limit.type,
    snapshot.limit.value,
    snapshot.multipv,
    snapshot.searchmoves ? [...snapshot.searchmoves].sort() : null,
    lineSig(bestLine),
    lineSig(playedLine),
    row.best_move_uci,
    row.classification,
    row.eval_delta,
  ])
}

/**
 * Evidence reuse hook. Exposes `considerCompletedSearch(snapshot, context)` — call
 * it whenever a visible search resolves. It applies the eligibility gate, builds
 * the row synchronously, dedupes by content signature, and submits at most once per
 * answered signature. No worker is created regardless of `sessionId`.
 *
 * When an accepted write returns a `MoveUpgrade`, `onAcceptedEvidence` fires so the
 * caller can patch the open MoveList immediately (g-xox0 Part B).
 */
export const useAnalysisEvidence = (
  sessionId: string | undefined,
  onAcceptedEvidence?: OnAcceptedEvidence,
) => {
  // Two disjoint dedupe sets guard BOTH resubmitting an answered search AND
  // launching a duplicate while the first POST is still open (§4.2).
  const inFlight = useRef<Set<string>>(new Set())
  const terminal = useRef<Set<string>>(new Set())
  const onAcceptedRef = useRef<OnAcceptedEvidence | undefined>(onAcceptedEvidence)
  useEffect(() => {
    onAcceptedRef.current = onAcceptedEvidence
  }, [onAcceptedEvidence])

  // Monotonic session generation. Clearing the dedupe sets is not enough on its
  // own: a POST launched under session A can resolve AFTER an in-place session
  // change, and its handler would otherwise repopulate the freshly-cleared
  // `terminal` set and invoke session B's `onAcceptedEvidence` with session A's
  // row. Each completion captures the generation at launch and no-ops if the
  // session has advanced since (§4.2).
  const generation = useRef(0)

  // Bump the generation and clear both sets whenever the session changes. This
  // MUST be commit-synchronous, not a passive effect: React can commit session B
  // and then defer passive effects, and if session A's POST settles (a microtask)
  // in that gap the completion handler would still read the pre-bump generation
  // and fire the stale callback. useLayoutEffect runs inside the commit, before
  // any such microtask, so the guard is already armed when A's POST resolves.
  useLayoutEffect(() => {
    generation.current += 1
    inFlight.current.clear()
    terminal.current.clear()
  }, [sessionId])

  const considerCompletedSearch = useCallback(
    (snapshot: CompletedRootAnalysis, context: EvidenceReuseContext) => {
      if (!sessionId) return
      if (!context.engineEnabled) {
        logReuse('not-eligible', 'engine-disabled')
        return
      }
      if (!context.isMainline) {
        logReuse('not-eligible', 'variation')
        return
      }
      if (!context.fenBefore || !context.moveUci) {
        logReuse('not-eligible', 'no-wire-key')
        return
      }

      const result = deriveEvidenceRow(snapshot, context.fenBefore, context.moveUci)
      if ('skip' in result) {
        logReuse(result.skip)
        return
      }
      const { row } = result

      const signature = completedSearchSignature(sessionId, snapshot, row)
      if (inFlight.current.has(signature) || terminal.current.has(signature)) {
        logReuse('duplicate')
        return
      }

      const launchGeneration = generation.current
      inFlight.current.add(signature)
      void submitAnalysisEvidence(sessionId, [row])
        .then((results) => {
          // Ignore a completion that landed after an in-place session change: its
          // signature belongs to a prior session and its callback target is stale.
          if (generation.current !== launchGeneration) {
            logReuse('stale-session')
            return
          }
          // Any HTTP 200 (accept OR per-row rejection) is terminal.
          inFlight.current.delete(signature)
          terminal.current.add(signature)
          const res = results.find(
            (r) => r.fen === row.fen && r.move_uci === row.move_uci,
          )
          if (res?.upgrade) {
            logReuse('submitted')
            onAcceptedRef.current?.(row.fen, row.move_uci, res.upgrade)
          } else {
            logReuse('endpoint-rejected', res?.reason)
          }
        })
        .catch(() => {
          if (generation.current !== launchGeneration) {
            logReuse('stale-session')
            return
          }
          // Network failure / throw (no HTTP response): clear inFlight so a later
          // completion of the same signature may retry.
          inFlight.current.delete(signature)
          logReuse('network-failed-retryable')
        })
    },
    [sessionId],
  )

  return { considerCompletedSearch }
}
