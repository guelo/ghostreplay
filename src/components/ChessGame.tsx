import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { SetStateAction } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Chess } from "chess.js";
import type { Square } from "chess.js";
import type { PieceDropHandlerArgs } from "react-chessboard";
import { useStockfishEngine } from "../hooks/useStockfishEngine";
import { useChessGameLifecycle } from "../hooks/useChessGameLifecycle";
import { useChessGameController } from "../hooks/useChessGameController";
import type { PlayerMoveApplyResult } from "../hooks/useChessGameController";
import { useOpponentMove } from "../hooks/useOpponentMove";
import { useGameStore } from "../stores/useGameStore";
import { strictnessFromCp } from "./chess-game/ui/DrillSetupPanel";
import type { OpeningRootItem } from "../utils/api";
import { checkDrillRoute, failDrill, getOpeningRoots } from "../utils/api";
import {
  gameAnalysisStore,
  AnalysisStoreProvider,
} from "../stores/createAnalysisStore";
import { useGameAnalysisCoordinator } from "../contexts/GameAnalysisCoordinatorContext";
import type { OpeningLookupResult } from "../openings/openingBook";
import { lookupOpeningByFen } from "../openings/openingBook";
import type { TargetBlunderSrs } from "../utils/api";
import { getStatsAchievements } from "../utils/api";
import {
  buildBlunderAlert,
  deriveBlunderArrows,
  deriveLastMoveSquares,
  type BlunderAlert,
  type DrillFailInfo,
  type ReviewFailInfo,
} from "./chess-game/domain/movePresentation";
import { deriveDisplayedOpening } from "./chess-game/domain/opening";
import {
  derivePerfectStreak,
  type PreviousPerfectStreakState,
  type PerfectStreakEvent,
} from "./chess-game/domain/perfectStreak";
import { hasReviewTargetAtFen } from "./chess-game/domain/reviewState";
import {
  deriveGameStatusBadge,
  deriveStatusText,
} from "./chess-game/domain/status";
import {
  MAIA_BOT_NAMES,
  MAIA_ELO_BINS,
  STARTING_FEN,
} from "./chess-game/config";
import { eloStakes } from "./chess-game/elo";
import type { OpenHistoryOptions, ResolvedReview } from "./chess-game/types";
import BoardStage from "./chess-game/ui/BoardStage";
import GameInfoPanel, { GameWarningStack } from "./chess-game/ui/GameInfoPanel";
import PostGameBanner from "./chess-game/ui/PostGameBanner";
import DrillStopActions from "./chess-game/ui/DrillStopActions";
import MaterialDisplay from "./MaterialDisplay";
import type { MoveMessage, SrsFailDetail } from "./MoveList";
import {
  ConnectedEvalBar,
  ConnectedAnalysisGraph,
  ConnectedMoveList,
} from "./chess-game/AnalysisConnectors";
import AnalysisEffects from "./chess-game/AnalysisEffects";
import type { AnalysisResult } from "../hooks/useMoveAnalysis";

type ChessGameProps = {
  onOpenHistory?: (options: OpenHistoryOptions) => void;
};

const isSquare = (value: string): value is Square => /^[a-h][1-8]$/.test(value);

const STRICTNESS_TIER_THRESHOLDS = {
  strict: 15,
  standard: 35,
  lenient: 50,
} as const;

const resolveStrictnessCp = (
  strictnessCp: number | null,
  strictness: "strict" | "standard" | "lenient" | null,
) => strictnessCp ?? STRICTNESS_TIER_THRESHOLDS[strictness ?? "standard"];

type DrillRecovery =
  | { kind: "analysis"; result: Extract<PlayerMoveApplyResult, { applied: true }> }
  | { kind: "opponent"; fen: string; uciHistory: string[] };

const ChessGame = ({ onOpenHistory }: ChessGameProps = {}) => {
  // Reconstruct Chess from store state so it stays in sync after remounts.
  // liveFen is authoritative; moveHistory is replayed only when consistent
  // (to preserve PGN). Falls back to liveFen if history diverges.
  const chess = useMemo(() => {
    const { liveFen, moveHistory } = useGameStore.getState();
    const replayed = new Chess();
    let historyValid = true;
    for (const move of moveHistory) {
      try {
        if (!replayed.move(move.san)) {
          historyValid = false;
          break;
        }
      } catch {
        historyValid = false;
        break;
      }
    }
    if (historyValid && replayed.fen() === liveFen) {
      return replayed;
    }
    return new Chess(liveFen);
  }, []);

  // Singleton analysis store — persists across remounts like the game store.
  const analysisStore = gameAnalysisStore;
  const coordinator = useGameAnalysisCoordinator();

  // --- Cross-boundary state from zustand store ---
  const fen = useGameStore((s) => s.liveFen);
  const boardOrientation = useGameStore((s) => s.boardOrientation);
  const setBoardOrientation = useGameStore((s) => s.setBoardOrientation);
  const playerColor = useGameStore((s) => s.playerColor);
  const playerColorChoice = useGameStore((s) => s.playerColorChoice);
  const setPlayerColorChoice = useGameStore((s) => s.setPlayerColorChoice);
  const engineElo = useGameStore((s) => s.engineElo);
  const setEngineElo = useGameStore((s) => s.setEngineElo);
  const moveHistory = useGameStore((s) => s.moveHistory);
  const viewIndex = useGameStore((s) => s.viewIndex); // null = viewing live position
  const setViewIndex = useGameStore((s) => s.setViewIndex);
  const {
    status: engineStatus,
    isThinking,
    evaluatePosition,
    resetEngine,
  } = useStockfishEngine();

  // Imperative-only — ChessGame does NOT subscribe to analysis state.
  // Analysis is delegated to the coordinator which survives route navigation.
  const analyzeMove = useCallback(
    (fen: string, move: string, playerColor: 'white' | 'black', moveIndex?: number, legalMoveCount?: number) =>
      coordinator.analyzeMove(fen, move, playerColor, moveIndex, legalMoveCount),
    [coordinator],
  );

  const [engineMessage, setEngineMessage] = useState<string | null>(null);
  const sessionId = useGameStore((s) => s.sessionId);
  const isGameActive = useGameStore((s) => s.isGameActive);
  const [liveOpening, setLiveOpening] = useState<OpeningLookupResult | null>(
    null,
  );
  const gameResult = useGameStore((s) => s.gameResult);
  const [isStartingGame, setIsStartingGame] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [showStartOverlay, setShowStartOverlay] = useState(true);
  const [blunderAlert, setBlunderAlert] = useState<BlunderAlert | null>(null);
  const [showFlash, setShowFlash] = useState(false);
  const [blunderReviewId, setBlunderReviewId] = useState<number | null>(null);
  const [blunderReviewSrs, setBlunderReviewSrs] =
    useState<TargetBlunderSrs | null>(null);
  const [resolvedReview, setResolvedReview] = useState<ResolvedReview | null>(null);
  const [blunderTargetFen, setBlunderTargetFen] = useState<string | null>(null);
  const [showGhostInfo, setShowGhostInfo] = useState(false);
  const ghostInfoAnchorRef = useRef<HTMLSpanElement>(null);
  const [, setShowPassToast] = useState(false);
  const [showRehookToast, setShowRehookToast] = useState(false);
  const [reviewFailModal, setReviewFailModal] = useState<ReviewFailInfo | null>(
    null,
  );
  const [drillFailInfo, setDrillFailInfo] = useState<DrillFailInfo | null>(null);
  const [showPostGamePrompt, setShowPostGamePrompt] = useState(false);
  const [analysisMapSnapshot, setAnalysisMapSnapshot] = useState(
    () => analysisStore.getState().analysisMap,
  );
  const [perfectStreak, setPerfectStreak] = useState({
    current: 0,
    bestInHistory: 0,
    personalBest: 0,
  });
  const [streakToast, setStreakToast] = useState<PerfectStreakEvent | null>(
    null,
  );
  const perfectStreakPersonalBestRef = useRef(0);
  const perfectStreakRecordBaselineRef = useRef<number | null>(null);
  const [isPerfectStreakBaselineLoaded, setIsPerfectStreakBaselineLoaded] =
    useState(false);
  const previousPerfectStreakRef =
    useRef<PreviousPerfectStreakState | null>(null);
  const celebratedPerfectStreakKeysRef = useRef<Set<string>>(new Set());
  const isRated = useGameStore((s) => s.isRated);
  const isPracticeContinuation = useGameStore((s) => s.isPracticeContinuation);
  const [showRevertWarning, setShowRevertWarning] = useState(false);
  const [isRevertPendingState, setIsRevertPendingState] = useState(false);
  const [revertError, setRevertError] = useState<string | null>(null);
  const [showResignWarning, setShowResignWarning] = useState(false);
  const playerRating = useGameStore((s) => s.playerRating);
  const isProvisional = useGameStore((s) => s.isProvisional);
  const ratingScores = useGameStore((s) => s.ratingScores);
  const ratingDisplayType = useGameStore((s) => s.ratingDisplayType);
  const scoreChanges = useGameStore((s) => s.scoreChanges);
  const ratingChange = useGameStore((s) => s.ratingChange);
  const [pendingPromotion, setPendingPromotion] = useState<{ from: string; to: string } | null>(null);
  const [selectedSquare, setSelectedSquare] = useState<string | null>(null);
  const [optionSquares, setOptionSquares] = useState<
    Record<string, React.CSSProperties>
  >({});
  const [boardInstanceKey, setBoardInstanceKey] = useState(0);
  const isRevertPendingRef = useRef(isRevertPendingState);
  const isContinuingDrillRef = useRef(false);
  const setIsRevertPending = useCallback((update: SetStateAction<boolean>) => {
    const nextValue =
      typeof update === "function"
        ? (update as (prev: boolean) => boolean)(isRevertPendingRef.current)
        : update;
    isRevertPendingRef.current = nextValue;
    setIsRevertPendingState(nextValue);
  }, []);
  const isRevertPending = isRevertPendingState;

  // ---- Drill state --------------------------------------------------
  const location = useLocation();
  const navigate = useNavigate();
  const [isDrillMode, setIsDrillMode] = useState(false);
  const [, setIsContinuingDrill] = useState(false);
  const [selectedDrillOpening, setSelectedDrillOpening] = useState<OpeningRootItem | null>(null);
  const [drillStrictnessCp, setDrillStrictnessCp] = useState(25);
  const [openingFamilies, setOpeningFamilies] = useState<Array<{ family_name: string; roots: OpeningRootItem[] }> | null>(null);
  const [isLoadingOpenings, setIsLoadingOpenings] = useState(false);
  const [drillRecovery, setDrillRecovery] = useState<DrillRecovery | null>(null);
  const pendingDrillSetupRef = useRef<{ openingKey: string; playerColor: string } | null>(null);
  const drillOpeningKey = useGameStore((s) => s.drillOpeningKey);
  const drillState = useGameStore((s) => s.drillState);
  const isStoppedDrill = drillOpeningKey !== null && drillState === "failed";
  const drillTerminalReason = useGameStore((s) => s.drillTerminalReason);
  // -------------------------------------------------------------------

  // Blunder tracking: only record the first blunder per session
  const blunderRecordedRef = useRef(false);
  // Store context for the pending move analysis (FEN before move, PGN after move)
  const pendingAnalysisContextRef = useRef<{
    fen: string;
    pgn: string;
    moveSan: string;
    moveUci: string;
    moveIndex: number;
  } | null>(null);
  const pendingSrsReviewRef = useRef<Map<string, {
    sessionId: string;
    analysisId: string;
    blunderId: number;
    moveIndex: number;
    userMoveSan: string;
    srs: TargetBlunderSrs | null;
  }>>(new Map());
  const openingLookupRequestIdRef = useRef(0);
  // Index 0 = starting position (before any move), index N = after move N
  const openingHistoryRef = useRef<(OpeningLookupResult | null)[]>([]);
  const moveMessagesRef = useRef<Map<number, MoveMessage[]>>(new Map());
  const [moveMessagesVersion, setMoveMessagesVersion] = useState(0);
  const previousOpponentModeRef = useRef<"ghost" | "engine" | null>(null);
  const handleGameEndRef = useRef<() => Promise<void>>(async () => {});
  const blunderBoardTimerRefs = useRef<ReturnType<typeof setTimeout>[]>([]);
  const handleGameEndStable = useCallback(
    () => handleGameEndRef.current(),
    [],
  );

  const displayedFen = useMemo(() => {
    if (viewIndex === null) {
      return fen; // Live position
    }
    if (viewIndex === -1) {
      return STARTING_FEN; // Starting position
    }
    return moveHistory[viewIndex]?.fen ?? fen;
  }, [viewIndex, fen, moveHistory]);
  const displayedIndex = useMemo(() => {
    if (viewIndex === null) {
      return moveHistory.length - 1;
    }
    return viewIndex;
  }, [moveHistory.length, viewIndex]);
  const displayedIndexRef = useRef(displayedIndex);
  displayedIndexRef.current = displayedIndex;
  const isBlunderBoardOverrideActive = blunderAlert !== null || drillFailInfo !== null;

  const clearBlunderBoardOverride = useCallback(() => {
    for (const timer of blunderBoardTimerRefs.current) {
      clearTimeout(timer);
    }
    blunderBoardTimerRefs.current = [];
  }, []);

  const lastMoveSquares = useMemo((): Record<string, React.CSSProperties> => {
    return deriveLastMoveSquares(moveHistory, viewIndex);
  }, [moveHistory, viewIndex]);

  // Compute arrows from review fail modal or blunder alert
  const blunderArrows = useMemo(() => {
    return deriveBlunderArrows(reviewFailModal, blunderAlert, drillFailInfo);
  }, [reviewFailModal, blunderAlert, drillFailInfo]);

  // Opening label that tracks with move navigation
  const displayedOpening = useMemo(() => {
    return deriveDisplayedOpening(openingHistoryRef.current, viewIndex);
  }, [viewIndex, liveOpening]); // liveOpening dependency triggers recalc when history updates

  // Whether the user can make moves (must be viewing live position)
  const isViewingLive = viewIndex === null;

  const isPlayerMoveIndex = useCallback(
    (index: number) => {
      if (index < 0) return false;
      const isWhiteMove = index % 2 === 0;
      return playerColor === "white" ? isWhiteMove : !isWhiteMove;
    },
    [playerColor],
  );

  const clearMoveHighlights = useCallback(() => {
    setSelectedSquare(null);
    setOptionSquares({});
  }, []);

  const handleNavigate = useCallback(
    (index: number | null) => {
      if (isRevertPendingRef.current) {
        return;
      }
      setViewIndex(index);
      setReviewFailModal(null);
      if (pendingPromotion) {
        setPendingPromotion(null);
        clearMoveHighlights();
      }

      // Re-show blunder alert when clicking on a player's blunder move
      if (index !== null && index >= 0) {
        const { analysisMap } = analysisStore.getState();
        const history = useGameStore.getState().moveHistory;
        const analysis = analysisMap.get(index);
        if (
          analysis?.blunder &&
          analysis.delta !== null &&
          isPlayerMoveIndex(index)
        ) {
          const moveSan = history[index]?.san ?? analysis.move;
          setBlunderAlert(
            buildBlunderAlert({
              moveHistory: history,
              moveIndex: index,
              moveSan,
              moveUci: analysis.move,
              bestMoveUci: analysis.bestMove,
              delta: analysis.delta,
            }),
          );
          return;
        }
      }

      // Clear blunder alert when navigating to a non-blunder move
      clearBlunderBoardOverride();
      setBlunderAlert(null);
      setDrillFailInfo(null);
    },
    [analysisStore, clearBlunderBoardOverride, clearMoveHighlights, isPlayerMoveIndex, pendingPromotion],
  );

  const getMoveOptions = useCallback(
    (square: string): boolean => {
      if (!isSquare(square)) {
        return false;
      }

      const moves = chess.moves({ square, verbose: true });
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
    [chess],
  );

  const isPlayersTurn = chess.turn() === (playerColor === "white" ? "w" : "b");
  const moveCount = moveHistory.length;
  const isReviewMomentActive =
    hasReviewTargetAtFen(blunderReviewId, blunderTargetFen, fen) &&
    isGameActive &&
    isPlayersTurn &&
    isViewingLive &&
    !chess.isGameOver();
  const blocksStreakToast =
    (showStartOverlay && (!isGameActive || isStoppedDrill)) ||
    showRevertWarning ||
    showResignWarning ||
    pendingPromotion !== null ||
    blunderAlert !== null ||
    isReviewMomentActive ||
    resolvedReview !== null ||
    showRehookToast;

  const statusText = deriveStatusText(chess);

  const appendMoveMessage = useCallback(
    (moveIndex: number, msg: MoveMessage) => {
      const map = moveMessagesRef.current;
      const existing = map.get(moveIndex);
      if (existing) {
        existing.push(msg);
      } else {
        map.set(moveIndex, [msg]);
      }
      setMoveMessagesVersion((v) => v + 1);
    },
    [],
  );

  // Build a stable snapshot for passing to ConnectedMoveList.
  // Preserves per-index array references when that index's messages haven't changed,
  // so MoveRow memoization can skip unchanged rows.
  // Invariant: appendMoveMessage only pushes (never edits in place at constant length).
  // A future "replace message" path would need to invalidate differently.
  const prevMoveMessagesSnapshotRef = useRef<ReadonlyMap<number, MoveMessage[]>>(new Map());
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const moveMessages = useMemo(() => {
    void moveMessagesVersion; // depend on version counter
    const prev = prevMoveMessagesSnapshotRef.current;
    const next = new Map<number, MoveMessage[]>();
    for (const [moveIndex, messages] of moveMessagesRef.current) {
      const prevArr = prev.get(moveIndex);
      if (prevArr && prevArr.length === messages.length) {
        // No new messages appended — reuse previous array reference
        next.set(moveIndex, prevArr);
      } else {
        next.set(moveIndex, [...messages]);
      }
    }
    const result = next as ReadonlyMap<number, MoveMessage[]>;
    prevMoveMessagesSnapshotRef.current = result;
    return result;
  }, [moveMessagesVersion]);

  const opponentColor = playerColor === "white" ? "black" : "white";

  const { applyPlayerMove, handleDrop, applyEngineMove, applyGhostMove } =
    useChessGameController({
      chess,
      blunderReviewId,
      blunderReviewSrs,
      blunderTargetFen,
      pendingAnalysisContextRef,
      pendingSrsReviewRef,
      setEngineMessage,
      setBlunderAlert,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setShowGhostInfo,
      resolvedReview,
      setResolvedReview,
      analyzeMove,
      evaluatePosition,
      handleGameEnd: handleGameEndStable,
      clearMoveHighlights,
      clearBlunderBoardOverride,
    });

  const { opponentMode, applyOpponentMove, resetMode } = useOpponentMove({
    sessionId,
    canApplyResult: (requestSessionId) => {
      const store = useGameStore.getState();
      return (
        store.isGameActive &&
        store.sessionId === requestSessionId &&
        store.drillState !== "failed" &&
        !isRevertPendingRef.current
      );
    },
    onApplyBackendMove: async (...args) => {
      if (useGameStore.getState().isGameActive && !isRevertPendingRef.current) {
        await applyGhostMove(...args);
      }
    },
    onApplyLocalFallback: async () => {
      if (useGameStore.getState().isGameActive && !isRevertPendingRef.current) {
        await applyEngineMove();
      }
    },
    shouldUseLocalFallback: () => {
      const store = useGameStore.getState();
      return !(store.drillOpeningKey && (store.drillState === "active" || store.drillState === "root_reached"));
    },
    onBackendFailure: async () => {
      const store = useGameStore.getState();
      if (store.drillOpeningKey && store.drillState === "root_reached") {
        setDrillRecovery({
          kind: "opponent",
          fen: chess.fen(),
          uciHistory: store.moveHistory.map((move) => move.uci),
        });
        setEngineMessage("Opponent move is unavailable. Try again or abandon the drill.");
        return;
      }
      setEngineMessage("Drill steering is unavailable. Try again or abandon the drill.");
    },
  });

  const checkPostPlayerDrillRoute = useCallback(
    async (result: Extract<PlayerMoveApplyResult, { applied: true }>) => {
      const store = useGameStore.getState();
      if (!store.sessionId || !store.drillOpeningKey || store.drillState !== "active") {
        return true;
      }

      try {
        const route = await checkDrillRoute(store.sessionId, {
          current_fen: result.fenAfter,
          previous_fen: result.fenBefore,
          played_uci: result.moveUci,
        });
        if (route.status === "failed") {
          setDrillRecovery(null);
          const reason = route.failure?.reason ?? null;
          useGameStore.getState().setDrillState("failed");
          useGameStore.getState().setDrillTerminalReason(reason);
          setDrillFailInfo({
            playedMoveUci: route.failure?.played_move_uci ?? result.moveUci,
            suggestionUcis: route.suggestions.map((suggestion) => suggestion.uci),
            correctionFen: route.failure?.correction_fen ?? result.fenBefore,
            moveIndex: result.moveIndex,
          });
          setEngineMessage(
            route.failure?.reason === "accuracy"
              ? "Bad move"
              : "That's not how you get to the opening.",
          );
          setViewIndex(result.moveIndex - 1);
          return false;
        }
        if (route.status === "root_reached") {
          setDrillFailInfo(null);
          setDrillRecovery(null);
          useGameStore.getState().setDrillState("root_reached");
          setEngineMessage("Opening root reached. Drill is live.");
          return true;
        }
        setDrillFailInfo(null);
        setDrillRecovery(null);
        return true;
      } catch (error) {
        const message =
          error instanceof Error ? error.message : "Failed to check drill route.";
        setEngineMessage(message);
        return false;
      }
    },
    [setEngineMessage, setViewIndex],
  );

  const {
    handleGameEnd,
    executeRevert,
    handleRevertClick,
    cancelRevert,
    handleNewGame,
    handleNewDrill,
    handleResignClick,
    executeResign,
    cancelResign,
    handleReset,
    handleShowStartOverlay,
    handleViewAnalysis,
    handleViewHistory,
    handleContinueDrill: convertRootReachedDrill,
  } = useChessGameLifecycle({
    chess,
    coordinator,
    openingHistoryRef,
    blunderRecordedRef,
    pendingAnalysisContextRef,
    pendingSrsReviewRef,
    clearMoveHighlights,
    resetMode,
    resetEngine,
    onOpenHistory,
    setEngineMessage,
    setIsStartingGame,
    setStartError,
    setShowStartOverlay,
    setLiveOpening,
    setBlunderAlert,
    setShowFlash,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setBlunderTargetFen,
    setShowPassToast,
    setShowRehookToast,
    setReviewFailModal,
    setShowPostGamePrompt,
    setIsRevertPending,
    setRevertError,
    showRevertWarning,
    setShowRevertWarning,
    setShowResignWarning,
    setResolvedReview,
    setPendingPromotion,
    clearBlunderBoardOverride,
  });

  const handleContinueDrill = useCallback(async () => {
    if (isContinuingDrillRef.current) {
      return;
    }
    isContinuingDrillRef.current = true;
    setIsContinuingDrill(true);
    try {
      const contract = await convertRootReachedDrill();
      if (!contract || contract.drill_state !== "converted") {
        return;
      }
      setDrillFailInfo(null);
      setDrillRecovery(null);
      useGameStore.getState().setViewIndex(null);

      const store = useGameStore.getState();
      if (
        !store.isGameActive ||
        store.viewIndex !== null ||
        isRevertPendingRef.current
      ) {
        return;
      }

      const turnColor = chess.turn() === "w" ? "white" : "black";
      if (turnColor !== opponentColor) {
        return;
      }

      await applyOpponentMove(
        chess.fen(),
        store.moveHistory.map((move) => move.uci),
      );
    } finally {
      isContinuingDrillRef.current = false;
      setIsContinuingDrill(false);
    }
  }, [applyOpponentMove, chess, convertRootReachedDrill, opponentColor]);

  useEffect(() => {
    handleGameEndRef.current = handleGameEnd;
  }, [handleGameEnd]);

  useEffect(() => {
    const unsubscribe = analysisStore.subscribe((state, previous) => {
      if (state.analysisMap !== previous.analysisMap) {
        setAnalysisMapSnapshot(state.analysisMap);
      }
    });
    return unsubscribe;
  }, [analysisStore]);

  useEffect(() => {
    let cancelled = false;
    void getStatsAchievements()
      .then((achievements) => {
        if (cancelled) return;
        const nextBest = achievements.perfect_streak.personal_best;
        perfectStreakRecordBaselineRef.current = nextBest;
        perfectStreakPersonalBestRef.current = Math.max(
          perfectStreakPersonalBestRef.current,
          nextBest,
        );
        setPerfectStreak((current) => ({
          ...current,
          personalBest: Math.max(current.personalBest, nextBest),
        }));
        setIsPerfectStreakBaselineLoaded(true);
      })
      .catch(() => {
        // The live streak remains useful even if the all-time best is unavailable.
      });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (moveHistory.length === 0) {
      previousPerfectStreakRef.current = null;
      celebratedPerfectStreakKeysRef.current = new Set();
      setStreakToast(null);
      setPerfectStreak((current) => ({
        current: 0,
        bestInHistory: 0,
        personalBest: Math.max(
          current.personalBest,
          perfectStreakPersonalBestRef.current,
        ),
      }));
      return;
    }

    const result = derivePerfectStreak({
      moveHistory,
      analysisMap: analysisMapSnapshot,
      playerColor,
      previousPersonalBest: perfectStreakPersonalBestRef.current,
      recordPersonalBest: isPerfectStreakBaselineLoaded
        ? perfectStreakRecordBaselineRef.current
        : null,
      previousState: previousPerfectStreakRef.current,
      celebratedEventKeys: celebratedPerfectStreakKeysRef.current,
    });

    previousPerfectStreakRef.current = {
      current: result.current,
      bestInHistory: result.bestInHistory,
      personalBest: result.personalBest,
      recordPersonalBest: isPerfectStreakBaselineLoaded
        ? perfectStreakRecordBaselineRef.current
        : null,
    };
    perfectStreakPersonalBestRef.current = result.personalBest;
    setPerfectStreak((current) =>
      current.current === result.current &&
      current.bestInHistory === result.bestInHistory &&
      current.personalBest === result.personalBest
        ? current
        : {
            current: result.current,
            bestInHistory: result.bestInHistory,
            personalBest: result.personalBest,
          },
    );

    if (result.event) {
      celebratedPerfectStreakKeysRef.current.add(result.event.key);
      if (
        !blocksStreakToast &&
        (result.event.type === "record" || result.event.streak >= 5)
      ) {
        setStreakToast(result.event);
      }
    }
  }, [
    analysisMapSnapshot,
    blocksStreakToast,
    isPerfectStreakBaselineLoaded,
    moveHistory,
    playerColor,
  ]);

  // Sync coordinator with existing active session on mount (e.g., after refresh)
  useEffect(() => {
    const { sessionId: sid, isGameActive: active } = useGameStore.getState();
    if (sid && active && coordinator.sessionId !== sid) {
      coordinator.startSession(sid);
    }
  }, [coordinator]);

  // ---- Drill effects ------------------------------------------------
  // Intercept location.state from /openings navigation
  useEffect(() => {
    const drillSetup = (location.state as { drillSetup?: { openingKey: string; playerColor: string } } | null)?.drillSetup;
    if (!drillSetup) return;

    setIsDrillMode(true);
    pendingDrillSetupRef.current = drillSetup;
    setShowStartOverlay(true);

    navigate(location.pathname, { replace: true, state: null });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.state, navigate, location.pathname]);

  // Fetch opening roots when drill mode active and overlay shown
  useEffect(() => {
    if (!showStartOverlay || !isDrillMode) return;
    let cancelled = false;
    setIsLoadingOpenings(true);
    getOpeningRoots()
      .then((data) => {
        if (cancelled) return;
        setOpeningFamilies(data.families);
      })
      .catch(() => {
        if (cancelled) return;
        setOpeningFamilies(null);
      })
      .finally(() => {
        if (cancelled) return;
        setIsLoadingOpenings(false);
      });
    return () => { cancelled = true; };
  }, [showStartOverlay, isDrillMode]);

  // Load sticky drill prefs when overlay opens
  useEffect(() => {
    if (!showStartOverlay) return;
    try {
      const raw = localStorage.getItem("ghostreplay_drill_prefs");
      if (!raw) return;
      const prefs = JSON.parse(raw);
      if (typeof prefs.strictnessCp === "number") {
        setDrillStrictnessCp(prefs.strictnessCp);
      }
      if (typeof prefs.engineElo === "number") {
        setEngineElo(prefs.engineElo);
      }
      if (prefs.playerColor === "white" || prefs.playerColor === "black" || prefs.playerColor === "random") {
        setPlayerColorChoice(prefs.playerColor);
      }
      if (prefs.openingKey && !pendingDrillSetupRef.current) {
        pendingDrillSetupRef.current = {
          openingKey: prefs.openingKey,
          playerColor: prefs.playerColor ?? "random",
        };
      }
    } catch {
      // ignore corrupted storage
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showStartOverlay]);

  // Match pending drill setup after openingFamilies loads
  useEffect(() => {
    if (!openingFamilies || !pendingDrillSetupRef.current) return;
    const opening = openingFamilies
      .flatMap((f) => f.roots)
      .find((r) => r.opening_key === pendingDrillSetupRef.current?.openingKey);
    if (opening) {
      setSelectedDrillOpening(opening);
    }
    const color = pendingDrillSetupRef.current.playerColor;
    if (color === "white" || color === "black" || color === "random") {
      setPlayerColorChoice(color);
    }
    pendingDrillSetupRef.current = null;
  }, [openingFamilies]);
  // -------------------------------------------------------------------

  const isPostRootMoveStillCurrent = useCallback(
    (
      capturedSessionId: string,
      result: Extract<PlayerMoveApplyResult, { applied: true }>,
    ) => {
      const store = useGameStore.getState();
      return (
        store.sessionId === capturedSessionId &&
        store.moveHistory[result.moveIndex]?.uci === result.moveUci &&
        store.drillState === "root_reached" &&
        !isRevertPendingRef.current
      );
    },
    [],
  );

  const stopPostRootDrillForAccuracy = useCallback(
    async (
      sessionId: string,
      result: Extract<PlayerMoveApplyResult, { applied: true }>,
      analysis: AnalysisResult,
    ) => {
      await failDrill(sessionId, "accuracy");
      if (!isPostRootMoveStillCurrent(sessionId, result)) {
        return;
      }
      const store = useGameStore.getState();
      store.setDrillState("failed");
      store.setDrillTerminalReason("accuracy");
      setDrillFailInfo({
        playedMoveUci: result.moveUci,
        suggestionUcis: analysis.bestMove ? [analysis.bestMove] : [],
        correctionFen: result.fenBefore,
        moveIndex: result.moveIndex,
      });
      setEngineMessage("That move exceeds the allowed centipawn loss.");
      setViewIndex(result.moveIndex - 1);
      setDrillRecovery(null);
    },
    [isPostRootMoveStillCurrent, setEngineMessage, setViewIndex],
  );

  const continueAfterPlayerMove = useCallback(
    async (result: Extract<PlayerMoveApplyResult, { applied: true }>) => {
      const captured = useGameStore.getState();
      const capturedSessionId = captured.sessionId;
      const capturedDrillState = captured.drillState;

      if (!capturedSessionId) {
        if (result.gameOver) {
          await handleGameEnd();
        } else if (!isRevertPendingRef.current) {
          await applyOpponentMove(result.fenAfter, result.uciHistory);
        }
        return;
      }

      if (captured.drillOpeningKey && capturedDrillState === "root_reached") {
        let analysis: AnalysisResult;
        try {
          analysis = await coordinator.waitForAnalysis(result.moveIndex);
        } catch (error) {
          if (!isPostRootMoveStillCurrent(capturedSessionId, result)) {
            return;
          }
          setDrillRecovery({ kind: "analysis", result });
          const message =
            error instanceof Error ? error.message : "Move analysis is unavailable.";
          setEngineMessage(`${message}. Try again or abandon the drill.`);
          return;
        }

        if (!isPostRootMoveStillCurrent(capturedSessionId, result)) {
          return;
        }

        const threshold = resolveStrictnessCp(
          useGameStore.getState().drillStrictnessCp,
          useGameStore.getState().drillStrictness,
        );
        if (analysis.delta !== null && analysis.delta > threshold) {
          try {
            await stopPostRootDrillForAccuracy(capturedSessionId, result, analysis);
          } catch (error) {
            if (!isPostRootMoveStillCurrent(capturedSessionId, result)) {
              return;
            }
            setDrillRecovery({ kind: "analysis", result });
            const message =
              error instanceof Error ? error.message : "Failed to record drill failure.";
            setEngineMessage(`${message}. Try again or abandon the drill.`);
          }
          return;
        }
      } else if (captured.drillOpeningKey && capturedDrillState === "active") {
        const canContinue = await checkPostPlayerDrillRoute(result);
        if (!canContinue) {
          return;
        }
      }

      if (result.gameOver) {
        await handleGameEnd();
      } else if (!isRevertPendingRef.current) {
        setDrillRecovery(null);
        await applyOpponentMove(result.fenAfter, result.uciHistory);
      }
    },
    [
      applyOpponentMove,
      checkPostPlayerDrillRoute,
      coordinator,
      handleGameEnd,
      isPostRootMoveStillCurrent,
      stopPostRootDrillForAccuracy,
      setEngineMessage,
    ],
  );

  const applyPlayerMoveAndAdvance = useCallback(
    (sourceSquare: string, targetSquare: string, promotion?: string): boolean => {
      const result = applyPlayerMove(sourceSquare, targetSquare, promotion);
      if (!result.applied) {
        if (result.requiresPromotion) {
          setPendingPromotion({ from: sourceSquare, to: targetSquare });
          return true; // consume the click
        }
        return false;
      }

      if (!isRevertPending) {
        void continueAfterPlayerMove(result);
      }

      return true;
    },
    [applyPlayerMove, continueAfterPlayerMove, isRevertPending],
  );

  // Clear move messages when a new game starts
  useEffect(() => {
    if (moveHistory.length === 0) {
      moveMessagesRef.current = new Map();
      setMoveMessagesVersion((v) => v + 1);
      setDrillFailInfo(null);
    }
  }, [moveHistory.length]);

  useEffect(() => {
    if (!isGameActive) {
      previousOpponentModeRef.current = null;
      setShowRehookToast(false);
      return;
    }

    const previousMode = previousOpponentModeRef.current;
    if (previousMode === "engine" && opponentMode === "ghost") {
      setShowRehookToast(true);
    }
    previousOpponentModeRef.current = opponentMode;
  }, [isGameActive, opponentMode]);

  const handleSquareClick = useCallback(
    ({ square }: { square: string }) => {
      if (pendingPromotion) {
        return; // picker is open, ignore board clicks (backdrop handles cancel)
      }

      if (isRevertPending || isBlunderBoardOverrideActive) {
        clearMoveHighlights();
        return;
      }

      const playersTurn =
        chess.turn() === (playerColor === "white" ? "w" : "b");
      if (!isGameActive || !playersTurn || !isViewingLive) {
        clearMoveHighlights();
        return;
      }

      // If a square is already selected, try to make a move to the clicked square
      if (selectedSquare) {
        const result = applyPlayerMoveAndAdvance(selectedSquare, square);
        if (result) {
          return;
        }

        // Move was illegal — fall through to try selecting the new square
      }

      // Try to select a new piece
      if (!isSquare(square)) {
        clearMoveHighlights();
        return;
      }

      const piece = chess.get(square);
      const playerSide = playerColor === "white" ? "w" : "b";
      if (piece && piece.color === playerSide) {
        setSelectedSquare(square);
        getMoveOptions(square);
      } else {
        clearMoveHighlights();
      }
    },
    [
      chess,
      isGameActive,
      isBlunderBoardOverrideActive,
      isRevertPending,
      isViewingLive,
      pendingPromotion,
      selectedSquare,
      playerColor,
      applyPlayerMoveAndAdvance,
      clearMoveHighlights,
      getMoveOptions,
    ],
  );

  useEffect(() => {
    if (!isGameActive) {
      openingLookupRequestIdRef.current += 1;
      setLiveOpening(null);
      return;
    }

    // Index 0 = starting position, index N = after move N
    const historyIdx = moveHistory.length;
    const requestId = openingLookupRequestIdRef.current + 1;
    openingLookupRequestIdRef.current = requestId;
    void lookupOpeningByFen(fen)
      .then((opening) => {
        if (openingLookupRequestIdRef.current !== requestId) {
          return;
        }
        const history = openingHistoryRef.current;
        if (opening) {
          history[historyIdx] = opening;
        } else {
          // Carry forward last known opening
          let lastKnown: OpeningLookupResult | null = null;
          for (let i = historyIdx - 1; i >= 0; i--) {
            if (history[i]) {
              lastKnown = history[i];
              break;
            }
          }
          history[historyIdx] = lastKnown;
        }
        setLiveOpening(history[historyIdx] ?? null);
      })
      .catch(() => {
        if (openingLookupRequestIdRef.current !== requestId) {
          return;
        }
      });
  }, [fen, isGameActive, moveHistory.length]);

  useEffect(() => {
    if (!isGameActive) {
      return;
    }

    if (isRevertPending) {
      return;
    }

    if (playerColor !== "black") {
      return;
    }

    if (moveCount > 0 || chess.turn() !== "w") {
      return;
    }

    if (engineStatus !== "ready" || isThinking || !isViewingLive) {
      return;
    }

    void applyOpponentMove(
      chess.fen(),
      useGameStore.getState().moveHistory.map((m) => m.uci),
    );
  }, [
    applyOpponentMove,
    chess,
    engineStatus,
    isGameActive,
    isRevertPending,
    isThinking,
    isViewingLive,
    moveCount,
    playerColor,
  ]);

  // Auto-dismiss flash after animation
  useEffect(() => {
    if (!showFlash) return;
    const timer = setTimeout(() => setShowFlash(false), 400);
    return () => clearTimeout(timer);
  }, [showFlash]);

  useEffect(() => {
    if (!blunderAlert) {
      clearBlunderBoardOverride();
      return;
    }

    if (!blunderAlert.shouldRewind) {
      clearBlunderBoardOverride();
      return;
    }

    clearMoveHighlights();
    for (const timer of blunderBoardTimerRefs.current) {
      clearTimeout(timer);
    }
    blunderBoardTimerRefs.current = [];

    setBoardInstanceKey((current) => current + 1);
    const startIndex = displayedIndexRef.current;
    const targetDisplayIndex = blunderAlert.moveIndex - 1;
    setViewIndex(startIndex);

    if (startIndex <= targetDisplayIndex) {
      const timer = setTimeout(() => {
        setViewIndex(targetDisplayIndex);
      }, 125);
      blunderBoardTimerRefs.current.push(timer);
      return () => {
        clearBlunderBoardOverride();
      };
    }

    for (let index = startIndex - 1, step = 0; index >= targetDisplayIndex; index -= 1, step += 1) {
      const timer = setTimeout(() => {
        setViewIndex(index);
      }, 125 + step * 240);
      blunderBoardTimerRefs.current.push(timer);
    }

    return () => {
      clearBlunderBoardOverride();
    };
  }, [
    blunderAlert,
    clearBlunderBoardOverride,
    clearMoveHighlights,
    setViewIndex,
  ]);

  // Auto-dismiss re-hook toast after 3 seconds
  useEffect(() => {
    if (!showRehookToast) return;
    const timer = setTimeout(() => setShowRehookToast(false), 3000);
    return () => clearTimeout(timer);
  }, [showRehookToast]);

  useEffect(() => {
    if (!streakToast) return;
    const timer = setTimeout(() => setStreakToast(null), 2400);
    return () => clearTimeout(timer);
  }, [streakToast]);

  useEffect(() => {
    if (blocksStreakToast) {
      setStreakToast(null);
    }
  }, [blocksStreakToast]);

  // Close ghost info popover on click outside
  useEffect(() => {
    if (!showGhostInfo) return;
    const handler = (e: MouseEvent) => {
      if (ghostInfoAnchorRef.current && !ghostInfoAnchorRef.current.contains(e.target as Node)) {
        setShowGhostInfo(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showGhostInfo]);

  const handleDropPiece = useCallback(
    ({
      sourceSquare,
      targetSquare,
    }: PieceDropHandlerArgs) => {
      if (isRevertPending || isBlunderBoardOverrideActive || !targetSquare) {
        return false;
      }

      const result = handleDrop(sourceSquare, targetSquare);
      if (!result.applied) {
        if (result.requiresPromotion) {
          setPendingPromotion({ from: sourceSquare, to: targetSquare });
        }
        return false; // piece snaps back in both cases
      }

      if (!isRevertPending) {
        void continueAfterPlayerMove(result);
      }

      return true;
    },
    [continueAfterPlayerMove, handleDrop, isBlunderBoardOverrideActive, isRevertPending],
  );

  const handlePromotionPick = useCallback(
    (piece: 'q' | 'r' | 'b' | 'n') => {
      if (!pendingPromotion) return;
      const store = useGameStore.getState();
      const isLive = store.viewIndex === null;
      const isCorrectTurn = chess.turn() === (store.playerColor === 'white' ? 'w' : 'b');
      if (!store.isGameActive || isRevertPending || !isLive || !isCorrectTurn) {
        setPendingPromotion(null);
        return;
      }
      const { from, to } = pendingPromotion;
      setPendingPromotion(null);
      applyPlayerMoveAndAdvance(from, to, piece);
    },
    [pendingPromotion, chess, applyPlayerMoveAndAdvance, isRevertPending],
  );

  const handlePromotionCancel = useCallback(() => {
    setPendingPromotion(null);
    clearMoveHighlights();
  }, [clearMoveHighlights]);

  const handleRevealSrsFail = useCallback(
    (detail: SrsFailDetail, moveIndex: number) => {
      if (isRevertPendingRef.current) {
        return;
      }
      clearBlunderBoardOverride();
      setBlunderAlert(null);
      setReviewFailModal({
        userMoveSan: detail.userMoveSan,
        bestMoveSan: detail.bestMoveSan,
        userMoveUci: detail.userMoveUci,
        bestMoveUci: detail.bestMoveUci,
        evalLoss: 0,
        moveIndex,
      });
      setViewIndex(moveIndex - 1);
    },
    [clearBlunderBoardOverride],
  );

  const flipBoard = () => {
    setBoardOrientation((current) => (current === "white" ? "black" : "white"));
  };

  const gameStatusBadge = deriveGameStatusBadge(isGameActive, gameResult);
  const { winDelta, lossDelta } = eloStakes(
    playerRating,
    engineElo,
    isProvisional,
  );
  const squareStyles = useMemo(
    () => ({ ...lastMoveSquares, ...optionSquares }),
    [lastMoveSquares, optionSquares],
  );

  const handleCloseStartOverlay = useCallback(
    () => setShowStartOverlay(false),
    [],
  );

  const handleStartDrill = useCallback(async () => {
    if (!selectedDrillOpening) return;
    const effectiveChoice = playerColorChoice ?? "random";
    const resolvedPlayerColor =
      effectiveChoice === "random"
        ? Math.random() < 0.5
          ? "white"
          : "black"
        : effectiveChoice;

    const result = await handleNewDrill({
      openingKey: selectedDrillOpening.opening_key,
      playerColor: resolvedPlayerColor,
      engineElo: engineElo,
      strictness: strictnessFromCp(drillStrictnessCp),
      strictnessCp: drillStrictnessCp,
      selectedOpening: selectedDrillOpening,
    });

    if (result && chess.turn() !== (resolvedPlayerColor === "white" ? "w" : "b")) {
      void applyOpponentMove(result.fen, result.uciHistory);
    }
  }, [
    selectedDrillOpening,
    playerColorChoice,
    engineElo,
    drillStrictnessCp,
    handleNewDrill,
    chess,
    applyOpponentMove,
  ]);

  const handleNewDrillSticky = useCallback(() => {
    // Intentionally do NOT setIsGameActive(false) here:
    // - For natural-end during drill, finishLocalGame has already cleared it.
    // - For mid-board route-check failed, we want isGameActive=true so that
    //   handleNewDrill's guard runs abandonDrill on the previous session.
    useGameStore.getState().setGameResult(null);

    try {
      const raw = localStorage.getItem("ghostreplay_drill_prefs");
      const prefs = raw ? JSON.parse(raw) : {};
      if (typeof prefs.strictnessCp === "number") {
        setDrillStrictnessCp(prefs.strictnessCp);
      }
      if (typeof prefs.engineElo === "number") {
        setEngineElo(prefs.engineElo);
      }
      if (prefs.playerColor === "white" || prefs.playerColor === "black" || prefs.playerColor === "random") {
        setPlayerColorChoice(prefs.playerColor);
      }
      if (prefs.openingKey) {
        pendingDrillSetupRef.current = {
          openingKey: prefs.openingKey,
          playerColor: prefs.playerColor ?? "random",
        };
      }
    } catch {
      // ignore corrupted storage
    }

    setIsDrillMode(true);
    setShowPostGamePrompt(false);
    setShowStartOverlay(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleSwitchToDrillMode = useCallback(() => setIsDrillMode(true), []);
  const handleSwitchToPlayMode = useCallback(() => setIsDrillMode(false), []);

  const handleEngineEloChange = useCallback(
    (elo: number) => setEngineElo(elo as (typeof MAIA_ELO_BINS)[number]),
    [],
  );
  const handlePlayWhite = useCallback(
    () => void handleNewGame("white"),
    [handleNewGame],
  );
  const handlePlayRandom = useCallback(
    () => void handleNewGame("random"),
    [handleNewGame],
  );
  const handlePlayBlack = useCallback(
    () => void handleNewGame("black"),
    [handleNewGame],
  );
  const handleToggleGhostInfo = useCallback(
    () => setShowGhostInfo((v) => !v),
    [],
  );
  const handleCloseGhostInfo = useCallback(
    () => setShowGhostInfo(false),
    [],
  );
  const handleDismissRehookToast = useCallback(
    () => setShowRehookToast(false),
    [],
  );
  const canRetryDrillSteering =
    Boolean(engineMessage) &&
    drillOpeningKey !== null &&
    (drillState === "active" || (drillState === "root_reached" && drillRecovery !== null)) &&
    isGameActive &&
    !isPlayersTurn &&
    !isRevertPending &&
    isViewingLive;
  const handleRetryDrillSteering = useCallback(() => {
    if (!canRetryDrillSteering) return;
    setEngineMessage(null);
    if (drillRecovery?.kind === "analysis") {
      const result = drillRecovery.result;
      coordinator.restartAnalysisWorker();
      coordinator.analyzeMove(result.fenBefore, result.moveUci, playerColor, result.moveIndex);
      void continueAfterPlayerMove(result);
      return;
    }
    if (drillRecovery?.kind === "opponent") {
      void applyOpponentMove(drillRecovery.fen, drillRecovery.uciHistory);
      return;
    }
    void applyOpponentMove(
      chess.fen(),
      useGameStore.getState().moveHistory.map((m) => m.uci),
    );
  }, [
    applyOpponentMove,
    canRetryDrillSteering,
    chess,
    continueAfterPlayerMove,
    coordinator,
    drillRecovery,
    playerColor,
    setEngineMessage,
  ]);

  const allowDragging =
    isGameActive &&
    engineStatus === "ready" &&
    isPlayersTurn &&
    !isRevertPending &&
    !isThinking &&
    isViewingLive &&
    !isBlunderBoardOverrideActive;
  const showEndedScrim = !isGameActive && gameResult !== null && !showStartOverlay;
  const hasBelowBoardContent = moveHistory.length > 0 || !isGameActive;

  return (
    <AnalysisStoreProvider value={analysisStore}>
      <section className="chess-section">
        <div className={`chess-layout ${hasBelowBoardContent ? 'has-graph' : ''}`}>
          <GameInfoPanel
            statusText={statusText}
            gameStatusBadge={gameStatusBadge}
            isRated={isRated}
            isPracticeContinuation={isPracticeContinuation}
            isStoppedDrill={isStoppedDrill}
            isGameActive={isGameActive}
            playerColorChoice={playerColorChoice}
            playerColor={playerColor}
            playerRating={playerRating}
            isProvisional={isProvisional}
            ratingScores={ratingScores}
            ratingDisplayType={ratingDisplayType}
            onRatingDisplayTypeChange={useGameStore.getState().setRatingDisplayType}
            opponentMode={opponentMode}
            opponentName={MAIA_BOT_NAMES[engineElo as keyof typeof MAIA_BOT_NAMES]}
            engineElo={engineElo}
            gameResult={gameResult}
            blunderReviewId={blunderReviewId}
            showGhostInfo={showGhostInfo}
            onToggleGhostInfo={handleToggleGhostInfo}
            onCloseGhostInfo={handleCloseGhostInfo}
            ghostInfoAnchorRef={ghostInfoAnchorRef}
            blunderTargetFen={blunderTargetFen}
            boardOrientation={boardOrientation}
            blunderReviewSrs={blunderReviewSrs}
            displayedOpening={displayedOpening}
            isReviewMomentActive={isReviewMomentActive}
            resolvedReview={resolvedReview}
            isViewingLive={isViewingLive}
            showRehookToast={showRehookToast}
            onDismissRehookToast={handleDismissRehookToast}
            perfectStreak={perfectStreak}
          />

          <div className="chessboard-wrapper">
            <div className="chessboard-board-with-eval">
              <ConnectedEvalBar />
              <BoardStage
                boardInstanceKey={boardInstanceKey}
                boardOrientation={boardOrientation}
                displayedFen={displayedFen}
                onPieceDrop={handleDropPiece}
                onSquareClick={handleSquareClick}
                allowDragging={allowDragging}
                squareStyles={squareStyles}
                arrows={blunderArrows}
                showStartOverlay={showStartOverlay}
                isGameActive={isGameActive}
                isStoppedDrill={isStoppedDrill}
                isStartingGame={isStartingGame}
                onCloseStartOverlay={handleCloseStartOverlay}
                maiaEloBins={MAIA_ELO_BINS}
                engineElo={engineElo}
                onEngineEloChange={handleEngineEloChange}
                botLabel={MAIA_BOT_NAMES[engineElo as keyof typeof MAIA_BOT_NAMES]}
                winDelta={winDelta}
                lossDelta={lossDelta}
                onPlayWhite={handlePlayWhite}
                onPlayRandom={handlePlayRandom}
                onPlayBlack={handlePlayBlack}
                startError={startError}
                showRevertWarning={showRevertWarning}
                isRevertPending={isRevertPending}
                revertError={revertError}
                onRevertAnyway={executeRevert}
                onCancelRevert={cancelRevert}
                showResignWarning={showResignWarning}
                isPracticeContinuation={isPracticeContinuation}
                onResignAnyway={executeResign}
                onCancelResign={cancelResign}
                showEndedScrim={showEndedScrim}
                showFlash={showFlash}
                pendingPromotion={pendingPromotion}
                playerColor={playerColor}
                onPromotionPick={handlePromotionPick}
                onPromotionCancel={handlePromotionCancel}
                streakToast={blocksStreakToast ? null : streakToast}
                isDrillMode={isDrillMode}
                onSwitchToPlayMode={handleSwitchToPlayMode}
                onSwitchToDrillMode={handleSwitchToDrillMode}
                openingFamilies={openingFamilies}
                selectedDrillOpening={selectedDrillOpening}
                drillPlayerColor={playerColorChoice}
                drillStrictnessCp={drillStrictnessCp}
                onSelectDrillOpening={setSelectedDrillOpening}
                onDrillPlayerColorChange={setPlayerColorChoice}
                onDrillStrictnessChange={setDrillStrictnessCp}
                onStartDrill={handleStartDrill}
                isLoadingOpenings={isLoadingOpenings}
              />
            </div>
          </div>
          <GameWarningStack
            className="chess-warning-stack--mobile"
            isGameActive={isGameActive}
            opponentMode={opponentMode}
            isReviewMomentActive={isReviewMomentActive}
            resolvedReview={resolvedReview}
            isViewingLive={isViewingLive}
            showRehookToast={showRehookToast}
            onDismissRehookToast={handleDismissRehookToast}
          />
          {hasBelowBoardContent && (
            <div className="chess-graph-area">
              <PostGameBanner
                isGameActive={isGameActive}
                isPracticeContinuation={isPracticeContinuation}
                showPostGamePrompt={showPostGamePrompt}
                gameResult={gameResult}
                drillOpeningKey={drillOpeningKey}
                drillState={drillState}
                onNewDrill={handleNewDrillSticky}
                ratingChange={ratingChange}
                scoreChanges={scoreChanges}
                ratingDisplayType={ratingDisplayType}
                onViewAnalysis={handleViewAnalysis}
                onShowStartOverlay={handleShowStartOverlay}
                onViewHistory={handleViewHistory}
              />
              <ConnectedAnalysisGraph onSelectMove={handleNavigate} />
            </div>
          )}

          <div className="moves-column">
            <MaterialDisplay fen={displayedFen} perspective={opponentColor} />
            {isGameActive && isStoppedDrill && !gameResult && (
              <DrillStopActions
                terminalReason={drillTerminalReason}
                onAnotherDrill={handleNewDrillSticky}
                onContinueAsNormal={handleContinueDrill}
              />
            )}
            {isGameActive && drillOpeningKey && engineMessage && !isStoppedDrill && (
              <div className="chess-start-error" role="alert">
                <p>{engineMessage}</p>
                <div className="chess-post-game-actions">
                  {canRetryDrillSteering && (
                    <button
                      className="chess-button primary"
                      type="button"
                      onClick={handleRetryDrillSteering}
                    >
                      Retry
                    </button>
                  )}
                  {drillState !== "converted" && (
                    <button
                      className="chess-button"
                      type="button"
                      onClick={handleResignClick}
                    >
                      Abandon
                    </button>
                  )}
                </div>
              </div>
            )}
            {isGameActive && !isPlayersTurn && (
              <span className="turn-label">Waiting for opponent</span>
            )}
            <ConnectedMoveList
              onNavigate={handleNavigate}
              messages={moveMessages}
              onRevealSrsFail={handleRevealSrsFail}
              revealedSrsFailIndex={reviewFailModal?.moveIndex ?? null}
              onResign={handleResignClick}
              isResignDisabled={!isGameActive || chess.isGameOver()}
              onRevert={handleRevertClick}
              isRevertDisabled={moveHistory.length === 0 || chess.isGameOver()}
              onFlipBoard={flipBoard}
              onReset={handleReset}
              isGameActive={isGameActive}
              isInteractionDisabled={isRevertPending}
            />
            {isGameActive && isPlayersTurn && (
              <span className="turn-label">Your turn</span>
            )}
            <MaterialDisplay fen={displayedFen} perspective={playerColor} />
          </div>
        </div>

        <AnalysisEffects
          pendingAnalysisContextRef={pendingAnalysisContextRef}
          blunderRecordedRef={blunderRecordedRef}
          pendingSrsReviewRef={pendingSrsReviewRef}
          appendMoveMessage={appendMoveMessage}
          setBlunderAlert={setBlunderAlert}
          setShowFlash={setShowFlash}
          setResolvedReview={setResolvedReview}
        />
      </section>
    </AnalysisStoreProvider>
  );
};

export default ChessGame;
