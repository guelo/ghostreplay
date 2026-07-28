/// <reference lib="webworker" />

import { Chess } from "chess.js";
import stockfishEngineUrl from "stockfish/bin/stockfish-18-lite-single.js?url";
import stockfishWasmUrl from "stockfish/bin/stockfish-18-lite-single.wasm?url";
import type {
  AnalysisProtocol,
  AnalysisStopReason,
  AnalysisWorkerRequest,
  AnalysisWorkerResponse,
  AnalyzeMoveMessage,
} from "./analysisMessages";
import type { EngineScore } from "./stockfishMessages";
import { CANDIDATE_PROTOCOLS, availableCandidateArms } from "./candidates";
import type {
  BenchAnalyzeMoveMessage,
  BenchInitMessage,
  BenchReadyMessage,
} from "./candidates/benchMessages";
import { isCandidateArm } from "./candidates/benchMessages";
import type { CandidateContext, CandidateOutcome, CandidateProtocol } from "./candidates/contract";
import { parseUciInfoLine } from "./parseInfo";
import {
  admitInfoLine,
  createSnapshotAssembler,
  recordCanceledSearch,
  recordLegacySelectorDivergence,
} from "./pvSnapshots";
import type { SnapshotAssembler } from "./pvSnapshots";
import {
  getSideToMove,
  computeAnalysisResult,
  scoreForPlayer,
  mateForPlayer,
  classifyMove,
  classifyMoveAdvanced,
} from "./analysisUtils";
import type { MoveClassification } from "./analysisUtils";

const ctx = self as DedicatedWorkerGlobalScope;

let engineReady = false;
let engine: Worker | null = null;

/**
 * The engine instance a fatal error was reported for, so its later output can be
 * ignored.
 *
 * `handleEngineError` deliberately does NOT terminate the engine (see its
 * comment), which leaves it as the current engine with its listeners attached
 * and still able to emit. Clearing `engineReady` is not enough on its own: a
 * `readyok` still in flight arrives to an EMPTY `resetAckQueue` — `failAllRequestReady`
 * just drained it — so `handleEngineLine` reads it as the init handshake, sets
 * `engineReady` back to true, posts `ready`, and drains queued analyses onto the
 * engine that just died. Identity is tracked rather than a bare boolean so the
 * flag cannot outlive the instance it describes and poison its replacement.
 */
let failedEngine: Worker | null = null;

/**
 * The current engine's own listeners, kept so `terminate` can detach exactly the
 * pair it attached. They are per-instance closures (see `ensureEngine`), so the
 * function identities differ between engines and a shared reference would remove
 * nothing.
 */
type EngineListeners = {
  message: (event: MessageEvent<string>) => void;
  error: (event: ErrorEvent) => void;
};
let engineListeners: EngineListeners | null = null;

/**
 * §15.1 C7's FIRST key: benchmark mode, set once at worker init by `bench-init`
 * and answered with `bench-ready`.
 *
 * A module-level flag rather than a per-message one, so BOTH keys are required
 * and neither is enough alone: without this, a stray `arm` on an analyze-move is
 * never even read; with it but no `arm`, the current protocol still runs. A
 * production build has no way to send `bench-init` — it is not on
 * `AnalysisWorkerRequest` — and §15.2 deletes it with `candidates/`.
 */
let benchMode = false;

/**
 * Total wall-clock budget for ONE analyze-move — reset + all three searches, not
 * per search (g-mk1d §1.6).
 *
 * SHIPS DORMANT (`null` == no deadline, every timer below inert). This is not
 * timidity: a healthy depth-17 analyze-move can legitimately run for many seconds
 * (g-f2mg recorded a SINGLE depth-14 iteration at 8.7s, and an analyze-move runs
 * up to three sequential searches DEEPER than that). So ANY finite value small
 * enough to bound live-play latency WILL truncate healthy depth-17 searches, demote
 * those moves to `browser-game-v1`, and change their classifications. There is no
 * "conservative provisional value" that both bounds latency and fires only on a
 * genuine hang — at depth 17, bounding latency IS the behavior change. The finite
 * value is therefore ratified and enabled TOGETHER with the `MAX_DEVICE_DEPTH`
 * raise, under the same benchmark gate, so this landing is a true no-op.
 *
 * ORTHOGONAL to the inactivity watchdog, not "below" it. The coordinator's ~8s
 * `ANALYSIS_INACTIVITY_TIMEOUT_MS` is an INACTIVITY bound, continuously reset by
 * the 1s liveness heartbeat; a healthy search can run far longer than 8s of total
 * wall-clock without tripping it. This is a TOTAL-DURATION bound. The two measure
 * different things, so there is NO ordering constraint between them — a ratified
 * `MAX_ANALYSIS_MS` may legitimately sit well above 8s.
 */
let MAX_ANALYSIS_MS: number | null = null;

/** Test-only: inject a finite budget so the dormant plumbing can be exercised. */
export const __setMaxAnalysisMsForTests = (value: number | null) => {
  MAX_ANALYSIS_MS = value;
};

/**
 * Atomic snapshot capture (g-atomic-snapshots, g-two-search-grade §4).
 *
 * INSTRUMENTATION ONLY. Batch assembly runs beside the legacy accumulators and
 * writes to a separate field; nothing on this path reads a snapshot. The atomic
 * selector is never named in this file — the one call to it goes through
 * `recordLegacySelectorDivergence`, which returns void — so capturing snapshots
 * provably cannot change default output. A test pins that absence. Production
 * switches selectors at §12 step 9, not here.
 */
let snapshotInstrumentationEnabled = true;

/**
 * Test-only: disable capture so the selector-identity test can prove the legacy
 * arm's (score, pv, reachedDepth) are the same with assembly on and off.
 */
export const __setSnapshotInstrumentationForTests = (enabled: boolean) => {
  snapshotInstrumentationEnabled = enabled;
};

/**
 * How long a deadline-issued `stop` may go unanswered before we declare the
 * engine wedged (g-mk1d code review, P2).
 *
 * `stop` is a REQUEST, not a guarantee: the worker cannot force the nested
 * Stockfish worker to emit `bestmove`. Without this second timer the deadline
 * path degenerates into an unbounded wait — and worse than a plain hang, because
 * `activeSearch` stays non-null so the unconditional liveness heartbeat keeps
 * vouching for the request and the coordinator's inactivity watchdog never trips.
 * The queue would then be wedged with no bound anywhere in the system.
 *
 * So MAX_ANALYSIS_MS alone does NOT bound an analyze-move; MAX_ANALYSIS_MS plus
 * this grace does. A healthy engine answers `stop` within a single search
 * iteration, so 2s is generous — exceeding it means genuinely stuck, and we take
 * the same fatal path as the reset timeout: terminate and rebuild the engine.
 *
 * ONE grace per analyze-move, not one per search (see `AnalysisBudget`): an
 * analyze-move runs up to three sequential searches, and every search entered
 * after the deadline stops immediately, so a per-search grace would let a move
 * run MAX_ANALYSIS_MS + 3x this value — the very ~3x overshoot the SHARED
 * deadline exists to prevent, reintroduced one level down.
 */
const STOP_GRACE_MS = 2000;

/**
 * The ONE wall-clock budget an analyze-move's reset + up-to-three searches share.
 *
 * Both fields are absolute timestamps so a search entered late inherits what is
 * LEFT of the move's budget instead of restarting it. Mutable by design:
 * `graceExpiresAt` is written by whichever search issues the FIRST deadline
 * `stop` and then read by every search after it.
 */
type AnalysisBudget = {
  /** When the search budget expires. `Infinity` while MAX_ANALYSIS_MS is dormant. */
  deadlineAt: number;
  /**
   * When the move stops waiting on an unanswered `stop`. `null` until the first
   * deadline `stop` is issued; from then on it bounds the WHOLE remaining move.
   */
  graceExpiresAt: number | null;
};

/** A budget that never fires — the shape `runSearch` assumes when none is passed. */
const dormantBudget = (): AnalysisBudget => ({
  deadlineAt: Infinity,
  graceExpiresAt: null,
});

/** Why a search stopped. Provenance honesty keys off this, never a depth compare. */
type StopReason = AnalysisStopReason;

type SearchResult = {
  bestmove: string;
  score: EngineScore | null;
  pv: string[] | null;
  /**
   * True ONLY when the shared analyze-move deadline issued the `stop`.
   *
   * Deliberately an EXPLICIT signal rather than `reachedDepth < requestedDepth`,
   * which is unreliable in BOTH directions: the stop can fire just AFTER
   * `info depth N` is reported (reached == requested, yet the run WAS truncated),
   * and a natural early termination — a forced mate, or no further legal
   * improvement — finishes BELOW N with no cap at all.
   */
  capFired: boolean;
  stopReason: StopReason;
  /** Deepest completed iteration seen on this search's info lines. */
  reachedDepth: number | null;
};
type ActiveSearch = {
  resolve: (value: SearchResult) => void;
  reject: (error: Error) => void;
  lastScore: EngineScore | null;
  lastPv: string[] | null;
  lastDepth: number | null;
  capFired: boolean;
  capTimer: ReturnType<typeof setTimeout> | null;
  /** Armed when the deadline sends `stop`; fires only if `bestmove` never comes. */
  graceTimer: ReturnType<typeof setTimeout> | null;
  onInfo?: (score: EngineScore, depth: number) => void;
  /** The depth actually requested, so a snapshot can be judged stale or not. */
  requestedDepth: number;
  /**
   * Whether a move ends the game in THIS search's root position, for §4.2's
   * exemption from the two-move-minimum. Carried per search because the selector
   * runs at `bestmove`, by which point the caller's position is long gone.
   */
  endsGame: (uci: string) => boolean;
  /**
   * Set when this search's request is canceled. Tracked on the SEARCH rather than
   * read from `canceledAnalyses` at `bestmove`: cancel rejects the reset waiter,
   * so analyzeMove can unwind — clearing the tombstone and `activeAnalysisId` —
   * before the engine answers `stop`, and by then the id may belong to the next
   * request.
   */
  canceled: boolean;
  /** Instrumentation only — never read by the fields resolved above. */
  snapshots: SnapshotAssembler;
};
let activeSearch: ActiveSearch | null = null;

/** Clear both of a search's deadline timers so neither outlives it. */
const clearSearchTimers = (search: ActiveSearch) => {
  if (search.capTimer !== null) {
    clearTimeout(search.capTimer);
    search.capTimer = null;
  }
  if (search.graceTimer !== null) {
    clearTimeout(search.graceTimer);
    search.graceTimer = null;
  }
};
let activeAnalysisId: string | null = null;
const canceledAnalyses = new Set<string>();

// Inactivity-watchdog liveness (analysis-progress). The coordinator/hook arm a
// per-request inactivity timer; any progress ping resets it. Throttle the
// info-line pings so a chatty Stockfish iteration does not flood the channel.
const PROGRESS_THROTTLE_MS = 250;
let lastProgressPostMs = Number.NEGATIVE_INFINITY;

// Wall-clock liveness heartbeat. Stockfish `info` output is event-driven (per
// completed depth), NOT time-periodic, so a single long iteration can exceed the
// coordinator's inactivity window with zero info lines (g-f2mg: an 8.7s depth-14
// iteration false-killed a live search whose only fault was that one iteration
// crossed no info-reporting boundary). The heartbeat posts analysis-progress on a
// fixed clock for as long as a search is active — UNCONDITIONALLY, not gated on
// engine output — so the coordinator watchdog tracks worker liveness instead of
// Stockfish's reporting cadence. Period must sit comfortably under
// ANALYSIS_INACTIVITY_TIMEOUT_MS (8s); ~1s gives wide margin.
//
// Deliberately NOT gated on a max engine-silence window. Any such ceiling would
// re-create the exact false-kill class g-f2mg fixes: a live search inside one
// >ceiling iteration emits no lines, the gated heartbeat would fall silent, and
// the watchdog would fail a live search around ~ceiling+8s. Liveness here means
// "the orchestrator worker is alive and a search is active"; a dead orchestrator
// stops the interval, so the genuinely-hung path still fails (the "dead worker
// emits zero pings" guard). Detecting a wedged ENGINE sub-worker (live
// orchestrator, dead engine) is a DIFFERENT problem that needs an explicit
// terminate-and-recreate backstop, NOT a silent liveness gate — tracked in
// g-5fng so this heartbeat is not "fixed" back into a false-kill.
const HEARTBEAT_INTERVAL_MS = 1000;
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;

/**
 * Unthrottled phase-boundary liveness ping, keyed by the active analysis id.
 * Info lines only cover the INSIDE of a search; the per-request reset and the
 * gaps BETWEEN the root/post-played/post-best searches emit no info, so a
 * `bestmove` arriving just before the watchdog window followed by a slow next
 * phase could false-kill a live request. Emitted at the per-request reset and at
 * each search's bestmove (1 + ≤3 per request — low volume), skipped when the
 * request is canceled. Separate from the throttled info-line ping; does NOT
 * touch `lastProgressPostMs`.
 */
const postPhaseProgress = () => {
  if (activeAnalysisId && !canceledAnalyses.has(activeAnalysisId)) {
    ctx.postMessage({
      type: "analysis-progress",
      id: activeAnalysisId,
    } satisfies AnalysisWorkerResponse);
  }
};

const stopHeartbeat = () => {
  if (heartbeatTimer !== null) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
};

/**
 * Start the wall-clock liveness heartbeat for the active search. Called from
 * runSearch once `activeSearch` is set. The interval body is self-guarding — it
 * only pings while a search is live and its analysis is neither cleared nor
 * canceled — so a stray tick after the search ends is inert. It is NOT gated on
 * engine output (see the heartbeat note above): vouching unconditionally while a
 * search is active is the whole point. Heartbeats are still cleared eagerly at
 * every search/request exit to avoid leaking a timer across requests. Clears any
 * prior timer first, so it is safe to re-call.
 */
const startHeartbeat = () => {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    if (
      activeSearch &&
      activeAnalysisId &&
      !canceledAnalyses.has(activeAnalysisId)
    ) {
      ctx.postMessage({
        type: "analysis-progress",
        id: activeAnalysisId,
      } satisfies AnalysisWorkerResponse);
    }
  }, HEARTBEAT_INTERVAL_MS);
};

// Per-request readiness waiters, distinct from the init engineReady handshake.
// Each `ucinewgame`+`isready` reset pushes one waiter; Stockfish answers every
// `isready` with exactly one `readyok`, so acknowledgments are matched in FIFO
// order. A canceled/errored request stays in the queue (marked `done`) until its
// own readyok arrives and is absorbed — this prevents a stale ack from a
// canceled request from satisfying the NEXT request's reset barrier.
type ResetWaiter = {
  resolve: () => void;
  reject: (error: Error) => void;
  done: boolean;
  /** Deadline timer for this reset; cleared whenever the waiter settles. */
  timer: ReturnType<typeof setTimeout> | null;
};

/** Clear a settled waiter's deadline timer so it cannot fire against a fresh engine. */
const clearWaiterTimer = (waiter: ResetWaiter) => {
  if (waiter.timer !== null) {
    clearTimeout(waiter.timer);
    waiter.timer = null;
  }
};
const resetAckQueue: ResetWaiter[] = [];

/**
 * What this worker ACCEPTS: the production union, with the analyze-move widened
 * by C7's optional selector, plus the bench handshake.
 *
 * Declared here rather than by widening `AnalysisWorkerRequest`, so production
 * callers still cannot express either key (C7) and §15.2's deletion of
 * `candidates/` takes both shapes with it.
 */
type AnalysisWorkerIncoming =
  | Exclude<AnalysisWorkerRequest, AnalyzeMoveMessage>
  | BenchAnalyzeMoveMessage
  | BenchInitMessage;

const pendingAnalyses: BenchAnalyzeMoveMessage[] = [];
let analysisInFlight = false;

// Stockfish's browser worker bootstrap reads the wasm asset from location.hash.
// This is a private package contract, so upgrades must be revalidated with the
// real-browser smoke test before changing the pinned stockfish version.
const createEngineWorkerUrl = () =>
  `${stockfishEngineUrl}#${encodeURIComponent(stockfishWasmUrl)}`;

const postLog = (message: string) => {
  ctx.postMessage({ type: "log", message } satisfies AnalysisWorkerResponse);
};

const sendEngineCommand = (command: string) => {
  postLog(`[analysisWorker ->] ${command}`);
  engine?.postMessage(command);
};

class AnalysisCanceledError extends Error {
  constructor() {
    super("Analysis canceled");
    this.name = "AnalysisCanceledError";
  }
}

const throwIfCanceled = (analysisId: string) => {
  if (canceledAnalyses.has(analysisId)) {
    throw new AnalysisCanceledError();
  }
};

/**
 * Reset the engine for ONE independent analysis. Sent once at the top of
 * analyzeMove (before the root search) and NEVER between the 3 related searches,
 * so a single position's eval triple stays internally consistent while distinct
 * positions no longer share leftover search state. Resolves on the next
 * `readyok` via the dedicated pendingRequestReady waiter.
 */
const awaitRequestReady = (deadlineAt: number) =>
  new Promise<void>((resolve, reject) => {
    const waiter: ResetWaiter = { resolve, reject, done: false, timer: null };
    resetAckQueue.push(waiter);
    // The deadline must bound the WHOLE analyze-move including this reset: a
    // stalled reset (no `readyok`) would otherwise burn the entire budget while
    // the searches after it merely inherit an already-expired clock.
    if (Number.isFinite(deadlineAt)) {
      waiter.timer = setTimeout(() => {
        if (waiter.done) return;
        // A reset timeout takes the FATAL path, NOT the usual leave-in-queue
        // absorber. That absorber is correct for cancel/error — the `isready`
        // was sent, so its `readyok` really is coming and must be swallowed
        // rather than released to the next request. It is WRONG here: a hung
        // engine's `readyok` will NEVER arrive, so a leftover `done` placeholder
        // would consume the NEXT request's ack, deadlock that reset, and desync
        // the FIFO a little further with every subsequent timeout. Terminating
        // the engine and clearing the whole queue is safe precisely because
        // MAX_ANALYSIS_MS is far larger than a healthy `ucinewgame`+`isready`
        // (near-instant on a live engine) — exceeding it means genuinely stuck.
        destroyEngine();
        failAllRequestReady(new ResetTimeoutError());
      }, Math.max(0, deadlineAt - Date.now()));
    }
    sendEngineCommand("ucinewgame");
    sendEngineCommand("isready");
  });

/**
 * Reject the request currently awaiting its reset (the most recently enqueued,
 * not-yet-settled waiter) from any analysis exit. The waiter is marked `done`
 * but LEFT in the FIFO queue so its still-in-flight `readyok` is absorbed rather
 * than released to the next request.
 */
const rejectRequestReady = (error: Error) => {
  for (let i = resetAckQueue.length - 1; i >= 0; i--) {
    const waiter = resetAckQueue[i];
    if (!waiter.done) {
      waiter.done = true;
      clearWaiterTimer(waiter);
      waiter.reject(error);
      return;
    }
  }
};

/**
 * Fatal teardown of the reset queue: the engine is gone (error/terminate) so no
 * further `readyok`s will arrive. Reject every outstanding waiter and drop the
 * placeholders that would otherwise wait forever for an absorbed ack.
 */
const failAllRequestReady = (error: Error) => {
  const waiters = resetAckQueue.splice(0);
  for (const waiter of waiters) {
    clearWaiterTimer(waiter);
    if (!waiter.done) {
      waiter.done = true;
      waiter.reject(error);
    }
  }
};

/**
 * Tear down the engine sub-worker so the next request rebuilds it from scratch.
 *
 * Used by the reset-deadline path: once we conclude the engine is hung, leaving it
 * running would keep a zombie that may emit a late `readyok` into a queue we have
 * cleared. Callers pair this with `failAllRequestReady` so no orphaned placeholder
 * survives to consume a future ack.
 */
const destroyEngine = () => {
  stopHeartbeat();
  if (engine) {
    engine.terminate();
    engine = null;
  }
  engineReady = false;
  // The tombstone describes an instance that no longer exists; keeping it would
  // hold a reference to a terminated worker for the rest of this worker's life.
  // Its listeners are deliberately NOT detached — they are bound to the instance
  // that is going away, and their source guard makes anything still queued from
  // it inert (see `ensureEngine`). `ensureEngine` replaces `engineListeners`.
  failedEngine = null;
  // Boot a replacement immediately rather than waiting for the next search to
  // lazily call ensureEngine: the NEXT request's first act is `awaitRequestReady`,
  // which posts to the engine — against a null engine those commands would vanish
  // and the request would wait forever for a reset that was never requested.
  // Recreating here re-runs the init handshake, whose `readyok` flips engineReady
  // and drains the queue.
  ensureEngine();
};

/**
 * The analyze-move exceeded its total wall-clock budget while waiting for the
 * engine reset — so no search ever started and there is NO partial bestmove to
 * return. Distinct from a search timeout, which still yields a usable (shallower)
 * result. Scoped to the originating request id by `drainQueue`.
 */
class ResetTimeoutError extends Error {
  constructor() {
    super("Analysis reset timed out");
    this.name = "ResetTimeoutError";
  }
}

/**
 * The engine did not answer the deadline's `stop` with a `bestmove` within
 * `STOP_GRACE_MS`. Unlike a normal deadline stop — which still yields a usable,
 * merely shallower result — there is nothing to return here: the engine is
 * unresponsive and is torn down. Scoped to the originating request id, so one
 * wedged move fails alone rather than failing all analysis.
 */
class SearchStopTimeoutError extends Error {
  constructor() {
    super("Engine did not stop before the analysis deadline grace expired");
    this.name = "SearchStopTimeoutError";
  }
}

const ensureEngine = async () => {
  if (engine) {
    return engine;
  }

  try {
    // Listeners BOUND to the instance, not shared across every engine this
    // worker ever builds. A `Worker`'s `error` is queued as a task on the
    // owner's event loop, independently of the message port that `terminate()`
    // empties — so an error raised by an instance we already replaced can be
    // delivered AFTER its replacement exists. A shared handler reading the
    // module-level `engine` would then tombstone the healthy replacement and
    // park the worker for good. `event.currentTarget` would say the same thing,
    // but only while the event is being dispatched, so the closure is both
    // simpler and testable.
    const created = new Worker(createEngineWorkerUrl());
    const listeners: EngineListeners = {
      message: (event) => handleEngineMessage(created, event),
      error: (event) => handleEngineError(created, event),
    };
    created.addEventListener("message", listeners.message);
    created.addEventListener("error", listeners.error);
    engine = created;
    engineListeners = listeners;
    sendEngineCommand("uci");
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Failed to initialize Stockfish";
    ctx.postMessage({
      type: "error",
      error: message,
    } satisfies AnalysisWorkerResponse);
  }

  return engine;
};

// Default in-game search depth. The analysis-board evidence driver overrides it
// with 21 (g-cache-stronger-evals); in-game callers omit depth and stay at 17.
const DEFAULT_SEARCH_DEPTH = 17;

/**
 * §15.1 C5's NEUTRAL extension: every field is optional, and omitting all of
 * them emits today's exact `position` / `go` pair with no `setoption` before it.
 * That identity is what C1 is proved on, so nothing here may acquire a default
 * that changes a command.
 */
type RunSearchOptions = {
  onInfo?: (score: EngineScore, depth: number) => void;
  depth?: number;
  /** Restrict the search to these root moves (§3.2's restricted resolution). */
  searchmoves?: string[];
  /** Requested MultiPV. `1` and omission are the same thing: emit nothing. */
  multipv?: number;
  budget?: AnalysisBudget;
};

const runSearch = async (
  fen: string,
  moves: string[],
  options: RunSearchOptions = {},
) => {
  const pendingEngine = await ensureEngine();

  if (!pendingEngine) {
    throw new Error("Stockfish engine unavailable");
  }

  if (activeSearch) {
    sendEngineCommand("stop");
  }

  const { onInfo, searchmoves } = options;
  const budget = options.budget ?? dormantBudget();
  const requestedDepth = options.depth ?? DEFAULT_SEARCH_DEPTH;
  const multipv = options.multipv ?? 1;
  // Only a RAISE needs undoing, and only the raiser may undo it: restoring
  // unconditionally would post a `setoption` around every ordinary search and
  // break the byte-identical UCI stream C1 is proved on.
  const raisedMultiPv = multipv !== 1;

  try {
    return await new Promise<SearchResult>(
    (resolve, reject) => {
      const search = {
        resolve,
        reject,
        lastScore: null,
        lastPv: null,
        lastDepth: null,
        capFired: false,
        capTimer: null as ReturnType<typeof setTimeout> | null,
        graceTimer: null as ReturnType<typeof setTimeout> | null,
        onInfo,
        requestedDepth,
        endsGame: makeEndsGame(fen, moves),
        canceled: false,
        // Fresh per search: `seq` is a per-search emission counter and batches
        // never cross a search boundary. K is whatever MultiPV this search asked
        // for, so a restricted search's batch needs all of its slots.
        snapshots: createSnapshotAssembler(multipv),
      };
      activeSearch = search;
      // Arm the wall-clock heartbeat for THIS search so a single long iteration
      // (which emits no info line) still proves worker liveness to the watchdog.
      startHeartbeat();
      // Each search of an analyze-move gets whatever is LEFT of the ONE shared
      // budget — BOTH halves of it — so reset + up to three sequential searches
      // together are bounded by MAX_ANALYSIS_MS + STOP_GRACE_MS rather than ~3x
      // either. A search entered after the deadline has already passed arms at 0
      // and stops immediately, still yielding a shallow bestmove for display.
      if (Number.isFinite(budget.deadlineAt)) {
        search.capTimer = setTimeout(() => {
          if (activeSearch !== search) return;
          search.capFired = true;
          // Stockfish answers `stop` with a `bestmove` for the best line so far,
          // so display still gets a usable — just shallower — result.
          sendEngineCommand("stop");
          // ...but only if it is alive to answer. `stop` is a request, not a
          // guarantee, and a wedged engine would leave this search pending
          // forever WITH its heartbeat still vouching for it. Bound the wait.
          //
          // The grace clock starts at the move's FIRST deadline `stop` and is
          // shared from there: later searches get what is LEFT of it, not a fresh
          // 2s each. A healthy engine answers `stop` in milliseconds, so in
          // practice each later search still sees nearly the full window; burning
          // the shared grace across several searches means the engine is already
          // taking seconds to acknowledge, which is the wedge this detects.
          if (budget.graceExpiresAt === null) {
            budget.graceExpiresAt = Date.now() + STOP_GRACE_MS;
          }
          const graceExpiresAt = budget.graceExpiresAt;
          search.graceTimer = setTimeout(() => {
            if (activeSearch !== search) return;
            activeSearch = null;
            clearSearchTimers(search);
            stopHeartbeat();
            // Same fatal handling as the reset timeout: a `bestmove` that never
            // came will never come, and a zombie engine could emit one into a
            // later request's search. Terminate, rebuild, clear the reset FIFO.
            destroyEngine();
            failAllRequestReady(new ResetTimeoutError());
            search.reject(new SearchStopTimeoutError());
          }, Math.max(0, graceExpiresAt - Date.now()));
        }, Math.max(0, budget.deadlineAt - Date.now()));
      }
      // §8: a phase sets its OWN MultiPV immediately before `position`/`go` and
      // never inherits the previous phase's value. Omission (K === 1) emits
      // nothing at all — the engine is already at 1, set at `uciok` and restored
      // by the `finally` below.
      if (raisedMultiPv) {
        sendEngineCommand(`setoption name MultiPV value ${multipv}`);
      }
      const movesSegment = moves.length > 0 ? ` moves ${moves.join(" ")}` : "";
      const searchmovesSegment =
        searchmoves && searchmoves.length > 0
          ? ` searchmoves ${searchmoves.join(" ")}`
          : "";
      sendEngineCommand(`position fen ${fen}${movesSegment}`);
      sendEngineCommand(`go depth ${requestedDepth}${searchmovesSegment}`);
    },
  );
  } finally {
    // C6: restoration is the WORKER's, not the candidate's, and it covers every
    // exit — bestmove, cancel, deadline, rejection.
    //
    // Guarded on identity AND readiness. Identity alone is not enough: a fatal
    // engine error does not terminate the engine, so the failed instance is
    // still `engine` and an identity-only guard would post a `setoption` into
    // it. `handleEngineError` clears `engineReady` BEFORE settling this promise,
    // which is what makes the second condition reachable rather than a restated
    // form of the first. A destroyed/rebuilt engine fails identity, and a fresh
    // engine sets MultiPV 1 at `uciok` before its first `go` anyway.
    if (raisedMultiPv && engine === pendingEngine && engineReady) {
      sendEngineCommand("setoption name MultiPV value 1");
    }
  }
};

/**
 * Test-only: drive ONE search with the C5 options.
 *
 * The minimal Variant A uses neither `searchmoves` nor `multipv` — §3.2's
 * restricted resolution lands in g-grade-variant-b — so without this hook the
 * emission and the C6 restoration would ship with no coverage at all until then,
 * which is the wrong order for a `setoption` that outlives its own search.
 * Unreachable from any message.
 *
 * `analysisId` binds the search to a cancelable request. `cancelAnalysis` keys on
 * `activeAnalysisId`, which only `drainQueue` sets — so without this a test can
 * imitate a cancel's OBSERVABLE ending (a truncated `bestmove`) but never take
 * the real path through `stop`, `ActiveSearch.canceled` and the canceled-search
 * tally. That path is the one C6's restoration has to survive.
 */
export const __runSearchForTests = (
  fen: string,
  moves: string[],
  options: RunSearchOptions = {},
  analysisId?: string,
) => {
  if (analysisId !== undefined) {
    activeAnalysisId = analysisId;
  }
  return runSearch(fen, moves, options);
};

const parseUciMove = (uci: string) => {
  const from = uci.slice(0, 2);
  const to = uci.slice(2, 4);
  const promotion = uci.slice(4, 5) || undefined;

  if (from.length !== 2 || to.length !== 2) {
    return null;
  }

  return { from, to, ...(promotion ? { promotion } : {}) };
};

const terminalScoreAfterMove = (
  fen: string,
  moveUci: string,
): EngineScore | null => {
  const move = parseUciMove(moveUci);
  if (!move) {
    return null;
  }

  const chess = new Chess(fen);
  const played = chess.move(move);
  if (!played || !chess.isGameOver()) {
    return null;
  }

  if (chess.isCheckmate()) {
    return { type: "mate", value: 0 };
  }

  return { type: "cp", value: 0 };
};

/**
 * The position a search actually runs from: `fen` after `moves`. Null when the
 * replay fails, so the caller degrades to "does not end the game" rather than
 * letting chess.js throw out of instrumentation.
 */
const positionAfterMoves = (fen: string, moves: string[]): string | null => {
  try {
    const chess = new Chess(fen);
    for (const uci of moves) {
      const move = parseUciMove(uci);
      if (!move) {
        return null;
      }
      chess.move(move);
    }
    return chess.fen();
  } catch {
    return null;
  }
};

/**
 * §4.2's exemption from the two-move minimum: a one-move PV is legitimate when
 * that move ends the game — a mate in one has nothing to continue with, and
 * without this a real mating line is miscounted as `pv-short` divergence.
 *
 * Evaluated LAZILY and memoized. The selector asks only about a PV shorter than
 * two moves, which is rare, so a normal search never pays the chess.js replay and
 * a K-slot batch pays it at most once.
 */
const makeEndsGame = (fen: string, moves: string[]) => {
  let searchFen: string | null | undefined;

  return (uci: string): boolean => {
    if (searchFen === undefined) {
      searchFen = positionAfterMoves(fen, moves);
    }
    if (searchFen === null) {
      return false;
    }
    try {
      return terminalScoreAfterMove(searchFen, uci) !== null;
    } catch {
      return false;
    }
  };
};

const buildBestLine = (
  bestMove: string,
  rootPv: string[] | null,
  continuationPv: string[] | null,
): string[] => {
  if (rootPv && rootPv.length > 1 && rootPv[0] === bestMove) {
    return rootPv;
  }

  if (continuationPv && continuationPv.length > 0) {
    return [bestMove, ...continuationPv];
  }

  return [bestMove];
};

/**
 * A fatal engine failure, settled so nothing is left waiting on it.
 *
 * The unscoped error and the stopped heartbeat are as they always were — the
 * bench runner's "worker compromised → rebuild" path reads that message and is
 * unchanged. What follows is the settlement this used to skip: the active
 * search's promise leaked, so `analyzeMove` never returned, `analysisInFlight`
 * stayed true, and `drainQueue` was blocked for the rest of the worker's life. A
 * `finally` inside `runSearch` would never have run either, which is why C6's
 * MultiPV restoration depends on this fix.
 *
 * Deliberately NOT widened to terminate and recreate the engine: that risks a
 * create → error-event → recreate loop, and the host already rebuilds the worker
 * on an unscoped error. Leaving `engineReady` false parks the queue until it
 * does — a broken engine serving requests is worse than a stalled one.
 */
const handleEngineError = (source: Worker, event: ErrorEvent) => {
  // A stale instance's failure is not this worker's failure. Its request was
  // already settled when it was replaced, and acting on it here would report an
  // unscoped error for a live engine and tombstone it — see `ensureEngine` for
  // why the delivery order makes this reachable.
  if (source !== engine) {
    return;
  }
  const message = event.message || "Failed to initialize Stockfish";
  stopHeartbeat();
  ctx.postMessage({
    type: "error",
    error: message,
  } satisfies AnalysisWorkerResponse);
  // Tombstone the instance BEFORE anything else, so a line already in flight from
  // it — a `readyok`, most dangerously — cannot be acted on. See `failedEngine`.
  failedEngine = source;
  // Mark the engine unavailable BEFORE anything can observe the failure — the
  // same idiom `destroyEngine` uses for the same condition, and what
  // `drainQueue` guards on, so the next queued request cannot be started on it.
  // It is also what makes C6's readiness guard reachable: without it the failed
  // engine is still the current engine and "same instance" and "usable" are the
  // same condition.
  engineReady = false;
  failAllRequestReady(new Error(message));
  const current = activeSearch;
  if (current) {
    activeSearch = null;
    clearSearchTimers(current);
    current.reject(new Error(message));
  }
};

const handleEngineMessage = (source: Worker, event: MessageEvent<string>) => {
  handleEngineLine(source, event.data);
};

const handleEngineLine = (source: Worker, line: string) => {
  postLog(`[analysisWorker <-] ${line}`);

  // Logged, then dropped: the transcript still shows what the engine said (the
  // device harness reads it back), but nothing acts on it. Two ways a line gets
  // here without being ours — a replaced instance still draining its port, and
  // the tombstoned instance whose late `readyok` would otherwise resurrect it
  // (see `failedEngine`).
  if (source !== engine || source === failedEngine) {
    return;
  }

  if (line === "uciok") {
    sendEngineCommand("setoption name Hash value 128");
    sendEngineCommand("setoption name MultiPV value 1");
    sendEngineCommand("isready");
    return;
  }

  if (line === "readyok") {
    // Per-request reset acks take precedence over the init handshake and are
    // matched in FIFO order. A `done` waiter (its request was canceled/errored)
    // absorbs its own ack without releasing the next request's barrier.
    if (resetAckQueue.length > 0) {
      const waiter = resetAckQueue.shift()!;
      if (!waiter.done) {
        waiter.done = true;
        // A completed LIVE per-request reset, just before the root search begins:
        // emit a phase-boundary liveness ping so the watchdog covers the silent
        // reset→root gap (the init-handshake readyok below is NOT per-request).
        postPhaseProgress();
        waiter.resolve();
      }
      return;
    }
    engineReady = true;
    ctx.postMessage({ type: "ready" } satisfies AnalysisWorkerResponse);
    drainQueue();
    return;
  }

  if (line.startsWith("bestmove")) {
    const current = activeSearch;
    activeSearch = null;
    // This search is over: stop its heartbeat. The next phase's runSearch arms a
    // fresh one; the bestmove→next-phase gap is covered by postPhaseProgress.
    stopHeartbeat();

    if (!current) {
      return;
    }
    clearSearchTimers(current);

    const parts = line.split(" ");
    const move = parts[1] ?? "";
    const stopReason: StopReason = current.capFired ? "deadline" : "bestmove";
    current.resolve({
      bestmove: move,
      score: current.lastScore,
      pv: current.lastPv,
      capFired: current.capFired,
      stopReason,
      reachedDepth: current.lastDepth,
    });
    // AFTER resolve, and void-returning: the legacy accumulators above are this
    // search's only selector. This call measures what §12 step 9 would change.
    if (snapshotInstrumentationEnabled) {
      if (current.canceled) {
        // A canceled request emits no row — analyzeMove unwinds via
        // AnalysisCanceledError and posts nothing — so its truncated search has
        // no output for the two selectors to disagree about. Selecting over it
        // anyway would charge a `stale-depth` rejection to the drift report for a
        // row that never existed, which is why §6.3 case 9a keeps cancellation in
        // its own tally rather than any failure counter.
        recordCanceledSearch();
      } else {
        recordLegacySelectorDivergence(
          {
            assembler: current.snapshots,
            requestedDepth: current.requestedDepth,
            bestMove: move,
            capFired: current.capFired,
            stopReason,
            endsGame: current.endsGame,
          },
          {
            score: current.lastScore,
            pv: current.lastPv,
            reachedDepth: current.lastDepth,
          },
        );
      }
    }
    // Search-phase completion (root / post-played / post-best): emit a
    // phase-boundary liveness ping so the watchdog covers the gap between this
    // bestmove and the next phase's first info line.
    postPhaseProgress();
    return;
  }

  const info = parseUciInfoLine(line);
  if (info && activeSearch) {
    // Throttled per-search liveness ping (any non-null info line: depth/score/pv),
    // BEFORE the score/pv handling so even a score/pv-only line surfaces activity.
    // Fires for ALL THREE searches including the previously-silent root, so the
    // inactivity watchdog sees continuous progress even within a long iteration.
    if (activeAnalysisId) {
      const now = Date.now();
      if (now - lastProgressPostMs >= PROGRESS_THROTTLE_MS) {
        lastProgressPostMs = now;
        ctx.postMessage({
          type: "analysis-progress",
          id: activeAnalysisId,
        } satisfies AnalysisWorkerResponse);
      }
    }
    if (info.depth !== undefined) {
      activeSearch.lastDepth = info.depth;
    }
    if (info.score) {
      activeSearch.lastScore = info.score;
      if (activeSearch.onInfo) {
        activeSearch.onInfo(info.score, info.depth ?? 0);
      }
    }
    // Retain the principal variation for the primary line so the root best
    // search can surface a full continuation. multipv > 1 lines belong to
    // restricted searches and must not overwrite the main PV.
    if (info.pv && (info.multipv === undefined || info.multipv === 1)) {
      activeSearch.lastPv = info.pv;
    }
    // Assembly runs LAST and writes only to `snapshots`, so every legacy field
    // above is settled before instrumentation sees the line. Score-only, PV-only,
    // and bounded lines are filtered out by `admitInfoLine`, having already
    // updated the streaming and heartbeat state above.
    if (snapshotInstrumentationEnabled) {
      admitInfoLine(activeSearch.snapshots, info);
    }
  }
};

const enqueueAnalysis = (message: BenchAnalyzeMoveMessage) => {
  pendingAnalyses.push(message);
  drainQueue();
};

const cancelAnalysis = (analysisId: string) => {
  if (activeAnalysisId === analysisId) {
    canceledAnalyses.add(analysisId);
    if (activeSearch) {
      // Mark the search itself so its `bestmove` can still be identified as
      // belonging to a canceled request — see ActiveSearch.canceled for why the
      // request tombstone is not enough at that point.
      activeSearch.canceled = true;
      sendEngineCommand("stop");
    }
    // Stop the heartbeat eagerly so a canceled request stops pinging immediately
    // (the interval body already self-guards on the cancel tombstone, but clear
    // the timer to avoid leaking it past the request).
    stopHeartbeat();
    // Cancel may land while awaiting the per-request reset (before any search
    // starts): reject the waiter so analyzeMove unwinds via AnalysisCanceledError
    // instead of hanging until a readyok that we no longer act on.
    rejectRequestReady(new AnalysisCanceledError());
    return;
  }

  const pendingIndex = pendingAnalyses.findIndex((entry) => entry.id === analysisId);
  if (pendingIndex >= 0) {
    pendingAnalyses.splice(pendingIndex, 1);
  }
};

const drainQueue = () => {
  if (!engineReady || analysisInFlight) {
    return;
  }

  let next: BenchAnalyzeMoveMessage | undefined;
  while (!next && pendingAnalyses.length > 0) {
    const candidate = pendingAnalyses.shift();
    if (!candidate) {
      continue;
    }
    // `delete()` returns true only when a cancel tombstone existed, and also
    // consumes it so future request-id reuse would not be poisoned.
    if (canceledAnalyses.delete(candidate.id)) {
      continue;
    }
    next = candidate;
  }

  if (!next) {
    return;
  }

  analysisInFlight = true;
  const request = next;
  activeAnalysisId = request.id;

  void analyzeMove(request)
    .catch((error) => {
      if (error instanceof AnalysisCanceledError) {
        return;
      }
      const message =
        error instanceof Error ? error.message : "Failed to analyze move";
      // Scope this failure to the originating request so the consumer can
      // attribute it to a single moveIndex rather than failing all analysis.
      // Engine/bootstrap/fatal failures remain unscoped (no `id`) and are
      // emitted elsewhere.
      ctx.postMessage({
        type: "error",
        id: request.id,
        error: message,
      } satisfies AnalysisWorkerResponse);
    })
    .finally(() => {
      canceledAnalyses.delete(request.id);
      if (activeAnalysisId === request.id) {
        activeAnalysisId = null;
      }
      // Per-request safety net: every request lifecycle ends here (success,
      // cancel, or error). The last search's bestmove normally stops the
      // heartbeat already, but a mid-search reject (e.g. AnalysisCanceledError)
      // can abandon activeSearch — clear the timer so it never leaks into the
      // next request. Same for that search's deadline timers: an orphaned
      // graceTimer still passes its `activeSearch === search` guard and would
      // destroy a healthy engine mid-way through the NEXT request.
      if (activeSearch) clearSearchTimers(activeSearch);
      stopHeartbeat();
      analysisInFlight = false;
      drainQueue();
    });
};

/**
 * Today's three-search protocol, verbatim — the other branch of the ONE dispatch
 * point below.
 *
 * Extracted rather than left inline so both arms hand the same
 * `CandidateOutcome` to the same tail: everything from the `(none)` early return
 * through `computeAnalysisResult`, the mate fields, the classification,
 * `buildBestLine` and the `postMessage` lives below the dispatch and is shared
 * (§15.1 C3). Nothing about the search sequence changed in the move.
 */
const runCurrentProtocol = async (
  request: AnalyzeMoveMessage,
  sideToMove: "w" | "b",
  budget: AnalysisBudget,
): Promise<CandidateOutcome> => {
  // Any constituent search truncated by the deadline poisons the whole move's
  // provenance: the tuple no longer describes a search that reached its limit.
  let capFired = false;

  const bestSearch = await runSearch(request.fen, [], {
    depth: request.depth,
    budget,
  });
  capFired = capFired || bestSearch.capFired;
  throwIfCanceled(request.id);
  const bestMove = bestSearch.bestmove;

  if (!bestMove || bestMove === "(none)") {
    return {
      bestMove: bestMove || "(none)",
      rootPv: null,
      continuationPv: null,
      postPlayedScore: null,
      postBestScore: null,
      capFired,
      reachedDepth: bestSearch.reachedDepth,
    };
  }

  // Evaluate the position after the played move, streaming intermediate evals
  const opponentToMove = sideToMove === "w" ? "b" : "w";
  const terminalPlayedScore = terminalScoreAfterMove(request.fen, request.move);
  const playedEvalSearch: SearchResult = terminalPlayedScore
    ? {
        bestmove: "(terminal)",
        score: terminalPlayedScore,
        pv: null,
        // A deterministic terminal score is not a search: it can never be
        // truncated, so it never poisons provenance.
        capFired: false,
        stopReason: "bestmove",
        reachedDepth: null,
      }
    : await runSearch(request.fen, [request.move], {
        onInfo: (score, depth) => {
          if (canceledAnalyses.has(request.id)) {
            return;
          }
          const cp = scoreForPlayer(score, opponentToMove, request.playerColor);
          if (cp !== null) {
            ctx.postMessage({
              type: "analysis-streaming",
              id: request.id,
              cp,
              depth,
            } satisfies AnalysisWorkerResponse);
          }
        },
        depth: request.depth,
        budget,
      });
  capFired = capFired || playedEvalSearch.capFired;
  throwIfCanceled(request.id);

  // When best != played, search after the best move too for an apples-to-apples
  // comparison. The pre-move minimax eval is unreliable in WASM Stockfish because
  // independent searches reach different depths, inflating the delta.
  let postBestScore = playedEvalSearch.score;
  let postBestSearch: SearchResult | null = null;
  if (request.move !== bestMove) {
    const terminalBestScore = terminalScoreAfterMove(request.fen, bestMove);
    if (terminalBestScore) {
      postBestScore = terminalBestScore;
    } else {
      postBestSearch = await runSearch(request.fen, [bestMove], {
        depth: request.depth,
        budget,
      });
      capFired = capFired || postBestSearch.capFired;
      postBestScore = postBestSearch.score;
    }
  }
  throwIfCanceled(request.id);

  return {
    bestMove,
    rootPv: bestSearch.pv,
    // Prefer the root PV when it begins with the final bestmove. If Stockfish's
    // last root PV is stale or short, reuse the already-run continuation search
    // after the best move so new sessions do not persist one-move lines.
    continuationPv:
      request.move === bestMove ? playedEvalSearch.pv : postBestSearch?.pv ?? null,
    postPlayedScore: playedEvalSearch.score,
    postBestScore,
    capFired,
    reachedDepth: bestSearch.reachedDepth,
  };
};

/**
 * Everything a candidate arm is allowed to touch (§15.1 C4), built per request.
 *
 * `search` closes over the ONE shared budget, so an arm cannot construct or
 * extend one; every other field is a pure helper. There is no engine handle, no
 * `activeSearch`, no heartbeat, no waiter queue, no `canceledAnalyses`, and no
 * `ctx.postMessage`.
 */
const candidateContext = (
  request: AnalyzeMoveMessage,
  sideToMove: "w" | "b",
  budget: AnalysisBudget,
): CandidateContext => {
  const opponentToMove = sideToMove === "w" ? "b" : "w";
  return {
    fen: request.fen,
    playedMove: request.move,
    playerColor: request.playerColor,
    sideToMove,
    requestedDepth: request.depth ?? DEFAULT_SEARCH_DEPTH,
    // `budget` last, so the shared deadline is not something an arm can pass.
    search: (moves, options) =>
      runSearch(request.fen, moves, { ...options, budget }),
    checkCanceled: () => throwIfCanceled(request.id),
    terminalScoreAfterMove: (move) => terminalScoreAfterMove(request.fen, move),
    streamPlayed: (score, depth) => {
      if (canceledAnalyses.has(request.id)) {
        return;
      }
      const cp = scoreForPlayer(score, opponentToMove, request.playerColor);
      if (cp !== null) {
        ctx.postMessage({
          type: "analysis-streaming",
          id: request.id,
          cp,
          depth,
        } satisfies AnalysisWorkerResponse);
      }
    },
  };
};

/**
 * §15.1 C7's SECOND key, read only while the first one is held.
 *
 * Returns the arm to dispatch, or null for the current protocol. Either key
 * alone is inert: no `bench-init` means the selector is never read, and no
 * selector means the current protocol runs even in bench mode. A named arm this
 * build cannot dispatch THROWS, so the failure is scoped to the request rather
 * than silently measuring the current protocol under a candidate's label.
 */
const selectCandidate = (
  request: BenchAnalyzeMoveMessage,
): CandidateProtocol | null => {
  if (!benchMode || request.arm === undefined) {
    return null;
  }
  const available = availableCandidateArms();
  if (!isCandidateArm(request.arm)) {
    throw new Error(
      `unknown candidate arm ${JSON.stringify(request.arm)} ` +
        `(this build dispatches: ${available.join(", ") || "none"})`,
    );
  }
  const protocol = CANDIDATE_PROTOCOLS[request.arm];
  if (!protocol) {
    throw new Error(
      `candidate arm "${request.arm}" is not available in this worker build ` +
        `(this build dispatches: ${available.join(", ") || "none"})`,
    );
  }
  return protocol;
};

const analyzeMove = async (request: BenchAnalyzeMoveMessage) => {
  throwIfCanceled(request.id);

  // Before `analysis-started`: a request labelled with an arm this build cannot
  // run has not started, and must not look like it did.
  const candidate = selectCandidate(request);

  ctx.postMessage({
    type: "analysis-started",
    id: request.id,
    move: request.move,
  } satisfies AnalysisWorkerResponse);

  // Reset the progress throttle so the FIRST info line of this new request emits
  // a progress ping promptly. NEGATIVE_INFINITY (not 0) — under fake timers
  // Date.now() starts at 0, so 0 would suppress the very first ping.
  lastProgressPostMs = Number.NEGATIVE_INFINITY;

  const sideToMove = getSideToMove(request.fen);

  if (!sideToMove) {
    throw new Error("Invalid FEN supplied for analysis");
  }

  // ONE shared budget for the whole analyze-move — the reset plus up to three
  // sequential searches — established here and passed down, rather than fresh
  // per-search timers that would let a single move consume ~3x the intended
  // budget. Covers BOTH the deadline and the post-`stop` grace, so the move's
  // total wall-clock bound is MAX_ANALYSIS_MS + STOP_GRACE_MS, full stop.
  // Infinity when MAX_ANALYSIS_MS is dormant, which makes every timer below inert.
  const budget: AnalysisBudget = {
    deadlineAt: MAX_ANALYSIS_MS === null ? Infinity : Date.now() + MAX_ANALYSIS_MS,
    graceExpiresAt: null,
  };

  // Reset the engine ONCE per independent analysis, before the root search and
  // never between the related searches. Then re-check cancellation: a cancel
  // delivered during the reset rejects the waiter, so we never start searching.
  await awaitRequestReady(budget.deadlineAt);
  throwIfCanceled(request.id);

  // ---- the ONE dispatch point (§15.1 C3) ------------------------------------
  const outcome = candidate
    ? await candidate.run(candidateContext(request, sideToMove, budget))
    : await runCurrentProtocol(request, sideToMove, budget);

  // ---- the ONE join, shared by both arms ------------------------------------
  const protocol: AnalysisProtocol = candidate ? candidate.arm : "legacy";
  /**
   * §9.1's discriminator. The legacy arm is `!capFired && canonical` — NOT
   * `!capFired` alone, because a completed search whose grading fell back to the
   * delta-band `classifyMove` is equally unfit to carry a depth claim. Candidate
   * arms are `false` unconditionally (C8), on every path.
   */
  const evidenceEligible = (canonical: boolean) =>
    candidate === null && !outcome.capFired && canonical;

  const capFired = outcome.capFired;
  const bestMove = outcome.bestMove;

  if (!bestMove || bestMove === "(none)") {
    ctx.postMessage({
      type: "analysis",
      id: request.id,
      move: request.move,
      bestMove: bestMove || "(none)",
      bestLine: [],
      bestEval: null,
      playedEval: null,
      bestEvalMate: null,
      playedEvalMate: null,
      delta: null,
      classification: null,
      canonical: false,
      capFired,
      stopReason: capFired ? "deadline" : "bestmove",
      reachedDepth: outcome.reachedDepth,
      evidenceEligible: evidenceEligible(false),
      protocol,
    } satisfies AnalysisWorkerResponse);
    return;
  }

  const opponentToMove = sideToMove === "w" ? "b" : "w";
  const postPlayedScore = outcome.postPlayedScore;
  const postBestScore = outcome.postBestScore;

  const { bestEval, playedEval, delta } = computeAnalysisResult({
    bestMove,
    playedMove: request.move,
    postPlayedScore,
    postBestScore,
    sideToMove,
    playerColor: request.playerColor,
  });

  // Mate counts are mover-relative, mirroring playedEval/bestEval (callers pass
  // the mover's color as request.playerColor). Both post scores are from the
  // opponent-to-move position.
  const playedEvalMate = mateForPlayer(
    postPlayedScore,
    opponentToMove,
    request.playerColor,
  );
  const bestEvalMate =
    bestMove === request.move
      ? playedEvalMate
      : mateForPlayer(postBestScore, opponentToMove, request.playerColor);

  const isBestMove = bestMove === request.move;
  const mover: "white" | "black" = sideToMove === "w" ? "white" : "black";
  const scorePov: "white" | "black" = sideToMove === "w" ? "black" : "white";

  let classification: MoveClassification | null = null;
  let canonical = false;
  if (postBestScore && postPlayedScore) {
    classification = classifyMoveAdvanced({
      prevScore: postBestScore,
      nextScore: postPlayedScore,
      scorePov,
      mover,
      isBestMove,
    });
    canonical = true;
  } else {
    // Legacy delta-band fallback: a non-canonical result (one of the post-move
    // scores is missing). Surface it for diagnostics; not persisted. An arm that
    // DECLINES to grade a row lands here too, by returning a null post-best.
    classification = classifyMove(delta);
    postLog(
      `[analysisWorker] non-canonical classification (delta-band fallback) for move ${request.move}`,
    );
  }

  const bestLine = buildBestLine(
    bestMove,
    outcome.rootPv,
    outcome.continuationPv,
  );

  ctx.postMessage({
    type: "analysis",
    id: request.id,
    move: request.move,
    bestMove,
    bestLine,
    bestEval,
    playedEval,
    bestEvalMate,
    playedEvalMate,
    delta,
    classification,
    canonical,
    capFired,
    stopReason: capFired ? "deadline" : "bestmove",
    reachedDepth: outcome.reachedDepth,
    evidenceEligible: evidenceEligible(canonical),
    protocol,
  } satisfies AnalysisWorkerResponse);
};

ensureEngine();

ctx.addEventListener(
  "message",
  (event: MessageEvent<AnalysisWorkerIncoming>) => {
    const message = event.data;

    switch (message.type) {
      case "analyze-move": {
        if (!engineReady) {
          enqueueAnalysis(message);
          return;
        }

        enqueueAnalysis(message);
        break;
      }
      case "bench-init": {
        // Malformed (`bench` not literally true) is IGNORED, not answered: a
        // half-formed opt-in must leave the worker exactly as it was, and an
        // unanswered handshake is already the runner's "no arms" outcome.
        if (message.bench !== true) {
          break;
        }
        benchMode = true;
        ctx.postMessage({
          type: "bench-ready",
          arms: availableCandidateArms(),
        } satisfies BenchReadyMessage);
        break;
      }
      case "cancel-analysis": {
        cancelAnalysis(message.id);
        break;
      }
      case "terminate": {
        if (engine && engineListeners) {
          engine.removeEventListener("message", engineListeners.message);
          engine.removeEventListener("error", engineListeners.error);
        }
        engine?.terminate();
        engine = null;
        engineListeners = null;
        engineReady = false;
        failedEngine = null;
        stopHeartbeat();
        failAllRequestReady(new Error("Analysis worker terminated"));
        // Clear the abandoned search's deadline timers BEFORE dropping it. A
        // leaked graceTimer would fire against a terminated worker and call
        // destroyEngine, resurrecting the engine this branch just tore down.
        if (activeSearch) clearSearchTimers(activeSearch);
        activeSearch = null;
        activeAnalysisId = null;
        canceledAnalyses.clear();
        analysisInFlight = false;
        pendingAnalyses.length = 0;
        lastProgressPostMs = Number.NEGATIVE_INFINITY;
        break;
      }
      default:
        message satisfies never;
    }
  },
);
