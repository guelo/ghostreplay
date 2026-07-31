import { performance } from 'node:perf_hooks'
import type { BenchMoveRecord, BenchPhaseRecord } from '../benchRecord'
import {
  BENCH_SCHEMA_VERSION,
  zeroedDivergences,
  zeroedRejections,
} from '../benchRecord'
import {
  classifyMove,
  classifyMoveAdvanced,
  computeAnalysisResult,
  mateForPlayer,
} from '../../workers/analysisUtils'
import type { MoveClassification } from '../../workers/analysisUtils'
import type { EngineScore } from '../../workers/stockfishMessages'
import type { CorpusPosition } from './corpus'
import type { NodeSearchRequest, NodeSearchResult } from './stockfish'
import { terminalScoreAfterMove } from './terminal'

export type CurrentProtocolEngine = {
  reset: (timeoutMs?: number) => Promise<number>
  search: (request: NodeSearchRequest) => Promise<NodeSearchResult>
}

export type CurrentProtocolOutcome = {
  result: NonNullable<BenchMoveRecord['result']>
  phases: BenchPhaseRecord[]
  resetMs: number
  error: string | null
}

const buildBestLine = (
  bestMove: string,
  rootPv: string[] | null,
  continuationPv: string[] | null,
): string[] => {
  if (rootPv && rootPv.length > 1 && rootPv[0] === bestMove) return rootPv
  if (continuationPv && continuationPv.length > 0) {
    return [bestMove, ...continuationPv]
  }
  return [bestMove]
}

/**
 * The shipping three-search protocol, with no deadline and no orchestration
 * shortcuts. One reset precedes the root; related phases intentionally share TT.
 */
export const runCurrentProtocol = async (
  engine: CurrentProtocolEngine,
  position: CorpusPosition,
  depth: number,
): Promise<CurrentProtocolOutcome> => {
  const resetMs = await engine.reset()
  const phases: BenchPhaseRecord[] = []
  const pushPhase = (search: NodeSearchResult) => {
    phases.push({ ...search.phase, index: phases.length })
  }

  const root = await engine.search({
    fen: position.fen,
    depth,
    phase: 'root',
  })
  pushPhase(root)
  const bestMove = root.bestmove
  if (!bestMove || bestMove === '(none)') {
    return {
      resetMs,
      phases,
      error: 'current protocol returned no legal best move for a corpus row',
      result: {
        bestMove: bestMove || '(none)',
        bestLine: [],
        bestEval: null,
        playedEval: null,
        bestEvalMate: null,
        playedEvalMate: null,
        delta: null,
        classification: null,
        canonical: false,
        capFired: false,
        stopReason: 'bestmove',
        reachedDepth: root.reachedDepth,
      },
    }
  }

  const playedTerminal = terminalScoreAfterMove(position.fen, position.playedMove)
  let postPlayedScore: EngineScore | null = playedTerminal?.postMove ?? null
  let playedSearch: NodeSearchResult | null = null
  if (!postPlayedScore) {
    playedSearch = await engine.search({
      fen: position.fen,
      moves: [position.playedMove],
      depth,
      phase: 'post-played',
    })
    pushPhase(playedSearch)
    postPlayedScore = playedSearch.score
  }

  let postBestScore = postPlayedScore
  let bestSearch: NodeSearchResult | null = null
  if (position.playedMove !== bestMove) {
    const bestTerminal = terminalScoreAfterMove(position.fen, bestMove)
    postBestScore = bestTerminal?.postMove ?? null
    if (!postBestScore) {
      bestSearch = await engine.search({
        fen: position.fen,
        moves: [bestMove],
        depth,
        phase: 'post-best',
      })
      pushPhase(bestSearch)
      postBestScore = bestSearch.score
    }
  }

  const sideToMove = position.playerColor === 'white' ? 'w' : 'b'
  const opponentToMove = sideToMove === 'w' ? 'b' : 'w'
  const { bestEval, playedEval, delta } = computeAnalysisResult({
    bestMove,
    playedMove: position.playedMove,
    postPlayedScore,
    postBestScore,
    sideToMove,
    playerColor: position.playerColor,
  })
  const playedEvalMate = mateForPlayer(
    postPlayedScore,
    opponentToMove,
    position.playerColor,
  )
  const bestEvalMate = bestMove === position.playedMove
    ? playedEvalMate
    : mateForPlayer(postBestScore, opponentToMove, position.playerColor)
  const isBestMove = bestMove === position.playedMove
  let classification: MoveClassification | null = null
  let canonical = false
  if (postBestScore && postPlayedScore) {
    classification = classifyMoveAdvanced({
      prevScore: postBestScore,
      nextScore: postPlayedScore,
      scorePov: position.playerColor === 'white' ? 'black' : 'white',
      mover: position.playerColor,
      isBestMove,
    })
    canonical = true
  } else {
    classification = classifyMove(delta)
  }

  const error =
    delta !== null && delta < 0
      ? `ordering inversion: played ${position.playedMove} scored ${Math.abs(delta)}cp above named best ${bestMove}`
      : null

  return {
    resetMs,
    phases,
    error,
    result: {
      bestMove,
      bestLine: buildBestLine(
        bestMove,
        root.pv,
        isBestMove ? playedSearch?.pv ?? null : bestSearch?.pv ?? null,
      ),
      bestEval,
      playedEval,
      bestEvalMate,
      playedEvalMate,
      delta,
      classification,
      canonical,
      capFired: false,
      stopReason: 'bestmove',
      reachedDepth: root.reachedDepth,
    },
  }
}
export type CurrentMoveRecordInput = {
  engine: CurrentProtocolEngine
  position: CorpusPosition
  depth: number
  runId: string
  seq: number
  runElapsedMs: number
  cohort: 'cold' | 'warm'
  workerBootMs: number | null
}

export const measureCurrentPosition = async (
  input: CurrentMoveRecordInput,
): Promise<BenchMoveRecord> => {
  const started = performance.now()
  const outcome = await runCurrentProtocol(input.engine, input.position, input.depth)
  const e2eMs = performance.now() - started

  const rejections = zeroedRejections()
  const divergenceByReason = zeroedDivergences()
  let legacySelectorDivergence = 0
  for (const phase of outcome.phases) {
    if (phase.snapshot && !phase.snapshot.accepted) {
      rejections[phase.snapshot.reason] += 1
    }
    if (phase.legacyDivergence) {
      divergenceByReason[phase.legacyDivergence] += 1
      legacySelectorDivergence += 1
    }
  }
  const nodes = outcome.phases
    .map((phase) => phase.nodes)
    .filter((value): value is number => value !== null)
  const times = outcome.phases
    .map((phase) => phase.timeMs)
    .filter((value): value is number => value !== null)

  return {
    kind: 'move',
    schemaVersion: BENCH_SCHEMA_VERSION,
    runId: input.runId,
    seq: input.seq,
    blockIndex: 0,
    repeat: 0,
    arm: 'current',
    orderIndex: 0,
    positionId: input.position.id,
    fen: input.position.fen,
    playedMove: input.position.playedMove,
    playerColor: input.position.playerColor,
    thermalIndex: null,
    cohort: input.cohort,
    warmup: false,
    workerRestarted: false,
    engineRebuilt: false,
    requestedDepth: input.depth,
    e2eMs,
    runElapsedMs: input.runElapsedMs,
    workerBootMs: input.workerBootMs,
    resetMs: outcome.resetMs,
    phases: outcome.phases,
    searchCount: outcome.phases.length,
    totalNodes: nodes.length > 0 ? nodes.reduce((sum, value) => sum + value, 0) : null,
    totalEngineMs: times.length > 0 ? times.reduce((sum, value) => sum + value, 0) : null,
    result: outcome.result,
    pEqualsB: outcome.result.bestMove === input.position.playedMove,
    rejections,
    legacySelectorDivergence,
    divergenceByReason,
    progressPings: 0,
    streamingPings: 0,
    error: outcome.error,
  }
}
