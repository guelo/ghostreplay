import { useCallback } from "react";
import type { Chess } from "chess.js";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { TargetBlunderSrs } from "../utils/api";
import type { MoveRecord } from "../components/chess-game/domain/movePresentation";

export type PendingAnalysisContext = {
  fen: string;
  pgn: string;
  moveSan: string;
  moveUci: string;
  moveIndex: number;
};

export type PendingSrsReview = {
  blunderId: number;
  moveIndex: number;
  userMoveSan: string;
};

export type PlayerMoveApplyResult =
  | { applied: false }
  | {
    applied: true;
    fenAfter: string;
    uciHistory: string[];
    gameOver: boolean;
    moveIndex: number;
    moveSan: string;
  };

type AnalyzeMoveFn = (
  fen: string,
  move: string,
  playerColor: "white" | "black",
  moveIndex?: number,
  legalMoveCount?: number,
) => void;

type EvaluatePositionFn = (fen: string) => Promise<{ move: string; raw: string }>;

type UseChessGameControllerOptions = {
  chess: Chess;
  playerColor: "white" | "black";
  opponentColor: "white" | "black";
  isPlayersTurn: boolean;
  isViewingLive: boolean;
  blunderReviewId: number | null;
  moveCountRef: MutableRefObject<number>;
  moveHistoryRef: MutableRefObject<MoveRecord[]>;
  pendingAnalysisContextRef: MutableRefObject<PendingAnalysisContext | null>;
  pendingSrsReviewRef: MutableRefObject<PendingSrsReview | null>;
  setFen: Dispatch<SetStateAction<string>>;
  setMoveHistory: Dispatch<SetStateAction<MoveRecord[]>>;
  setViewIndex: Dispatch<SetStateAction<number | null>>;
  setEngineMessage: Dispatch<SetStateAction<string | null>>;
  setBlunderAlert: Dispatch<
    SetStateAction<{
      moveSan: string;
      moveUci: string;
      bestMoveUci: string;
      bestMoveSan: string;
      delta: number;
    } | null>
  >;
  setBlunderReviewId: Dispatch<SetStateAction<number | null>>;
  setBlunderReviewSrs: Dispatch<SetStateAction<TargetBlunderSrs | null>>;
  setBlunderTargetFen: Dispatch<SetStateAction<string | null>>;
  setShowGhostInfo: Dispatch<SetStateAction<boolean>>;
  analyzeMove: AnalyzeMoveFn;
  evaluatePosition: EvaluatePositionFn;
  handleGameEnd: () => Promise<void>;
  clearMoveHighlights: () => void;
};

type AppliedMove = NonNullable<ReturnType<Chess["move"]>>;

export const useChessGameController = ({
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
}: UseChessGameControllerOptions) => {
  const commitAppliedMove = useCallback(
    (
      appliedMove: AppliedMove,
      fenBeforeMove: string,
      legalMoveCount: number,
      analysisColor: "white" | "black",
    ) => {
      const newFen = chess.fen();
      const moveIndex = moveCountRef.current++;
      const uciMove = `${appliedMove.from}${appliedMove.to}${appliedMove.promotion ?? ""}`;
      const nextMove = { san: appliedMove.san, fen: newFen, uci: uciMove };
      const nextMoveHistory = [...moveHistoryRef.current, nextMove];

      moveHistoryRef.current = nextMoveHistory;
      setFen(newFen);
      setMoveHistory(nextMoveHistory);
      setViewIndex(null);

      analyzeMove(
        fenBeforeMove,
        uciMove,
        analysisColor,
        moveIndex,
        legalMoveCount,
      );

      return {
        fenAfter: newFen,
        moveIndex,
        moveSan: appliedMove.san,
        uciMove,
        uciHistory: nextMoveHistory.map((m) => m.uci),
      };
    },
    [analyzeMove, chess, moveCountRef, moveHistoryRef, setFen, setMoveHistory, setViewIndex],
  );

  const applyPlayerMove = useCallback(
    (sourceSquare: string, targetSquare: string): PlayerMoveApplyResult => {
      const fenBeforeMove = chess.fen();
      const legalMoveCount = chess.moves().length;

      let move: AppliedMove | null = null;
      try {
        move = chess.move({
          from: sourceSquare,
          to: targetSquare,
          promotion: "q",
        });
      } catch {
        return { applied: false };
      }

      if (!move) {
        return { applied: false };
      }

      clearMoveHighlights();
      setBlunderAlert(null);

      const committed = commitAppliedMove(
        move,
        fenBeforeMove,
        legalMoveCount,
        playerColor,
      );

      if (blunderReviewId !== null) {
        pendingSrsReviewRef.current = {
          blunderId: blunderReviewId,
          moveIndex: committed.moveIndex,
          userMoveSan: committed.moveSan,
        };
        setBlunderReviewId(null);
        setBlunderReviewSrs(null);
      }

      pendingAnalysisContextRef.current = {
        fen: fenBeforeMove,
        pgn: chess.pgn(),
        moveSan: committed.moveSan,
        moveUci: committed.uciMove,
        moveIndex: committed.moveIndex,
      };

      return {
        applied: true,
        fenAfter: committed.fenAfter,
        uciHistory: committed.uciHistory,
        gameOver: chess.isGameOver(),
        moveIndex: committed.moveIndex,
        moveSan: committed.moveSan,
      };
    },
    [
      blunderReviewId,
      chess,
      clearMoveHighlights,
      commitAppliedMove,
      pendingAnalysisContextRef,
      pendingSrsReviewRef,
      playerColor,
      setBlunderAlert,
      setBlunderReviewId,
      setBlunderReviewSrs,
    ],
  );

  const handleDrop = useCallback(
    (sourceSquare: string, targetSquare: string | undefined): PlayerMoveApplyResult => {
      if (!targetSquare) {
        return { applied: false };
      }

      if (!isPlayersTurn || !isViewingLive) {
        return { applied: false };
      }

      if (sourceSquare === targetSquare) {
        return { applied: false };
      }

      return applyPlayerMove(sourceSquare, targetSquare);
    },
    [applyPlayerMove, isPlayersTurn, isViewingLive],
  );

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

      commitAppliedMove(
        appliedMove,
        fenBeforeMove,
        legalMoveCount,
        opponentColor,
      );
      setEngineMessage(null);

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
  }, [
    chess,
    commitAppliedMove,
    evaluatePosition,
    handleGameEnd,
    opponentColor,
    setEngineMessage,
  ]);

  const applyGhostMove = useCallback(
    async (
      sanMove: string,
      targetBlunderId: number | null,
      targetBlunderSrs: TargetBlunderSrs | null,
      targetFen: string | null,
    ) => {
      try {
        const fenBeforeMove = chess.fen();
        const legalMoveCount = chess.moves().length;
        const appliedMove = chess.move(sanMove);

        if (!appliedMove) {
          throw new Error(`Ghost returned illegal move: ${sanMove}`);
        }

        commitAppliedMove(
          appliedMove,
          fenBeforeMove,
          legalMoveCount,
          opponentColor,
        );
        setEngineMessage(null);

        // Mark position as under review if ghost-move targets a blunder
        // and it's now the player's turn.
        const sideToMove = chess.turn() === "w" ? "white" : "black";
        if (targetBlunderId !== null && sideToMove === playerColor) {
          setBlunderReviewId(targetBlunderId);
          setBlunderReviewSrs(targetBlunderSrs);
          setBlunderTargetFen(targetFen);
        } else {
          setBlunderReviewId(null);
          setBlunderReviewSrs(null);
          setBlunderTargetFen(null);
          setShowGhostInfo(false);
        }

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
    [
      chess,
      commitAppliedMove,
      handleGameEnd,
      opponentColor,
      playerColor,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setEngineMessage,
      setShowGhostInfo,
    ],
  );

  return {
    applyPlayerMove,
    handleDrop,
    applyEngineMove,
    applyGhostMove,
  };
};
