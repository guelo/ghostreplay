import { useCallback, useEffect, useRef } from 'react'
import type {
  AnalysisWorkerResponse,
  AnalyzeMoveMessage,
} from '../workers/analysisMessages'
import { getSideToMove, playerToWhite, playerToWhiteMate } from '../workers/analysisUtils'
import { submitAnalysisEvidence } from '../utils/api'
import type { AnalysisEvidenceRow } from '../utils/api'

/**
 * Analysis-board evidence driver (g-cache-stronger-evals).
 *
 * Owns a SECOND Stockfish analyzer-worker instance (separate from the display
 * `stockfishWorker`) that re-runs the browser-game post-move protocol at depth 21
 * for the mainline move the user is dwelling on, then persists the coherent result
 * through the authenticated analysis-evidence endpoint. Runs only when a
 * `sessionId` is present; the ephemeral drill board never writes evidence.
 */

/** Depth for the evidence searches — deeper than the in-game default (17). */
export const EVIDENCE_SEARCH_DEPTH = 21

type AnalysisMessage = Extract<AnalysisWorkerResponse, { type: 'analysis' }>

const createRequestId = () => {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID()
  }
  return Math.random().toString(36).slice(2)
}

const evidenceKey = (fen: string, moveUci: string) => `${fen}::${moveUci}`

/**
 * Map a completed depth-21 analyzer result to an endpoint evidence row, or `null`
 * when the result is not coherent, complete, canonical evidence:
 *   - `canonical === true` (both post-move scores existed)
 *   - non-null `bestMove` (not "(none)"), `classification`, `bestEval`, `playedEval`
 *   - `bestLine.length > 1` (v2 requires a multi-move PV)
 *   - both white-relative evals resolve
 *
 * Evals are converted mover-relative -> white-relative; `eval_delta` is recomputed
 * from the white-relative evals (never `message.delta`) and clamped at `>= 0`,
 * making the row self-consistent with the server's resolver-complete-v2 contract.
 */
export const buildEvidenceRow = (
  fen: string,
  moveUci: string,
  message: AnalysisMessage,
): AnalysisEvidenceRow | null => {
  if (!message.canonical) return null
  if (!message.bestMove || message.bestMove === '(none)') return null
  if (!message.classification) return null
  if (message.bestEval === null || message.playedEval === null) return null
  if (!message.bestLine || message.bestLine.length <= 1) return null

  const turn = getSideToMove(fen)
  if (turn !== 'w' && turn !== 'b') return null
  const moverColor = turn === 'w' ? 'white' : 'black'

  const whiteBest = playerToWhite(message.bestEval, moverColor)
  const whitePlayed = playerToWhite(message.playedEval, moverColor)
  if (whiteBest === null || whitePlayed === null) return null

  const evalDelta = Math.max(
    turn === 'w' ? whiteBest - whitePlayed : whitePlayed - whiteBest,
    0,
  )

  return {
    fen,
    move_uci: moveUci,
    best_move_uci: message.bestMove,
    best_line_uci: message.bestLine,
    played_eval: whitePlayed,
    played_eval_mate: playerToWhiteMate(message.playedEvalMate, moverColor),
    best_eval: whiteBest,
    best_eval_mate: playerToWhiteMate(message.bestEvalMate, moverColor),
    eval_delta: evalDelta,
    classification: message.classification,
  }
}

/**
 * Evidence-driver hook. Exposes `requestEvidence` (start a depth-21 evidence search
 * for one mainline move) and `cancel` (abort the in-flight search — used on
 * navigation so an interrupted search never submits). Runs one search at a time and
 * dedupes by `(fen, move_uci)` completed this mount. No-ops entirely when
 * `sessionId` is absent (no worker is created).
 */
export const useAnalysisEvidence = (sessionId: string | undefined) => {
  const workerRef = useRef<Worker | null>(null)
  // Request id + key of the search currently in flight (one at a time).
  const activeIdRef = useRef<string | null>(null)
  const activeKeyRef = useRef<{ fen: string; move: string } | null>(null)
  // Keys analyzed to completion this mount — never re-run/re-submit them.
  const doneKeys = useRef<Set<string>>(new Set())

  useEffect(() => {
    // The driver only runs for a saved-game session. No sessionId -> no worker.
    if (!sessionId) return

    const worker = new Worker(
      new URL('../workers/analysisWorker.ts', import.meta.url),
      { type: 'module' },
    )
    workerRef.current = worker

    const handleMessage = (event: MessageEvent<AnalysisWorkerResponse>) => {
      const message = event.data
      if (message.type === 'analysis') {
        // Only the current in-flight request may submit; a stale/canceled id is
        // dropped so an interrupted search never writes.
        if (message.id !== activeIdRef.current) return
        const key = activeKeyRef.current
        activeIdRef.current = null
        activeKeyRef.current = null
        if (!key) return
        // Mark analyzed-to-completion so we never re-run this exact move.
        doneKeys.current.add(evidenceKey(key.fen, key.move))
        const row = buildEvidenceRow(key.fen, key.move, message)
        if (!row) return
        // Best-effort, like lookup: a rejected upgrade is never a hard error.
        void submitAnalysisEvidence(sessionId, [row]).catch(() => {})
      } else if (message.type === 'error') {
        // A scoped/unscoped error frees the slot without marking the key done, so
        // the move can be retried on a later dwell.
        if (message.id === undefined || message.id === activeIdRef.current) {
          activeIdRef.current = null
          activeKeyRef.current = null
        }
      }
    }

    worker.addEventListener('message', handleMessage)

    return () => {
      // Mirror useMoveAnalysis teardown: terminate the worker directly (the nested
      // engine worker is torn down with it). No 'terminate' postMessage — it would
      // race the synchronous terminate() below and be dropped.
      worker.removeEventListener('message', handleMessage)
      worker.terminate()
      workerRef.current = null
      activeIdRef.current = null
      activeKeyRef.current = null
    }
  }, [sessionId])

  const cancel = useCallback(() => {
    const worker = workerRef.current
    if (worker && activeIdRef.current) {
      worker.postMessage({ type: 'cancel-analysis', id: activeIdRef.current })
    }
    activeIdRef.current = null
    activeKeyRef.current = null
  }, [])

  const requestEvidence = useCallback(
    (fen: string, move: string, playerColor: 'white' | 'black') => {
      const worker = workerRef.current
      if (!sessionId || !worker) return
      const key = evidenceKey(fen, move)
      if (doneKeys.current.has(key)) return // dedupe completed keys
      // Already running this exact key -> let it finish.
      const active = activeKeyRef.current
      if (active && evidenceKey(active.fen, active.move) === key) return
      // A different move is dwelled now: cancel the in-flight search first so only
      // the latest position's evidence runs (one at a time).
      if (activeIdRef.current) {
        worker.postMessage({ type: 'cancel-analysis', id: activeIdRef.current })
      }
      const id = createRequestId()
      activeIdRef.current = id
      activeKeyRef.current = { fen, move }
      worker.postMessage({
        type: 'analyze-move',
        id,
        fen,
        move,
        playerColor,
        depth: EVIDENCE_SEARCH_DEPTH,
      } satisfies AnalyzeMoveMessage)
    },
    [sessionId],
  )

  return { requestEvidence, cancel }
}
