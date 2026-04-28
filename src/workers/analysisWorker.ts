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
import {
  parseScoreInfo,
  getSideToMove,
  computeAnalysisResult,
  scoreForPlayer,
  classifyMove,
  classifyMoveAdvanced,
} from "./analysisUtils";
import type { MoveClassification } from "./analysisUtils";

const ctx = self as DedicatedWorkerGlobalScope;

let engineReady = false;
let engine: Worker | null = null;
let activeSearch: {
  resolve: (value: { bestmove: string; score: EngineScore | null }) => void;
  reject: (error: Error) => void;
  lastScore: EngineScore | null;
  onInfo?: (score: EngineScore, depth: number) => void;
} | null = null;
let activeAnalysisId: string | null = null;
const canceledAnalyses = new Set<string>();

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

  return new Promise<{ bestmove: string; score: EngineScore | null }>(
    (resolve, reject) => {
      activeSearch = { resolve, reject, lastScore: null, onInfo };
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

const handleEngineError = (event: ErrorEvent) => {
  const message = event.message || "Failed to initialize Stockfish";
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
    engineReady = true;
    ctx.postMessage({ type: "ready" } satisfies AnalysisWorkerResponse);
    drainQueue();
    return;
  }

  if (line.startsWith("bestmove")) {
    const current = activeSearch;
    activeSearch = null;

    if (!current) {
      return;
    }

    const parts = line.split(" ");
    const move = parts[1] ?? "";
    current.resolve({ bestmove: move, score: current.lastScore });
    return;
  }

  const info = parseScoreInfo(line);
  if (info?.score && activeSearch) {
    activeSearch.lastScore = info.score;
    if (activeSearch.onInfo) {
      const tokens = line.split(" ");
      const depthIdx = tokens.indexOf("depth");
      const depth = depthIdx >= 0 ? Number(tokens[depthIdx + 1]) : 0;
      activeSearch.onInfo(info.score, depth);
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
      ctx.postMessage({
        type: "error",
        error: message,
      } satisfies AnalysisWorkerResponse);
    })
    .finally(() => {
      canceledAnalyses.delete(request.id);
      if (activeAnalysisId === request.id) {
        activeAnalysisId = null;
      }
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

  const sideToMove = getSideToMove(request.fen);

  if (!sideToMove) {
    throw new Error("Invalid FEN supplied for analysis");
  }

  const bestSearch = await runSearch(request.fen, []);
  throwIfCanceled(request.id);
  const bestMove = bestSearch.bestmove;

  if (!bestMove || bestMove === "(none)") {
    ctx.postMessage({
      type: "analysis",
      id: request.id,
      move: request.move,
      bestMove: bestMove || "(none)",
      bestEval: null,
      playedEval: null,
      delta: null,
      classification: null,
    } satisfies AnalysisWorkerResponse);
    return;
  }

  // Evaluate the position after the played move, streaming intermediate evals
  const opponentToMove = sideToMove === "w" ? "b" : "w";
  const terminalPlayedScore = terminalScoreAfterMove(request.fen, request.move);
  const playedEvalSearch = terminalPlayedScore
    ? { bestmove: "(terminal)", score: terminalPlayedScore }
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
  if (request.move !== bestMove) {
    const terminalBestScore = terminalScoreAfterMove(request.fen, bestMove);
    postBestScore =
      terminalBestScore ?? (await runSearch(request.fen, [bestMove])).score;
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

  const isBestMove = bestMove === request.move;
  const mover: "white" | "black" = sideToMove === "w" ? "white" : "black";
  const scorePov: "white" | "black" = sideToMove === "w" ? "black" : "white";

  let classification: MoveClassification | null = null;
  if (postBestScore && playedEvalSearch.score) {
    classification = classifyMoveAdvanced({
      prevScore: postBestScore,
      nextScore: playedEvalSearch.score,
      scorePov,
      mover,
      isBestMove,
    });
  } else {
    classification = classifyMove(delta);
  }

  ctx.postMessage({
    type: "analysis",
    id: request.id,
    move: request.move,
    bestMove,
    bestEval,
    playedEval,
    delta,
    classification,
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
        activeSearch = null;
        activeAnalysisId = null;
        canceledAnalyses.clear();
        analysisInFlight = false;
        pendingAnalyses.length = 0;
        break;
      }
      default:
        message satisfies never;
    }
  },
);
