import { useCallback } from "react";
import type { Chess } from "chess.js";
import type { Square } from "chess.js";
import type { Dispatch, SetStateAction } from "react";
import type { DrillRouteMetadata, SessionDecisionSource, TargetBlunderSrs } from "../utils/api";
import type { BlunderAlert } from "../components/chess-game/domain/movePresentation";
import {
  canArmReviewTarget,
  hasReviewTargetAtFen,
} from "../components/chess-game/domain/reviewState";
import type { ResolvedReview } from "../components/chess-game/types";
import { useGameStore } from "../stores/useGameStore";
import { playMoveSound } from "../utils/moveSound";
import type { DecisionOwner } from "../services/DecisionOwner";

export type PendingAnalysisContext = {
  fen: string;
  pgn: string;
  moveSan: string;
  moveUci: string;
  moveIndex: number;
};

export type PendingSrsReview = {
  sessionId: string;
  analysisId: string;
  blunderId: number;
  moveIndex: number;
  userMoveSan: string;
  srs: TargetBlunderSrs | null;
  /** Stable logical-review id, minted at registration and preserved across
   *  request-id retries (forward-prep for g-hpw4; no backend change here). */
  srsDecisionId: string;
};

export type PlayerMoveApplyResult =
  | { applied: false; requiresPromotion?: true }
  | {
    applied: true;
    fenAfter: string;
    fenBefore: string;
    uciHistory: string[];
    gameOver: boolean;
    moveIndex: number;
    moveSan: string;
    moveUci: string;
  };

type AnalyzeMoveFn = (
  fen: string,
  move: string,
  playerColor: "white" | "black",
  moveIndex?: number,
  legalMoveCount?: number,
) => string | undefined;

type EvaluatePositionFn = (fen: string) => Promise<{ move: string; raw: string }>;

type UseChessGameControllerOptions = {
  chess: Chess;
  blunderReviewId: number | null;
  blunderReviewSrs: TargetBlunderSrs | null;
  blunderTargetFen: string | null;
  /** Coordinator-lifetime recording/SRS owner the controller registers
   *  blunder-context and SRS reviews onto (g-2m0p). */
  decisionOwner: DecisionOwner;
  /** Coordinator-facing skip emission for synthetic (worker-unavailable) ids. */
  markSkipped: (moveIndex: number, requestId: string) => void;
  setEngineMessage: Dispatch<SetStateAction<string | null>>;
  setBlunderAlert: Dispatch<SetStateAction<BlunderAlert | null>>;
  setBlunderReviewId: Dispatch<SetStateAction<number | null>>;
  setBlunderReviewSrs: Dispatch<SetStateAction<TargetBlunderSrs | null>>;
  setBlunderTargetFen: Dispatch<SetStateAction<string | null>>;
  setShowGhostInfo: Dispatch<SetStateAction<boolean>>;
  resolvedReview: ResolvedReview | null;
  setResolvedReview: Dispatch<SetStateAction<ResolvedReview | null>>;
  analyzeMove: AnalyzeMoveFn;
  evaluatePosition: EvaluatePositionFn;
  handleGameEnd: () => Promise<void>;
  clearMoveHighlights: () => void;
  clearBlunderBoardOverride?: () => void;
};

function isPromotionNeeded(chess: Chess, from: string, to: string): boolean {
  const piece = chess.get(from as Square);
  if (!piece || piece.type !== 'p') return false;
  const toRank = to[1];
  if (piece.color === 'w' && toRank !== '8') return false;
  if (piece.color === 'b' && toRank !== '1') return false;
  return chess.moves({ verbose: true }).some((m) => m.from === from && m.to === to);
}

type AppliedMove = NonNullable<ReturnType<Chess["move"]>>;

export const useChessGameController = ({
  chess,
  blunderReviewId,
  blunderReviewSrs,
  blunderTargetFen,
  decisionOwner,
  markSkipped,
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
  handleGameEnd,
  clearMoveHighlights,
  clearBlunderBoardOverride,
}: UseChessGameControllerOptions) => {
  const clearReviewTarget = useCallback(() => {
    setBlunderReviewId(null);
    setBlunderReviewSrs(null);
    setBlunderTargetFen(null);
  }, [setBlunderReviewId, setBlunderReviewSrs, setBlunderTargetFen]);

  const commitAppliedMove = useCallback(
    (
      appliedMove: AppliedMove,
      fenBeforeMove: string,
      legalMoveCount: number,
      analysisColor: "white" | "black",
      metadata?: {
        decisionSource?: SessionDecisionSource;
        targetBlunderId?: number | null;
      },
      // Player moves register a BlunderContext keyed by the request id so the
      // outcome-channel recording frontier can pair the resolved analysis with
      // its own move (H2). Opponent moves skip this but still emit `skipped`
      // below if analysis could not be scheduled.
      registerBlunderContext = false,
    ) => {
      playMoveSound(Boolean(appliedMove.captured));

      const store = useGameStore.getState();
      const newFen = chess.fen();
      const moveIndex = store.moveHistory.length;
      const uciMove = `${appliedMove.from}${appliedMove.to}${appliedMove.promotion ?? ""}`;
      const nextMove = {
        san: appliedMove.san,
        fen: newFen,
        uci: uciMove,
        decisionSource: metadata?.decisionSource,
        targetBlunderId: metadata?.targetBlunderId ?? null,
      };
      const nextMoveHistory = [...store.moveHistory, nextMove];

      store.setLiveFen(newFen);
      store.setMoveHistory(nextMoveHistory);
      store.setViewIndex(null);

      const scheduledId = analyzeMove(
        fenBeforeMove,
        uciMove,
        analysisColor,
        moveIndex,
        legalMoveCount,
      );
      const analysisId = scheduledId ?? `analysis-${moveIndex}-${uciMove}`;

      // K3: write context BEFORE the synchronous markSkipped so the consumer's
      // frontier slot is created with a consistent moveIndex/context.
      if (registerBlunderContext) {
        decisionOwner.registerBlunderContext(analysisId, {
          fen: fenBeforeMove,
          pgn: chess.pgn(),
          moveSan: appliedMove.san,
          moveUci: uciMove,
          moveIndex,
        });
      }

      // analyzeMove returned undefined (worker unavailable → synthetic id): no
      // real outcome will ever fire for this index, so emit the sole `skipped`
      // terminal to unblock the recording frontier.
      if (scheduledId === undefined) {
        markSkipped(moveIndex, analysisId);
      }

      return {
        analysisId,
        fenAfter: newFen,
        moveIndex,
        moveSan: appliedMove.san,
        uciMove,
        uciHistory: nextMoveHistory.map((m) => m.uci),
      };
    },
    [analyzeMove, chess, markSkipped, decisionOwner],
  );

  const applyPlayerMove = useCallback(
    (sourceSquare: string, targetSquare: string, promotion?: string): PlayerMoveApplyResult => {
      const fenBeforeMove = chess.fen();
      const legalMoveCount = chess.moves().length;

      if (!promotion && isPromotionNeeded(chess, sourceSquare, targetSquare)) {
        return { applied: false, requiresPromotion: true };
      }

      let move: AppliedMove | null = null;
      try {
        move = chess.move({
          from: sourceSquare,
          to: targetSquare,
          promotion: promotion ?? "q",
        });
      } catch {
        return { applied: false };
      }

      if (!move) {
        return { applied: false };
      }

      clearMoveHighlights();
      clearBlunderBoardOverride?.();
      setBlunderAlert(null);

      // Clear any existing resolved review overlay before processing
      if (resolvedReview !== null) {
        setResolvedReview(null);
      }

      const isTargetedReviewMove = hasReviewTargetAtFen(
        blunderReviewId,
        blunderTargetFen,
        fenBeforeMove,
      );

      if (blunderReviewId !== null && !isTargetedReviewMove) {
        clearReviewTarget();
      }

      const playerColor = useGameStore.getState().playerColor;
      const committed = commitAppliedMove(
        move,
        fenBeforeMove,
        legalMoveCount,
        playerColor,
        undefined,
        true,
      );

      if (isTargetedReviewMove) {
        const sessionId = useGameStore.getState().sessionId;
        clearReviewTarget();
        if (sessionId) {
          decisionOwner.registerSrsReview(committed.analysisId, {
            sessionId,
            blunderId: blunderReviewId,
            moveIndex: committed.moveIndex,
            userMoveSan: committed.moveSan,
            srs: blunderReviewSrs,
            srsDecisionId: crypto.randomUUID(),
          });
          setResolvedReview({
            analysisId: committed.analysisId,
            moveIndex: committed.moveIndex,
            result: "pending",
          });
        }
      }

      return {
        applied: true,
        fenAfter: committed.fenAfter,
        fenBefore: fenBeforeMove,
        uciHistory: committed.uciHistory,
        gameOver: chess.isGameOver(),
        moveIndex: committed.moveIndex,
        moveSan: committed.moveSan,
        moveUci: committed.uciMove,
      };
    },
    [
      blunderReviewId,
      blunderReviewSrs,
      blunderTargetFen,
      chess,
      clearMoveHighlights,
      clearReviewTarget,
      commitAppliedMove,
      decisionOwner,
      resolvedReview,
      setBlunderAlert,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setResolvedReview,
      clearBlunderBoardOverride,
    ],
  );

  const handleDrop = useCallback(
    (
      sourceSquare: string | null,
      targetSquare: string | null | undefined,
      promotion?: string,
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

      return applyPlayerMove(sourceSquare, targetSquare, promotion);
    },
    [applyPlayerMove, chess],
  );

  const applyEngineMove = useCallback(async () => {
    try {
      const fenBeforeMove = chess.fen();
      const legalMoveCount = chess.moves().length;
      const sessionIdBeforeMove = useGameStore.getState().sessionId;
      const result = await evaluatePosition(fenBeforeMove);

      const storeAfterSearch = useGameStore.getState();
      if (
        storeAfterSearch.sessionId !== sessionIdBeforeMove ||
        storeAfterSearch.liveFen !== fenBeforeMove ||
        chess.fen() !== fenBeforeMove ||
        !storeAfterSearch.isGameActive
      ) {
        return;
      }

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
        { decisionSource: "local_fallback" },
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
      decisionSource: Exclude<SessionDecisionSource, "local_fallback">,
      targetBlunderId: number | null,
      targetBlunderSrs: TargetBlunderSrs | null,
      targetFen: string | null,
      drillRoute?: DrillRouteMetadata | null,
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
          { decisionSource, targetBlunderId },
        );
        setEngineMessage(null);

        // Mark position as under review if ghost-move targets a blunder
        // and it's now the player's turn.
        const sideToMove = chess.turn() === "w" ? "white" : "black";
        if (canArmReviewTarget(targetBlunderId, targetFen, sideToMove, playerColor)) {
          setResolvedReview(null);
          setBlunderReviewId(targetBlunderId);
          setBlunderReviewSrs(targetBlunderSrs);
          setBlunderTargetFen(targetFen);
        } else {
          clearReviewTarget();
          setShowGhostInfo(false);
        }

        if (drillRoute?.status === "root_reached") {
          useGameStore.getState().setDrillState("root_reached");
          setEngineMessage("Opening root reached. Drill is live.");
        } else if (chess.isGameOver()) {
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
      clearReviewTarget,
      commitAppliedMove,
      handleGameEnd,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setEngineMessage,
      setResolvedReview,
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
