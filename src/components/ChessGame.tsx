import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Chess } from "chess.js";
import type { Square } from "chess.js";
import type { PieceDropHandlerArgs } from "react-chessboard";
import { useStockfishEngine } from "../hooks/useStockfishEngine";
import { useChessGameLifecycle } from "../hooks/useChessGameLifecycle";
import { useChessGameController } from "../hooks/useChessGameController";
import { useOpponentMove } from "../hooks/useOpponentMove";
import { useGameStore } from "../stores/useGameStore";
import {
  gameAnalysisStore,
  AnalysisStoreProvider,
} from "../stores/createAnalysisStore";
import { useGameAnalysisCoordinator } from "../contexts/GameAnalysisCoordinatorContext";
import type { OpeningLookupResult } from "../openings/openingBook";
import { lookupOpeningByFen } from "../openings/openingBook";
import type { TargetBlunderSrs } from "../utils/api";
import { normalize_fen } from "../utils/fen";
import {
  buildBlunderAlert,
  deriveBlunderArrows,
  deriveLastMoveSquares,
  type BlunderAlert,
  type ReviewFailInfo,
} from "./chess-game/domain/movePresentation";
import { deriveDisplayedOpening } from "./chess-game/domain/opening";
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
import type { OpenHistoryOptions } from "./chess-game/types";
import BoardStage from "./chess-game/ui/BoardStage";
import GameInfoPanel from "./chess-game/ui/GameInfoPanel";
import PostGameBanner from "./chess-game/ui/PostGameBanner";
import MaterialDisplay from "./MaterialDisplay";
import type { MoveMessage, SrsFailDetail } from "./MoveList";
import {
  ConnectedEvalBar,
  ConnectedAnalysisGraph,
  ConnectedMoveList,
} from "./chess-game/AnalysisConnectors";
import AnalysisEffects from "./chess-game/AnalysisEffects";

type ChessGameProps = {
  onOpenHistory?: (options: OpenHistoryOptions) => void;
};

const isSquare = (value: string): value is Square => /^[a-h][1-8]$/.test(value);

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

  const [, setEngineMessage] = useState<string | null>(null);
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
  const [blunderTargetFen, setBlunderTargetFen] = useState<string | null>(null);
  const [showGhostInfo, setShowGhostInfo] = useState(false);
  const ghostInfoAnchorRef = useRef<HTMLSpanElement>(null);
  const [, setShowPassToast] = useState(false);
  const [showRehookToast, setShowRehookToast] = useState(false);
  const [reviewFailModal, setReviewFailModal] = useState<ReviewFailInfo | null>(
    null,
  );
  const [showPostGamePrompt, setShowPostGamePrompt] = useState(false);
  const isRated = useGameStore((s) => s.isRated);
  const [showRevertWarning, setShowRevertWarning] = useState(false);
  const [showResignWarning, setShowResignWarning] = useState(false);
  const playerRating = useGameStore((s) => s.playerRating);
  const isProvisional = useGameStore((s) => s.isProvisional);
  const ratingChange = useGameStore((s) => s.ratingChange);
  const [selectedSquare, setSelectedSquare] = useState<string | null>(null);
  const [optionSquares, setOptionSquares] = useState<
    Record<string, React.CSSProperties>
  >({});
  const [boardInstanceKey, setBoardInstanceKey] = useState(0);

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
  const isBlunderBoardOverrideActive = blunderAlert !== null;

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
    },
    [analysisStore, clearBlunderBoardOverride, isPlayerMoveIndex],
  );

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
      pendingAnalysisContextRef,
      pendingSrsReviewRef,
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
      clearBlunderBoardOverride,
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
    handleResignClick,
    executeResign,
    cancelResign,
    handleReset,
    handleShowStartOverlay,
    handleViewAnalysis,
    handleViewHistory,
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
    setShowPassToast,
    setShowRehookToast,
    setReviewFailModal,
    setShowPostGamePrompt,
    showRevertWarning,
    setShowRevertWarning,
    setShowResignWarning,
    clearBlunderBoardOverride,
  });

  useEffect(() => {
    handleGameEndRef.current = handleGameEnd;
  }, [handleGameEnd]);

  // Sync coordinator with existing active session on mount (e.g., after refresh)
  useEffect(() => {
    const { sessionId: sid, isGameActive: active } = useGameStore.getState();
    if (sid && active && coordinator.sessionId !== sid) {
      coordinator.startSession(sid);
    }
  }, [coordinator]);

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
      if (isBlunderBoardOverrideActive) {
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
      isBlunderBoardOverrideActive,
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
      useGameStore.getState().moveHistory.map((m) => m.uci),
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
      if (isBlunderBoardOverrideActive) {
        return false;
      }

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
    },
    [applyOpponentMove, handleDrop, handleGameEnd, isBlunderBoardOverrideActive],
  );

  const handleRevealSrsFail = useCallback(
    (detail: SrsFailDetail, moveIndex: number) => {
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

  const allowDragging =
    isGameActive &&
    engineStatus === "ready" &&
    isPlayersTurn &&
    !isThinking &&
    isViewingLive &&
    !isBlunderBoardOverrideActive;
  const showEndedScrim = !isGameActive && gameResult !== null && !showStartOverlay;

  return (
    <AnalysisStoreProvider value={analysisStore}>
      <section className="chess-section">
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
            opponentName={MAIA_BOT_NAMES[engineElo as keyof typeof MAIA_BOT_NAMES]}
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
                onRevertAnyway={executeRevert}
                onCancelRevert={cancelRevert}
                showResignWarning={showResignWarning}
                onResignAnyway={executeResign}
                onCancelResign={cancelResign}
                showEndedScrim={showEndedScrim}
                showFlash={showFlash}
                showRehookToast={showRehookToast}
                onDismissRehookToast={handleDismissRehookToast}
              />
            </div>
            <PostGameBanner
              isGameActive={isGameActive}
              showPostGamePrompt={showPostGamePrompt}
              gameResult={gameResult}
              ratingChange={ratingChange}
              onViewAnalysis={handleViewAnalysis}
              onShowStartOverlay={handleShowStartOverlay}
              onViewHistory={handleViewHistory}
            />
            <ConnectedAnalysisGraph onSelectMove={handleNavigate} />
          </div>

          <div className="moves-column">
            <MaterialDisplay fen={displayedFen} perspective={opponentColor} />
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
            />
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
        />
      </section>
    </AnalysisStoreProvider>
  );
};

export default ChessGame;
