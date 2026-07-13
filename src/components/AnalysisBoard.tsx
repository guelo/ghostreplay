import { forwardRef, memo, Profiler, useCallback, useEffect, useId, useImperativeHandle, useMemo, useRef, useState } from "react";
import type { ProfilerOnRenderCallback } from "react";
import { Chess } from "chess.js";
import type { Square } from "chess.js";
import { Chessboard, defaultArrowOptions } from "react-chessboard";
import type { PieceDropHandlerArgs, SquareHandlerArgs } from "react-chessboard";
import type { AnalysisMove, MoveUpgrade, PositionAnalysis } from "../utils/api";
import type { HighlightedMoves } from "../utils/gameStats";
import type { EngineInfo } from "../workers/stockfishMessages";
import type { VariationNodeId, VarNode } from "../types/variationTree";
import { useMoveAnalysis } from "../hooks/useMoveAnalysis";
import { useVariationTree } from "../hooks/useVariationTree";
import { useStockfishEngine } from "../hooks/useStockfishEngine";
import { useAnalysisEvidence } from "../services/analysisEvidence";
import { createAnalysisStore } from "../stores/createAnalysisStore";
import { useStore } from "zustand";
import { isTrustedExactBestHit, playerToWhite, playerToWhiteMate, toWhitePerspective } from "../workers/analysisUtils";
import { isCheckmateFen, whiteCpForMove } from "../utils/moveGraphEval";
import { projectExactBest } from "../utils/projectExactBest";
import AnalysisGraph from "./AnalysisGraph";
import EvalBar from "./EvalBar";
import MoveList from "./MoveList";
import HorizontalMoveList from "./HorizontalMoveList";
import { useMediaQuery } from "../hooks/useMediaQuery";
import MaterialDisplay from "./MaterialDisplay";
import { formatWhiteEval } from "./MoveRow.helpers";
import {
  buildEngineArrows,
  computeBoardEvalIcon,
  scoreToNum,
  type BoardEvalIcon,
} from "./AnalysisBoard.helpers";
import {
  isAnalysisBoardDiagnosticsEnabled,
  logAnalysisBoardDiagnostic,
} from "../utils/analysisBoardDiagnostics";

const STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const ENGINE_SEARCH_DEPTH = 21;
const ENGINE_EVALUATION_DEBOUNCE_MS = 120;
// Dwell before the evidence driver starts a depth-21 search on the displayed
// mainline position (g-cache-stronger-evals). Long enough that scrubbing through
// moves does not spawn a search per intermediate position.
const EVIDENCE_DWELL_MS = 700;

type AnalysisBoardProps = {
  moves: AnalysisMove[];
  boardOrientation: "white" | "black";
  startingFen?: string;
  initialMoveIndex?: number;
  footer?: React.ReactNode;
  mobileToolbar?: React.ReactNode;
  positionAnalysis?: Record<string, PositionAnalysis>;
  highlightedMoves?: HighlightedMoves | null;
  onGraphMoveClick?: () => void;
  /**
   * Saved-game session id (g-cache-stronger-evals). When present, the analysis-board
   * evidence driver persists stronger depth-21 evidence for dwelled mainline moves.
   * Omitted by the ephemeral drill board so it never writes evidence.
   */
  sessionId?: string;
};

// Convert SAN move to start/end squares using chess.js
const sanToSquares = (
  fen: string,
  san: string,
): { from: string; to: string } | null => {
  try {
    const tempChess = new Chess(fen);
    const result = tempChess.move(san);
    if (!result) return null;
    return { from: result.from, to: result.to };
  } catch {
    return null;
  }
};

const isSquare = (value: string): value is Square => /^[a-h][1-8]$/.test(value);

/** Extract side-to-move from a FEN string without constructing a Chess instance. */
const fenSideToMove = (fen: string): "w" | "b" => {
  const idx = fen.indexOf(" ");
  return (idx >= 0 ? fen[idx + 1] : "w") as "w" | "b";
};

type MoveSquares = { from: string; to: string };
type MainLineMoveDetails = {
  fenBefore: string;
  playedSquares: MoveSquares | null;
  bestSquares: MoveSquares | null;
};
type NavigationTrace = {
  id: number;
  startMs: number;
  fromIndex: number | null;
  toIndex: number | null;
  moveCount: number;
  engineLines: boolean;
  inVariation: boolean;
  selectedVarNodeId: VariationNodeId | null;
};

type EngineLinePreview = {
  sourceSlot: number;
  sanMoves: string[];
  /** Move-number prefix per ply (e.g. "2." or "26...") or "" for trailing black moves. */
  movePrefixes: string[];
  fenAfterPly: string[];
  uciMoves: string[];
  evalText: string;
  depth: number;
  /** "canonical" = stored deep best line (mixed-source eval); "live" = engine search at ENGINE_SEARCH_DEPTH. */
  source: "canonical" | "live";
};

const toWhitePerspectiveMate = (
  moverPerspectiveMate: number | null,
  moveIndex: number | null | undefined,
) => {
  if (
    moverPerspectiveMate === null ||
    moveIndex === null ||
    moveIndex === undefined
  ) {
    return moverPerspectiveMate;
  }

  return moveIndex % 2 === 0 ? moverPerspectiveMate : -moverPerspectiveMate;
};

const buildEngineLinePreview = (
  line: EngineInfo,
  sourceSlot: number,
  displayedFen: string,
  sideToMove: "w" | "b",
  canonical: boolean,
): EngineLinePreview | null => {
  if (!line?.pv?.length) return null;

  const tempChess = new Chess(displayedFen);
  const sanMoves: string[] = [];
  const movePrefixes: string[] = [];
  const fenAfterPly: string[] = [];
  const uciMoves: string[] = [];

  // Track move numbering from the displayed FEN's fullmove/side-to-move state.
  const fenParts = displayedFen.split(" ");
  let turn: "w" | "b" = fenParts[1] === "b" ? "b" : "w";
  let fullmove = Number.parseInt(fenParts[5] ?? "1", 10);
  if (!Number.isFinite(fullmove) || fullmove < 1) fullmove = 1;
  let isFirstPly = true;

  for (const uci of line.pv) {
    try {
      const from = uci.slice(0, 2);
      const to = uci.slice(2, 4);
      const promotion = uci.length > 4 ? uci[4] : undefined;
      const result = tempChess.move({ from, to, promotion });
      if (!result) break;
      sanMoves.push(result.san);

      if (turn === "w") {
        movePrefixes.push(`${fullmove}.`);
      } else if (isFirstPly) {
        movePrefixes.push(`${fullmove}...`);
      } else {
        movePrefixes.push("");
      }

      fenAfterPly.push(tempChess.fen());
      uciMoves.push(uci);

      if (turn === "b") fullmove += 1;
      turn = turn === "w" ? "b" : "w";
      isFirstPly = false;
    } catch {
      break;
    }
  }

  if (sanMoves.length === 0) return null;

  let evalText = "";
  if (line.score) {
    if (line.score.type === "mate") {
      const mate = sideToMove === "w" ? line.score.value : -line.score.value;
      evalText = mate >= 0 ? `M${mate}` : `-M${Math.abs(mate)}`;
    } else {
      const cp = sideToMove === "w" ? line.score.value : -line.score.value;
      const val = cp / 100;
      evalText = `${val >= 0 ? "+" : ""}${val.toFixed(1)}`;
    }
  }

  return {
    sourceSlot,
    sanMoves,
    movePrefixes,
    fenAfterPly,
    uciMoves,
    evalText,
    depth: line.depth ?? 0,
    source: canonical ? "canonical" : "live",
  };
};

export const buildMainLineMoveDetails = (
  moves: AnalysisMove[],
  startingFen: string,
): MainLineMoveDetails[] => {
  return moves.map((move, index) => {
    // Prefer the exact wire `fen_before` (g-cache-stronger-evals); fall back to the
    // previous move's `fen_after` chain only for legacy sessions whose wire field is
    // null, so existing games still render.
    const fenBefore =
      move.fen_before ??
      (index === 0 ? startingFen : moves[index - 1]?.fen_after ?? startingFen);
    const playedSquares = sanToSquares(fenBefore, move.move_san);
    const bestSquares =
      move.best_move_san && move.best_move_san !== move.move_san
        ? sanToSquares(fenBefore, move.best_move_san)
        : null;

    return {
      fenBefore,
      playedSquares,
      bestSquares,
    };
  });
};

/** Check whether a FEN already has a pending analysis request in flight. */
const hasPendingForFen = (pending: Map<string, string>, fen: string): boolean => {
  for (const v of pending.values()) {
    if (v === fen) return true;
  }
  return false;
};

export interface AnalysisBoardRef {
  jumpToMove(index: number): void;
}

const AnalysisBoard = forwardRef<AnalysisBoardRef, AnalysisBoardProps>(({
  moves,
  boardOrientation,
  startingFen = STARTING_FEN,
  initialMoveIndex,
  footer,
  mobileToolbar,
  positionAnalysis,
  highlightedMoves,
  onGraphMoveClick,
  sessionId,
}, ref) => {
  const debugEnabled = useMemo(() => isAnalysisBoardDiagnosticsEnabled(), []);
  const isNarrow = useMediaQuery("(max-width: 720px)");
  const reactBoardId = useId();
  const chessboardId = useMemo(
    () => `analysis-board-${reactBoardId.replace(/[^a-zA-Z0-9_-]/g, "")}`,
    [reactBoardId],
  );
  const navigationTraceRef = useRef<NavigationTrace | null>(null);
  const navigationTraceIdRef = useRef(0);
  const [currentIndex, setCurrentIndex] = useState<number | null>(
    initialMoveIndex ?? null,
  );
  const [analysisStore] = useState(() => createAnalysisStore());
  // rejectPending comes from useVariationTree (declared below), but
  // useMoveAnalysis needs the failure callback now. Bridge through a ref so the
  // stable callback resolves the latest rejectPending at call time (Finding F3).
  const rejectPendingRef = useRef<((id: string) => void) | null>(null);
  const handleVariationError = useCallback((id: string) => {
    rejectPendingRef.current?.(id);
  }, []);
  const { analyzeMove } = useMoveAnalysis(analysisStore, handleVariationError);
  const lastAnalysis = useStore(analysisStore, (s) => s.lastAnalysis);
  const variationStreamingEval = useStore(
    analysisStore,
    (s) => s.variationStreamingEval,
  );
  const [showEngineArrows, setShowEngineArrows] = useState(true);
  // On narrow viewports the panel collapses to just the toggle row: the PV list
  // (and with it the hover/click popup) costs vertical space the board needs.
  const showEngineLineList = showEngineArrows && !isNarrow;
  // Single source of truth for the engine-line popup: which line and whether it
  // is a transient hover preview or a click-persisted popup. Deriving the index
  // from this object avoids an "index set but mode stale" race.
  const [enginePopup, setEnginePopup] = useState<{ index: number; mode: "hover" | "persist" } | null>(null);
  const selectedEngineLineIndex = enginePopup?.index ?? null;
  const graceTimerRef = useRef<number | null>(null);
  const [selectedEnginePlyIndex, setSelectedEnginePlyIndex] = useState(0);
  const [enginePopupPosition, setEnginePopupPosition] = useState<React.CSSProperties | null>(null);
  const [selectedSquare, setSelectedSquare] = useState<string | null>(null);
  const [optionSquares, setOptionSquares] = useState<Record<string, React.CSSProperties>>({});
  const clearMoveHints = useCallback(() => {
    setSelectedSquare(null);
    setOptionSquares({});
  }, []);
  const boardRootRef = useRef<HTMLDivElement>(null);
  const boardFrameRef = useRef<HTMLDivElement>(null);
  const engineLineRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const enginePopupRef = useRef<HTMLDivElement>(null);
  const { info: engineLines, infoFen: engineInfoFen, isThinking: engineThinking, evaluatePosition, stopSearch } =
    useStockfishEngine({ enabled: showEngineArrows });

  // Variation tree hook
  const {
    tree,
    selectedVarNodeId,
    setSelectedVarNode,
    addMove,
    navigateUp,
    navigateDown,
    getAbsolutePly,
    getVarAnalysis,
    analysisCacheVersion,
    registerPending,
    resolvePending,
    rejectPending,
    pendingRequestsRef,
  } = useVariationTree();
  useEffect(() => {
    rejectPendingRef.current = rejectPending;
  }, [rejectPending]);

  const isInVariation = selectedVarNodeId != null;
  const selectedVarNode = useMemo(() => {
    if (!selectedVarNodeId) return null;
    return tree.nodes.get(selectedVarNodeId) ?? null;
  }, [selectedVarNodeId, tree]);

  const effectiveIndex = currentIndex ?? moves.length - 1;

  // Immediate MoveList patch (g-xox0 Part B): local overlay of accepted upgrades
  // this session's dwell produced, keyed by `${fen_before}::${move_uci}`. `moves` is
  // a prop, so the upgrade lives here as local state and is applied at the display
  // seam only (`effectiveMoves`); the page-level review stats deliberately stay on
  // the pre-upgrade array.
  const [liveUpgrades, setLiveUpgrades] = useState<Map<string, MoveUpgrade>>(
    () => new Map(),
  );
  const handleAcceptedEvidence = useCallback(
    (fen: string, moveUci: string, upgrade: MoveUpgrade) => {
      setLiveUpgrades((prev) => {
        const next = new Map(prev);
        next.set(`${fen}::${moveUci}`, upgrade);
        return next;
      });
    },
    [],
  );

  // Analysis-board evidence driver (g-cache-stronger-evals): persist stronger
  // depth-21 evidence for the mainline move the user is dwelling on. No-ops when
  // sessionId is absent (drill/ephemeral boards). On an accepted write it fires
  // handleAcceptedEvidence so the open MoveList patches immediately (Part B).
  const { requestEvidence: requestAnalysisEvidence, cancel: cancelAnalysisEvidence } =
    useAnalysisEvidence(sessionId, handleAcceptedEvidence);

  // Drive one evidence search for the NEXT mainline move after a dwell. Fires only
  // on the mainline (never a variation), only when a real next move exists with
  // exact wire keys, and only once the display engine has settled — so the two
  // engines do not fight. Navigation reruns this effect: cleanup cancels any
  // in-flight evidence search so an interrupted search never submits.
  useEffect(() => {
    if (!sessionId || isInVariation) return;
    const nextMove = moves[effectiveIndex + 1];
    const fen = nextMove?.fen_before;
    const move = nextMove?.move_uci;
    // Skip legacy moves with null wire fields; the driver never reconstructs them.
    if (!fen || !move) return;
    // Defer to the display engine: while it is searching, wait for it to settle.
    if (engineThinking) return;
    const playerColor = fenSideToMove(fen) === "w" ? "white" : "black";
    const timer = window.setTimeout(() => {
      requestAnalysisEvidence(fen, move, playerColor);
    }, EVIDENCE_DWELL_MS);
    return () => {
      clearTimeout(timer);
      cancelAnalysisEvidence();
    };
  }, [
    sessionId,
    isInVariation,
    effectiveIndex,
    moves,
    engineThinking,
    requestAnalysisEvidence,
    cancelAnalysisEvidence,
  ]);

  const traceNavigation = useCallback(
    (toIndex: number | null) => {
      if (!debugEnabled || typeof performance === "undefined") return;
      navigationTraceRef.current = {
        id: ++navigationTraceIdRef.current,
        startMs: performance.now(),
        fromIndex: currentIndex,
        toIndex,
        moveCount: moves.length,
        engineLines: showEngineArrows,
        inVariation: isInVariation,
        selectedVarNodeId,
      };
    },
    [
      currentIndex,
      debugEnabled,
      isInVariation,
      moves.length,
      selectedVarNodeId,
      showEngineArrows,
    ],
  );

  const handleProfilerRender = useCallback<ProfilerOnRenderCallback>(
    (
      id,
      phase,
      actualDuration,
      baseDuration,
      startTime,
      commitTime,
    ) => {
      if (!debugEnabled) return;
      const trace = navigationTraceRef.current;
      if (!trace && actualDuration < 12) return;

      logAnalysisBoardDiagnostic("react-render", {
        id,
        phase,
        actualDurationMs: Number(actualDuration.toFixed(2)),
        baseDurationMs: Number(baseDuration.toFixed(2)),
        renderToCommitMs: Number((commitTime - startTime).toFixed(2)),
        traceId: trace?.id ?? null,
        currentIndex,
        effectiveIndex,
        moveCount: moves.length,
        engineLines: showEngineArrows,
        inVariation: isInVariation,
      });
    },
    [
      currentIndex,
      debugEnabled,
      effectiveIndex,
      isInVariation,
      moves.length,
      showEngineArrows,
    ],
  );

  // Source-aware re-annotation overlay (g-xox0 Parts B + C). Per move, take the live
  // dwell upgrade (Part B, this session) if present, else the fetch-time `upgraded`
  // (Part C); live wins, and they should agree. A NON-authoritative (browser-
  // analysis) overlay is SKIPPED on a TRUSTED position so it never overrides trusted
  // position truth — this covers the trusted-but-played≠best case promotion-only
  // projectExactBest cannot repair. An AUTHORITATIVE/canonical overlay ALWAYS applies
  // (authority comes from the backend-stamped flag, never re-derived here). Then
  // projectExactBest (pure, promotion-only, idempotent) promotes any played==trusted-
  // best move — belt-and-suspenders after a skip AND the correction for HistoryPage,
  // which feeds RAW `analysis.moves`. GameAnalysisPage is already projected, so the
  // second pass is a no-op there. EVERY display derivation below reads this array;
  // navigation and the evidence driver keep raw `moves` (immutable wire keys, stable
  // index — the overlay changes labels/evals only, never array identity or length).
  const effectiveMoves = useMemo(() => {
    const overlaid = moves.map((move, index) => {
      const key =
        move.fen_before && move.move_uci
          ? `${move.fen_before}::${move.move_uci}`
          : null;
      // Source precedence: the live dwell upgrade (Part B) wins over the fetch-time
      // one (Part C) — they should agree — EXCEPT never let a stale non-authoritative
      // live row shadow an AUTHORITATIVE fetched one. A later refetch can bring a
      // canonical upgrade for a key this session already dwelled at d21; the canonical
      // truth must win (and must not be dropped by the trusted-position skip below).
      const live = key ? liveUpgrades.get(key) : undefined;
      const fetched = move.upgraded;
      const up =
        fetched?.authoritative && !live?.authoritative ? fetched : live ?? fetched;
      if (!up) return move;
      const fenBefore =
        move.fen_before ??
        (index === 0 ? startingFen : moves[index - 1]?.fen_after ?? startingFen);
      const entry = positionAnalysis?.[fenBefore];
      const positionTrusted = !!entry && isTrustedExactBestHit(entry);
      if (!up.authoritative && positionTrusted) return move; // SKIP
      return {
        ...move,
        classification: up.classification,
        eval_cp: up.eval_cp,
        eval_mate: up.eval_mate,
        best_move_san: up.best_move_san,
        best_move_eval_cp: up.best_move_eval_cp,
        eval_delta: up.eval_delta,
      };
    });
    return projectExactBest(overlaid, positionAnalysis, startingFen);
  }, [moves, liveUpgrades, positionAnalysis, startingFen]);

  const mainLineMoveDetails = useMemo(
    () => buildMainLineMoveDetails(effectiveMoves, startingFen),
    [effectiveMoves, startingFen],
  );

  // Map AnalysisMove[] to Move[] for MoveList
  const mappedMoves = useMemo(
    () =>
      effectiveMoves.map((m, i) => ({
        san: m.move_san,
        classification: m.classification,
        eval: toWhitePerspective(m.eval_cp, i),
        evalMate: toWhitePerspectiveMate(m.eval_mate, i),
      })),
    [effectiveMoves],
  );

  // Extract eval values for the graph. whiteCpForMove prefers the CP channel,
  // then a correctly-signed mate cp; for the terminal ply it also synthesizes an
  // unevaluated checkmate (both channels null) from fen_after so a final mate
  // never lands on the equal line. fen_after is passed only for the last ply —
  // checkmate can only be the last move.
  const evals = useMemo(() => {
    const lastIdx = effectiveMoves.length - 1;
    return effectiveMoves.map((m, i) =>
      whiteCpForMove(m.eval_cp, m.eval_mate, i, i === lastIdx ? m.fen_after : null),
    );
  }, [effectiveMoves]);

  // FEN at the position before the current move (needed for arrow SAN→UCI)
  const fenBeforeCurrentMove = useMemo(() => {
    if (isInVariation) return null; // no cached best arrows in variations
    if (effectiveIndex < 0) return null;
    return mainLineMoveDetails[effectiveIndex]?.fenBefore ?? null;
  }, [isInVariation, effectiveIndex, mainLineMoveDetails]);

  // Displayed FEN
  const displayedFen = useMemo(() => {
    if (isInVariation && selectedVarNode) return selectedVarNode.fen;
    if (effectiveIndex === -1) return startingFen;
    return moves[effectiveIndex]?.fen_after ?? startingFen;
  }, [isInVariation, selectedVarNode, effectiveIndex, moves, startingFen]);
  const previousDisplayedFenRef = useRef(displayedFen);

  // Side-to-move derived from FEN (avoids constructing Chess just for turn())
  const sideToMove = useMemo(() => fenSideToMove(displayedFen), [displayedFen]);

  useEffect(() => {
    if (!debugEnabled) return;
    if (typeof PerformanceObserver === "undefined") {
      logAnalysisBoardDiagnostic("long-task-observer-unavailable", {});
      return;
    }

    let observer: PerformanceObserver | null = null;
    try {
      observer = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          logAnalysisBoardDiagnostic(
            "browser-long-task",
            {
              durationMs: Number(entry.duration.toFixed(2)),
              startMs: Number(entry.startTime.toFixed(2)),
              name: entry.name,
            },
            "warn",
          );
        }
      });
      observer.observe({ entryTypes: ["longtask"] });
      logAnalysisBoardDiagnostic("enabled", {
        moveCount: moves.length,
        engineLines: showEngineArrows,
      });
    } catch (error) {
      logAnalysisBoardDiagnostic("long-task-observer-error", {
        error: error instanceof Error ? error.message : String(error),
      });
    }

    return () => observer?.disconnect();
  }, [debugEnabled, moves.length, showEngineArrows]);

  useEffect(() => {
    if (!debugEnabled || typeof window === "undefined") return;
    const trace = navigationTraceRef.current;
    if (!trace) return;

    const commitMs = performance.now() - trace.startMs;
    const frameId = window.requestAnimationFrame(() => {
      const paintMs = performance.now() - trace.startMs;
      logAnalysisBoardDiagnostic(
        paintMs >= 100 ? "navigation-slow-paint" : "navigation-paint",
        {
          traceId: trace.id,
          fromIndex: trace.fromIndex,
          toIndex: trace.toIndex,
          currentIndex,
          effectiveIndex,
          moveCount: trace.moveCount,
          engineLines: trace.engineLines,
          wasInVariation: trace.inVariation,
          selectedVarNodeId: trace.selectedVarNodeId,
          displayedFen: displayedFen.slice(0, 32),
          commitMs: Number(commitMs.toFixed(2)),
          paintMs: Number(paintMs.toFixed(2)),
        },
        paintMs >= 100 ? "warn" : "info",
      );

      if (navigationTraceRef.current?.id === trace.id) {
        navigationTraceRef.current = null;
      }
    });

    return () => window.cancelAnimationFrame(frameId);
  }, [
    currentIndex,
    debugEnabled,
    displayedFen,
    effectiveIndex,
    selectedVarNodeId,
  ]);

  // Cached best move for the displayed position (from pre-existing game analysis)
  const cachedBest = positionAnalysis?.[displayedFen] ?? null;

  // Only a TRUSTED canonical position winner with a comparable cp eval may drive
  // the search-skip optimization and the line-1 prepend (g-54h5). Untrusted seeds
  // (browser-game rows, local drill-snapshot worker results) or seeds without a
  // comparable cp eval fall through to a full multipv search so the live engine
  // ranks every move at one depth — no "line 2 better than line 1" contradiction.
  const trustedBest =
    cachedBest &&
    cachedBest.position_trusted === true &&
    (cachedBest.best_move_eval_cp != null ||
      cachedBest.best_move_eval_mate != null)
      ? cachedBest
      : null;

  // Legal moves excluding cached best move (for restricted engine search)
  const searchmoves = useMemo(() => {
    if (!showEngineArrows || !trustedBest) return undefined;
    try {
      const chess = new Chess(displayedFen);
      const allMoves = chess.moves({ verbose: true });
      const filtered = allMoves
        .map((m) => m.from + m.to + (m.promotion ?? ""))
        .filter((uci) => uci !== trustedBest.best_move_uci);
      return filtered.length > 0 ? filtered : undefined;
    } catch {
      return undefined;
    }
  }, [displayedFen, trustedBest, showEngineArrows]);

  // Start new evaluation after render
  useEffect(() => {
    if (!showEngineArrows) return;

    if (!displayedFen) return;

    stopSearch();

    const timerId = window.setTimeout(() => {
      if (trustedBest && searchmoves && searchmoves.length > 0) {
        evaluatePosition(displayedFen, { depth: ENGINE_SEARCH_DEPTH, multipv: 2, searchmoves }).catch(() => {});
      } else {
        evaluatePosition(displayedFen, { depth: ENGINE_SEARCH_DEPTH, multipv: 3 }).catch(() => {});
      }
    }, ENGINE_EVALUATION_DEBOUNCE_MS);

    return () => window.clearTimeout(timerId);
  }, [displayedFen, evaluatePosition, showEngineArrows, trustedBest, searchmoves]);

  // Whether the restricted search path is active (same condition as the engine request)
  const useRestrictedSearch = !!(
    showEngineArrows &&
    trustedBest &&
    searchmoves &&
    searchmoves.length > 0
  );

  const activeEngineLines = showEngineArrows && engineInfoFen === displayedFen
    ? engineLines
    : [];
  const activeEngineDepth = activeEngineLines[0]?.depth ?? 0;
  const cappedEngineDepth = Math.min(activeEngineDepth, ENGINE_SEARCH_DEPTH);
  const engineProgressPercent = (cappedEngineDepth / ENGINE_SEARCH_DEPTH) * 100;

  // Merge cached best move into engine lines so arrows and panel stay in sync.
  // Only merge when the restricted search was actually used — otherwise the
  // engine already searched for the full top-line set including the best move.
  // EngineInfo.score.value is side-to-move-relative; best_move_eval_cp is also
  // side-to-move-relative, so we pass it through without sign conversion.
  const mergedEngineLines: EngineInfo[] = useMemo(() => {
    if (!showEngineArrows) return [];
    if (!useRestrictedSearch || !trustedBest) return activeEngineLines;

    const cachedLine: EngineInfo = {
      // Prefer the stored root PV (validated to start with best_move_uci) so the
      // popup can render a full continuation; legacy rows fall back to one move.
      pv:
        trustedBest.best_line_uci && trustedBest.best_line_uci.length > 0
          ? trustedBest.best_line_uci
          : [trustedBest.best_move_uci],
      score:
        trustedBest.best_move_eval_mate != null
          ? { type: "mate" as const, value: trustedBest.best_move_eval_mate }
          : trustedBest.best_move_eval_cp != null
            ? { type: "cp" as const, value: trustedBest.best_move_eval_cp }
            : undefined,
      depth: undefined,
    };

    // Re-rank by one consistent eval so displayed line 1 is always >= every other
    // line (g-54h5). Stable: ties keep the trusted cached line first (canonical
    // best preserved when live merely matches it). A higher searched line (depth/
    // source mismatch) sorts above the cached line instead of "line 2 better than
    // line 1". Both cachedLine.score and live scores are side-to-move-relative cp,
    // so scoreToNum compares them on one scale.
    //
    // Filter holes first: useStockfishEngine fills slots by multipv index, so
    // streaming output can be a sparse array (e.g. [<hole>, line2] before line 1
    // arrives). The spread materializes that hole as `undefined`, which would
    // crash the score map below — drop missing entries before scoring/sorting.
    return [cachedLine, ...activeEngineLines]
      .filter((line): line is EngineInfo => line != null)
      .map((line, i) => ({ line, i, n: scoreToNum(line.score) }))
      .sort((a, b) => {
        if (a.n === b.n) return a.i - b.i; // stable tiebreak (keeps cached first)
        if (a.n === null) return 1; // null eval sinks to the bottom
        if (b.n === null) return -1;
        return b.n - a.n; // higher eval first
      })
      .map((x) => x.line);
  }, [showEngineArrows, useRestrictedSearch, trustedBest, activeEngineLines]);

  // Engine lines with SAN moves, replay FENs, and formatted evals for display/preview
  const engineLinesDisplay = useMemo(() => {
    if (!showEngineArrows) return [];
    if (mergedEngineLines.length === 0) return [];
    return mergedEngineLines.map((line, index) => {
      // The cached canonical line is only ever prepended in the restricted-merge
      // path; gate on useRestrictedSearch so we don't mislabel a live full-search
      // line as canonical when trustedBest is set but the merge didn't happen
      // (e.g. the trusted best is the only legal move → searchmoves undefined →
      // full multipv:3 search returns live depth-21 output). Within that path the
      // cached line is the only one whose first move is the trusted best (it's
      // excluded from searchmoves, so no live line shares it), which identifies it
      // wherever Edit 2's re-rank placed it.
      const canonical =
        useRestrictedSearch &&
        !!trustedBest &&
        line?.pv?.[0] === trustedBest.best_move_uci;
      return buildEngineLinePreview(
        line,
        index,
        displayedFen,
        sideToMove,
        canonical,
      );
    });
  }, [
    showEngineArrows,
    mergedEngineLines,
    displayedFen,
    sideToMove,
    trustedBest,
    useRestrictedSearch,
  ]);

  const selectedEngineLine =
    selectedEngineLineIndex === null
      ? null
      : engineLinesDisplay[selectedEngineLineIndex] ?? null;

  const selectedEnginePlyRenderIndex = selectedEngineLine
    ? Math.min(selectedEnginePlyIndex, selectedEngineLine.fenAfterPly.length - 1)
    : 0;

  const selectedPreviewFen =
    selectedEngineLine?.fenAfterPly[selectedEnginePlyRenderIndex] ?? null;

  const selectedPreviewMove = selectedEngineLine?.uciMoves[selectedEnginePlyRenderIndex] ?? null;
  const previewSquareStyles = useMemo((): Record<string, React.CSSProperties> => {
    if (!selectedPreviewMove) return {};
    const style: React.CSSProperties = { background: "rgba(59, 130, 246, 0.3)" };
    return {
      [selectedPreviewMove.slice(0, 2)]: style,
      [selectedPreviewMove.slice(2, 4)]: style,
    };
  }, [selectedPreviewMove]);

  const clearGraceTimer = useCallback(() => {
    if (graceTimerRef.current !== null) {
      window.clearTimeout(graceTimerRef.current);
      graceTimerRef.current = null;
    }
  }, []);

  const closeEnginePopup = useCallback(() => {
    clearGraceTimer();
    setEnginePopup(null);
    setEnginePopupPosition(null);
  }, [clearGraceTimer]);

  const updateEnginePopupPosition = useCallback(() => {
    if (selectedEngineLineIndex === null) return;
    const root = boardRootRef.current;
    const anchor = engineLineRefs.current[selectedEngineLineIndex];
    if (!root || !anchor) return;

    const rootRect = root.getBoundingClientRect();
    const anchorRect = anchor.getBoundingClientRect();
    const popupWidth = enginePopupRef.current?.offsetWidth ?? Math.min(192, window.innerWidth - 32);
    const viewportPadding = 16;
    const preferredLeft = anchorRect.right - rootRect.left - popupWidth;
    const minLeft = viewportPadding - rootRect.left;
    const maxLeft = window.innerWidth - viewportPadding - rootRect.left - popupWidth;
    const left = Math.min(Math.max(preferredLeft, minLeft), Math.max(minLeft, maxLeft));
    const top = anchorRect.bottom - rootRect.top + 8;

    setEnginePopupPosition({ left, top });
  }, [selectedEngineLineIndex]);

  useEffect(() => {
    if (previousDisplayedFenRef.current !== displayedFen) {
      previousDisplayedFenRef.current = displayedFen;
      // Sync popup/hint UI to the displayed position when it changes.
      /* eslint-disable-next-line react-hooks/set-state-in-effect */
      closeEnginePopup();
      clearMoveHints();
    }
  }, [displayedFen, closeEnginePopup, clearMoveHints]);

  useEffect(() => {
    if (!showEngineLineList) {
      // Close the popup when the lines that anchor it are gone (engine arrows
      // toggled off, or a resize into the narrow layout).
      /* eslint-disable-next-line react-hooks/set-state-in-effect */
      closeEnginePopup();
    }
  }, [showEngineLineList, closeEnginePopup]);

  useEffect(() => {
    const root = boardRootRef.current;
    const boardFrame = boardFrameRef.current;
    if (!root || !boardFrame) return;

    const syncBoardHeight = () => {
      const height = boardFrame.getBoundingClientRect().height;
      if (height > 0) {
        root.style.setProperty("--analysis-board-main-height", `${height}px`);
      }
    };

    syncBoardHeight();

    if (typeof ResizeObserver === "undefined") {
      return;
    }

    const observer = new ResizeObserver(syncBoardHeight);
    observer.observe(boardFrame);

    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (selectedEngineLineIndex === null) return;
    const line = engineLinesDisplay[selectedEngineLineIndex];
    if (!line) {
      // Selected line vanished — close its popup.
      /* eslint-disable-next-line react-hooks/set-state-in-effect */
      closeEnginePopup();
      return;
    }

    setSelectedEnginePlyIndex((prev) => Math.min(prev, line.fenAfterPly.length - 1));
    window.requestAnimationFrame(updateEnginePopupPosition);
  }, [engineLinesDisplay, selectedEngineLineIndex, closeEnginePopup, updateEnginePopupPosition]);

  useEffect(() => {
    if (selectedEngineLineIndex === null) return;
    updateEnginePopupPosition();

    const handleResize = () => updateEnginePopupPosition();
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [selectedEngineLineIndex, updateEnginePopupPosition]);

  useEffect(() => {
    if (selectedEngineLineIndex === null) return;

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (enginePopupRef.current?.contains(target)) return;
      if (engineLineRefs.current.some((button) => button?.contains(target))) return;
      closeEnginePopup();
    };

    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [selectedEngineLineIndex, closeEnginePopup]);

  // Arrow keys drive the preview PV only when the popup is persisted (clicked).
  // In hover mode the arrows keep driving the main move list (see MoveList's
  // suppressKeyboardNavigation prop, also gated on persist).
  useEffect(() => {
    if (enginePopup?.mode !== "persist" || !selectedEngineLine) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      event.stopPropagation();
      setSelectedEnginePlyIndex((prev) => {
        if (event.key === "ArrowLeft") return Math.max(0, prev - 1);
        return Math.min(selectedEngineLine.fenAfterPly.length - 1, prev + 1);
      });
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [enginePopup, selectedEngineLine]);

  // Clear any pending grace timer on unmount.
  useEffect(() => clearGraceTimer, [clearGraceTimer]);

  // Engine-recommended move arrows — best move is blue, others grey with
  // opacity proportional to their centipawn loss relative to the best move.
  const engineArrows = useMemo(
    () => showEngineArrows ? buildEngineArrows(mergedEngineLines) : [],
    [showEngineArrows, mergedEngineLines],
  );

  // Arrows for the current position
  const arrows = useMemo(() => {
    if (isInVariation) return [];
    if (effectiveIndex < 0 || !fenBeforeCurrentMove) return [];

    const move = effectiveMoves[effectiveIndex];
    if (!move) return [];

    const result: { startSquare: string; endSquare: string; color: string }[] =
      [];

    // Only show played/best arrows when there's a different best move
    if (move.best_move_san && move.best_move_san !== move.move_san) {
      const details = mainLineMoveDetails[effectiveIndex];
      if (details?.playedSquares) {
        result.push({
          startSquare: details.playedSquares.from,
          endSquare: details.playedSquares.to,
          color: "rgba(248, 113, 113, 0.8)",
        });
      }

      if (details?.bestSquares) {
        result.push({
          startSquare: details.bestSquares.from,
          endSquare: details.bestSquares.to,
          color: "rgba(52, 211, 153, 0.8)",
        });
      }
    }

    return result;
  }, [
    isInVariation,
    effectiveIndex,
    fenBeforeCurrentMove,
    effectiveMoves,
    mainLineMoveDetails,
  ]);

  // Merge: played/best arrows take priority over engine arrows on same squares
  const allArrows = useMemo(() => {
    const visible = showEngineArrows ? engineArrows : [];
    const seen = new Set(arrows.map((a) => `${a.startSquare}-${a.endSquare}`));
    const deduped = visible.filter(
      (a) => !seen.has(`${a.startSquare}-${a.endSquare}`),
    );
    const merged = [...deduped, ...arrows];
    return merged.length > 0 ? merged : undefined;
  }, [engineArrows, arrows, showEngineArrows]);

  // Current move data for position info panel. Reads the overlaid array so the eval
  // bar / current-move readout follow an upgrade consistently with the badge.
  const currentMove = useMemo(() => {
    if (isInVariation || effectiveIndex < 0) return null;
    return effectiveMoves[effectiveIndex] ?? null;
  }, [isInVariation, effectiveIndex, effectiveMoves]);

  // Live engine eval from top PV line (white perspective)
  const liveEngineEvalCp = useMemo(() => {
    if (!showEngineArrows) return null;
    const topLine = mergedEngineLines[0];
    if (!topLine?.score) return null;
    const raw = topLine.score.type === "cp" ? topLine.score.value : null;
    if (raw === null) return null;
    return sideToMove === "w" ? raw : -raw;
  }, [showEngineArrows, mergedEngineLines, sideToMove]);

  const liveEngineEvalMate = useMemo(() => {
    if (!showEngineArrows) return null;
    const topLine = mergedEngineLines[0];
    if (!topLine?.score) return null;
    if (topLine.score.type !== "mate") return null;
    return sideToMove === "w" ? topLine.score.value : -topLine.score.value;
  }, [showEngineArrows, mergedEngineLines, sideToMove]);

  // When engine arrows are on AND the live search has produced a score, the eval
  // bar should read both cp and mate from that single live source. Otherwise it
  // falls back to the cached variation analysis (both channels). This keeps the
  // cp/mate pair consistent so a stale cached mate can't override a live cp.
  const useLiveEngineEval =
    showEngineArrows &&
    (liveEngineEvalCp !== null || liveEngineEvalMate !== null);

  const currentEvalCp = useMemo(() => {
    if (isInVariation || effectiveIndex < 0) return null;
    return toWhitePerspective(currentMove?.eval_cp ?? null, effectiveIndex);
  }, [isInVariation, effectiveIndex, currentMove]);

  const currentEvalMate = useMemo(() => {
    if (isInVariation || effectiveIndex < 0) return null;
    return toWhitePerspectiveMate(
      currentMove?.eval_mate ?? null,
      effectiveIndex,
    );
  }, [isInVariation, effectiveIndex, currentMove]);

  // Badge checkmate signal for the main line, derived from the displayed FEN.
  // currentEvalMate === 0 misses an unevaluated terminal mate (both eval channels
  // null) — the exact shape this bug is about — so drive the # badge from the FEN.
  const isCheckmateDisplayed = useMemo(
    () => isCheckmateFen(displayedFen),
    [displayedFen],
  );

  // Variation eval for eval bar (white perspective)
  const varEvalCp = useMemo(() => {
    // getVarAnalysis reads a ref; depend on the cache version so this re-runs
    // when an analysis resolves.
    void analysisCacheVersion;
    if (!isInVariation || !selectedVarNode) return null;
    const cached = getVarAnalysis(selectedVarNode.fen);
    if (!cached || cached.playedEval == null) return null;
    return playerToWhite(cached.playedEval, boardOrientation);
  }, [isInVariation, selectedVarNode, getVarAnalysis, boardOrientation, analysisCacheVersion]);

  // Variation mate-in-N for eval bar (white perspective)
  const varEvalMate = useMemo(() => {
    void analysisCacheVersion;
    if (!isInVariation || !selectedVarNode) return null;
    const cached = getVarAnalysis(selectedVarNode.fen);
    if (!cached) return null;
    return playerToWhiteMate(cached.playedEvalMate, boardOrientation);
  }, [isInVariation, selectedVarNode, getVarAnalysis, boardOrientation, analysisCacheVersion]);

  // Variation header eval for MoveList (mate code takes precedence over cp)
  const varHeaderEval = useMemo((): string | null => {
    if (!isInVariation) return null;
    if (varEvalCp == null && varEvalMate == null) return null;
    return formatWhiteEval(varEvalCp, varEvalMate);
  }, [isInVariation, varEvalCp, varEvalMate]);

  // What-if line for the graph overlay: dotted path tracing the selected
  // variation up to (and including) the selected move. Future/deselected moves
  // are excluded by walking only the ancestor chain of the selected node.
  const variationLine = useMemo(() => {
    void analysisCacheVersion; // re-run when a variation analysis resolves
    if (!isInVariation || !selectedVarNode || !selectedVarNodeId) return null;

    // Walk parentId chain to build ancestors root→selected.
    const chain: VarNode[] = [];
    let current: VarNode | undefined = selectedVarNode;
    while (current) {
      chain.unshift(current);
      current = current.parentId
        ? tree.nodes.get(current.parentId)
        : undefined;
    }
    if (chain.length === 0) return null;

    const rootNode = chain[0];
    const baseIndex = rootNode.parentGameIndex;
    const selectedPly = getAbsolutePly(selectedVarNodeId);

    // In-flight streaming tip: match on the selected node's FEN (not just ply —
    // sibling variations can share an absolute ply). Worker cp is
    // player-perspective, so convert to white-perspective for the graph.
    const streaming =
      variationStreamingEval &&
      variationStreamingEval.fen === selectedVarNode.fen
        ? {
            index: selectedPly,
            cp: playerToWhite(variationStreamingEval.cp, boardOrientation) ?? 0,
          }
        : null;

    const anchor =
      baseIndex >= 0 ? { index: baseIndex, cp: evals[baseIndex] ?? 0 } : null;

    const points = chain
      .map((node, depth) => ({ node, index: baseIndex + 1 + depth }))
      // Exclude the in-flight tip; it is drawn as the streaming segment instead.
      .filter(({ index }) => !(streaming && index === streaming.index))
      .map(({ node, index }) => {
        const cached = getVarAnalysis(node.fen);
        const cp =
          cached && cached.playedEval != null
            ? playerToWhite(cached.playedEval, boardOrientation) ?? 0
            : 0;
        return { index, cp, pending: cached == null };
      });

    return { anchor, points, streaming };
  }, [
    isInVariation,
    selectedVarNode,
    selectedVarNodeId,
    tree,
    evals,
    getVarAnalysis,
    getAbsolutePly,
    variationStreamingEval,
    boardOrientation,
    analysisCacheVersion,
  ]);

  // Highlight from/to squares of the last move
  const lastMoveSquares = useMemo((): Record<string, React.CSSProperties> => {
    const style: React.CSSProperties = { background: "rgba(255, 255, 0, 0.4)" };
    if (isInVariation && selectedVarNode) {
      const sq = sanToSquares(selectedVarNode.fenBefore, selectedVarNode.san);
      if (!sq) return {};
      return { [sq.from]: style, [sq.to]: style };
    }
    if (effectiveIndex < 0 || !fenBeforeCurrentMove) return {};
    const move = moves[effectiveIndex];
    if (!move) return {};
    const sq = mainLineMoveDetails[effectiveIndex]?.playedSquares ?? null;
    if (!sq) return {};
    return { [sq.from]: style, [sq.to]: style };
  }, [
    isInVariation,
    selectedVarNode,
    effectiveIndex,
    fenBeforeCurrentMove,
    moves,
    mainLineMoveDetails,
  ]);

  // Eval-icon badge rendered on the board at the current move's destination.
  // Skipped in variations (no classification) and for good/null moves.
  const boardEvalIcon = useMemo((): BoardEvalIcon | null => {
    if (isInVariation || effectiveIndex < 0) return null;
    const square = mainLineMoveDetails[effectiveIndex]?.playedSquares?.to ?? null;
    return computeBoardEvalIcon({
      square,
      classification: effectiveMoves[effectiveIndex]?.classification ?? null,
      boardOrientation,
    });
  }, [
    isInVariation,
    effectiveIndex,
    mainLineMoveDetails,
    effectiveMoves,
    boardOrientation,
  ]);

  // Merge last-move highlight with click-to-select option dots (option styles win)
  const squareStyles = useMemo(
    (): Record<string, React.CSSProperties> => ({ ...lastMoveSquares, ...optionSquares }),
    [lastMoveSquares, optionSquares],
  );

  // Resolve pending variation analysis when lastAnalysis fires
  useEffect(() => {
    if (!lastAnalysis) return;
    resolvePending(lastAnalysis.id, lastAnalysis);
  }, [lastAnalysis, resolvePending]);

  // Handle MoveList navigation
  const handleNavigate = useCallback(
    (index: number | null) => {
      traceNavigation(index);
      clearMoveHints();
      setSelectedVarNode(null);
      setCurrentIndex(index);
    },
    [setSelectedVarNode, traceNavigation, clearMoveHints],
  );

  useImperativeHandle(ref, () => ({
    jumpToMove: (index: number) => {
      handleNavigate(index);
    }
  }), [handleNavigate]);

  // Graph click: navigate + clear pinned highlights
  const handleGraphSelect = useCallback(
    (index: number) => {
      handleNavigate(index);
      onGraphMoveClick?.();
    },
    [handleNavigate, onGraphMoveClick],
  );

  // Handle variation node selection from MoveList
  const handleVarSelect = useCallback(
    (nodeId: VariationNodeId | null) => {
      clearMoveHints();
      setSelectedVarNode(nodeId);
    },
    [setSelectedVarNode, clearMoveHints],
  );

  // Shared move-execution core for both drag-drop and click-to-move
  const tryMove = useCallback(
    (sourceSquare: string, targetSquare: string | null): boolean => {
      // Determine the FEN to play from
      const baseFen = isInVariation && selectedVarNode
        ? selectedVarNode.fen
        : displayedFen;

      try {
        const tempChess = new Chess(baseFen);
        if (!targetSquare) return false;
        const result = tempChess.move({
          from: sourceSquare,
          to: targetSquare,
          promotion: "q",
        });
        if (!result) return false;

        // Main-line continuation check: if the next game move matches, just advance
        if (!isInVariation) {
          const nextIdx = effectiveIndex + 1;
          if (nextIdx < moves.length && moves[nextIdx].move_san === result.san) {
            setCurrentIndex(nextIdx >= moves.length - 1 ? null : nextIdx);
            return true;
          }
        }

        // Determine parent context for variation tree
        const parentContext = isInVariation && selectedVarNodeId
          ? { type: 'variation' as const, nodeId: selectedVarNodeId }
          : { type: 'game' as const, moveIndex: effectiveIndex };

        const resultFen = tempChess.fen();
        const uciMove = `${sourceSquare}${targetSquare}${result.promotion ?? ""}`;

        const nodeId = addMove({
          san: result.san,
          fen: resultFen,
          fenBefore: baseFen,
          uci: uciMove,
          parentContext,
        });
        if (!nodeId) return false;

        setSelectedVarNode(nodeId);

        // Dedup-aware analysis: skip if already cached or pending
        const alreadyCached = !!getVarAnalysis(resultFen);
        const alreadyPending = hasPendingForFen(pendingRequestsRef.current, resultFen);
        if (!alreadyCached && !alreadyPending) {
          const reqId = analyzeMove(
            baseFen,
            uciMove,
            boardOrientation,
            undefined,
            undefined,
            getAbsolutePly(nodeId),
            resultFen,
          );
          if (reqId) {
            registerPending(reqId, resultFen);
          }
        }

        return true;
      } catch {
        return false;
      }
    },
    [
      isInVariation,
      selectedVarNode,
      selectedVarNodeId,
      displayedFen,
      effectiveIndex,
      moves,
      addMove,
      setSelectedVarNode,
      getVarAnalysis,
      pendingRequestsRef,
      analyzeMove,
      boardOrientation,
      registerPending,
      getAbsolutePly,
    ],
  );

  // Handle piece drop for what-if exploration
  const handleDrop = useCallback(
    ({ sourceSquare, targetSquare }: PieceDropHandlerArgs) => {
      const moved = tryMove(sourceSquare, targetSquare);
      if (moved) clearMoveHints();
      return moved;
    },
    [tryMove, clearMoveHints],
  );

  // Build legal-move hint styles for a clicked piece; mirrors ChessGame.getMoveOptions
  const getMoveOptions = useCallback(
    (square: string): boolean => {
      if (!isSquare(square)) {
        return false;
      }

      const baseFen = isInVariation && selectedVarNode
        ? selectedVarNode.fen
        : displayedFen;

      let moves;
      let chess;
      try {
        chess = new Chess(baseFen);
        moves = chess.moves({ square, verbose: true });
      } catch {
        return false;
      }
      if (moves.length === 0) {
        return false;
      }

      const sourcePiece = chess.get(square);
      const newSquares: Record<string, React.CSSProperties> = {};
      for (const move of moves) {
        const target = chess.get(move.to);
        const isCapture =
          sourcePiece != null &&
          target != null &&
          target.color !== sourcePiece.color;
        newSquares[move.to] = {
          background: isCapture
            ? "rgba(255, 0, 0, 0.4)"
            : "radial-gradient(circle, rgba(0,0,0,.1) 25%, transparent 25%)",
          borderRadius: "50%",
        };
      }

      newSquares[square] = {
        background: "rgba(255, 255, 0, 0.4)",
      };

      setOptionSquares(newSquares);
      return true;
    },
    [isInVariation, selectedVarNode, displayedFen],
  );

  // Click-to-select / click-to-move on the analysis board
  const handleSquareClick = useCallback(
    ({ square }: SquareHandlerArgs) => {
      // If a square is already selected, try to make the move
      if (selectedSquare) {
        const moved = tryMove(selectedSquare, square);
        if (moved) {
          clearMoveHints();
          return;
        }
        // Illegal — fall through to (re)selection
      }

      if (!isSquare(square)) {
        clearMoveHints();
        return;
      }

      const baseFen = isInVariation && selectedVarNode
        ? selectedVarNode.fen
        : displayedFen;
      let piece;
      try {
        piece = new Chess(baseFen).get(square);
      } catch {
        clearMoveHints();
        return;
      }

      if (piece && piece.color === sideToMove) {
        if (getMoveOptions(square)) {
          setSelectedSquare(square);
          return;
        }
      }
      clearMoveHints();
    },
    [
      selectedSquare,
      tryMove,
      clearMoveHints,
      isInVariation,
      selectedVarNode,
      displayedFen,
      sideToMove,
      getMoveOptions,
    ],
  );

  const handleToggleEngineArrows = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const next = e.target.checked;
      if (!next) {
        stopSearch();
      }
      setShowEngineArrows(next);
    },
    [stopSearch],
  );

  // Click: open (or promote) the popup to persisted mode. Works from any state.
  const handleEngineLineClick = useCallback((index: number) => {
    clearGraceTimer();
    setSelectedEnginePlyIndex(0);
    setEnginePopup({ index, mode: "persist" });
    window.requestAnimationFrame(updateEnginePopupPosition);
  }, [clearGraceTimer, updateEnginePopupPosition]);

  // Hover a line: open/replace as a transient preview, but never downgrade a
  // persisted popup — that only changes via an explicit click or outside-click.
  const handleEngineLineHover = useCallback((index: number) => {
    clearGraceTimer();
    if (enginePopup?.mode === "persist") return;
    if (enginePopup?.index === index && enginePopup.mode === "hover") return;
    setSelectedEnginePlyIndex(0);
    setEnginePopup({ index, mode: "hover" });
    window.requestAnimationFrame(updateEnginePopupPosition);
  }, [clearGraceTimer, enginePopup, updateEnginePopupPosition]);

  // Mouse leaves a line button or the popup: close after a short grace window,
  // but only if still in hover mode. Persisted popups are never closed by leave.
  const handleEngineLineLeave = useCallback(() => {
    clearGraceTimer();
    graceTimerRef.current = window.setTimeout(() => {
      graceTimerRef.current = null;
      setEnginePopup((prev) => (prev?.mode === "hover" ? null : prev));
    }, 80);
  }, [clearGraceTimer]);

  // Any interaction inside the popup promotes a hover preview to persisted.
  const handlePopupInteract = useCallback(() => {
    clearGraceTimer();
    setEnginePopup((prev) => (prev && prev.mode === "hover" ? { ...prev, mode: "persist" } : prev));
  }, [clearGraceTimer]);

  const content = (
    <div className="analysis-board" ref={boardRootRef}>
      {isNarrow && mobileToolbar && (
        <div className="analysis-board__mobile-toolbar">
          <div className="analysis-board__material analysis-board__material--opponent">
            <MaterialDisplay
              fen={displayedFen}
              perspective={boardOrientation === "white" ? "black" : "white"}
            />
          </div>
          <div className="analysis-board__mobile-toolbar-content">
            {mobileToolbar}
          </div>
        </div>
      )}
      <div className="analysis-board__layout">
        <div className="analysis-board__board-col">
          <div className="analysis-board__board-with-eval">
            <EvalBar
              whitePerspectiveCp={
                isInVariation
                  ? (useLiveEngineEval ? liveEngineEvalCp : varEvalCp)
                  : (currentEvalCp ??
                    (effectiveIndex >= 0 ? evals[effectiveIndex] ?? null : null))
              }
              whitePerspectiveMate={
                isInVariation
                  ? (useLiveEngineEval ? liveEngineEvalMate : varEvalMate)
                  : currentEvalMate
              }
              whiteOnBottom={boardOrientation === "white"}
            />
            <div
              className={`analysis-board__board-frame${
                isInVariation ? " analysis-board__board-frame--variation" : ""
              }`}
              ref={boardFrameRef}
            >
              <Chessboard
                options={{
                  id: `${chessboardId}-main`,
                  position: displayedFen,
                  boardOrientation,
                  onPieceDrop: handleDrop,
                  onSquareClick: handleSquareClick,
                  allowDragging: true,
                  animationDurationInMs: 200,
                  squareStyles,
                  arrows: allArrows,
                  // Drive arrow opacity solely via each arrow's rgba alpha (best
                  // move reaches a true 1.0) instead of the 0.65 default global
                  // multiplier. arrowOptions is full-replace, so spread defaults.
                  arrowOptions: { ...defaultArrowOptions, opacity: 1 },
                  boardStyle: {
                    borderRadius: "0",
                    boxShadow: "0 20px 45px rgba(2, 6, 23, 0.5)",
                  },
                }}
              />
              {boardEvalIcon && (
                <div className="board-eval-icons" aria-hidden="true">
                  <div
                    className={`board-eval-icon move-icon--${boardEvalIcon.classification}`}
                    style={{ left: boardEvalIcon.left, top: boardEvalIcon.top }}
                    title={boardEvalIcon.title}
                  >
                    {boardEvalIcon.icon}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
        <div className="analysis-board__moves-col">
          <div className="analysis-board__engine-panel">
          <div className="analysis-board__engine-header">
            {showEngineArrows && activeEngineDepth > 0 && (
              <div
                className="analysis-board__engine-progress"
                role="progressbar"
                aria-label="Engine analysis depth"
                aria-valuemin={0}
                aria-valuemax={ENGINE_SEARCH_DEPTH}
                aria-valuenow={cappedEngineDepth}
              >
                <div
                  className={`analysis-board__engine-progress-fill${
                    engineThinking ? " analysis-board__engine-progress-fill--thinking" : ""
                  }`}
                  style={{ width: `${engineProgressPercent}%` }}
                />
              </div>
            )}
            <label className="analysis-board__toggle">
              <input
                type="checkbox"
                checked={showEngineArrows}
                onChange={handleToggleEngineArrows}
              />
              Engine lines
            </label>
            {showEngineArrows && activeEngineDepth > 0 && (
              <span className="analysis-board__engine-depth">
                d{activeEngineDepth}
              </span>
            )}
          </div>
          {showEngineLineList && (
            <div className="analysis-board__engine-lines">
              {[0, 1, 2].map((i) => {
                const line = engineLinesDisplay[i];
                return (
                  line ? (
                  <button
                    key={i}
                    ref={(node) => {
                      engineLineRefs.current[i] = node;
                    }}
                    type="button"
                    className="analysis-board__engine-line"
                    aria-label={`Show engine line ${i + 1}`}
                    aria-pressed={selectedEngineLineIndex === line.sourceSlot}
                    onClick={() => handleEngineLineClick(i)}
                    onMouseEnter={() => handleEngineLineHover(i)}
                    onMouseLeave={handleEngineLineLeave}
                    style={{
                      opacity: i === 0 ? 1 : 0.6,
                    }}
                  >
                    <span className="analysis-board__engine-eval">
                      {line.evalText || "+0.0"}
                    </span>{" "}
                    {line.source === "canonical" && (
                      <span
                        className="analysis-board__engine-source"
                        title="Canonical deep analysis"
                        aria-label="Canonical deep analysis"
                      >
                        C
                      </span>
                    )}
                    <span className="analysis-board__engine-pv">
                      {line.sanMoves[0]}
                    </span>
                  </button>
                  ) : (
                    <span
                      key={i}
                      className="analysis-board__engine-line analysis-board__engine-line--placeholder"
                      aria-hidden="true"
                    >
                      <span className="analysis-board__engine-eval">+0.0</span>{" "}
                      <span className="analysis-board__engine-pv">---</span>
                    </span>
                  )
                );
              })}
            </div>
          )}
          </div>
          {(!isNarrow || !mobileToolbar) && (
            <div className="analysis-board__material analysis-board__material--opponent">
              <MaterialDisplay
                fen={displayedFen}
                perspective={boardOrientation === "white" ? "black" : "white"}
              />
            </div>
          )}
          {(() => {
            const Component = isNarrow ? HorizontalMoveList : MoveList;
            return (
              <Component
                moves={mappedMoves}
                currentIndex={currentIndex}
                onNavigate={handleNavigate}
                playerColor={boardOrientation}
                variationTree={tree}
                selectedVarNodeId={selectedVarNodeId}
                onVarSelect={handleVarSelect}
                getAbsolutePly={getAbsolutePly}
                navigateUp={navigateUp}
                navigateDown={navigateDown}
                headerEvalOverride={varHeaderEval}
                suppressKeyboardNavigation={enginePopup?.mode === "persist"}
              />
            );
          })()}
          <div className="analysis-board__material analysis-board__material--player">
            <MaterialDisplay
              fen={displayedFen}
              perspective={boardOrientation}
            />
          </div>
        </div>
      </div>

      {selectedEngineLine && selectedPreviewFen && (
        <div
          ref={enginePopupRef}
          className="analysis-board__engine-popup"
          style={enginePopupPosition ?? undefined}
          role="dialog"
          aria-label="Engine line preview"
          onMouseEnter={clearGraceTimer}
          onMouseLeave={handleEngineLineLeave}
          onPointerDown={handlePopupInteract}
        >
          <div className="analysis-board__engine-popup-header">
            <span className="analysis-board__engine-popup-eval">
              {selectedEngineLine.evalText || "+0.0"}
            </span>
            {selectedEngineLine.source === "canonical" ? (
              <span className="analysis-board__engine-popup-depth">
                Canonical
              </span>
            ) : (
              selectedEngineLine.depth > 0 && (
                <span className="analysis-board__engine-popup-depth">
                  d{selectedEngineLine.depth}
                </span>
              )
            )}
          </div>
          <div className="analysis-board__engine-popup-board">
            <Chessboard
              options={{
                id: `${chessboardId}-preview`,
                position: selectedPreviewFen,
                boardOrientation,
                allowDragging: false,
                animationDurationInMs: 0,
                squareStyles: previewSquareStyles,
                boardStyle: {
                  borderRadius: "0",
                  boxShadow: "none",
                },
              }}
            />
          </div>
          <div className="analysis-board__engine-popup-pv" aria-label="Full engine line">
            {selectedEngineLine.sanMoves.map((san, index) => (
              <button
                key={`${san}-${index}`}
                type="button"
                className="analysis-board__engine-popup-move"
                aria-label={san}
                aria-current={index === selectedEnginePlyRenderIndex ? "step" : undefined}
                onClick={() => setSelectedEnginePlyIndex(index)}
              >
                {selectedEngineLine.movePrefixes[index] ? (
                  <span className="analysis-board__engine-popup-move-number" aria-hidden="true">
                    {selectedEngineLine.movePrefixes[index]}
                  </span>
                ) : null}
                {san}
              </button>
            ))}
          </div>
        </div>
      )}

      {(evals.length > 0 || variationLine) && (
        <div className="analysis-board__graph-row">
          <AnalysisGraph
            evals={evals}
            currentIndex={
              isInVariation && selectedVarNodeId
                ? getAbsolutePly(selectedVarNodeId)
                : currentIndex
            }
            onSelectMove={handleGraphSelect}
            playerColor={boardOrientation}
            highlightedMoves={highlightedMoves}
            evalCp={
              isInVariation
                ? varEvalCp
                : currentEvalCp ?? evals[effectiveIndex] ?? null
            }
            evalMate={isInVariation ? varEvalMate : currentEvalMate}
            isCheckmate={
              (!isInVariation && isCheckmateDisplayed) ||
              (isInVariation && varEvalMate === 0)
            }
            variationLine={variationLine}
          />
          {footer && (
            <div className="analysis-board__graph-footer">{footer}</div>
          )}
        </div>
      )}

    </div>
  );

  if (debugEnabled) {
    return (
      <Profiler id="AnalysisBoard" onRender={handleProfilerRender}>
        {content}
      </Profiler>
    );
  }

  return content;
});

export default memo(AnalysisBoard);
