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
import type {
  AppliedPlayerMove,
  DrillRootConfirmRequest,
} from "../stores/useGameStore";
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
  // Composed from the store's AppliedPlayerMove so the durable pending-route-move
  // record and this result can never drift apart.
  | ({ applied: true } & AppliedPlayerMove);

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
  /** Confirms an applied root-reaching move with the backend. Resolves true only
   *  when the drill actually transitioned to root_reached. Must engage its own
   *  barrier SYNCHRONOUSLY, before awaiting. */
  confirmDrillRoot: (request: DrillRootConfirmRequest) => Promise<boolean>;
  /** True while a root confirmation is in flight or has failed without recovery. */
  isDrillRootConfirmPending: () => boolean;
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
  confirmDrillRoot,
  isDrillRootConfirmPending,
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

      // Preserve the pre-commit navigation mode instead of unconditionally
      // returning to live. Player moves only commit from live view, so this
      // stays null (live) for them. But an opponent reply that lands while the
      // user — or an in-flight blunder rewind — is viewing a historical
      // position must keep that historical viewIndex: appending to liveFen /
      // moveHistory below does not shift existing indices, so the same index
      // still resolves to the same board. Forcing viewIndex to null here would
      // overwrite the rewind while leaving blunderAlert active, wedging the
      // board with arrows shown and no legal move entry (g-i9v8). Return-to-live
      // happens only through explicit handleNavigate(null).
      const preCommitViewIndex = store.viewIndex;

      store.setLiveFen(newFen);
      store.setMoveHistory(nextMoveHistory);
      if (preCommitViewIndex === null) {
        store.setViewIndex(null);
      }

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
      // Gameplay barrier. Until the applied root-reaching move is confirmed the
      // drill is not root-reached, so no further move may be played onto it. One
      // guard covers drag and click alike — both funnel through here.
      if (isDrillRootConfirmPending()) {
        return { applied: false };
      }

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
      isDrillRootConfirmPending,
      resolvedReview,
      setBlunderAlert,
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
      decisionId?: string | null,
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
        const committed = commitAppliedMove(
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

        if (drillRoute?.reaches_root) {
          // A barrier, not a transition. confirmDrillRoot engages the block
          // SYNCHRONOUSLY before it awaits, and only IT sets drillState —
          // on a confirmation the backend proved, never on the serve.
          //
          // sessionId is read from the store (the same getState() idiom this file
          // already uses for SRS reviews) and FAILS CLOSED: null means the session
          // was torn down — new game or reset — while the move was in flight, so
          // there is nothing to confirm, nothing to transition, and no game to end.
          const sessionId = useGameStore.getState().sessionId;
          if (sessionId) {
            const confirmed = await confirmDrillRoot({
              decisionId: decisionId ?? null,
              sessionId,
              fen: chess.fen(),
              ply: committed.uciHistory.length,
              uci: committed.uciMove,
            });
            // Ordering is deliberate: a root-reaching move that also ends the game
            // ends it only AFTER the barrier clears. Previously such a move never
            // reached handleGameEnd at all.
            if (confirmed && chess.isGameOver()) {
              await handleGameEnd();
            }
          }
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
      confirmDrillRoot,
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
