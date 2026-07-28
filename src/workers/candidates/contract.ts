/**
 * The contract every candidate arm runs against (g-two-search-grade §15.1
 * C3/C4).
 *
 * C4 is enforced by what this type does NOT offer. An arm gets a search function
 * and three pure helpers; it never sees the engine handle, `activeSearch`, the
 * heartbeat, the reset waiters, `canceledAnalyses`, or `ctx.postMessage`, and it
 * cannot construct or extend a budget — `search` already carries the ONE shared
 * `AnalysisBudget` the whole analyze-move is bounded by (§8).
 *
 * C3 is enforced by `CandidateOutcome` being exactly the shared tail's inputs.
 * An arm returns those and stops; `computeAnalysisResult`, the mate fields, the
 * classification, `buildBestLine` and the `postMessage` all live below the one
 * dispatch point and are shared with the current protocol.
 */

import type { EngineScore } from '../stockfishMessages'
import type { CandidateArm } from './benchMessages'

/**
 * The optional half of §15.1 C5's neutral `runSearch` extension, as an arm sees
 * it. Omitting all of them emits today's exact commands.
 */
export type CandidateSearchOptions = {
  /** Defaults to the request's depth. §2 bounds analyze-move `go depth` below 21. */
  depth?: number
  /** Restrict the search to these root moves (§3.2). Omitted → unrestricted. */
  searchmoves?: string[]
  /** `setoption name MultiPV value K` immediately before `position`/`go` (§8). */
  multipv?: number
  /** Per-info-line callback, for the eval-bar streaming updates. */
  onInfo?: (score: EngineScore, depth: number) => void
}

/** What one search yields. A subset of the worker's own `SearchResult`. */
export type CandidateSearchResult = {
  bestmove: string
  score: EngineScore | null
  pv: string[] | null
  /** True only when the shared analyze-move deadline issued the `stop`. */
  capFired: boolean
  reachedDepth: number | null
}

export type CandidateContext = {
  /** The analyze-move's root position. */
  fen: string
  /** The move being graded, in UCI. */
  playedMove: string
  playerColor: 'white' | 'black'
  sideToMove: 'w' | 'b'
  /** What the CALLER asked for — `N`. An arm derives its own depths from it. */
  requestedDepth: number
  /**
   * Run one search from `fen` after `moves`, on the shared budget.
   *
   * Opaque by design: the arm names moves and options, and the worker owns
   * everything else — the reset policy, the deadline, the stop grace, the
   * heartbeat, and MultiPV restoration (C6).
   */
  search: (
    moves: string[],
    options?: CandidateSearchOptions,
  ) => Promise<CandidateSearchResult>
  /** Throws `AnalysisCanceledError` when this request has been canceled. */
  checkCanceled: () => void
  /**
   * The exact score after `move` when it ends the game, else null. A terminal
   * position is not searchable, and its score is deterministic.
   */
  terminalScoreAfterMove: (move: string) => EngineScore | null
  /**
   * Post one `analysis-streaming` eval-bar update for the post-played search.
   * Passed as `onInfo` rather than called directly, so the arm never builds a
   * response message itself (C3).
   */
  streamPlayed: (score: EngineScore, depth: number) => void
}

/**
 * Exactly the inputs the shared tail consumes — no more, so an arm cannot reach
 * an emitted value except through it.
 *
 * `postBestScore: null` means the arm DECLINES to grade this row: the tail emits
 * `canonical: false` and no classification rather than inventing a comparison.
 */
export type CandidateOutcome = {
  bestMove: string
  /** The root search's principal variation, for `buildBestLine`. */
  rootPv: string[] | null
  /** The continuation after `bestMove`, when a search of that position ran. */
  continuationPv: string[] | null
  postPlayedScore: EngineScore | null
  postBestScore: EngineScore | null
  /** True when ANY constituent search was cut short by the shared deadline. */
  capFired: boolean
  /** Deepest completed ROOT iteration, mirroring the current protocol. */
  reachedDepth: number | null
}

export type CandidateProtocol = {
  readonly arm: CandidateArm
  run: (context: CandidateContext) => Promise<CandidateOutcome>
}
