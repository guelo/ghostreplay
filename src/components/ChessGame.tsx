import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Chess } from "chess.js";
import type { Square } from "chess.js";
import type { PieceDropHandlerArgs } from "react-chessboard";
import { useStockfishEngine } from "../hooks/useStockfishEngine";
import { useChessGameLifecycle } from "../hooks/useChessGameLifecycle";
import { useMoveAnalysis, type AnalysisResult } from "../hooks/useMoveAnalysis";
import { useChessGameController } from "../hooks/useChessGameController";
import { useOpponentMove } from "../hooks/useOpponentMove";
import AnalysisGraph from "./AnalysisGraph";
import type { OpeningLookupResult } from "../openings/openingBook";
import { lookupOpeningByFen } from "../openings/openingBook";
import {
  recordBlunder,
  recordManualBlunder,
  reviewSrsBlunder,
  type RatingChange,
  type TargetBlunderSrs,
} from "../utils/api";
import { shouldRecordBlunder } from "../utils/blunder";
import { normalize_fen } from "../utils/fen";
import {
  isWithinRecordingMoveCap,
  toWhitePerspective,
} from "../workers/analysisUtils";
import {
  deriveAnnotatedMoves,
  deriveBlunderArrows,
  deriveLastMoveSquares,
  type BlunderAlert,
  type MoveRecord,
  type ReviewFailInfo,
} from "./chess-game/domain/movePresentation";
import { deriveDisplayedOpening } from "./chess-game/domain/opening";
import {
  deriveGameStatusBadge,
  deriveStatusText,
  type GameResult,
} from "./chess-game/domain/status";
import {
  BLUNDER_AUDIO_CLIPS,
  MAIA_BOT_NAMES,
  MAIA_ELO_BINS,
  SRS_REVIEW_FAIL_THRESHOLD_CP,
  STARTING_FEN,
} from "./chess-game/config";
import { eloStakes } from "./chess-game/elo";
import type { BoardOrientation, OpenHistoryOptions } from "./chess-game/types";
import BoardStage from "./chess-game/ui/BoardStage";
import GameInfoPanel from "./chess-game/ui/GameInfoPanel";
import PostGameBanner from "./chess-game/ui/PostGameBanner";
import MaterialDisplay from "./MaterialDisplay";
import MoveList from "./MoveList";
import type { MoveMessage, SrsFailDetail } from "./MoveList";

type ChessGameProps = {
  onOpenHistory?: (options: OpenHistoryOptions) => void;
};

const isSquare = (value: string): value is Square => /^[a-h][1-8]$/.test(value);

const playRandomBlunderAudio = () => {
  if (typeof Audio === "undefined" || BLUNDER_AUDIO_CLIPS.length === 0) {
    return;
  }

  const randomIndex = Math.floor(Math.random() * BLUNDER_AUDIO_CLIPS.length);
  const clip = BLUNDER_AUDIO_CLIPS[randomIndex];
  const audio = new Audio(clip);
  void audio.play().catch(() => {
    // Ignore playback failures (missing file, browser policy, etc.).
  });
};

const ChessGame = ({ onOpenHistory }: ChessGameProps = {}) => {
  const chess = useMemo(() => new Chess(), []);
  const [fen, setFen] = useState(chess.fen());
  const [boardOrientation, setBoardOrientation] =
    useState<BoardOrientation>("white");
  const [playerColor, setPlayerColor] = useState<BoardOrientation>("white");
  const [playerColorChoice, setPlayerColorChoice] = useState<
    BoardOrientation | "random"
  >("random");
  const [engineElo, setEngineElo] =
    useState<(typeof MAIA_ELO_BINS)[number]>(800);
  const [moveHistory, setMoveHistory] = useState<MoveRecord[]>([]);
  const [viewIndex, setViewIndex] = useState<number | null>(null); // null = viewing live position
  const {
    status: engineStatus,
    isThinking,
    evaluatePosition,
    resetEngine,
  } = useStockfishEngine();
  const {
    analyzeMove,
    lastAnalysis,
    analysisMap,
    status: analysisStatus,
    isAnalyzing,
    clearAnalysis,
  } = useMoveAnalysis();
  const [, setEngineMessage] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [isGameActive, setIsGameActive] = useState(false);
  const [liveOpening, setLiveOpening] = useState<OpeningLookupResult | null>(
    null,
  );
  const [gameResult, setGameResult] = useState<GameResult | null>(null);
  const [isStartingGame, setIsStartingGame] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [showStartOverlay, setShowStartOverlay] = useState(true);
  const [isAddingToLibrary, setIsAddingToLibrary] = useState(false);
  const [blunderAlert, setBlunderAlert] = useState<BlunderAlert | null>(null);
  const [showFlash, setShowFlash] = useState(false);
  const [blunderReviewId, setBlunderReviewId] = useState<number | null>(null);
  const [blunderReviewSrs, setBlunderReviewSrs] =
    useState<TargetBlunderSrs | null>(null);
  const [blunderTargetFen, setBlunderTargetFen] = useState<string | null>(null);
  const [showGhostInfo, setShowGhostInfo] = useState(false);
  const ghostInfoAnchorRef = useRef<HTMLSpanElement>(null);
  const [, setShowPassToast] = useState(false);
  const [showRehookToast, setShowRehookToast] = useState(false);
  const [reviewFailModal, setReviewFailModal] = useState<ReviewFailInfo | null>(
    null,
  );
  const [showPostGamePrompt, setShowPostGamePrompt] = useState(false);
  const [isRated, setIsRated] = useState(true);
  const [showRevertWarning, setShowRevertWarning] = useState(false);
  const [playerRating, setPlayerRating] = useState<number>(1200);
  const [isProvisional, setIsProvisional] = useState(true);
  const [ratingChange, setRatingChange] = useState<RatingChange | null>(null);
  const [selectedSquare, setSelectedSquare] = useState<string | null>(null);
  const [optionSquares, setOptionSquares] = useState<
    Record<string, React.CSSProperties>
  >({});

  // Tracks next move index synchronously so async callbacks (engine/ghost)
  // don't read stale moveHistory.length from closures.
  const moveCountRef = useRef(0);

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
  const pendingSrsReviewRef = useRef<{
    blunderId: number;
    moveIndex: number;
    userMoveSan: string;
    srs: TargetBlunderSrs | null;
  } | null>(null);
  const openingLookupRequestIdRef = useRef(0);
  // Index 0 = starting position (before any move), index N = after move N
  const openingHistoryRef = useRef<(OpeningLookupResult | null)[]>([]);
  const analysisMapRef = useRef<Map<number, AnalysisResult>>(new Map());
  const moveMessagesRef = useRef<Map<number, MoveMessage[]>>(new Map());
  const [moveMessagesVersion, setMoveMessagesVersion] = useState(0);
  const moveHistoryRef = useRef<MoveRecord[]>([]);
  const analysisStatusRef = useRef(analysisStatus);
  const isAnalyzingRef = useRef(isAnalyzing);
  const uploadedAnalysisSessionsRef = useRef<Set<string>>(new Set());
  const previousOpponentModeRef = useRef<"ghost" | "engine" | null>(null);
  const handleGameEndRef = useRef<() => Promise<void>>(async () => {});
  const handleGameEndStable = useCallback(
    () => handleGameEndRef.current(),
    [],
  );

  useEffect(() => {
    analysisMapRef.current = analysisMap;
  }, [analysisMap]);

  useEffect(() => {
    moveHistoryRef.current = moveHistory;
  }, [moveHistory]);

  useEffect(() => {
    analysisStatusRef.current = analysisStatus;
  }, [analysisStatus]);

  useEffect(() => {
    isAnalyzingRef.current = isAnalyzing;
  }, [isAnalyzing]);

  // Get the FEN to display on the board (accounts for viewing past positions)
  const displayedFen = useMemo(() => {
    if (viewIndex === null) {
      return fen; // Live position
    }
    if (viewIndex === -1) {
      return STARTING_FEN; // Starting position
    }
    return moveHistory[viewIndex]?.fen ?? fen;
  }, [viewIndex, fen, moveHistory]);

  const lastMoveSquares = useMemo((): Record<string, React.CSSProperties> => {
    return deriveLastMoveSquares(moveHistory, viewIndex);
  }, [viewIndex, moveHistory]);

  // Enrich moves with analysis data for MoveList annotations
  const annotatedMoves = useMemo(() => {
    return deriveAnnotatedMoves(moveHistory, analysisMap);
  }, [moveHistory, analysisMap]);

  // Compute arrows from review fail modal or blunder alert
  const blunderArrows = useMemo(() => {
    return deriveBlunderArrows(reviewFailModal, blunderAlert);
  }, [reviewFailModal, blunderAlert]);

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

  const handleNavigate = useCallback(
    (index: number | null) => {
      setViewIndex(index);
      setReviewFailModal(null);

      // Re-show blunder alert when clicking on a player's blunder move
      if (index !== null && index >= 0) {
        const analysis = analysisMap.get(index);
        if (
          analysis?.blunder &&
          analysis.delta !== null &&
          isPlayerMoveIndex(index)
        ) {
          const moveSan = moveHistory[index]?.san ?? analysis.move;
          let bestMoveSan = analysis.bestMove;
          try {
            const fenBeforeMove =
              index === 0 ? STARTING_FEN : moveHistory[index - 1]?.fen;
            if (fenBeforeMove) {
              const tempChess = new Chess(fenBeforeMove);
              const from = analysis.bestMove.slice(0, 2);
              const to = analysis.bestMove.slice(2, 4);
              const promotion = analysis.bestMove.slice(4) || undefined;
              const bestMoveResult = tempChess.move({ from, to, promotion });
              if (bestMoveResult) {
                bestMoveSan = bestMoveResult.san;
              }
            }
          } catch {
            // Fall back to UCI notation
          }
          setBlunderAlert({
            moveSan,
            moveUci: analysis.move,
            bestMoveUci: analysis.bestMove,
            bestMoveSan,
            delta: analysis.delta,
          });
          return;
        }
      }

      // Clear blunder alert when navigating to a non-blunder move
      setBlunderAlert(null);
    },
    [analysisMap, isPlayerMoveIndex, moveHistory],
  );

  const selectedMoveIndex = useMemo(() => {
    if (moveHistory.length === 0) {
      return null;
    }
    return viewIndex ?? moveHistory.length - 1;
  }, [moveHistory.length, viewIndex]);

  const selectedEvalCp = useMemo(() => {
    if (selectedMoveIndex === null || selectedMoveIndex < 0) {
      return null;
    }

    // Keep showing the most recent known eval while the latest move's
    // analysis is still in flight.
    for (let idx = selectedMoveIndex; idx >= 0; idx -= 1) {
      const analysis = analysisMap.get(idx);
      if (analysis?.playedEval == null) {
        continue;
      }
      return toWhitePerspective(analysis.playedEval, idx);
    }

    return null;
  }, [analysisMap, selectedMoveIndex]);

  const evals = useMemo(
    () =>
      moveHistory.map((_, i) => {
        const a = analysisMap.get(i);
        return a?.playedEval != null ? toWhitePerspective(a.playedEval, i) : null;
      }),
    [moveHistory, analysisMap],
  );

  const canAddSelectedMove = useMemo(() => {
    if (!sessionId || selectedMoveIndex === null) {
      return false;
    }
    return isPlayerMoveIndex(selectedMoveIndex);
  }, [sessionId, selectedMoveIndex, isPlayerMoveIndex]);

  const clearMoveHighlights = useCallback(() => {
    setSelectedSquare(null);
    setOptionSquares({});
  }, []);

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

  const buildManualCapturePayload = useCallback(
    (moveIndex: number) => {
      if (moveIndex < 0 || moveIndex >= moveHistory.length) {
        return null;
      }

      const preMoveFen =
        moveIndex === 0 ? STARTING_FEN : moveHistory[moveIndex - 1]?.fen;
      if (!preMoveFen) {
        return null;
      }

      const replay = new Chess();
      for (let i = 0; i <= moveIndex; i += 1) {
        const applied = replay.move(moveHistory[i].san);
        if (!applied) {
          return null;
        }
      }

      const analysis = analysisMap.get(moveIndex);
      const userMove = moveHistory[moveIndex].san;

      return {
        pgn: replay.pgn(),
        fen: preMoveFen,
        userMove,
        bestMove: analysis?.bestMove ?? userMove,
        evalBefore: analysis?.bestEval ?? 0,
        evalAfter: analysis?.playedEval ?? analysis?.bestEval ?? 0,
      };
    },
    [analysisMap, moveHistory],
  );

  const handleAddSelectedMove = useCallback(
    async (moveIndex: number) => {
      if (!sessionId) {
        return;
      }

      if (!isPlayerMoveIndex(moveIndex)) {
        return;
      }

      const payload = buildManualCapturePayload(moveIndex);
      if (!payload) {
        return;
      }

      setIsAddingToLibrary(true);
      try {
        await recordManualBlunder(
          sessionId,
          payload.pgn,
          payload.fen,
          payload.userMove,
          payload.bestMove,
          payload.evalBefore,
          payload.evalAfter,
        );
      } catch (error) {
        console.error(
          "[BlunderLibrary] Failed to record manual blunder:",
          error,
        );
      } finally {
        setIsAddingToLibrary(false);
      }
    },
    [buildManualCapturePayload, isPlayerMoveIndex, sessionId],
  );

  const isPlayersTurn = chess.turn() === (playerColor === "white" ? "w" : "b");
  const moveCount = moveHistory.length;
  const isReviewMomentActive =
    blunderReviewId !== null &&
    blunderTargetFen !== null &&
    normalize_fen(fen) === normalize_fen(blunderTargetFen) &&
    isGameActive &&
    isPlayersTurn &&
    isViewingLive &&
    !chess.isGameOver();

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

  // Build a stable snapshot for passing to MoveList
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const moveMessages = useMemo(() => {
    void moveMessagesVersion; // depend on version counter
    return moveMessagesRef.current as ReadonlyMap<number, MoveMessage[]>;
  }, [moveMessagesVersion]);


  // Show spinner on every move that hasn't received an analysis result yet
  const analyzingIndices = useMemo(() => {
    if (!isGameActive) return new Set<number>();
    const pending = new Set<number>();
    for (let i = 0; i < moveHistory.length; i++) {
      if (!analysisMap.has(i)) {
        pending.add(i);
      }
    }
    return pending;
  }, [isGameActive, moveHistory.length, analysisMap]);

  const opponentColor = playerColor === "white" ? "black" : "white";

  const { applyPlayerMove, handleDrop, applyEngineMove, applyGhostMove } =
    useChessGameController({
      chess,
      playerColor,
      opponentColor,
      isPlayersTurn,
      isViewingLive,
      blunderReviewId,
      blunderReviewSrs,
      moveCountRef,
      moveHistoryRef,
      pendingAnalysisContextRef,
      pendingSrsReviewRef,
      setFen,
      setMoveHistory,
      setViewIndex,
      setEngineMessage,
      setBlunderAlert,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setShowGhostInfo,
      analyzeMove,
      evaluatePosition,
      handleGameEnd: handleGameEndStable,
      clearMoveHighlights,
    });

  const { opponentMode, applyOpponentMove, resetMode } = useOpponentMove({
    sessionId,
    onApplyBackendMove: applyGhostMove,
    onApplyLocalFallback: applyEngineMove,
  });

  const {
    handleGameEnd,
    executeRevert,
    handleRevertClick,
    cancelRevert,
    handleNewGame,
    handleResign,
    handleReset,
    handleShowStartOverlay,
    handleViewAnalysis,
    handleViewHistory,
  } = useChessGameLifecycle({
    chess,
    sessionId,
    isGameActive,
    isRated,
    playerColor,
    playerColorChoice,
    engineElo,
    playerRating,
    moveHistory,
    moveCountRef,
    moveHistoryRef,
    analysisMapRef,
    analysisStatusRef,
    isAnalyzingRef,
    uploadedAnalysisSessionsRef,
    openingHistoryRef,
    blunderRecordedRef,
    pendingAnalysisContextRef,
    pendingSrsReviewRef,
    clearMoveHighlights,
    resetMode,
    resetEngine,
    clearAnalysis,
    onOpenHistory,
    setEngineMessage,
    setPlayerRating,
    setIsProvisional,
    setEngineElo,
    setIsStartingGame,
    setStartError,
    setPlayerColor,
    setPlayerColorChoice,
    setBoardOrientation,
    setSessionId,
    setIsGameActive,
    setShowStartOverlay,
    setFen,
    setGameResult,
    setRatingChange,
    setMoveHistory,
    setViewIndex,
    setLiveOpening,
    setBlunderAlert,
    setShowFlash,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setShowPassToast,
    setShowRehookToast,
    setReviewFailModal,
    setShowPostGamePrompt,
    setIsRated,
    showRevertWarning,
    setShowRevertWarning,
  });

  useEffect(() => {
    handleGameEndRef.current = handleGameEnd;
  }, [handleGameEnd]);

  const applyPlayerMoveAndAdvance = useCallback(
    (sourceSquare: string, targetSquare: string): boolean => {
      const result = applyPlayerMove(sourceSquare, targetSquare);
      if (!result.applied) {
        return false;
      }

      if (result.gameOver) {
        void handleGameEnd();
      } else {
        void applyOpponentMove(result.fenAfter, result.uciHistory);
      }

      return true;
    },
    [applyOpponentMove, applyPlayerMove, handleGameEnd],
  );

  // Clear move messages when a new game starts
  useEffect(() => {
    if (moveHistory.length === 0) {
      moveMessagesRef.current = new Map();
      setMoveMessagesVersion((v) => v + 1);
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
      const playersTurn =
        chess.turn() === (playerColor === "white" ? "w" : "b");
      if (!isGameActive || !playersTurn || !isViewingLive) {
        clearMoveHighlights();
        return;
      }

      // If a square is already selected, try to make a move to the clicked square
      if (selectedSquare) {
        if (applyPlayerMoveAndAdvance(selectedSquare, square)) {
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
      isViewingLive,
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
      moveHistoryRef.current.map((m) => m.uci),
    );
  }, [
    applyOpponentMove,
    chess,
    engineStatus,
    isGameActive,
    isThinking,
    isViewingLive,
    moveCount,
    playerColor,
  ]);

  // Blunder detection: POST /api/blunder on first blunder this session
  useEffect(() => {
    const blunderData = shouldRecordBlunder({
      analysis: lastAnalysis,
      context: pendingAnalysisContextRef.current,
      sessionId,
      isGameActive,
      alreadyRecorded: blunderRecordedRef.current,
    });

    if (!blunderData) {
      return;
    }

    // Mark as recorded before the async call to prevent duplicates
    blunderRecordedRef.current = true;

    const postBlunder = async () => {
      try {
        await recordBlunder(
          blunderData.sessionId,
          blunderData.pgn,
          blunderData.fen,
          blunderData.userMove,
          blunderData.bestMove,
          blunderData.evalBefore,
          blunderData.evalAfter,
        );
        console.log("[Blunder] Recorded blunder to backend");
      } catch (error) {
        console.error("[Blunder] Failed to record blunder:", error);
        // Don't reset blunderRecordedRef - backend may have received it
      }
    };

    void postBlunder();
  }, [lastAnalysis, sessionId, isGameActive]);

  // SRS review grading: evaluate user move from a targeted blunder position.
  useEffect(() => {
    if (
      !sessionId ||
      !isGameActive ||
      !lastAnalysis ||
      lastAnalysis.moveIndex === null
    ) {
      return;
    }

    const pendingReview = pendingSrsReviewRef.current;
    if (!pendingReview || pendingReview.moveIndex !== lastAnalysis.moveIndex) {
      return;
    }

    pendingSrsReviewRef.current = null;

    if (lastAnalysis.delta === null) {
      return;
    }

    const evalLossCp = Math.max(lastAnalysis.delta, 0);
    const passed = evalLossCp < SRS_REVIEW_FAIL_THRESHOLD_CP;

    if (passed) {
      const srs = pendingReview.srs;
      appendMoveMessage(lastAnalysis.moveIndex, {
        key: `srs-${lastAnalysis.moveIndex}`,
        text: "Correct! You avoided your past mistake.",
        variant: "srs-pass",
        srsStats: srs
          ? {
              passCount: srs.pass_count + 1,
              failCount: srs.fail_count,
              streak: srs.pass_streak + 1,
            }
          : undefined,
      });
    }

    if (!passed) {
      let bestMoveSan = lastAnalysis.bestMove;
      const fenBeforeMove =
        lastAnalysis.moveIndex === 0
          ? STARTING_FEN
          : moveHistoryRef.current[lastAnalysis.moveIndex - 1]?.fen;
      if (fenBeforeMove) {
        try {
          const tempChess = new Chess(fenBeforeMove);
          const from = lastAnalysis.bestMove.slice(0, 2);
          const to = lastAnalysis.bestMove.slice(2, 4);
          const promotion = lastAnalysis.bestMove.slice(4) || undefined;
          const bestMoveResult = tempChess.move({ from, to, promotion });
          if (bestMoveResult) {
            bestMoveSan = bestMoveResult.san;
          }
        } catch {
          // Fall back to UCI notation
        }
      }

      const srs = pendingReview.srs;
      appendMoveMessage(lastAnalysis.moveIndex, {
        key: `srs-${lastAnalysis.moveIndex}`,
        text: "You made this mistake again!",
        variant: "srs-fail",
        srsFailDetail: {
          userMoveSan: pendingReview.userMoveSan,
          bestMoveSan,
          userMoveUci: lastAnalysis.move,
          bestMoveUci: lastAnalysis.bestMove,
        },
        srsStats: srs
          ? {
              passCount: srs.pass_count,
              failCount: srs.fail_count + 1,
              streak: 0,
            }
          : undefined,
      });
    }

    const postReview = async () => {
      try {
        await reviewSrsBlunder(
          sessionId,
          pendingReview.blunderId,
          passed,
          pendingReview.userMoveSan,
          evalLossCp,
        );
      } catch (error) {
        console.error("[SRS] Failed to record review:", error);
      }
    };

    void postReview();
  }, [isGameActive, lastAnalysis, sessionId]);

  // Blunder alert: show flash + toast + arrows for player blunders
  useEffect(() => {
    if (
      !lastAnalysis?.blunder ||
      lastAnalysis.delta === null ||
      lastAnalysis.moveIndex === null
    ) {
      return;
    }

    // Skip the very first move of the game (not reachable by ghost mode)
    if (lastAnalysis.moveIndex === 0) {
      return;
    }

    if (!isWithinRecordingMoveCap(lastAnalysis.moveIndex)) {
      return;
    }

    if (!isPlayerMoveIndex(lastAnalysis.moveIndex)) {
      return;
    }

    const moveSan =
      moveHistory[lastAnalysis.moveIndex]?.san ?? lastAnalysis.move;

    let bestMoveSan = lastAnalysis.bestMove;
    try {
      const fenBeforeMove =
        lastAnalysis.moveIndex === 0
          ? STARTING_FEN
          : moveHistory[lastAnalysis.moveIndex - 1]?.fen;
      if (fenBeforeMove) {
        const tempChess = new Chess(fenBeforeMove);
        const from = lastAnalysis.bestMove.slice(0, 2);
        const to = lastAnalysis.bestMove.slice(2, 4);
        const promotion = lastAnalysis.bestMove.slice(4) || undefined;
        const bestMoveResult = tempChess.move({ from, to, promotion });
        if (bestMoveResult) {
          bestMoveSan = bestMoveResult.san;
        }
      }
    } catch {
      // Fall back to UCI notation
    }

    setBlunderAlert({
      moveSan,
      moveUci: lastAnalysis.move,
      bestMoveUci: lastAnalysis.bestMove,
      bestMoveSan,
      delta: lastAnalysis.delta,
    });
    setShowFlash(true);
    playRandomBlunderAudio();
  }, [lastAnalysis, isPlayerMoveIndex, moveHistory]);

  // Auto-dismiss flash after animation
  useEffect(() => {
    if (!showFlash) return;
    const timer = setTimeout(() => setShowFlash(false), 400);
    return () => clearTimeout(timer);
  }, [showFlash]);

  // Auto-dismiss blunder toast after 4 seconds
  useEffect(() => {
    if (!blunderAlert) return;
    const timer = setTimeout(() => setBlunderAlert(null), 4000);
    return () => clearTimeout(timer);
  }, [blunderAlert]);

  // Auto-dismiss re-hook toast after 3 seconds
  useEffect(() => {
    if (!showRehookToast) return;
    const timer = setTimeout(() => setShowRehookToast(false), 3000);
    return () => clearTimeout(timer);
  }, [showRehookToast]);

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

  const handleDropPiece = ({
    sourceSquare,
    targetSquare,
  }: PieceDropHandlerArgs) => {
    const result = handleDrop(sourceSquare, targetSquare);
    if (!result.applied) {
      return false;
    }

    if (result.gameOver) {
      void handleGameEnd();
    } else {
      void applyOpponentMove(result.fenAfter, result.uciHistory);
    }

    return true;
  };

  const handleRevealSrsFail = useCallback(
    (detail: SrsFailDetail, moveIndex: number) => {
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
    [],
  );

  const handleDismissReviewFail = useCallback(() => {
    setReviewFailModal(null);
    setViewIndex(null);
  }, []);

  const flipBoard = () => {
    setBoardOrientation((current) => (current === "white" ? "black" : "white"));
  };

  const gameStatusBadge = deriveGameStatusBadge(isGameActive, gameResult);
  const { winDelta, lossDelta } = eloStakes(
    playerRating,
    engineElo,
    isProvisional,
  );
  const allowDragging =
    isGameActive &&
    engineStatus === "ready" &&
    isPlayersTurn &&
    !isThinking &&
    isViewingLive;
  const showEndedScrim = !isGameActive && gameResult !== null && !showStartOverlay;

  return (
    <section className="chess-section">
      <header className="chess-header">
        <p className="eyebrow">SRS Chess</p>
      </header>

      <div className="chess-layout">
        <GameInfoPanel
          statusText={statusText}
          gameStatusBadge={gameStatusBadge}
          isRated={isRated}
          isGameActive={isGameActive}
          playerColorChoice={playerColorChoice}
          playerColor={playerColor}
          playerRating={playerRating}
          isProvisional={isProvisional}
          opponentMode={opponentMode}
          opponentName={MAIA_BOT_NAMES[engineElo]}
          blunderReviewId={blunderReviewId}
          showGhostInfo={showGhostInfo}
          onToggleGhostInfo={() => setShowGhostInfo((v) => !v)}
          onCloseGhostInfo={() => setShowGhostInfo(false)}
          ghostInfoAnchorRef={ghostInfoAnchorRef}
          blunderTargetFen={blunderTargetFen}
          boardOrientation={boardOrientation}
          blunderReviewSrs={blunderReviewSrs}
          displayedOpening={displayedOpening}
          isReviewMomentActive={isReviewMomentActive}
          onResign={handleResign}
          onRevert={handleRevertClick}
          isResignDisabled={!isGameActive || chess.isGameOver()}
          isRevertDisabled={moveHistory.length === 0 || chess.isGameOver()}
          onFlipBoard={flipBoard}
          onReset={handleReset}
        />

        <div className="chessboard-wrapper">
          <BoardStage
            selectedEvalCp={selectedEvalCp}
            boardOrientation={boardOrientation}
            displayedFen={displayedFen}
            onPieceDrop={handleDropPiece}
            onSquareClick={handleSquareClick}
            allowDragging={allowDragging}
            squareStyles={{ ...lastMoveSquares, ...optionSquares }}
            arrows={blunderArrows}
            showStartOverlay={showStartOverlay}
            isGameActive={isGameActive}
            isStartingGame={isStartingGame}
            onCloseStartOverlay={() => setShowStartOverlay(false)}
            maiaEloBins={MAIA_ELO_BINS}
            engineElo={engineElo}
            onEngineEloChange={(elo) => {
              setEngineElo(elo as (typeof MAIA_ELO_BINS)[number]);
            }}
            botLabel={MAIA_BOT_NAMES[engineElo]}
            winDelta={winDelta}
            lossDelta={lossDelta}
            onPlayWhite={() => {
              void handleNewGame("white");
            }}
            onPlayRandom={() => {
              void handleNewGame("random");
            }}
            onPlayBlack={() => {
              void handleNewGame("black");
            }}
            startError={startError}
            showRevertWarning={showRevertWarning}
            onRevertAnyway={executeRevert}
            onCancelRevert={cancelRevert}
            showEndedScrim={showEndedScrim}
            showFlash={showFlash}
            showRehookToast={showRehookToast}
            onDismissRehookToast={() => setShowRehookToast(false)}
          />
          <PostGameBanner
            isGameActive={isGameActive}
            showPostGamePrompt={showPostGamePrompt}
            gameResult={gameResult}
            ratingChange={ratingChange}
            onViewAnalysis={handleViewAnalysis}
            onShowStartOverlay={handleShowStartOverlay}
            onViewHistory={handleViewHistory}
          />
          {evals.length > 0 && evals.some((e) => e !== null) && (
            <AnalysisGraph
              evals={evals}
              currentIndex={selectedMoveIndex}
              onSelectMove={handleNavigate}
              playerColor={playerColor}
              evalCp={selectedEvalCp}
            />
          )}
        </div>

        <div className="moves-column">
          <MaterialDisplay fen={displayedFen} perspective={opponentColor} />
          <MoveList
            moves={annotatedMoves}
            currentIndex={viewIndex}
            onNavigate={handleNavigate}
            canAddSelectedMove={canAddSelectedMove}
            isAddingSelectedMove={isAddingToLibrary}
            onAddSelectedMove={handleAddSelectedMove}
            messages={moveMessages}
            analyzingIndices={analyzingIndices}
            playerColor={playerColor}
            onRevealSrsFail={handleRevealSrsFail}
            revealedSrsFailIndex={reviewFailModal?.moveIndex ?? null}
          />
          <MaterialDisplay fen={displayedFen} perspective={playerColor} />
        </div>
      </div>
    </section>
  );
};

export default ChessGame;
