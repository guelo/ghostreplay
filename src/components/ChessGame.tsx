import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Chess } from "chess.js";
import { Chessboard } from "react-chessboard";
import type { PieceDropHandlerArgs } from "react-chessboard";
import { useStockfishEngine } from "../hooks/useStockfishEngine";
import { useMoveAnalysis } from "../hooks/useMoveAnalysis";
import { useOpponentMove } from "../hooks/useOpponentMove";
import type { OpeningLookupResult } from "../openings/openingBook";
import { lookupOpeningByFen } from "../openings/openingBook";
import { startGame, endGame, recordBlunder } from "../utils/api";
import { shouldRecordBlunder } from "../utils/blunder";
import { classifyMove, toWhitePerspective } from "../workers/analysisUtils";
import MoveList from "./MoveList";

type BoardOrientation = "white" | "black";

type MoveRecord = {
  san: string;
  fen: string; // Position after this move
};

const formatScore = (score?: { type: "cp" | "mate"; value: number }) => {
  if (!score) {
    return null;
  }

  if (score.type === "mate") {
    return `M${score.value}`;
  }

  const value = score.value / 100;
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
};

type GameResult = {
  type: "checkmate_win" | "checkmate_loss" | "draw" | "resign";
  message: string;
};

const STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

const ChessGame = () => {
  const chess = useMemo(() => new Chess(), []);
  const [fen, setFen] = useState(chess.fen());
  const [boardOrientation, setBoardOrientation] =
    useState<BoardOrientation>("white");
  const [playerColor, setPlayerColor] = useState<BoardOrientation>("white");
  const [playerColorChoice, setPlayerColorChoice] = useState<
    BoardOrientation | "random"
  >("white");
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
    null
  );
  const [gameResult, setGameResult] = useState<GameResult | null>(null);
  const [isStartingGame, setIsStartingGame] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [showStartOverlay, setShowStartOverlay] = useState(false);

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
  } | null>(null);
  const openingLookupRequestIdRef = useRef(0);

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

  // Whether the user can make moves (must be viewing live position)
  const isViewingLive = viewIndex === null;

  const handleNavigate = useCallback((index: number | null) => {
    setViewIndex(index);
  }, []);

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
        await endGame(sessionId, result.type, chess.pgn());
        setIsGameActive(false);
        setGameResult(result);
      } catch (error) {
        const message =
          error instanceof Error ? error.message : "Failed to end game.";
        setEngineMessage(message);
      }
    }
  }, [chess, isGameActive, playerColor, sessionId]);

  const isPlayersTurn = chess.turn() === (playerColor === "white" ? "w" : "b");
  const moveCount = moveHistory.length;

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

  const engineStatusText = (() => {
    if (engineError) {
      return engineError;
    }

    if (engineMessage) {
      return engineMessage;
    }

    if (engineStatus === "booting") {
      return "Stockfish is warming up…";
    }

    if (engineStatus === "error") {
      return "Stockfish is unavailable.";
    }

    if (isThinking) {
      const formattedScore = formatScore(engineInfo?.score);
      const parts = [
        "Stockfish is thinking…",
        engineInfo?.depth ? `depth ${engineInfo.depth}` : null,
        formattedScore ? `eval ${formattedScore}` : null,
      ].filter(Boolean);
      return parts.join(" · ");
    }

    return "Stockfish is ready.";
  })();

  const analysisStatusText = (() => {
    if (analysisStatus === "booting") {
      return "Analyst is warming up…";
    }

    if (analysisStatus === "error") {
      return "Analyst is unavailable.";
    }

    if (isAnalyzing && analyzingMove) {
      return `Analyzing ${analyzingMove}…`;
    }

    if (!lastAnalysis) {
      return "Analyst is ready.";
    }

    const lastMoveIndex =
      lastAnalysis.moveIndex ?? (moveHistory.length > 0 ? moveHistory.length - 1 : null);
    const whitePerspectiveEval = toWhitePerspective(
      lastAnalysis.currentPositionEval,
      lastMoveIndex,
    );
    const evalText =
      whitePerspectiveEval !== null
        ? ` Eval: ${whitePerspectiveEval > 0 ? "+" : ""}${(whitePerspectiveEval / 100).toFixed(2)}`
        : "";

    if (lastAnalysis.blunder && lastAnalysis.delta !== null) {
      return `⚠️ ${lastAnalysis.move}: Blunder! Lost ${Math.max(lastAnalysis.delta, 0)}cp. Best: ${lastAnalysis.bestMove}.${evalText}`;
    }

    if (lastAnalysis.delta !== null) {
      if (lastAnalysis.delta === 0) {
        return `✓ ${lastAnalysis.move}: Best move!${evalText}`;
      }
      if (lastAnalysis.delta < 0) {
        return `✓ ${lastAnalysis.move}: Great move! Gained ${Math.abs(lastAnalysis.delta)}cp. Best: ${lastAnalysis.bestMove}.${evalText}`;
      }
      if (lastAnalysis.delta < 50) {
        return `✓ ${lastAnalysis.move}: Good move. Lost ${lastAnalysis.delta}cp. Best: ${lastAnalysis.bestMove}.${evalText}`;
      }
      return `${lastAnalysis.move}: Inaccuracy. Lost ${lastAnalysis.delta}cp. Best: ${lastAnalysis.bestMove}.${evalText}`;
    }

    return "Analyst is ready.";
  })();

  const opponentColor = playerColor === "white" ? "black" : "white";

  const applyEngineMove = useCallback(async () => {
    try {
      const fenBeforeMove = chess.fen();
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
      setFen(newFen);
      setMoveHistory((prev) => [
        ...prev,
        { san: appliedMove.san, fen: newFen },
      ]);
      setViewIndex(null); // Ensure we're viewing the live position
      setEngineMessage(null);

      const uciMove = `${appliedMove.from}${appliedMove.to}${appliedMove.promotion ?? ""}`;
      analyzeMove(fenBeforeMove, uciMove, opponentColor, moveIndex);

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
    async (sanMove: string) => {
      try {
        const fenBeforeMove = chess.fen();
        const appliedMove = chess.move(sanMove);

        if (!appliedMove) {
          throw new Error(`Ghost returned illegal move: ${sanMove}`);
        }

        const newFen = chess.fen();
        const moveIndex = moveCountRef.current++;
        setFen(newFen);
        setMoveHistory((prev) => [
          ...prev,
          { san: appliedMove.san, fen: newFen },
        ]);
        setViewIndex(null);
        setEngineMessage(null);

        const uciMove = `${appliedMove.from}${appliedMove.to}${appliedMove.promotion ?? ""}`;
        analyzeMove(fenBeforeMove, uciMove, opponentColor, moveIndex);

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
    [chess, handleGameEnd, analyzeMove, opponentColor]
  );

  const { opponentMode, applyOpponentMove, resetMode } = useOpponentMove({
    sessionId,
    onApplyGhostMove: applyGhostMove,
    onApplyEngineMove: applyEngineMove,
  });

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
        if (opening) {
          setLiveOpening(opening);
        }
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

    void applyOpponentMove(chess.fen());
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

  const handleDrop = ({ sourceSquare, targetSquare }: PieceDropHandlerArgs) => {
    if (!targetSquare) {
      return false;
    }

    if (!isPlayersTurn || !isViewingLive) {
      return false;
    }

    const fenBeforeMove = chess.fen();
    const move = chess.move({
      from: sourceSquare,
      to: targetSquare,
      promotion: "q",
    });

    if (!move) {
      return false;
    }

    const newFen = chess.fen();
    const moveIndex = moveCountRef.current++;
    setFen(newFen);
    setMoveHistory((prev) => [...prev, { san: move.san, fen: newFen }]);
    setViewIndex(null); // Ensure we're viewing the live position

    const uciMove = `${move.from}${move.to}${move.promotion ?? ""}`;
    // Store context for blunder detection before engine moves
    pendingAnalysisContextRef.current = {
      fen: fenBeforeMove,
      pgn: chess.pgn(),
      moveSan: move.san,
      moveUci: uciMove,
    };
    analyzeMove(fenBeforeMove, uciMove, playerColor, moveIndex);

    if (chess.isGameOver()) {
      void handleGameEnd();
    } else {
      void applyOpponentMove(newFen);
    }

    return true;
  };

  const handleNewGame = async () => {
    try {
      setIsStartingGame(true);
      setStartError(null);
      // End current session if active
      if (sessionId && isGameActive) {
        await endGame(sessionId, "abandon", chess.pgn());
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

      const response = await startGame(1500, resolvedPlayerColor);
      setSessionId(response.session_id);
      setIsGameActive(true);
      setIsStartingGame(false);
      setShowStartOverlay(false);

      // Reset the board
      chess.reset();
      setFen(chess.fen());
      setEngineMessage(null);
      setGameResult(null);
      setMoveHistory([]);
      moveCountRef.current = 0;
      setViewIndex(null);
      setLiveOpening(null);
      resetEngine();
      clearAnalysis();
      blunderRecordedRef.current = false;
      pendingAnalysisContextRef.current = null;
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
      await endGame(sessionId, "resign", chess.pgn());
      setIsGameActive(false);
      setGameResult({ type: "resign", message: "You resigned." });
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Failed to resign game.";
      setEngineMessage(message);
    }
  };

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
    setShowStartOverlay(false);
    blunderRecordedRef.current = false;
    pendingAnalysisContextRef.current = null;
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
    setShowStartOverlay(true);
  };

  return (
    <section className="chess-section">
      <header className="chess-header">
        <p className="eyebrow">SRS Chess</p>
      </header>

      <div className="chess-layout">
        <div className="chess-panel" aria-live="polite">
          <p className="chess-status">{statusText}</p>
          <p className="chess-meta">
            Orientation: {boardOrientation === "white" ? "White" : "Black"} on
            bottom
          </p>
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
            Session:{" "}
            <span className={isGameActive ? "chess-meta-strong" : ""}>
              {isGameActive ? "Active" : "None (click New game to start)"}
            </span>
          </p>
          {isGameActive && (
            <p className="chess-meta">
              Opponent:{" "}
              <span className="chess-meta-strong">
                {opponentMode === "ghost" ? "Ghost" : "Engine"}
              </span>
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
          <div className="chess-controls">
            <button
              className="chess-button danger"
              type="button"
              onClick={handleResign}
              disabled={!isGameActive || chess.isGameOver()}
            >
              Resign
            </button>
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
          {showStartOverlay && !isGameActive && (
            <div className="chessboard-overlay">
              <div className="chess-start-panel">
                <p className="chess-start-title">Choose your side</p>
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
          <Chessboard
            options={{
              position: displayedFen,
              onPieceDrop: handleDrop,
              boardOrientation,
              animationDurationInMs: 200,
              allowDragging:
                isGameActive &&
                engineStatus === "ready" &&
                isPlayersTurn &&
                !isThinking &&
                isViewingLive,
              boardStyle: {
                borderRadius: "0",
                boxShadow: "0 20px 45px rgba(2, 6, 23, 0.5)",
              },
            }}
          />
          {!isGameActive && (
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
          <div className="engine-status">
            <p className="chess-meta">
              Engine status:{" "}
              <span className="chess-meta-strong">{engineStatusText}</span>
            </p>
            {engineInfo?.pv && isThinking && (
              <p className="chess-meta">
                Candidate line: {engineInfo.pv.slice(0, 4).join(" ")}
              </p>
            )}
          </div>
          <div className="engine-status">
            <p className="chess-meta">
              Analyst status:{" "}
              <span className="chess-meta-strong">{analysisStatusText}</span>
            </p>
          </div>
        </div>

        <MoveList
          moves={annotatedMoves}
          currentIndex={viewIndex}
          onNavigate={handleNavigate}
        />
      </div>
    </section>
  );
};

export default ChessGame;
