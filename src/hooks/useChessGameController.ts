import { useCallback } from "react";
import type { Chess } from "chess.js";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { TargetBlunderSrs } from "../utils/api";
import type { BlunderAlert } from "../components/chess-game/domain/movePresentation";
import { useGameStore } from "../stores/useGameStore";

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
  srs: TargetBlunderSrs | null;
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
  blunderReviewId: number | null;
  blunderReviewSrs: TargetBlunderSrs | null;
  pendingAnalysisContextRef: MutableRefObject<PendingAnalysisContext | null>;
  pendingSrsReviewRef: MutableRefObject<PendingSrsReview | null>;
  setEngineMessage: Dispatch<SetStateAction<string | null>>;
  setBlunderAlert: Dispatch<SetStateAction<BlunderAlert | null>>;
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
      const store = useGameStore.getState();
      const newFen = chess.fen();
      const moveIndex = store.moveHistory.length;
      const uciMove = `${appliedMove.from}${appliedMove.to}${appliedMove.promotion ?? ""}`;
      const nextMove = { san: appliedMove.san, fen: newFen, uci: uciMove };
      const nextMoveHistory = [...store.moveHistory, nextMove];

      store.setLiveFen(newFen);
      store.setMoveHistory(nextMoveHistory);
      store.setViewIndex(null);

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
    [analyzeMove, chess],
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

      const playerColor = useGameStore.getState().playerColor;
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
          srs: blunderReviewSrs,
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
      blunderReviewSrs,
      chess,
      clearMoveHighlights,
      commitAppliedMove,
      pendingAnalysisContextRef,
      pendingSrsReviewRef,
      setBlunderAlert,
      setBlunderReviewId,
      setBlunderReviewSrs,
    ],
  );

  const handleDrop = useCallback(
    (
      sourceSquare: string | null,
      targetSquare: string | null | undefined,
    ): PlayerMoveApplyResult => {
      if (!sourceSquare) {
        return { applied: false };
      }

      if (!targetSquare) {
        return { applied: false };
      }

      const store = useGameStore.getState();
      const isViewingLive = store.viewIndex === null;
      const isPlayersTurn =
        chess.turn() === (store.playerColor === "white" ? "w" : "b");

      if (!isPlayersTurn || !isViewingLive) {
        return { applied: false };
      }

      if (sourceSquare === targetSquare) {
        return { applied: false };
      }

      return applyPlayerMove(sourceSquare, targetSquare);
    },
    [applyPlayerMove, chess],
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

      const opponentColor =
        useGameStore.getState().playerColor === "white" ? "black" : "white";
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

        const playerColor = useGameStore.getState().playerColor;
        const opponentColor =
          playerColor === "white" ? "black" : "white";
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
