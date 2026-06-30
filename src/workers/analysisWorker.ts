/// <reference lib="webworker" />

import { Chess } from "chess.js";
import stockfishEngineUrl from "stockfish/bin/stockfish-18-lite-single.js?url";
import stockfishWasmUrl from "stockfish/bin/stockfish-18-lite-single.wasm?url";
import type {
  AnalysisWorkerRequest,
  AnalysisWorkerResponse,
  AnalyzeMoveMessage,
} from "./analysisMessages";
import type { EngineScore } from "./stockfishMessages";
import { parseUciInfoLine } from "./parseInfo";
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
type SearchResult = {
  bestmove: string;
  score: EngineScore | null;
  pv: string[] | null;
};
let activeSearch: {
  resolve: (value: SearchResult) => void;
  reject: (error: Error) => void;
  lastScore: EngineScore | null;
  lastPv: string[] | null;
  onInfo?: (score: EngineScore, depth: number) => void;
} | null = null;
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
};
const resetAckQueue: ResetWaiter[] = [];

const pendingAnalyses: AnalyzeMoveMessage[] = [];
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
const awaitRequestReady = () =>
  new Promise<void>((resolve, reject) => {
    resetAckQueue.push({ resolve, reject, done: false });
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
    if (!waiter.done) {
      waiter.done = true;
      waiter.reject(error);
    }
  }
};

const ensureEngine = async () => {
  if (engine) {
    return engine;
  }

  try {
    engine = new Worker(createEngineWorkerUrl());
    engine.addEventListener("message", handleEngineMessage);
    engine.addEventListener("error", handleEngineError);
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

const runSearch = async (
  fen: string,
  moves: string[],
  onInfo?: (score: EngineScore, depth: number) => void,
) => {
  const pendingEngine = await ensureEngine();

  if (!pendingEngine) {
    throw new Error("Stockfish engine unavailable");
  }

  if (activeSearch) {
    sendEngineCommand("stop");
  }

  return new Promise<SearchResult>(
    (resolve, reject) => {
      activeSearch = { resolve, reject, lastScore: null, lastPv: null, onInfo };
      // Arm the wall-clock heartbeat for THIS search so a single long iteration
      // (which emits no info line) still proves worker liveness to the watchdog.
      startHeartbeat();
      const movesSegment = moves.length > 0 ? ` moves ${moves.join(" ")}` : "";
      sendEngineCommand(`position fen ${fen}${movesSegment}`);
      sendEngineCommand("go depth 17");
    },
  );
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

const handleEngineError = (event: ErrorEvent) => {
  const message = event.message || "Failed to initialize Stockfish";
  // The engine is broken: settle all in-flight resets so analysisInFlight cannot
  // stick and no placeholder waits for an ack that will never come. Stop the
  // heartbeat too, so a dead engine cannot keep emitting liveness pings for a
  // search that will never produce a bestmove.
  stopHeartbeat();
  failAllRequestReady(new Error(message));
  ctx.postMessage({
    type: "error",
    error: message,
  } satisfies AnalysisWorkerResponse);
};

const handleEngineMessage = (event: MessageEvent<string>) => {
  handleEngineLine(event.data);
};

const handleEngineLine = (line: string) => {
  postLog(`[analysisWorker <-] ${line}`);

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

    const parts = line.split(" ");
    const move = parts[1] ?? "";
    current.resolve({
      bestmove: move,
      score: current.lastScore,
      pv: current.lastPv,
    });
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
  }
};

const enqueueAnalysis = (message: AnalyzeMoveMessage) => {
  pendingAnalyses.push(message);
  drainQueue();
};

const cancelAnalysis = (analysisId: string) => {
  if (activeAnalysisId === analysisId) {
    canceledAnalyses.add(analysisId);
    if (activeSearch) {
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

  let next: AnalyzeMoveMessage | undefined;
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
      // next request.
      stopHeartbeat();
      analysisInFlight = false;
      drainQueue();
    });
};

const analyzeMove = async (request: AnalyzeMoveMessage) => {
  throwIfCanceled(request.id);

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

  // Reset the engine ONCE per independent analysis, before the root search and
  // never between the 3 related searches. Then re-check cancellation: a cancel
  // delivered during the reset rejects the waiter, so we never start searching.
  await awaitRequestReady();
  throwIfCanceled(request.id);

  const bestSearch = await runSearch(request.fen, []);
  throwIfCanceled(request.id);
  const bestMove = bestSearch.bestmove;

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
    } satisfies AnalysisWorkerResponse);
    return;
  }

  // Evaluate the position after the played move, streaming intermediate evals
  const opponentToMove = sideToMove === "w" ? "b" : "w";
  const terminalPlayedScore = terminalScoreAfterMove(request.fen, request.move);
  const playedEvalSearch: SearchResult = terminalPlayedScore
    ? { bestmove: "(terminal)", score: terminalPlayedScore, pv: null }
    : await runSearch(
        request.fen,
        [request.move],
        (score, depth) => {
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
      );
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
      postBestSearch = await runSearch(request.fen, [bestMove]);
      postBestScore = postBestSearch.score;
    }
  }
  throwIfCanceled(request.id);

  const { bestEval, playedEval, delta } = computeAnalysisResult({
    bestMove,
    playedMove: request.move,
    postPlayedScore: playedEvalSearch.score,
    postBestScore,
    sideToMove,
    playerColor: request.playerColor,
  });

  // Mate counts are mover-relative, mirroring playedEval/bestEval (callers pass
  // the mover's color as request.playerColor). Both post scores are from the
  // opponent-to-move position.
  const playedEvalMate = mateForPlayer(
    playedEvalSearch.score,
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
  if (postBestScore && playedEvalSearch.score) {
    classification = classifyMoveAdvanced({
      prevScore: postBestScore,
      nextScore: playedEvalSearch.score,
      scorePov,
      mover,
      isBestMove,
    });
    canonical = true;
  } else {
    // Legacy delta-band fallback: a non-canonical result (one of the post-move
    // searches produced no score). Surface it for diagnostics; not persisted.
    classification = classifyMove(delta);
    postLog(
      `[analysisWorker] non-canonical classification (delta-band fallback) for move ${request.move}`,
    );
  }

  // Prefer the root PV when it begins with the final bestmove. If Stockfish's
  // last root PV is stale or short, reuse the already-run continuation search
  // after the best move so new sessions do not persist one-move lines.
  const continuationPv =
    request.move === bestMove ? playedEvalSearch.pv : postBestSearch?.pv ?? null;
  const bestLine = buildBestLine(bestMove, bestSearch.pv, continuationPv);

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
  } satisfies AnalysisWorkerResponse);
};

ensureEngine();

ctx.addEventListener(
  "message",
  (event: MessageEvent<AnalysisWorkerRequest>) => {
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
      case "cancel-analysis": {
        cancelAnalysis(message.id);
        break;
      }
      case "terminate": {
        engine?.removeEventListener("message", handleEngineMessage);
        engine?.removeEventListener("error", handleEngineError);
        engine?.terminate();
        engine = null;
        engineReady = false;
        stopHeartbeat();
        failAllRequestReady(new Error("Analysis worker terminated"));
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
