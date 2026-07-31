import type { BenchPhaseRecord } from '../benchRecord'
import { classifyRootAlternative, mateToCp } from '../../workers/analysisUtils'
import { compareRootScores } from '../../workers/compareRootScores'
import type { MoveClassification } from '../../workers/analysisUtils'
import type { PvSnapshot } from '../../workers/pvSnapshots'
import type { EngineScore } from '../../workers/stockfishMessages'
import type { CorpusPosition } from './corpus'
import type { NodeSearchRequest, NodeSearchResult } from './stockfish'
import {
  opponentToMoverScore,
  postToRootScore,
  rootToPostScore,
  terminalScoreAfterMove,
} from './terminal'

export const REFERENCE_DEPTHS = {
  primaryRoot: 26,
  primaryRestricted: 27,
  biasRoot: 27,
  biasPostPlayed: 26,
  biasResolution: 27,
} as const

/**
 * Reference searches are intentionally allowed to run far longer than ordinary
 * interactive analysis. A timeout is still finite so a wedged engine cannot
 * hang a resumable capture forever, but a merely slow depth-27 search is not
 * silently turned into an oracle decline by the interactive ten-minute default.
 */
export const REFERENCE_SEARCH_TIMEOUT_MS = 60 * 60_000

export type ReferenceDepths = {
  primaryRoot: number
  primaryRestricted: number
  biasRoot: number
  biasPostPlayed: number
  biasResolution: number
}

export type ReferenceEngine = {
  reset: (timeoutMs?: number) => Promise<number>
  search: (request: NodeSearchRequest) => Promise<NodeSearchResult>
}

export type ReferenceDeclineReason =
  | 'engine-error'
  | 'root-no-bestmove'
  | 'root-snapshot-rejected'
  | 'restricted-snapshot-rejected'
  | 'post-played-snapshot-rejected'
  | 'resolution-snapshot-rejected'
  | 'representation-conflict'
  | 'ordering-violation'

export type AcceptedReference = {
  status: 'accepted'
  bestMove: string
  playedMove: string
  bestRoot: EngineScore
  playedRoot: EngineScore
  bestPost: EngineScore
  playedPost: EngineScore
  deltaCp: number
  classification: MoveClassification
  bestLine: string[] | null
  resolutionUsed: boolean
  pEqualsB: boolean
}

export type DeclinedReference = {
  status: 'declined'
  reason: ReferenceDeclineReason
  detail: string
}

export type ReferenceVerdict = AcceptedReference | DeclinedReference

export type ReferenceObservation = {
  resetMs: number
  phases: BenchPhaseRecord[]
  verdict: ReferenceVerdict
}

export type PositionReferences = {
  positionId: string
  primary: ReferenceObservation
  bias: ReferenceObservation
  adjudication:
    | { status: 'adjudicated'; reference: AcceptedReference }
    | {
      status: 'unadjudicable'
      reason:
        | 'primary-declined'
        | 'bias-declined'
        | 'best-move-disagreement'
        | 'classification-disagreement'
      detail: string
    }
}

const decline = (
  reason: ReferenceDeclineReason,
  detail: string,
): DeclinedReference => ({ status: 'declined', reason, detail })

const engineErrorObservation = (
  resetMs: number,
  phases: BenchPhaseRecord[],
  error: unknown,
): ReferenceObservation => ({
  resetMs,
  phases,
  verdict: decline(
    'engine-error',
    error instanceof Error ? error.message : String(error),
  ),
})

const acceptedSlots = (
  search: NodeSearchResult,
  reason: ReferenceDeclineReason,
): PvSnapshot[] | DeclinedReference =>
  search.selection.accepted
    ? search.selection.slots
    : decline(reason, `${search.phase.name}: ${search.selection.reason}`)

const slotForMove = (slots: readonly PvSnapshot[], move: string): PvSnapshot | null =>
  slots.find((slot) => slot.pv[0] === move) ?? null

/** Mover-relative post score → the CP surface persisted by today's contract. */
export const moverPostScoreToCp = (score: EngineScore): number =>
  score.type === 'cp' ? score.value : -mateToCp(-score.value)

const sign = (value: number): -1 | 0 | 1 =>
  value === 0 ? 0 : value < 0 ? -1 : 1

type RankInput = {
  mover: 'white' | 'black'
  rootNamedBest: string
  playedMove: string
  namedBestRoot: EngineScore
  playedRoot: EngineScore
  jointFirstMove: string | null
  jointLines: ReadonlyMap<string, string[]>
  namedBestLine: string[] | null
  playedTerminal: boolean
  namedBestTerminal: boolean
  tieAuthority: 'joint' | 'root'
  resolutionUsed: boolean
}

/**
 * Apply §6.2 once two root-frame operands exist. This is the only constructor
 * for accepted references, so no caller can bypass ordering or CP-conflict checks.
 */
export const rankReference = (input: RankInput): ReferenceVerdict => {
  const relation = compareRootScores(input.playedRoot, input.namedBestRoot)
  let bestMove = input.rootNamedBest
  let bestRoot = input.namedBestRoot
  let bestLine = input.namedBestTerminal ? null : input.namedBestLine

  if (relation > 0) {
    bestMove = input.playedMove
    bestRoot = input.playedRoot
    bestLine = input.playedTerminal
      ? null
      : input.jointLines.get(input.playedMove) ??
        (input.playedMove === input.rootNamedBest ? input.namedBestLine : null)
  } else if (
    relation === 0 &&
    input.tieAuthority === 'joint' &&
    input.jointFirstMove === input.playedMove
  ) {
    bestMove = input.playedMove
    bestRoot = input.playedRoot
    bestLine = input.playedTerminal
      ? null
      : input.jointLines.get(input.playedMove) ?? null
  }

  if (bestMove === input.playedMove) {
    const post = rootToPostScore(input.playedRoot)
    return {
      status: 'accepted',
      bestMove,
      playedMove: input.playedMove,
      bestRoot: input.playedRoot,
      playedRoot: input.playedRoot,
      bestPost: post,
      playedPost: post,
      deltaCp: 0,
      classification: 'best',
      bestLine,
      resolutionUsed: input.resolutionUsed,
      pEqualsB: true,
    }
  }

  if (compareRootScores(bestRoot, input.playedRoot) < 0) {
    return decline(
      'ordering-violation',
      `played ${input.playedMove} outranks retained best ${bestMove}`,
    )
  }

  const bestPost = rootToPostScore(bestRoot)
  const playedPost = rootToPostScore(input.playedRoot)
  const deltaCp = moverPostScoreToCp(bestPost) - moverPostScoreToCp(playedPost)
  const typed = compareRootScores(bestRoot, input.playedRoot)
  if (sign(typed) !== sign(deltaCp)) {
    return decline(
      'representation-conflict',
      `typed relation ${typed} disagrees with raw CP delta ${deltaCp}`,
    )
  }
  if (deltaCp < 0) {
    return decline('ordering-violation', `negative raw CP delta ${deltaCp}`)
  }

  return {
    status: 'accepted',
    bestMove,
    playedMove: input.playedMove,
    bestRoot,
    playedRoot: input.playedRoot,
    bestPost,
    playedPost,
    deltaCp,
    classification: classifyRootAlternative({
      bestScore: bestRoot,
      playedScore: input.playedRoot,
      mover: input.mover,
      isBestMove: false,
    }),
    bestLine,
    resolutionUsed: input.resolutionUsed,
    pEqualsB: false,
  }
}

type RestrictedInput = {
  engine: ReferenceEngine
  position: CorpusPosition
  namedBest: string
  depth: number
  phaseReason: 'restricted-snapshot-rejected' | 'resolution-snapshot-rejected'
  resolutionUsed: boolean
}

const restrictedRanking = async (
  input: RestrictedInput,
): Promise<{ search: NodeSearchResult | null; verdict: ReferenceVerdict }> => {
  const played = input.position.playedMove
  const playedTerminal = terminalScoreAfterMove(input.position.fen, played)
  const bestTerminal = terminalScoreAfterMove(input.position.fen, input.namedBest)

  if (played === input.namedBest && playedTerminal) {
    return {
      search: null,
      verdict: {
        status: 'accepted',
        bestMove: played,
        playedMove: played,
        bestRoot: playedTerminal.root,
        playedRoot: playedTerminal.root,
        bestPost: playedTerminal.postMove,
        playedPost: playedTerminal.postMove,
        deltaCp: 0,
        classification: 'best',
        bestLine: null,
        resolutionUsed: input.resolutionUsed,
        pEqualsB: true,
      },
    }
  }

  const searchedMoves =
    played === input.namedBest
      ? [played]
      : [
        ...(!bestTerminal ? [input.namedBest] : []),
        ...(!playedTerminal ? [played] : []),
      ]

  if (searchedMoves.length === 0) {
    return {
      search: null,
      verdict: rankReference({
        mover: input.position.playerColor,
        rootNamedBest: input.namedBest,
        playedMove: played,
        namedBestRoot: bestTerminal!.root,
        playedRoot: playedTerminal!.root,
        jointFirstMove: null,
        jointLines: new Map(),
        namedBestLine: null,
        playedTerminal: true,
        namedBestTerminal: true,
        tieAuthority: 'root',
        resolutionUsed: input.resolutionUsed,
      }),
    }
  }

  const search = await input.engine.search({
    fen: input.position.fen,
    depth: input.depth,
    phase: 'other',
    searchmoves: searchedMoves,
    multipv: searchedMoves.length,
    timeoutMs: REFERENCE_SEARCH_TIMEOUT_MS,
  })
  const slots = acceptedSlots(search, input.phaseReason)
  if (!Array.isArray(slots)) return { search, verdict: slots }
  const lines = new Map(slots.map((slot) => [slot.pv[0], slot.pv]))
  const bestSlot = slotForMove(slots, input.namedBest)
  const playedSlot = slotForMove(slots, played)
  const namedBestRoot = bestTerminal?.root ?? bestSlot?.score ?? null
  const playedRoot = playedTerminal?.root ?? playedSlot?.score ?? null
  if (!namedBestRoot || !playedRoot) {
    return {
      search,
      verdict: decline(input.phaseReason, 'restricted batch did not yield both operands'),
    }
  }

  return {
    search,
    verdict: rankReference({
      mover: input.position.playerColor,
      rootNamedBest: input.namedBest,
      playedMove: played,
      namedBestRoot,
      playedRoot,
      jointFirstMove: slots[0]?.pv[0] ?? null,
      jointLines: lines,
      namedBestLine: bestTerminal ? null : bestSlot?.pv ?? null,
      playedTerminal: playedTerminal !== null,
      namedBestTerminal: bestTerminal !== null,
      tieAuthority:
        playedTerminal || bestTerminal || searchedMoves.length < 2 ? 'root' : 'joint',
      resolutionUsed: input.resolutionUsed,
    }),
  }
}

export const runPrimaryReference = async (
  engine: ReferenceEngine,
  position: CorpusPosition,
  depths: ReferenceDepths = REFERENCE_DEPTHS,
): Promise<ReferenceObservation> => {
  let resetMs = 0
  const phases: BenchPhaseRecord[] = []
  try {
    resetMs = await engine.reset()
    const root = await engine.search({
      fen: position.fen,
      depth: depths.primaryRoot,
      phase: 'root',
      timeoutMs: REFERENCE_SEARCH_TIMEOUT_MS,
    })
    phases.push({ ...root.phase, index: phases.length })
    if (!root.bestmove || root.bestmove === '(none)') {
      return { resetMs, phases, verdict: decline('root-no-bestmove', root.bestmove || 'empty') }
    }
    const rootSlots = acceptedSlots(root, 'root-snapshot-rejected')
    if (!Array.isArray(rootSlots)) return { resetMs, phases, verdict: rootSlots }

    const ranked = await restrictedRanking({
      engine,
      position,
      namedBest: root.bestmove,
      depth: depths.primaryRestricted,
      phaseReason: 'restricted-snapshot-rejected',
      resolutionUsed: false,
    })
    if (ranked.search) phases.push({ ...ranked.search.phase, index: phases.length })
    return { resetMs, phases, verdict: ranked.verdict }
  } catch (error) {
    return engineErrorObservation(resetMs, phases, error)
  }
}

export const runBiasReference = async (
  engine: ReferenceEngine,
  position: CorpusPosition,
  depths: ReferenceDepths = REFERENCE_DEPTHS,
): Promise<ReferenceObservation> => {
  let resetMs = 0
  const phases: BenchPhaseRecord[] = []
  try {
    resetMs = await engine.reset()
    const root = await engine.search({
      fen: position.fen,
      depth: depths.biasRoot,
      phase: 'root',
      timeoutMs: REFERENCE_SEARCH_TIMEOUT_MS,
    })
    phases.push({ ...root.phase, index: phases.length })
    if (!root.bestmove || root.bestmove === '(none)') {
      return { resetMs, phases, verdict: decline('root-no-bestmove', root.bestmove || 'empty') }
    }
    const rootSlots = acceptedSlots(root, 'root-snapshot-rejected')
    if (!Array.isArray(rootSlots)) return { resetMs, phases, verdict: rootSlots }
    const namedBestRoot = rootSlots[0].score

    const terminal = terminalScoreAfterMove(position.fen, position.playedMove)
    let playedRoot = terminal?.root ?? null
    if (!playedRoot) {
      const post = await engine.search({
        fen: position.fen,
        moves: [position.playedMove],
        depth: depths.biasPostPlayed,
        phase: 'post-played',
        timeoutMs: REFERENCE_SEARCH_TIMEOUT_MS,
      })
      phases.push({ ...post.phase, index: phases.length })
      const postSlots = acceptedSlots(post, 'post-played-snapshot-rejected')
      if (!Array.isArray(postSlots)) return { resetMs, phases, verdict: postSlots }
      playedRoot = postToRootScore(opponentToMoverScore(postSlots[0].score))
    }

    if (position.playedMove !== root.bestmove &&
        compareRootScores(playedRoot, namedBestRoot) > 0) {
      const resolved = await restrictedRanking({
        engine,
        position,
        namedBest: root.bestmove,
        depth: depths.biasResolution,
        phaseReason: 'resolution-snapshot-rejected',
        resolutionUsed: true,
      })
      if (resolved.search) phases.push({ ...resolved.search.phase, index: phases.length })
      return { resetMs, phases, verdict: resolved.verdict }
    }

    const verdict = rankReference({
      mover: position.playerColor,
      rootNamedBest: root.bestmove,
      playedMove: position.playedMove,
      namedBestRoot,
      playedRoot,
      jointFirstMove: null,
      jointLines: new Map(),
      namedBestLine: rootSlots[0].pv,
      playedTerminal: terminal !== null,
      namedBestTerminal: terminalScoreAfterMove(position.fen, root.bestmove) !== null,
      tieAuthority: 'root',
      resolutionUsed: false,
    })
    return { resetMs, phases, verdict }
  } catch (error) {
    return engineErrorObservation(resetMs, phases, error)
  }
}

export const adjudicatePosition = async (
  engine: ReferenceEngine,
  position: CorpusPosition,
  depths: ReferenceDepths = REFERENCE_DEPTHS,
): Promise<PositionReferences> => {
  const primary = await runPrimaryReference(engine, position, depths)
  const bias = await runBiasReference(engine, position, depths)
  if (primary.verdict.status !== 'accepted') {
    return {
      positionId: position.id,
      primary,
      bias,
      adjudication: {
        status: 'unadjudicable',
        reason: 'primary-declined',
        detail: `${primary.verdict.reason}: ${primary.verdict.detail}`,
      },
    }
  }
  if (bias.verdict.status !== 'accepted') {
    return {
      positionId: position.id,
      primary,
      bias,
      adjudication: {
        status: 'unadjudicable',
        reason: 'bias-declined',
        detail: `${bias.verdict.reason}: ${bias.verdict.detail}`,
      },
    }
  }
  if (primary.verdict.bestMove !== bias.verdict.bestMove) {
    return {
      positionId: position.id,
      primary,
      bias,
      adjudication: {
        status: 'unadjudicable',
        reason: 'best-move-disagreement',
        detail: `primary ${primary.verdict.bestMove}, bias ${bias.verdict.bestMove}`,
      },
    }
  }
  if (primary.verdict.classification !== bias.verdict.classification) {
    return {
      positionId: position.id,
      primary,
      bias,
      adjudication: {
        status: 'unadjudicable',
        reason: 'classification-disagreement',
        detail: `primary ${primary.verdict.classification}, bias ${bias.verdict.classification}`,
      },
    }
  }
  return {
    positionId: position.id,
    primary,
    bias,
    adjudication: { status: 'adjudicated', reference: primary.verdict },
  }
}
