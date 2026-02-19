import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Chess } from "chess.js";
import type { Square } from "chess.js";
import { Chessboard } from "react-chessboard";
import type { PieceDropHandlerArgs } from "react-chessboard";
import { useStockfishEngine } from "../hooks/useStockfishEngine";
import { useMoveAnalysis, type AnalysisResult } from "../hooks/useMoveAnalysis";
import { useOpponentMove } from "../hooks/useOpponentMove";
import type { OpeningLookupResult } from "../openings/openingBook";
import { lookupOpeningByFen } from "../openings/openingBook";
import {
  startGame,
  endGame,
  fetchCurrentRating,
  recordBlunder,
  recordManualBlunder,
  reviewSrsBlunder,
  uploadSessionMoves,
  type RatingChange,
  type SessionMoveUpload,
} from "../utils/api";
import { shouldRecordBlunder } from "../utils/blunder";
import {
  classifyMove,
  classifySessionMove,
  isWithinRecordingMoveCap,
  toWhitePerspective,
} from "../workers/analysisUtils";
import EvalBar from "./EvalBar";
import MoveList from "./MoveList";
import type { MoveListBubble } from "./MoveList";

/** Maia3 ELO bins – must match backend/app/maia3_client.py:ELO_BINS */
const MAIA_ELO_BINS = [
  600, 800, 1000, 1100, 1200, 1300, 1400, 1500,
  1600, 1700, 1800, 1900, 2000, 2200, 2400, 2600,
] as const;

const GhostIcon = () => (
  <svg
    className="ghost-icon"
    width="24"
    height="24"
    viewBox="0 0 24 24"
    fill="currentColor"
    aria-hidden="true"
  >
    <path d="M12 2C7.58 2 4 5.58 4 10v10.5c0 .83 1 1.25 1.59.66l1.41-1.41 1.41 1.41a.996.996 0 0 0 1.41 0L11.24 19.75l1.41 1.41a.996.996 0 0 0 1.41 0l1.41-1.41 1.41 1.41c.59.59 1.59.17 1.59-.66V10c0-4.42-3.58-8-8-8Zm-2 11a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3Zm4 0a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3Z" />
  </svg>
);

const WarningTriangleIcon = () => (
  <svg
    className="review-warning-toast__icon"
    width="48"
    height="48"
    viewBox="0 0 24 24"
    fill="currentColor"
    aria-hidden="true"
  >
    <path d="M1 21h22L12 2 1 21Zm12-3h-2v-2h2v2Zm0-4h-2v-4h2v4Z" />
  </svg>
);

type BoardOrientation = "white" | "black";

type MoveRecord = {
  san: string;
  fen: string; // Position after this move
  uci: string;
};

type GameResult = {
  type: "checkmate_win" | "checkmate_loss" | "draw" | "resign";
  message: string;
};

type BlunderAlert = {
  moveSan: string;
  moveUci: string;
  bestMoveUci: string;
  bestMoveSan: string;
  delta: number;
};

type ReviewFailInfo = {
  userMoveSan: string;
  bestMoveSan: string;
  userMoveUci: string;
  bestMoveUci: string;
  evalLoss: number;
  moveIndex: number;
};

type OpenHistoryOptions = {
  select: "latest";
  source: "post_game_view_analysis" | "post_game_history";
};

type ChessGameProps = {
  onOpenHistory?: (options: OpenHistoryOptions) => void;
};

const STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const ANALYSIS_UPLOAD_TIMEOUT_MS = 6000;
const SRS_REVIEW_FAIL_THRESHOLD_CP = 50;

const isSquare = (value: string): value is Square => /^[a-h][1-8]$/.test(value);

const sleep = (ms: number) =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, ms);
  });

const ChessGame = ({ onOpenHistory }: ChessGameProps = {}) => {
  const chess = useMemo(() => new Chess(), []);
  const [fen, setFen] = useState(chess.fen());
  const [boardOrientation, setBoardOrientation] =
    useState<BoardOrientation>("white");
  const [playerColor, setPlayerColor] = useState<BoardOrientation>("white");
  const [playerColorChoice, setPlayerColorChoice] = useState<
    BoardOrientation | "random"
  >("random");
  const [engineElo, setEngineElo] = useState(800);
  const [moveHistory, setMoveHistory] = useState<MoveRecord[]>([]);
  const [viewIndex, setViewIndex] = useState<number | null>(null); // null = viewing live position
  const {
    status: engineStatus,
    error: engineError,
    info: engineInfo,
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
    analyzingMove,
    clearAnalysis,
  } = useMoveAnalysis();
  const [engineMessage, setEngineMessage] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [isGameActive, setIsGameActive] = useState(false);
  const [liveOpening, setLiveOpening] = useState<OpeningLookupResult | null>(
    null,
  );
  const [gameResult, setGameResult] = useState<GameResult | null>(null);
  const [isStartingGame, setIsStartingGame] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [showStartOverlay, setShowStartOverlay] = useState(false);
  const [isAddingToLibrary, setIsAddingToLibrary] = useState(false);
  const [blunderAlert, setBlunderAlert] = useState<BlunderAlert | null>(null);
  const [showFlash, setShowFlash] = useState(false);
  const [blunderReviewId, setBlunderReviewId] = useState<number | null>(null);
  const [showPassToast, setShowPassToast] = useState(false);
  const [showRehookToast, setShowRehookToast] = useState(false);
  const [reviewFailModal, setReviewFailModal] = useState<ReviewFailInfo | null>(null);
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
  } | null>(null);
  const openingLookupRequestIdRef = useRef(0);
  const analysisMapRef = useRef<Map<number, AnalysisResult>>(new Map());
  const moveHistoryRef = useRef<MoveRecord[]>([]);
  const analysisStatusRef = useRef(analysisStatus);
  const isAnalyzingRef = useRef(isAnalyzing);
  const uploadedAnalysisSessionsRef = useRef<Set<string>>(new Set());
  const previousOpponentModeRef = useRef<"ghost" | "engine" | null>(null);

  useEffect(() => {
    fetchCurrentRating()
      .then((data) => {
        setPlayerRating(data.current_rating);
        setIsProvisional(data.is_provisional);
      })
      .catch(() => {});
  }, []);

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

  // Enrich moves with analysis data for MoveList annotations
  const annotatedMoves = useMemo(() => {
    return moveHistory.map((m, i) => {
      const analysis = analysisMap.get(i);
      return {
        san: m.san,
        classification: analysis ? classifyMove(analysis.delta) : undefined,
        eval:
          analysis?.playedEval != null
            ? toWhitePerspective(analysis.playedEval, i)
            : undefined,
      };
    });
  }, [moveHistory, analysisMap]);

  // Compute arrows from review fail modal or blunder alert
  const blunderArrows = useMemo(() => {
    if (reviewFailModal) {
      return [
        {
          startSquare: reviewFailModal.userMoveUci.slice(0, 2),
          endSquare: reviewFailModal.userMoveUci.slice(2, 4),
          color: "rgba(248, 113, 113, 0.8)",
        },
        {
          startSquare: reviewFailModal.bestMoveUci.slice(0, 2),
          endSquare: reviewFailModal.bestMoveUci.slice(2, 4),
          color: "rgba(52, 211, 153, 0.8)",
        },
      ];
    }
    if (!blunderAlert) return [];
    return [
      {
        startSquare: blunderAlert.moveUci.slice(0, 2),
        endSquare: blunderAlert.moveUci.slice(2, 4),
        color: "rgba(248, 113, 113, 0.8)",
      },
      {
        startSquare: blunderAlert.bestMoveUci.slice(0, 2),
        endSquare: blunderAlert.bestMoveUci.slice(2, 4),
        color: "rgba(52, 211, 153, 0.8)",
      },
    ];
  }, [reviewFailModal, blunderAlert]);

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
    const selectedAnalysis = analysisMap.get(selectedMoveIndex);
    return toWhitePerspective(
      selectedAnalysis?.playedEval ?? null,
      selectedMoveIndex,
    );
  }, [analysisMap, selectedMoveIndex]);

  const canAddSelectedMove = useMemo(() => {
    if (!sessionId || selectedMoveIndex === null) {
      return false;
    }
    return isPlayerMoveIndex(selectedMoveIndex);
  }, [sessionId, selectedMoveIndex, isPlayerMoveIndex]);

  const parseUciToSan = useCallback(
    (fenBeforeMove: string, uciMove: string) => {
      if (!uciMove || uciMove === "(none)" || uciMove.length < 4) {
        return null;
      }

      try {
        const replay = new Chess(fenBeforeMove);
        const from = uciMove.slice(0, 2);
        const to = uciMove.slice(2, 4);
        const promotion = uciMove.slice(4) || undefined;
        const result = replay.move({ from, to, promotion });
        return result?.san ?? null;
      } catch {
        return null;
      }
    },
    [],
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
          sourcePiece != null && target != null && target.color !== sourcePiece.color;
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

  const buildSessionMoveUploads = useCallback(
    (
      history: MoveRecord[],
      analysesByIndex: Map<number, AnalysisResult>,
    ): SessionMoveUpload[] => {
      return history.map((move, index) => {
        const analysis = analysesByIndex.get(index);
        const fenBeforeMove =
          index === 0
            ? STARTING_FEN
            : (history[index - 1]?.fen ?? STARTING_FEN);

        return {
          move_number: Math.floor(index / 2) + 1,
          color: index % 2 === 0 ? "white" : "black",
          move_san: move.san,
          fen_after: move.fen,
          eval_cp: analysis?.playedEval ?? null,
          eval_mate: null,
          best_move_san: analysis
            ? parseUciToSan(fenBeforeMove, analysis.bestMove)
            : null,
          best_move_eval_cp: analysis?.bestEval ?? null,
          eval_delta: analysis?.delta ?? null,
          classification: classifySessionMove(analysis?.delta ?? null),
        };
      });
    },
    [parseUciToSan],
  );

  const waitForQueuedAnalyses = useCallback(async (expectedMoves: number) => {
    const analysisHasErrored = () => analysisStatusRef.current === "error";

    if (expectedMoves <= 0) {
      return;
    }

    if (analysisHasErrored() || analysisMapRef.current.size >= expectedMoves) {
      return;
    }

    const initialSize = analysisMapRef.current.size;
    await sleep(150);
    if (analysisHasErrored() || analysisMapRef.current.size >= expectedMoves) {
      return;
    }

    if (
      !isAnalyzingRef.current &&
      analysisMapRef.current.size === initialSize
    ) {
      return;
    }

    const deadline = Date.now() + ANALYSIS_UPLOAD_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (
        analysisHasErrored() ||
        analysisMapRef.current.size >= expectedMoves
      ) {
        return;
      }

      if (!isAnalyzingRef.current) {
        const sizeBeforeIdleCheck = analysisMapRef.current.size;
        await sleep(100);
        if (analysisMapRef.current.size === sizeBeforeIdleCheck) {
          return;
        }
      } else {
        await sleep(50);
      }
    }
  }, []);

  const uploadSessionAnalysisBatch = useCallback(
    async (targetSessionId: string, expectedMoveCount: number) => {
      if (uploadedAnalysisSessionsRef.current.has(targetSessionId)) {
        return;
      }

      await waitForQueuedAnalyses(expectedMoveCount);

      const historySnapshot = [...moveHistoryRef.current];
      if (historySnapshot.length === 0) {
        uploadedAnalysisSessionsRef.current.add(targetSessionId);
        return;
      }

      const analysesSnapshot = new Map(analysisMapRef.current);
      const payload = buildSessionMoveUploads(
        historySnapshot,
        analysesSnapshot,
      );
      await uploadSessionMoves(targetSessionId, payload);
      uploadedAnalysisSessionsRef.current.add(targetSessionId);
    },
    [buildSessionMoveUploads, waitForQueuedAnalyses],
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

  const handleGameEnd = useCallback(async () => {
    if (!sessionId || !isGameActive) return;

    let result: GameResult | null = null;

    if (chess.isCheckmate()) {
      const loser = chess.turn() === "w" ? "white" : "black";
      const playerWon = playerColor !== loser;
      result = playerWon
        ? { type: "checkmate_win", message: "Checkmate! You won!" }
        : { type: "checkmate_loss", message: "Checkmate! You lost." };
    } else if (chess.isStalemate()) {
      result = { type: "draw", message: "Stalemate! The game is a draw." };
    } else if (chess.isThreefoldRepetition()) {
      result = { type: "draw", message: "Draw by threefold repetition." };
    } else if (chess.isInsufficientMaterial()) {
      result = { type: "draw", message: "Draw by insufficient material." };
    } else if (chess.isDraw()) {
      result = { type: "draw", message: "The game is a draw." };
    }

    if (result) {
      try {
        try {
          await uploadSessionAnalysisBatch(sessionId, moveCountRef.current);
        } catch (uploadError) {
          console.error(
            "[SessionMoves] Failed to upload session moves:",
            uploadError,
          );
        }
        const endResponse = await endGame(sessionId, result.type, chess.pgn(), isRated);
        if (endResponse.rating) {
          setRatingChange(endResponse.rating);
          setPlayerRating(endResponse.rating.rating_after);
          setIsProvisional(endResponse.rating.is_provisional);
        }
        setIsGameActive(false);
        setGameResult(result);
        setShowPostGamePrompt(true);
      } catch (error) {
        const message =
          error instanceof Error ? error.message : "Failed to end game.";
        setEngineMessage(message);
      }
    }
  }, [chess, isGameActive, isRated, playerColor, sessionId, uploadSessionAnalysisBatch]);

  const isPlayersTurn = chess.turn() === (playerColor === "white" ? "w" : "b");
  const moveCount = moveHistory.length;
  const isReviewMomentActive =
    blunderReviewId !== null &&
    isGameActive &&
    isPlayersTurn &&
    isViewingLive &&
    !chess.isGameOver();

  const statusText = (() => {
    if (chess.isCheckmate()) {
      const winningColor = chess.turn() === "w" ? "Black" : "White";
      return `${winningColor} wins by checkmate`;
    }

    if (chess.isDraw()) {
      return "Drawn position";
    }

    if (chess.isGameOver()) {
      return "Game over";
    }

    const active = chess.turn() === "w" ? "White" : "Black";
    const suffix = chess.inCheck() ? " (check)" : "";
    return `${active} to move${suffix}`;
  })();


  const moveBubble = useMemo((): MoveListBubble | null => {
    if (isAnalyzing && analyzingMove) {
      const idx = moveHistory.length - 1;
      if (idx < 0) return null;
      return { moveIndex: idx, text: `Analyzing ${analyzingMove}\u2026`, variant: "analyzing" };
    }

    if (!lastAnalysis || lastAnalysis.moveIndex == null) return null;

    const idx = lastAnalysis.moveIndex;
    const delta = lastAnalysis.delta;

    if (lastAnalysis.blunder && delta !== null) {
      return {
        moveIndex: idx,
        text: `Blunder! Lost ${Math.max(delta, 0)}cp. Best: ${lastAnalysis.bestMove}`,
        variant: "blunder",
      };
    }

    if (delta !== null) {
      if (delta === 0) return { moveIndex: idx, text: "Best move!", variant: "best" };
      if (delta < 0) return { moveIndex: idx, text: `Great move! Gained ${Math.abs(delta)}cp`, variant: "great" };
      if (delta < 50) return { moveIndex: idx, text: `Good. Lost ${delta}cp`, variant: "good" };
      return { moveIndex: idx, text: `Inaccuracy. Lost ${delta}cp. Best: ${lastAnalysis.bestMove}`, variant: "inaccuracy" };
    }

    return null;
  }, [isAnalyzing, analyzingMove, lastAnalysis, moveHistory.length]);

  const opponentColor = playerColor === "white" ? "black" : "white";

  const applyEngineMove = useCallback(async () => {
    try {
      const fenBeforeMove = chess.fen();
      const legalMoveCount = chess.moves().length;
      const result = await evaluatePosition(fenBeforeMove);

      if (result.move === "(none)") {
        setEngineMessage("Stockfish has no legal moves.");
        return;
      }

      const from = result.move.slice(0, 2);
      const to = result.move.slice(2, 4);
      const promotion = result.move.slice(4) || undefined;
      const appliedMove = chess.move({ from, to, promotion });

      if (!appliedMove) {
        throw new Error(`Engine returned illegal move: ${result.move}`);
      }

      const newFen = chess.fen();
      const moveIndex = moveCountRef.current++;
      const uciMove = `${appliedMove.from}${appliedMove.to}${appliedMove.promotion ?? ""}`;
      const nextMove = { san: appliedMove.san, fen: newFen, uci: uciMove };
      const nextMoveHistory = [...moveHistoryRef.current, nextMove];
      moveHistoryRef.current = nextMoveHistory;
      setFen(newFen);
      setMoveHistory(nextMoveHistory);
      setViewIndex(null); // Ensure we're viewing the live position
      setEngineMessage(null);
      analyzeMove(fenBeforeMove, uciMove, opponentColor, moveIndex, legalMoveCount);

      // Check if the engine's move ended the game
      if (chess.isGameOver()) {
        await handleGameEnd();
      }
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "Unable to apply Stockfish move.";
      setEngineMessage(message);
    }
  }, [chess, evaluatePosition, handleGameEnd, analyzeMove, opponentColor]);

  const applyGhostMove = useCallback(
    async (sanMove: string, targetBlunderId: number | null) => {
      try {
        const fenBeforeMove = chess.fen();
        const legalMoveCount = chess.moves().length;
        const appliedMove = chess.move(sanMove);

        if (!appliedMove) {
          throw new Error(`Ghost returned illegal move: ${sanMove}`);
        }

        const newFen = chess.fen();
        const moveIndex = moveCountRef.current++;
        const uciMove = `${appliedMove.from}${appliedMove.to}${appliedMove.promotion ?? ""}`;
        const nextMove = { san: appliedMove.san, fen: newFen, uci: uciMove };
        const nextMoveHistory = [...moveHistoryRef.current, nextMove];
        moveHistoryRef.current = nextMoveHistory;
        setFen(newFen);
        setMoveHistory(nextMoveHistory);
        setViewIndex(null);
        setEngineMessage(null);

        // Mark position as under review if ghost-move targets a blunder
        // and it's now the player's turn (side-to-move matches playerColor)
        const sideToMove = chess.turn() === "w" ? "white" : "black";
        if (targetBlunderId !== null && sideToMove === playerColor) {
          setBlunderReviewId(targetBlunderId);
        } else {
          setBlunderReviewId(null);
        }

        analyzeMove(fenBeforeMove, uciMove, opponentColor, moveIndex, legalMoveCount);

        if (chess.isGameOver()) {
          await handleGameEnd();
        }
      } catch (error) {
        const message =
          error instanceof Error
            ? error.message
            : "Unable to apply ghost move.";
        setEngineMessage(message);
      }
    },
    [chess, handleGameEnd, analyzeMove, opponentColor, playerColor],
  );

  const { opponentMode, applyOpponentMove, resetMode } = useOpponentMove({
    sessionId,
    onApplyBackendMove: applyGhostMove,
    onApplyLocalFallback: applyEngineMove,
  });

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
        let move = null;
        try {
          const fenBeforeMove = chess.fen();
          const legalMoveCount = chess.moves().length;
          move = chess.move({
            from: selectedSquare,
            to: square,
            promotion: "q",
          });

          if (move) {
            clearMoveHighlights();
            setBlunderAlert(null);

            const newFen = chess.fen();
            const moveIndex = moveCountRef.current++;
            const uciMove = `${move.from}${move.to}${move.promotion ?? ""}`;
            const nextMove = { san: move.san, fen: newFen, uci: uciMove };
            const nextMoveHistory = [...moveHistoryRef.current, nextMove];
            moveHistoryRef.current = nextMoveHistory;
            setFen(newFen);
            setMoveHistory(nextMoveHistory);
            setViewIndex(null);

            if (blunderReviewId !== null) {
              pendingSrsReviewRef.current = {
                blunderId: blunderReviewId,
                moveIndex,
                userMoveSan: move.san,
              };
              setBlunderReviewId(null);
            }

            pendingAnalysisContextRef.current = {
              fen: fenBeforeMove,
              pgn: chess.pgn(),
              moveSan: move.san,
              moveUci: uciMove,
              moveIndex,
            };
            analyzeMove(fenBeforeMove, uciMove, playerColor, moveIndex, legalMoveCount);

            if (chess.isGameOver()) {
              void handleGameEnd();
            } else {
              void applyOpponentMove(newFen, moveHistoryRef.current.map(m => m.uci));
            }
            return;
          }
        } catch {
          // Invalid move (e.g. clicking a friendly piece) — fall through
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
      blunderReviewId,
      chess,
      isGameActive,
      isViewingLive,
      selectedSquare,
      playerColor,
      analyzeMove,
      applyOpponentMove,
      clearMoveHighlights,
      getMoveOptions,
      handleGameEnd,
    ],
  );

  useEffect(() => {
    if (!isGameActive) {
      openingLookupRequestIdRef.current += 1;
      setLiveOpening(null);
      return;
    }

    const requestId = openingLookupRequestIdRef.current + 1;
    openingLookupRequestIdRef.current = requestId;
    void lookupOpeningByFen(fen)
      .then((opening) => {
        if (openingLookupRequestIdRef.current !== requestId) {
          return;
        }
        setLiveOpening(opening ?? null);
      })
      .catch(() => {
        if (openingLookupRequestIdRef.current !== requestId) {
          return;
        }
      });
  }, [fen, isGameActive]);

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

    void applyOpponentMove(chess.fen(), moveHistoryRef.current.map(m => m.uci));
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
      setShowPassToast(true);
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

      setReviewFailModal({
        userMoveSan: pendingReview.userMoveSan,
        bestMoveSan,
        userMoveUci: lastAnalysis.move,
        bestMoveUci: lastAnalysis.bestMove,
        evalLoss: evalLossCp,
        moveIndex: lastAnalysis.moveIndex,
      });
      setViewIndex(lastAnalysis.moveIndex - 1);
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

  // Auto-dismiss pass toast after 3 seconds
  useEffect(() => {
    if (!showPassToast) return;
    const timer = setTimeout(() => setShowPassToast(false), 3000);
    return () => clearTimeout(timer);
  }, [showPassToast]);

  // Auto-dismiss re-hook toast after 3 seconds
  useEffect(() => {
    if (!showRehookToast) return;
    const timer = setTimeout(() => setShowRehookToast(false), 3000);
    return () => clearTimeout(timer);
  }, [showRehookToast]);

  const handleDrop = ({ sourceSquare, targetSquare }: PieceDropHandlerArgs) => {
    if (!targetSquare) {
      return false;
    }

    if (!isPlayersTurn || !isViewingLive) {
      return false;
    }

    const fenBeforeMove = chess.fen();
    const legalMoveCount = chess.moves().length;
    const move = chess.move({
      from: sourceSquare,
      to: targetSquare,
      promotion: "q",
    });

    if (!move) {
      return false;
    }

    clearMoveHighlights();
    setBlunderAlert(null);

    const newFen = chess.fen();
    const moveIndex = moveCountRef.current++;
    const uciMove = `${move.from}${move.to}${move.promotion ?? ""}`;
    const nextMove = { san: move.san, fen: newFen, uci: uciMove };
    const nextMoveHistory = [...moveHistoryRef.current, nextMove];
    moveHistoryRef.current = nextMoveHistory;
    setFen(newFen);
    setMoveHistory(nextMoveHistory);
    setViewIndex(null); // Ensure we're viewing the live position

    if (blunderReviewId !== null) {
      pendingSrsReviewRef.current = {
        blunderId: blunderReviewId,
        moveIndex,
        userMoveSan: move.san,
      };
      setBlunderReviewId(null);
    }

    // Store context for blunder detection before engine moves
    pendingAnalysisContextRef.current = {
      fen: fenBeforeMove,
      pgn: chess.pgn(),
      moveSan: move.san,
      moveUci: uciMove,
      moveIndex,
    };
    analyzeMove(fenBeforeMove, uciMove, playerColor, moveIndex, legalMoveCount);

    if (chess.isGameOver()) {
      void handleGameEnd();
    } else {
      void applyOpponentMove(newFen, moveHistoryRef.current.map(m => m.uci));
    }

    return true;
  };

  const handleDismissReviewFail = useCallback(() => {
    setReviewFailModal(null);
    setViewIndex(null);
  }, []);

  const handleNewGame = async () => {
    try {
      setIsStartingGame(true);
      setStartError(null);
      // End current session if active
      if (sessionId && isGameActive) {
        try {
          await uploadSessionAnalysisBatch(sessionId, moveCountRef.current);
        } catch (uploadError) {
          console.error(
            "[SessionMoves] Failed to upload session moves:",
            uploadError,
          );
        }
        await endGame(sessionId, "abandon", chess.pgn(), isRated);
      }

      // Start new game session
      const resolvedPlayerColor =
        playerColorChoice === "random"
          ? Math.random() < 0.5
            ? "white"
            : "black"
          : playerColorChoice;
      setPlayerColor(resolvedPlayerColor);
      setBoardOrientation(resolvedPlayerColor);

      const response = await startGame(engineElo, resolvedPlayerColor);
      setSessionId(response.session_id);
      setIsGameActive(true);
      setIsStartingGame(false);
      setShowStartOverlay(false);

      // Reset the board
      chess.reset();
      setFen(chess.fen());
      setEngineMessage(null);
      setGameResult(null);
      setRatingChange(null);
      setMoveHistory([]);
      moveCountRef.current = 0;
      setViewIndex(null);
      setLiveOpening(null);
      resetEngine();
      clearAnalysis();
      moveHistoryRef.current = [];
      uploadedAnalysisSessionsRef.current.clear();
      setBlunderAlert(null);
      setShowFlash(false);
      setBlunderReviewId(null);
      setShowPassToast(false);
      setReviewFailModal(null);
      setShowPostGamePrompt(false);
      setIsRated(true);
      setShowRevertWarning(false);
      clearMoveHighlights();
      blunderRecordedRef.current = false;
      pendingAnalysisContextRef.current = null;
      pendingSrsReviewRef.current = null;
      resetMode();
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Failed to start new game.";
      setEngineMessage(message);
      setStartError(message);
      setIsStartingGame(false);
    }
  };

  const handleResign = async () => {
    if (!sessionId || !isGameActive) {
      return;
    }

    try {
      try {
        await uploadSessionAnalysisBatch(sessionId, moveCountRef.current);
      } catch (uploadError) {
        console.error(
          "[SessionMoves] Failed to upload session moves:",
          uploadError,
        );
      }
      const endResponse = await endGame(sessionId, "resign", chess.pgn(), isRated);
      if (endResponse.rating) {
        setRatingChange(endResponse.rating);
        setPlayerRating(endResponse.rating.rating_after);
        setIsProvisional(endResponse.rating.is_provisional);
      }
      setIsGameActive(false);
      setGameResult({ type: "resign", message: "You resigned." });
      setShowPostGamePrompt(true);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Failed to resign game.";
      setEngineMessage(message);
    }
  };

  const executeRevert = useCallback(() => {
    if (!isGameActive || moveHistory.length === 0 || chess.isGameOver()) return;

    setIsRated(false);
    setShowRevertWarning(false);

    // Determine how many half-moves to undo:
    // If it's the player's turn, opponent already replied → undo 2 (opponent + player)
    // If it's the opponent's turn, player just moved → undo 1 (player only)
    const isPlayerTurn = chess.turn() === (playerColor === "white" ? "w" : "b");
    const undoCount = isPlayerTurn && moveHistory.length >= 2 ? 2 : 1;

    for (let i = 0; i < undoCount; i++) {
      chess.undo();
    }

    const newHistory = moveHistory.slice(0, -undoCount);
    moveHistoryRef.current = newHistory;
    moveCountRef.current = newHistory.length;
    setMoveHistory(newHistory);
    setFen(chess.fen());
    setViewIndex(null);
    setBlunderReviewId(null);
    setBlunderAlert(null);
    pendingSrsReviewRef.current = null;
    pendingAnalysisContextRef.current = null;
  }, [chess, isGameActive, moveHistory, playerColor]);

  const handleRevertClick = useCallback(() => {
    if (isRated) {
      setShowRevertWarning(true);
    } else {
      executeRevert();
    }
  }, [isRated, executeRevert]);

  const cancelRevert = useCallback(() => {
    setShowRevertWarning(false);
  }, []);

  const handleReset = () => {
    chess.reset();
    setFen(chess.fen());
    setBoardOrientation(playerColor);
    setEngineMessage(null);
    setSessionId(null);
    setIsGameActive(false);
    setGameResult(null);
    setMoveHistory([]);
    moveCountRef.current = 0;
    setViewIndex(null);
    setLiveOpening(null);
    resetEngine();
    clearAnalysis();
    moveHistoryRef.current = [];
    uploadedAnalysisSessionsRef.current.clear();
    setBlunderAlert(null);
    setShowFlash(false);
    setShowPassToast(false);
    setShowRehookToast(false);
    setReviewFailModal(null);
    setShowPostGamePrompt(false);
    setShowStartOverlay(false);
    setBlunderReviewId(null);
    setIsRated(true);
    setShowRevertWarning(false);
    clearMoveHighlights();
    blunderRecordedRef.current = false;
    pendingAnalysisContextRef.current = null;
    pendingSrsReviewRef.current = null;
    resetMode();
  };

  const flipBoard = () => {
    setBoardOrientation((current) => (current === "white" ? "black" : "white"));
  };

  const handleSelectPlayerColor = (color: BoardOrientation) => {
    setPlayerColorChoice(color);
    setPlayerColor(color);
    if (!isGameActive) {
      setBoardOrientation(color);
    }
  };

  const handleRandomColor = () => {
    setPlayerColorChoice("random");
  };

  const handleShowStartOverlay = () => {
    setPlayerColorChoice("random");
    setEngineElo(800);
    setShowPostGamePrompt(false);
    setShowStartOverlay(true);
  };

  const handleViewAnalysis = () => {
    setShowPostGamePrompt(false);
    onOpenHistory?.({ select: "latest", source: "post_game_view_analysis" });
  };

  const handleViewHistory = () => {
    setShowPostGamePrompt(false);
    onOpenHistory?.({ select: "latest", source: "post_game_history" });
  };

  const gameStatusBadge = (() => {
    if (isGameActive) {
      return { label: "Live", className: "game-status-badge--live" };
    }
    if (!gameResult) return null;
    switch (gameResult.type) {
      case "checkmate_win":
        return {
          label: "Win — Checkmate",
          className: "game-status-badge--win",
        };
      case "checkmate_loss":
        return {
          label: "Loss — Checkmate",
          className: "game-status-badge--loss",
        };
      case "draw":
        return { label: "Draw", className: "game-status-badge--other" };
      case "resign":
        return { label: "Resigned", className: "game-status-badge--other" };
      default:
        return null;
    }
  })();

  return (
    <section className="chess-section">
      <header className="chess-header">
        <p className="eyebrow">SRS Chess</p>
      </header>

      <div className="chess-layout">
        <div className="chess-panel" aria-live="polite">
          <p className="chess-status">{statusText}</p>
          {gameStatusBadge && (
            <span className={`game-status-badge ${gameStatusBadge.className}`}>
              {gameStatusBadge.label}
            </span>
          )}
          {!isRated && isGameActive && (
            <span className="unrated-badge">Unrated</span>
          )}
          <p className="chess-meta">
            Playing as:{" "}
            <span className="chess-meta-strong">
              {playerColorChoice === "random" && !isGameActive
                ? "Random"
                : playerColor === "white"
                  ? "White"
                  : "Black"}
            </span>
          </p>
          <p className="chess-meta">
            Rating:{" "}
            <span className="chess-meta-strong">
              {playerRating}
              {isProvisional ? "?" : ""}
            </span>
          </p>
          <p className="chess-meta">
            Session:{" "}
            <span className={isGameActive ? "chess-meta-strong" : ""}>
              {isGameActive ? "Active" : "None (click New game to start)"}
            </span>
          </p>
          {isGameActive && (
            <p
              className={`chess-meta${opponentMode === "ghost" ? " chess-meta--ghost" : ""}`}
            >
              Opponent:{" "}
              {opponentMode === "ghost" ? (
                <>
                  <GhostIcon />{" "}
                  <span className="chess-meta-strong ghost-mode-label">
                    Ghost
                  </span>
                </>
              ) : (
                <span className="chess-meta-strong">Engine</span>
              )}
            </p>
          )}
          {isGameActive && (
            <p className="chess-meta">
              Opening:{" "}
              <span className="chess-meta-strong">
                {liveOpening
                  ? `${liveOpening.eco} ${liveOpening.name}`
                  : "Unknown"}
              </span>
            </p>
          )}
          {isReviewMomentActive && (
            <div className="review-warning-toast" role="alert">
              <div className="review-warning-toast__header">
                <WarningTriangleIcon />
                <span className="review-warning-toast__label">
                  Review Position
                </span>
              </div>
              <p className="review-warning-toast__detail">
                Be careful. You've messed this position up before.
              </p>
            </div>
          )}
          {reviewFailModal && (
            <div className="review-fail-panel" role="alert">
              <span className="review-fail-panel__label">
                You made this mistake again!
              </span>
              <p className="review-fail-panel__detail">
                You played:{" "}
                <strong className="review-fail-panel__bad">
                  {reviewFailModal.userMoveSan}
                </strong>
              </p>
              <p className="review-fail-panel__detail">
                Best was:{" "}
                <span className="review-fail-panel__best">
                  {reviewFailModal.bestMoveSan}
                </span>
              </p>
              <button
                className="chess-button primary"
                type="button"
                onClick={handleDismissReviewFail}
              >
                Continue
              </button>
            </div>
          )}
          <div className="chess-controls">
            <button
              className="chess-button danger"
              type="button"
              onClick={handleResign}
              disabled={!isGameActive || chess.isGameOver()}
            >
              Resign
            </button>
            {isGameActive && (
              <button
                className="chess-button"
                type="button"
                onClick={handleRevertClick}
                disabled={moveHistory.length === 0 || chess.isGameOver()}
              >
                Revert
              </button>
            )}
            <button className="chess-button" type="button" onClick={flipBoard}>
              Flip board
            </button>
            <button
              className="chess-button"
              type="button"
              onClick={handleReset}
            >
              Reset
            </button>
          </div>
        </div>

        <div className="chessboard-wrapper">
          <div className="chessboard-board-with-eval">
            <EvalBar
              whitePerspectiveCp={selectedEvalCp}
              whiteOnBottom={boardOrientation === "white"}
            />
            <div className="chessboard-board-area">
            {showStartOverlay && !isGameActive && (
              <div className="chessboard-overlay">
                <div className="chess-start-panel">
                  <button
                    className="chess-start-close"
                    type="button"
                    onClick={() => setShowStartOverlay(false)}
                    disabled={isStartingGame}
                    aria-label="Close"
                  >
                    ×
                  </button>
                  <p className="chess-start-title">Difficulty</p>
                  <div className="chess-elo-selector">
                    <input
                      type="range"
                      min={0}
                      max={MAIA_ELO_BINS.length - 1}
                      step={1}
                      value={MAIA_ELO_BINS.indexOf(engineElo)}
                      onChange={(e) => setEngineElo(MAIA_ELO_BINS[Number(e.target.value)])}
                      disabled={isStartingGame}
                      className="chess-elo-slider"
                    />
                    <span className="chess-elo-label">{engineElo}</span>
                  </div>
                  <p className="chess-start-title">Side</p>
                  <div className="chess-start-options">
                    <button
                      className={`chess-button toggle ${playerColorChoice === "white" ? "active" : ""}`}
                      type="button"
                      onClick={() => handleSelectPlayerColor("white")}
                      aria-pressed={playerColorChoice === "white"}
                      disabled={isStartingGame}
                    >
                      Play White
                    </button>
                    <button
                      className={`chess-button toggle ${playerColorChoice === "random" ? "active" : ""}`}
                      type="button"
                      onClick={handleRandomColor}
                      aria-pressed={playerColorChoice === "random"}
                      disabled={isStartingGame}
                    >
                      Play Random
                    </button>
                    <button
                      className={`chess-button toggle ${playerColorChoice === "black" ? "active" : ""}`}
                      type="button"
                      onClick={() => handleSelectPlayerColor("black")}
                      aria-pressed={playerColorChoice === "black"}
                      disabled={isStartingGame}
                    >
                      Play Black
                    </button>
                  </div>
                  {startError && (
                    <p className="chess-start-error">{startError}</p>
                  )}
                  <button
                    className="chess-button primary overlay-button"
                    type="button"
                    onClick={handleNewGame}
                    disabled={isStartingGame}
                  >
                    {isStartingGame ? "Starting…" : "Play"}
                  </button>
                </div>
              </div>
            )}
            {showRevertWarning && (
              <div className="chessboard-overlay">
                <div className="revert-warning-dialog" role="alertdialog" aria-labelledby="revert-warning-title">
                  <WarningTriangleIcon />
                  <p id="revert-warning-title" className="revert-warning-dialog__title">
                    This game will not be rated
                  </p>
                  <p className="revert-warning-dialog__body">
                    Reverting a move removes this game from your rating history. This cannot be undone.
                  </p>
                  <div className="revert-warning-dialog__actions">
                    <button
                      className="chess-button danger"
                      type="button"
                      onClick={executeRevert}
                    >
                      Revert anyway
                    </button>
                    <button
                      className="chess-button"
                      type="button"
                      onClick={cancelRevert}
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              </div>
            )}
            {!isGameActive && gameResult && !showStartOverlay && (
              <div className="chessboard-ended-scrim" />
            )}
            {showFlash && <div className="blunder-flash" />}
            <Chessboard
              options={{
                position: displayedFen,
                onPieceDrop: handleDrop,
                onSquareClick: handleSquareClick,
                boardOrientation,
                animationDurationInMs: 200,
                allowDragging:
                  isGameActive &&
                  engineStatus === "ready" &&
                  isPlayersTurn &&
                  !isThinking &&
                  isViewingLive,
                squareStyles: optionSquares,
                arrows: blunderArrows.length > 0 ? blunderArrows : undefined,
                boardStyle: {
                  borderRadius: "0",
                  boxShadow: "0 20px 45px rgba(2, 6, 23, 0.5)",
                },
              }}
            />
            {blunderAlert && (
              <div
                className="blunder-toast"
                onClick={() => setBlunderAlert(null)}
                role="alert"
              >
                <div className="blunder-toast__header">
                  <span>Blunder</span>
                  <span className="blunder-toast__delta">
                    &minus;{(blunderAlert.delta / 100).toFixed(1)}
                  </span>
                </div>
                <p className="blunder-toast__detail">
                  You played: <strong>{blunderAlert.moveSan}</strong>
                </p>
                <p className="blunder-toast__detail">
                  Best was:{" "}
                  <span className="blunder-toast__best">
                    {blunderAlert.bestMoveSan}
                  </span>
                </p>
              </div>
            )}
            {showPassToast && (
              <div
                className="review-pass-toast"
                onClick={() => setShowPassToast(false)}
                role="status"
              >
                <span className="review-pass-toast__label">Correct!</span>
                <p className="review-pass-toast__detail">
                  You avoided your past mistake.
                </p>
              </div>
            )}
            {showRehookToast && (
              <div
                className="rehook-toast"
                onClick={() => setShowRehookToast(false)}
                role="status"
              >
                <span className="rehook-toast__label">Ghost reactivated</span>
                <p className="rehook-toast__detail">
                  Ghost reactivated: steering to past mistake
                </p>
              </div>
            )}
            </div>
          </div>
          {showPostGamePrompt && gameResult && (
            <div
              className="game-end-banner"
              role="region"
              aria-label="Post-game options"
            >
              <p className="game-end-banner-message">{gameResult.message}</p>
              {ratingChange && (
                <p className={`rating-delta ${ratingChange.rating_after >= ratingChange.rating_before ? "rating-delta--up" : "rating-delta--down"}`}>
                  {ratingChange.rating_after >= ratingChange.rating_before ? "+" : ""}
                  {ratingChange.rating_after - ratingChange.rating_before}
                  {" "}
                  <span className="rating-delta__value">
                    ({ratingChange.rating_before} → {ratingChange.rating_after}{ratingChange.is_provisional ? "?" : ""})
                  </span>
                </p>
              )}
              <div className="chess-post-game-actions">
                <button
                  className="chess-button primary"
                  type="button"
                  onClick={handleViewAnalysis}
                >
                  View Analysis
                </button>
                <button
                  className="chess-button"
                  type="button"
                  onClick={handleShowStartOverlay}
                >
                  New Game
                </button>
                <button
                  className="chess-button"
                  type="button"
                  onClick={handleViewHistory}
                >
                  History
                </button>
              </div>
            </div>
          )}
          {!isGameActive && !showPostGamePrompt && (
            <div className="game-end-banner">
              <p className="game-end-banner-message">
                {gameResult ? gameResult.message : "Ready for a new game?"}
              </p>
              <button
                className="chess-button primary"
                type="button"
                onClick={handleShowStartOverlay}
              >
                New game
              </button>
            </div>
          )}
        </div>

        <MoveList
          moves={annotatedMoves}
          currentIndex={viewIndex}
          onNavigate={handleNavigate}
          canAddSelectedMove={canAddSelectedMove}
          isAddingSelectedMove={isAddingToLibrary}
          onAddSelectedMove={handleAddSelectedMove}
          bubble={moveBubble}
        />
      </div>
    </section>
  );
};

export default ChessGame;
