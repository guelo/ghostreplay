import { describe, it, expect, vi, beforeEach } from "vitest";
import { Chess } from "chess.js";
import { renderHook, act } from "@testing-library/react";
import type { TargetBlunderSrs } from "../utils/api";
import type { MoveRecord } from "../components/chess-game/domain/movePresentation";
import type { ResolvedReview } from "../components/chess-game/types";
import type { DecisionOwner } from "../services/DecisionOwner";
import { useGameStore } from "../stores/useGameStore";
import { playMoveSound } from "../utils/moveSound";
import {
  useChessGameController,
  type PlayerMoveApplyResult,
} from "./useChessGameController";

vi.mock("../utils/moveSound", () => ({
  playMoveSound: vi.fn(),
}));

const initialStoreState = useGameStore.getInitialState();

/** Spy standing in for the coordinator-owned DecisionOwner — the controller only
 *  calls registerBlunderContext / registerSrsReview on it (g-2m0p). */
const createDecisionOwnerSpy = () => ({
  registerBlunderContext: vi.fn(),
  registerSrsReview: vi.fn(),
});
type DecisionOwnerSpy = ReturnType<typeof createDecisionOwnerSpy>;

type SetupOptions = {
  chess?: Chess;
  playerColor?: "white" | "black";
  blunderReviewId?: number | null;
  blunderReviewSrs?: TargetBlunderSrs | null;
  blunderTargetFen?: string | null;
  resolvedReview?: ResolvedReview | null;
  moveHistory?: MoveRecord[];
  sessionId?: string | null;
  isGameActive?: boolean;
  decisionOwner?: DecisionOwnerSpy;
};

const createSetup = ({
  chess = new Chess(),
  playerColor = "white",
  blunderReviewId = null,
  blunderReviewSrs = null,
  blunderTargetFen = null,
  resolvedReview = null,
  moveHistory = [],
  sessionId = "session-1",
  isGameActive = true,
  decisionOwner = createDecisionOwnerSpy(),
}: SetupOptions = {}) => {
  // Set up store state
  useGameStore.setState({
    ...initialStoreState,
    playerColor,
    sessionId,
    isGameActive,
    liveFen: chess.fen(),
    moveHistory: [...moveHistory],
  });

  const markSkipped = vi.fn();
  const setEngineMessage = vi.fn();
  const setBlunderAlert = vi.fn();
  const setBlunderReviewId = vi.fn();
  const setBlunderReviewSrs = vi.fn();
  const setBlunderTargetFen = vi.fn();
  const setShowGhostInfo = vi.fn();
  const setResolvedReview = vi.fn();
  const analyzeMove = vi.fn();
  const evaluatePosition = vi.fn().mockResolvedValue({ move: "(none)", raw: "" });
  const handleGameEnd = vi.fn().mockResolvedValue(undefined);
  const clearMoveHighlights = vi.fn();

  const { result } = renderHook(() =>
    useChessGameController({
      chess,
      blunderReviewId,
      blunderReviewSrs,
      blunderTargetFen,
      decisionOwner: decisionOwner as unknown as DecisionOwner,
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
    })
  );

  return {
    result,
    chess,
    decisionOwner,
    markSkipped,
    setEngineMessage,
    setBlunderAlert,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setBlunderTargetFen,
    setShowGhostInfo,
    setResolvedReview,
    analyzeMove,
    evaluatePosition,
    handleGameEnd,
    clearMoveHighlights,
  };
};

beforeEach(() => {
  useGameStore.setState(initialStoreState, true);
  vi.mocked(playMoveSound).mockReset();
});

describe("useChessGameController", () => {
  it("applies a player move and records analysis context", () => {
    const {
      result,
      chess,
      decisionOwner,
      analyzeMove,
      markSkipped,
      clearMoveHighlights,
      setBlunderAlert,
    } = createSetup();
    const startingFen = chess.fen();

    let moveResult: PlayerMoveApplyResult = {
      applied: false,
    };
    act(() => {
      moveResult = result.current.applyPlayerMove("e2", "e4");
    });

    expect(moveResult.applied).toBe(true);

    expect(clearMoveHighlights).toHaveBeenCalledTimes(1);
    expect(setBlunderAlert).toHaveBeenCalledWith(null);

    const store = useGameStore.getState();
    expect(store.moveHistory.length).toBe(1);
    expect(store.moveHistory[0]?.uci).toBe("e2e4");
    expect(store.viewIndex).toBeNull();

    expect(analyzeMove).toHaveBeenCalledWith(
      startingFen,
      "e2e4",
      "white",
      0,
      20,
    );
    expect(decisionOwner.registerBlunderContext).toHaveBeenCalledTimes(1);
    expect(decisionOwner.registerBlunderContext).toHaveBeenCalledWith(
      "analysis-0-e2e4",
      expect.objectContaining({ moveUci: "e2e4", moveIndex: 0 }),
    );
    // analyzeMove returned undefined (mock) → synthetic id → markSkipped emitted
    // after the context was registered (K3).
    expect(markSkipped).toHaveBeenCalledWith(0, "analysis-0-e2e4");
  });

  it("captures pending SRS review metadata for targeted player moves", () => {
    const chess = new Chess();
    const {
      result,
      decisionOwner,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
    } = createSetup({
      chess,
      blunderReviewId: 42,
      blunderTargetFen: chess.fen(),
    });

    let moveResult: PlayerMoveApplyResult = {
      applied: false,
    };
    act(() => {
      moveResult = result.current.applyPlayerMove("e2", "e4");
    });

    expect(moveResult.applied).toBe(true);
    expect(decisionOwner.registerSrsReview).toHaveBeenCalledWith(
      expect.any(String),
      {
        sessionId: "session-1",
        blunderId: 42,
        moveIndex: 0,
        userMoveSan: "e4",
        srs: null,
        srsDecisionId: expect.any(String),
      },
    );
    expect(setBlunderReviewId).toHaveBeenCalledWith(null);
    expect(setBlunderReviewSrs).toHaveBeenCalledWith(null);
    expect(setBlunderTargetFen).toHaveBeenCalledWith(null);
  });

  it("sets resolvedReview to pending synchronously when clearing blunder review", () => {
    const chess = new Chess();
    const {
      result,
      setResolvedReview,
    } = createSetup({
      chess,
      blunderReviewId: 42,
      blunderTargetFen: chess.fen(),
    });

    act(() => {
      result.current.applyPlayerMove("e2", "e4");
    });

    expect(setResolvedReview).toHaveBeenCalledWith({
      analysisId: expect.any(String),
      moveIndex: 0,
      result: "pending",
    });
  });

  it("registers each targeted SRS review on the owner without overwriting", () => {
    const decisionOwner = createDecisionOwnerSpy();

    const firstChess = new Chess();
    const first = createSetup({
      chess: firstChess,
      blunderReviewId: 42,
      blunderTargetFen: firstChess.fen(),
      sessionId: "session-1",
      decisionOwner,
    });
    first.analyzeMove.mockReturnValueOnce("analysis-one");

    act(() => {
      first.result.current.applyPlayerMove("e2", "e4");
    });

    const secondChess = new Chess();
    secondChess.move("d4");
    const second = createSetup({
      chess: secondChess,
      blunderReviewId: 99,
      blunderTargetFen: secondChess.fen(),
      sessionId: "session-2",
      moveHistory: [{ san: "d4", fen: secondChess.fen(), uci: "d2d4" }],
      decisionOwner,
    });
    second.analyzeMove.mockReturnValueOnce("analysis-two");

    act(() => {
      second.result.current.applyPlayerMove("g8", "f6");
    });

    expect(decisionOwner.registerSrsReview.mock.calls).toEqual([
      [
        "analysis-one",
        expect.objectContaining({
          sessionId: "session-1",
          blunderId: 42,
          moveIndex: 0,
          userMoveSan: "e4",
        }),
      ],
      [
        "analysis-two",
        expect.objectContaining({
          sessionId: "session-2",
          blunderId: 99,
          moveIndex: 1,
          userMoveSan: "Nf6",
        }),
      ],
    ]);
  });

  it("clears a targeted review without pending UI when session id is missing", () => {
    const chess = new Chess();
    const {
      result,
      decisionOwner,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setResolvedReview,
    } = createSetup({
      chess,
      blunderReviewId: 42,
      blunderTargetFen: chess.fen(),
      sessionId: null,
    });

    act(() => {
      result.current.applyPlayerMove("e2", "e4");
    });

    expect(decisionOwner.registerSrsReview).not.toHaveBeenCalled();
    expect(setBlunderReviewId).toHaveBeenCalledWith(null);
    expect(setBlunderReviewSrs).toHaveBeenCalledWith(null);
    expect(setBlunderTargetFen).toHaveBeenCalledWith(null);
    expect(setResolvedReview).not.toHaveBeenCalledWith(
      expect.objectContaining({ result: "pending" }),
    );
  });

  it("clears stale review targeting instead of grading a hidden review", () => {
    const {
      result,
      decisionOwner,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setResolvedReview,
    } = createSetup({ blunderReviewId: 42 });

    act(() => {
      result.current.applyPlayerMove("e2", "e4");
    });

    expect(decisionOwner.registerSrsReview).not.toHaveBeenCalled();
    expect(setBlunderReviewId).toHaveBeenCalledWith(null);
    expect(setBlunderReviewSrs).toHaveBeenCalledWith(null);
    expect(setBlunderTargetFen).toHaveBeenCalledWith(null);
    expect(setResolvedReview).not.toHaveBeenCalledWith(
      expect.objectContaining({ result: "pending" }),
    );
  });

  it("clears previous resolvedReview on next move", () => {
    const {
      result,
      setResolvedReview,
    } = createSetup({
      resolvedReview: {
        analysisId: "analysis-0-e2e4",
        moveIndex: 0,
        result: "pass",
      },
    });

    act(() => {
      result.current.applyPlayerMove("e2", "e4");
    });

    expect(setResolvedReview).toHaveBeenCalledWith(null);
  });

  it("rejects drop moves when interaction preconditions fail", () => {
    // Set viewIndex to non-null so isViewingLive is false
    const { result, analyzeMove } = createSetup();
    useGameStore.setState({ viewIndex: 0 });

    let dropResult: PlayerMoveApplyResult = {
      applied: false,
    };
    act(() => {
      dropResult = result.current.handleDrop("e2", "e4");
    });

    expect(dropResult.applied).toBe(false);
    expect(analyzeMove).not.toHaveBeenCalled();
  });

  it("applies an engine move and analyzes from opponent perspective", async () => {
    const chess = new Chess();
    const whiteMove = chess.move("e4");
    if (!whiteMove) {
      throw new Error("Unable to initialize engine test position");
    }
    const previousMove: MoveRecord = {
      san: whiteMove.san,
      fen: chess.fen(),
      uci: `${whiteMove.from}${whiteMove.to}${whiteMove.promotion ?? ""}`,
    };
    const fenBeforeEngineMove = chess.fen();

    const {
      result,
      evaluatePosition,
      analyzeMove,
      setEngineMessage,
      handleGameEnd,
    } = createSetup({
      chess,
      moveHistory: [previousMove],
    });

    evaluatePosition.mockResolvedValueOnce({ move: "d7d5", raw: "bestmove d7d5" });

    await act(async () => {
      await result.current.applyEngineMove();
    });

    expect(evaluatePosition).toHaveBeenCalledWith(fenBeforeEngineMove);

    const store = useGameStore.getState();
    expect(store.moveHistory.length).toBe(2);
    expect(store.moveHistory[1]?.uci).toBe("d7d5");

    expect(analyzeMove).toHaveBeenCalledWith(
      fenBeforeEngineMove,
      "d7d5",
      "black",
      1,
      20,
    );
    expect(setEngineMessage).toHaveBeenCalledWith(null);
    expect(handleGameEnd).not.toHaveBeenCalled();
  });

  it("drops an engine result if the session changes during search", async () => {
    const chess = new Chess();
    const whiteMove = chess.move("e4");
    if (!whiteMove) {
      throw new Error("Unable to initialize engine test position");
    }
    const previousMove: MoveRecord = {
      san: whiteMove.san,
      fen: chess.fen(),
      uci: `${whiteMove.from}${whiteMove.to}${whiteMove.promotion ?? ""}`,
    };

    let resolveSearch!: (value: { move: string; raw: string }) => void;
    const { result, evaluatePosition, analyzeMove } = createSetup({
      chess,
      moveHistory: [previousMove],
    });
    evaluatePosition.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveSearch = resolve;
      }),
    );

    const pending = act(async () => {
      await result.current.applyEngineMove();
    });

    useGameStore.getState().setSessionId("session-2");
    resolveSearch({ move: "d7d5", raw: "bestmove d7d5" });
    await pending;

    const store = useGameStore.getState();
    expect(store.moveHistory.length).toBe(1);
    expect(analyzeMove).not.toHaveBeenCalled();
  });

  it("applies a ghost move and toggles review targeting state", async () => {
    const chess = new Chess();
    const whiteMove = chess.move("e4");
    if (!whiteMove) {
      throw new Error("Unable to initialize ghost test position");
    }
    const previousMove: MoveRecord = {
      san: whiteMove.san,
      fen: chess.fen(),
      uci: `${whiteMove.from}${whiteMove.to}${whiteMove.promotion ?? ""}`,
    };
    const targetSrs: TargetBlunderSrs = {
      last_reviewed_at: "2026-02-01T00:00:00Z",
      created_at: "2026-01-15T00:00:00Z",
      pass_count: 2,
      fail_count: 1,
      pass_streak: 1,
    };

    const {
      result,
      analyzeMove,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setResolvedReview,
      setShowGhostInfo,
    } = createSetup({
      chess,
      moveHistory: [previousMove],
    });

    await act(async () => {
      await result.current.applyGhostMove("e5", "ghost_path", 77, targetSrs, "target-fen");
    });

    expect(analyzeMove).toHaveBeenCalledWith(
      expect.any(String),
      "e7e5",
      "black",
      1,
      20,
    );
    expect(setBlunderReviewId).toHaveBeenCalledWith(77);
    expect(setBlunderReviewSrs).toHaveBeenCalledWith(targetSrs);
    expect(setBlunderTargetFen).toHaveBeenCalledWith("target-fen");
    expect(setResolvedReview).toHaveBeenCalledWith(null);
    expect(setShowGhostInfo).not.toHaveBeenCalled();
  });

  it("normal pawn move (e2→e4) returns applied: true without requiresPromotion", () => {
    const { result } = createSetup();
    let moveResult: PlayerMoveApplyResult = { applied: false };
    act(() => {
      moveResult = result.current.applyPlayerMove("e2", "e4");
    });
    expect(moveResult.applied).toBe(true);
    expect((moveResult as { applied: false; requiresPromotion?: true }).requiresPromotion).toBeUndefined();
  });

  it("white pawn e7→e8 without promotion arg returns requiresPromotion: true", () => {
    // Set up a position where white pawn is on e7
    const chess = new Chess("8/4P3/8/8/8/8/8/4K2k w - - 0 1");
    useGameStore.setState({ ...initialStoreState, playerColor: "white", liveFen: chess.fen(), isGameActive: true });
    const { result } = createSetup({ chess });
    let moveResult: PlayerMoveApplyResult = { applied: false };
    act(() => {
      moveResult = result.current.applyPlayerMove("e7", "e8");
    });
    expect(moveResult.applied).toBe(false);
    expect((moveResult as { applied: false; requiresPromotion?: true }).requiresPromotion).toBe(true);
    // Chess state must be unchanged
    expect(chess.fen()).toBe("8/4P3/8/8/8/8/8/4K2k w - - 0 1");
  });

  it("white pawn e7→e8 with promotion 'r' returns applied: true with rook in SAN", () => {
    const chess = new Chess("8/4P3/8/8/8/8/8/4K2k w - - 0 1");
    useGameStore.setState({ ...initialStoreState, playerColor: "white", liveFen: chess.fen(), isGameActive: true });
    const { result } = createSetup({ chess });
    let moveResult!: PlayerMoveApplyResult;
    act(() => {
      moveResult = result.current.applyPlayerMove("e7", "e8", "r");
    });
    expect(moveResult.applied).toBe(true);
    if (moveResult.applied) {
      expect(moveResult.moveSan).toMatch(/R/);
    }
  });

  it("plays a non-capture move sound for a player move", () => {
    const { result } = createSetup();

    act(() => {
      result.current.applyPlayerMove("e2", "e4");
    });

    expect(playMoveSound).toHaveBeenCalledTimes(1);
    expect(playMoveSound).toHaveBeenCalledWith(false);
  });

  it("plays a capture move sound for a capturing player move", () => {
    // 1. e4 d5 2. exd5 — the third move is a capture.
    const chess = new Chess();
    chess.move("e4");
    chess.move("d5");
    useGameStore.setState({
      ...initialStoreState,
      playerColor: "white",
      liveFen: chess.fen(),
      moveHistory: [
        { san: "e4", fen: chess.fen(), uci: "e2e4" },
        { san: "d5", fen: chess.fen(), uci: "d7d5" },
      ],
    });
    const { result } = createSetup({ chess });
    vi.mocked(playMoveSound).mockReset();

    act(() => {
      result.current.applyPlayerMove("e4", "d5");
    });

    expect(playMoveSound).toHaveBeenCalledTimes(1);
    expect(playMoveSound).toHaveBeenCalledWith(true);
  });

  it("plays a move sound for an engine move", async () => {
    const chess = new Chess();
    const whiteMove = chess.move("e4");
    if (!whiteMove) {
      throw new Error("Unable to initialize engine test position");
    }
    const previousMove: MoveRecord = {
      san: whiteMove.san,
      fen: chess.fen(),
      uci: `${whiteMove.from}${whiteMove.to}${whiteMove.promotion ?? ""}`,
    };

    const { result, evaluatePosition } = createSetup({
      chess,
      moveHistory: [previousMove],
    });
    evaluatePosition.mockResolvedValueOnce({ move: "d7d5", raw: "bestmove d7d5" });
    vi.mocked(playMoveSound).mockReset();

    await act(async () => {
      await result.current.applyEngineMove();
    });

    expect(playMoveSound).toHaveBeenCalledTimes(1);
    expect(playMoveSound).toHaveBeenCalledWith(false);
  });

  it("plays a move sound for a ghost move", async () => {
    const chess = new Chess();
    const whiteMove = chess.move("e4");
    if (!whiteMove) {
      throw new Error("Unable to initialize ghost test position");
    }
    const previousMove: MoveRecord = {
      san: whiteMove.san,
      fen: chess.fen(),
      uci: `${whiteMove.from}${whiteMove.to}${whiteMove.promotion ?? ""}`,
    };

    const { result } = createSetup({
      chess,
      moveHistory: [previousMove],
    });
    vi.mocked(playMoveSound).mockReset();

    await act(async () => {
      await result.current.applyGhostMove("e5", "ghost_path", 77, null, "target-fen");
    });

    expect(playMoveSound).toHaveBeenCalledTimes(1);
    expect(playMoveSound).toHaveBeenCalledWith(false);
  });
});
