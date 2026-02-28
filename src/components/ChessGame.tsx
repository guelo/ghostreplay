import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Chess } from "chess.js";
import type { Square } from "chess.js";
import type { PieceDropHandlerArgs } from "react-chessboard";
import { useStockfishEngine } from "../hooks/useStockfishEngine";
import { useMoveAnalysis, type AnalysisResult } from "../hooks/useMoveAnalysis";
import { useChessGameController } from "../hooks/useChessGameController";
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
import { buildSessionMoveUploads } from "./chess-game/domain/sessionUpload";
import {
  deriveGameStatusBadge,
  deriveStatusText,
  type GameResult,
} from "./chess-game/domain/status";
import BoardStage from "./chess-game/ui/BoardStage";
import GameInfoPanel from "./chess-game/ui/GameInfoPanel";
import PostGameBanner from "./chess-game/ui/PostGameBanner";
import MaterialDisplay from "./MaterialDisplay";
import MoveList from "./MoveList";
import type { MoveListBubble } from "./MoveList";

/** Maia3 ELO bins – must match backend/app/maia3_client.py:ELO_BINS */
const MAIA_ELO_BINS = [
  600, 800, 1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900, 2000,
  2200, 2400, 2600,
] as const;

/** Compute expected Elo stakes (win/loss deltas) for the difficulty selector. */
function eloStakes(
  playerRating: number,
  opponentRating: number,
  isProvisional: boolean,
): { winDelta: number; lossDelta: number } {
  const k = isProvisional ? 40 : 20;
  const expected =
    1.0 / (1.0 + 10.0 ** ((opponentRating - playerRating) / 400.0));
  return {
    winDelta: Math.round(k * (1 - expected)),
    lossDelta: Math.round(k * (0 - expected)),
  };
}

/** Gaussian-sample a difficulty bin near the user's Elo (σ controls spread). */
function sampleEloBin(
  userElo: number,
  sigma = 125,
): (typeof MAIA_ELO_BINS)[number] {
  const weights = MAIA_ELO_BINS.map((bin) =>
    Math.exp(-((userElo - bin) ** 2) / (2 * sigma ** 2)),
  );
  const total = weights.reduce((a, b) => a + b, 0);
  let r = Math.random() * total;
  for (let i = 0; i < weights.length; i++) {
    r -= weights[i];
    if (r <= 0) return MAIA_ELO_BINS[i];
  }
  return MAIA_ELO_BINS[MAIA_ELO_BINS.length - 1];
}

const MAIA_BOT_NAMES: Record<(typeof MAIA_ELO_BINS)[number], string> = {
  600: "Boo Bud 600",
  800: "Wisp Cub 800",
  1000: "Phantom Puff 1000",
  1100: "Misty Paws 1100",
  1200: "Specter Scout 1200",
  1300: "Boo Bishop 1300",
  1400: "Wisp Gambit 1400",
  1500: "Phantom Tempo 1500",
  1600: "Misty Sharp 1600",
  1700: "Specter Prep 1700",
  1800: "Boo Tactician 1800",
  1900: "Wraith Endgame 1900",
  2000: "Ghost Master 2000",
  2200: "Phantom Engine 2200",
  2400: "Specter Legend 2400",
  2600: "Wraith Nova 2600",
};

type BoardOrientation = "white" | "black";

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
const BLUNDER_AUDIO_CLIPS = Array.from(
  { length: 10 },
  (_, index) => `/audio/blunder${index + 1}.m4a`,
);

const isSquare = (value: string): value is Square => /^[a-h][1-8]$/.test(value);

const sleep = (ms: number) =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, ms);
  });

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
    analyzingMove,
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
  const [showPassToast, setShowPassToast] = useState(false);
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
  } | null>(null);
  const openingLookupRequestIdRef = useRef(0);
  // Index 0 = starting position (before any move), index N = after move N
  const openingHistoryRef = useRef<(OpeningLookupResult | null)[]>([]);
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
        setEngineElo(sampleEloBin(data.current_rating));
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
        STARTING_FEN,
      );
      await uploadSessionMoves(targetSessionId, payload);
      uploadedAnalysisSessionsRef.current.add(targetSessionId);
    },
    [waitForQueuedAnalyses],
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
        const endResponse = await endGame(
          sessionId,
          result.type,
          chess.pgn(),
          isRated,
        );
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
  }, [
    chess,
    isGameActive,
    isRated,
    playerColor,
    sessionId,
    uploadSessionAnalysisBatch,
  ]);

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

  const moveBubble = useMemo((): MoveListBubble | null => {
    if (isAnalyzing && analyzingMove) {
      const idx = moveHistory.length - 1;
      if (idx < 0) return null;
      return {
        moveIndex: idx,
        text: `Analyzing ${analyzingMove}\u2026`,
        variant: "analyzing",
      };
    }

    if (!lastAnalysis || lastAnalysis.moveIndex == null) return null;
    if (!isPlayerMoveIndex(lastAnalysis.moveIndex)) return null;

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
      if (delta === 0)
        return { moveIndex: idx, text: "Best move!", variant: "best" };
      if (delta < 0)
        return {
          moveIndex: idx,
          text: `Great move! Gained ${Math.abs(delta)}cp`,
          variant: "great",
        };
      if (delta < 50)
        return {
          moveIndex: idx,
          text: `Good. Lost ${delta}cp`,
          variant: "good",
        };
      return {
        moveIndex: idx,
        text: `Inaccuracy. Lost ${delta}cp. Best: ${lastAnalysis.bestMove}`,
        variant: "inaccuracy",
      };
    }

    return null;
  }, [
    isAnalyzing,
    analyzingMove,
    lastAnalysis,
    moveHistory.length,
    isPlayerMoveIndex,
  ]);

  const opponentColor = playerColor === "white" ? "black" : "white";

  const { applyPlayerMove, handleDrop, applyEngineMove, applyGhostMove } =
    useChessGameController({
      chess,
      playerColor,
      opponentColor,
      isPlayersTurn,
      isViewingLive,
      blunderReviewId,
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
      handleGameEnd,
      clearMoveHighlights,
    });

  const { opponentMode, applyOpponentMove, resetMode } = useOpponentMove({
    sessionId,
    onApplyBackendMove: applyGhostMove,
    onApplyLocalFallback: applyEngineMove,
  });

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

  const handleDismissReviewFail = useCallback(() => {
    setReviewFailModal(null);
    setViewIndex(null);
  }, []);

  const handleNewGame = async (colorOverride?: BoardOrientation | "random") => {
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
      const effectiveChoice = colorOverride ?? playerColorChoice;
      const resolvedPlayerColor =
        effectiveChoice === "random"
          ? Math.random() < 0.5
            ? "white"
            : "black"
          : effectiveChoice;
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
      openingHistoryRef.current = [];
      resetEngine();
      clearAnalysis();
      moveHistoryRef.current = [];
      uploadedAnalysisSessionsRef.current.clear();
      setBlunderAlert(null);
      setShowFlash(false);
      setBlunderReviewId(null);
      setBlunderReviewSrs(null);
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
      const endResponse = await endGame(
        sessionId,
        "resign",
        chess.pgn(),
        isRated,
      );
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
    setBlunderReviewSrs(null);
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
    openingHistoryRef.current = [];
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
    setShowStartOverlay(true);
    setBlunderReviewId(null);
    setBlunderReviewSrs(null);
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

  const handleShowStartOverlay = () => {
    setPlayerColorChoice("random");
    setEngineElo(sampleEloBin(playerRating));
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
          reviewFailModal={reviewFailModal}
          onDismissReviewFail={handleDismissReviewFail}
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
            blunderAlert={blunderAlert}
            onDismissBlunderAlert={() => setBlunderAlert(null)}
            showPassToast={showPassToast}
            onDismissPassToast={() => setShowPassToast(false)}
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
            bubble={moveBubble}
            playerColor={playerColor}
          />
          <MaterialDisplay fen={displayedFen} perspective={playerColor} />
        </div>
      </div>
    </section>
  );
};

export default ChessGame;
