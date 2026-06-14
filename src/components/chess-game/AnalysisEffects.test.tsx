import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, act, waitFor } from "../../test/utils";
import AnalysisEffects from "./AnalysisEffects";
import { useGameStore } from "../../stores/useGameStore";
import {
  AnalysisStoreProvider,
  createAnalysisStore,
} from "../../stores/createAnalysisStore";
import type { Dispatch, SetStateAction } from "react";
import type { AnalysisResult } from "../../hooks/useMoveAnalysis";
import type { BlunderAlert } from "./domain/movePresentation";
import type { MoveMessage, SrsFailDetail } from "../MoveList";
import type { ResolvedReview } from "./types";
import {
  DecisionOwner,
  type DecisionOwnerGameState,
} from "../../services/DecisionOwner";
import type { AnalysisOutcome } from "../../services/GameAnalysisCoordinator";

const mockPlayBling = vi.fn();
const mockPlayBuzzer = vi.fn();
const recordBlunderMock = vi.fn();
const reviewSrsBlunderMock = vi.fn();
vi.mock("../../utils/blingSound", () => ({
  playBling: () => mockPlayBling(),
}));
vi.mock("../../utils/buzzerSound", () => ({
  playBuzzer: () => mockPlayBuzzer(),
}));
const mockPlayBlunderAudio = vi.fn();
vi.mock("./blunderAudio", () => ({
  playRandomBlunderAudio: () => mockPlayBlunderAudio(),
}));
// Keep ApiError/errorCodeOf real (the owner's retry classification depends on
// them); only the two POST endpoints are spied.
vi.mock("../../utils/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../utils/api")>();
  return {
    ...actual,
    recordBlunder: (...args: unknown[]) => recordBlunderMock(...args),
    reviewSrsBlunder: (...args: unknown[]) => reviewSrsBlunderMock(...args),
  };
});

const makeResult = (
  overrides: Partial<AnalysisResult> = {},
): AnalysisResult => ({
  id: crypto.randomUUID(),
  move: "e2e4",
  currentPositionEval: 0,
  playedEvalMate: null,
  currentPositionEvalMate: null,
  moveIndex: 0,
  playedEval: 0,
  bestEval: 0,
  bestMove: "e2e4",
  delta: 0,
  classification: "good",
  blunder: false,
  recordable: false,
  ...overrides,
});

const initialGameState = useGameStore.getInitialState();

// Host exposing a real coordinator-owned DecisionOwner (g-2m0p). AnalysisEffects
// no longer registers a listener — it leases UI callbacks onto this owner — so
// tests drive outcomes/resets straight into handleOutcome/handleReset, and
// register blunder-context / SRS reviews onto the same owner the component leases.
let outcomeSeq = 0;
function makeHost() {
  const decisionOwner = new DecisionOwner({
    getGameState: (): DecisionOwnerGameState => {
      const s = useGameStore.getState();
      return {
        sessionId: s.sessionId,
        isGameActive: s.isGameActive,
        isPracticeContinuation: s.isPracticeContinuation,
        playerColor: s.playerColor,
        moveHistory: s.moveHistory,
      };
    },
  });
  const emit = (o: {
    moveIndex: number;
    requestId: string;
    status: AnalysisOutcome["status"];
    previousRequestId?: string;
    result?: AnalysisResult;
  }) =>
    decisionOwner.handleOutcome({
      seq: outcomeSeq++,
      generation: 0,
      sessionId: null,
      ...o,
    } as AnalysisOutcome);
  return {
    decisionOwner,
    scheduled(moveIndex: number, requestId: string, previousRequestId?: string) {
      emit({ moveIndex, requestId, status: "scheduled", previousRequestId });
    },
    resolved(result: AnalysisResult, requestId = result.id) {
      emit({ moveIndex: result.moveIndex ?? 0, requestId, status: "resolved", result });
    },
    skipped(moveIndex: number, requestId = `skip-${moveIndex}`) {
      emit({ moveIndex, requestId, status: "skipped" });
    },
    failed(moveIndex: number, requestId = `fail-${moveIndex}`) {
      emit({ moveIndex, requestId, status: "failed" });
    },
    reset(info: Record<string, unknown> = {}) {
      decisionOwner.handleReset({ generation: 0, sessionId: null, ...info });
    },
  };
}

type RenderOverrides = Partial<{
  appendMoveMessage: (moveIndex: number, msg: MoveMessage) => void;
  setBlunderAlert: Dispatch<SetStateAction<BlunderAlert | null>>;
  setShowFlash: Dispatch<SetStateAction<boolean>>;
  setResolvedReview: Dispatch<SetStateAction<ResolvedReview | null>>;
  onSrsFail: (detail: SrsFailDetail, moveIndex: number) => void;
}>;

describe("AnalysisEffects", () => {
  let store: ReturnType<typeof createAnalysisStore>;

  beforeEach(() => {
    mockPlayBling.mockClear();
    mockPlayBuzzer.mockClear();
    mockPlayBlunderAudio.mockClear();
    recordBlunderMock.mockReset();
    reviewSrsBlunderMock.mockReset();
    recordBlunderMock.mockResolvedValue({});
    reviewSrsBlunderMock.mockResolvedValue({});
    useGameStore.setState(initialGameState, true);
    store = createAnalysisStore();
    outcomeSeq = 0;
  });

  function renderEffects(
    host: ReturnType<typeof makeHost>,
    overrides: RenderOverrides = {},
  ) {
    return render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          appendMoveMessage={overrides.appendMoveMessage ?? vi.fn()}
          setBlunderAlert={overrides.setBlunderAlert ?? vi.fn()}
          setShowFlash={overrides.setShowFlash ?? vi.fn()}
          setResolvedReview={overrides.setResolvedReview ?? vi.fn()}
          onSrsFail={overrides.onSrsFail ?? vi.fn()}
          coordinator={host as never}
        />
      </AnalysisStoreProvider>,
    );
  }

  // --- best-move bling (unchanged behavior) ---

  it("plays bling when best-move analysis arrives for a player move", () => {
    useGameStore.setState({ playerColor: "white", isGameActive: true });
    renderEffects(makeHost());

    act(() => {
      store.getState().resolveAnalysis(0, makeResult({ moveIndex: 0, classification: "best" }));
    });

    expect(mockPlayBling).toHaveBeenCalledTimes(1);
  });

  it("plays bling when best-move analysis arrives for a black player move", () => {
    useGameStore.setState({ playerColor: "black", isGameActive: true });
    renderEffects(makeHost());

    act(() => {
      store.getState().resolveAnalysis(1, makeResult({ moveIndex: 1, classification: "best" }));
    });

    expect(mockPlayBling).toHaveBeenCalledTimes(1);
  });

  it("does NOT play bling for engine moves with best classification", () => {
    useGameStore.setState({ playerColor: "white", isGameActive: true });
    renderEffects(makeHost());

    act(() => {
      store.getState().resolveAnalysis(1, makeResult({ moveIndex: 1, classification: "best" }));
    });

    expect(mockPlayBling).not.toHaveBeenCalled();
  });

  it("does NOT play bling for non-best player moves", () => {
    useGameStore.setState({ playerColor: "white", isGameActive: true });
    renderEffects(makeHost());

    act(() => {
      store.getState().resolveAnalysis(0, makeResult({ moveIndex: 0, classification: "good" }));
    });

    expect(mockPlayBling).not.toHaveBeenCalled();
  });

  // --- durable recording through the leased owner ---

  it("does not record blunders during practice continuation", () => {
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: true,
    });

    const host = makeHost();
    renderEffects(host);
    host.decisionOwner.registerBlunderContext("req-1", {
      fen: "fen-before", pgn: "1. e4", moveSan: "e4", moveUci: "e2e4", moveIndex: 1,
    });

    act(() => {
      host.resolved(makeResult({ id: "seed-0", moveIndex: 0, delta: 0 }));
      host.resolved(makeResult({
        id: "req-1", moveIndex: 1, move: "e2e4", bestMove: "d2d4",
        delta: 250, classification: "blunder", blunder: true, recordable: true,
      }));
    });

    expect(recordBlunderMock).not.toHaveBeenCalled();
  });

  it("records the first recordable blunder via the leased owner", async () => {
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
    });

    const host = makeHost();
    renderEffects(host);
    host.decisionOwner.registerBlunderContext("req-1", {
      fen: "fen-before", pgn: "1. e4 e5 2. Nf3", moveSan: "Nf3", moveUci: "g1f3", moveIndex: 2,
    });

    act(() => {
      host.resolved(makeResult({ id: "seed-0", moveIndex: 0, delta: 0 }));
      host.resolved(makeResult({ id: "seed-1", moveIndex: 1, delta: 0 }));
      host.resolved(makeResult({
        id: "req-1", moveIndex: 2, move: "g1f3", bestMove: "d2d4",
        delta: 250, classification: "blunder", blunder: true, recordable: true,
      }));
    });

    await waitFor(() => expect(recordBlunderMock).toHaveBeenCalledTimes(1));
  });

  it("records a resolved blunder durably even while unmounted (no alert)", async () => {
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
      moveHistory: [
        { san: "e4", fen: "f0", uci: "e2e4" },
        { san: "e5", fen: "f1", uci: "e7e5" },
        { san: "Nf3", fen: "f2", uci: "g1f3" },
      ],
    });

    const host = makeHost();
    const setBlunderAlert = vi.fn();
    const { unmount } = renderEffects(host, { setBlunderAlert });
    unmount(); // drop the UI lease — durable path must still run

    host.decisionOwner.registerBlunderContext("req-1", {
      fen: "fen-2", pgn: "p2", moveSan: "Nf3", moveUci: "g1f3", moveIndex: 2,
    });

    await act(async () => {
      host.resolved(makeResult({ id: "seed-0", moveIndex: 0, delta: 0 }));
      host.resolved(makeResult({ id: "seed-1", moveIndex: 1, delta: 0 }));
      host.resolved(makeResult({
        id: "req-1", moveIndex: 2, move: "g1f3", bestMove: "d2d4",
        delta: 300, classification: "blunder", blunder: true, recordable: true,
      }));
      await Promise.resolve();
    });

    expect(recordBlunderMock).toHaveBeenCalledTimes(1);
    expect(setBlunderAlert).not.toHaveBeenCalled();
  });

  // --- leased SRS UI ---

  it("submits an armed SRS review immediately on a resolved outcome", async () => {
    const analysisId = "analysis-terminal";
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: false,
      isPracticeContinuation: false,
      moveHistory: [{ san: "Qg7#", fen: "mate-fen", uci: "g6g7" }],
    });

    const host = makeHost();
    const appendMoveMessage = vi.fn();
    renderEffects(host, { appendMoveMessage });
    host.decisionOwner.registerSrsReview(analysisId, {
      sessionId: "session-1", blunderId: 42, moveIndex: 0, userMoveSan: "Qg7#",
      srs: { last_reviewed_at: "2026-04-28T00:00:00Z", created_at: null, fail_count: 1, pass_count: 2, pass_streak: 1 },
      srsDecisionId: "decision-pass",
    });

    act(() => {
      host.resolved(makeResult({
        id: analysisId, move: "g6g7", bestMove: "g6e8", moveIndex: 0, delta: 0,
        classification: "excellent",
      }));
    });

    await waitFor(() => {
      expect(reviewSrsBlunderMock).toHaveBeenCalledWith(
        "session-1", 42, true, "Qg7#", 0, expect.any(String),
      );
    });
    expect(appendMoveMessage).toHaveBeenCalledWith(
      0, expect.objectContaining({ variant: "srs-pass" }),
    );
  });

  it("triggers the spotlight + buzzer and reveals the fail on a repeat mistake", async () => {
    const analysisId = "analysis-fail";
    useGameStore.setState({
      sessionId: "session-fail",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
      moveHistory: [
        { san: "Nf3", fen: "fen-before-nf3", uci: "g1f3" },
        { san: "e5", fen: "fen-after-e5", uci: "e7e5" },
      ],
    });

    const host = makeHost();
    const appendMoveMessage = vi.fn();
    const onSrsFail = vi.fn();
    renderEffects(host, { appendMoveMessage, onSrsFail });
    host.decisionOwner.registerSrsReview(analysisId, {
      sessionId: "session-fail", blunderId: 7, moveIndex: 1,
      userMoveSan: "Nf3", srs: null, srsDecisionId: "decision-fail",
    });

    act(() => {
      host.resolved(makeResult({
        id: analysisId, move: "g1f3", bestMove: "f1b5", moveIndex: 1, delta: 250,
      }));
    });

    await waitFor(() => {
      expect(reviewSrsBlunderMock).toHaveBeenCalledWith(
        "session-fail", 7, false, "Nf3", 250, expect.any(String),
      );
    });
    expect(appendMoveMessage).toHaveBeenCalledWith(
      1, expect.objectContaining({ variant: "srs-fail" }),
    );
    expect(mockPlayBuzzer).toHaveBeenCalled();
    expect(onSrsFail).toHaveBeenCalledWith(
      expect.objectContaining({ userMoveSan: "Nf3", userMoveUci: "g1f3" }), 1,
    );
  });

  it("processes SRS immediately even while an earlier index is still pending (I3)", async () => {
    useGameStore.setState({
      sessionId: "live-session",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
      moveHistory: [
        { san: "e4", fen: "fen-after-e4", uci: "e2e4" },
        { san: "Nf6", fen: "fen-after-nf6", uci: "g8f6" },
      ],
    });

    const host = makeHost();
    renderEffects(host);
    host.decisionOwner.registerSrsReview("analysis-two", {
      sessionId: "session-two", blunderId: 99, moveIndex: 1,
      userMoveSan: "Nf6", srs: null, srsDecisionId: "decision-two",
    });

    // Index 0 still pending (only scheduled); index 1 resolves → SRS fires now.
    act(() => {
      host.scheduled(0, "req-0");
      host.resolved(makeResult({ id: "analysis-two", move: "g8f6", moveIndex: 1, delta: 0 }));
    });

    await waitFor(() => expect(reviewSrsBlunderMock).toHaveBeenCalledTimes(1));
    expect(reviewSrsBlunderMock).toHaveBeenCalledWith(
      "session-two", 99, true, "Nf6", 0, expect.any(String),
    );
  });

  it("migrates a pending SRS review across a scheduled retry (N2)", async () => {
    useGameStore.setState({
      sessionId: "s",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
      moveHistory: [
        { san: "e4", fen: "f0", uci: "e2e4" },
        { san: "Nf3", fen: "f1", uci: "g1f3" },
      ],
    });

    const host = makeHost();
    renderEffects(host);
    host.decisionOwner.registerSrsReview("old-id", {
      sessionId: "s", blunderId: 7, moveIndex: 1,
      userMoveSan: "Nf3", srs: null, srsDecisionId: "decision-x",
    });

    act(() => {
      host.skipped(1, "old-id");
      host.scheduled(1, "new-id", "old-id");
    });
    // The slot's live request id migrated to the new request; decision id preserved.
    const slot = host.decisionOwner.findSrsSlotBySrsDecisionId("decision-x");
    expect(slot?.requestId).toBe("new-id");
    expect(slot?.status).toBe("awaiting_analysis");

    act(() => {
      host.resolved(makeResult({ id: "new-id", move: "g1f3", moveIndex: 1, delta: 0 }));
    });

    await waitFor(() => expect(reviewSrsBlunderMock).toHaveBeenCalledTimes(1));
  });

  it("posts nothing when the SRS grade is unavailable (null delta)", () => {
    const analysisId = "analysis-unavailable";
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
    });

    const host = makeHost();
    renderEffects(host);
    host.decisionOwner.registerSrsReview(analysisId, {
      sessionId: "session-one", blunderId: 42, moveIndex: 0,
      userMoveSan: "e4", srs: null, srsDecisionId: "decision-unavailable",
    });

    act(() => {
      host.resolved(makeResult({ id: analysisId, moveIndex: 0, delta: null }));
    });

    expect(reviewSrsBlunderMock).not.toHaveBeenCalled();
  });

  it("on mount keeps a still-pending overlay but clears one resolved while unmounted", () => {
    useGameStore.setState({
      sessionId: "s",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
    });

    const host = makeHost();
    // "live-id" is still pending on the owner; "stale-id" is not (resolved durably
    // while unmounted), so its frozen overlay should clear.
    host.decisionOwner.registerSrsReview("live-id", {
      sessionId: "s", blunderId: 1, moveIndex: 0, userMoveSan: "e4",
      srs: null, srsDecisionId: "d-live",
    });
    const setResolvedReview = vi.fn();
    renderEffects(host, { setResolvedReview });

    // The mount effect clears stale pending overlays via a single updater.
    const updater = setResolvedReview.mock.calls[0][0] as (
      p: ResolvedReview | null,
    ) => ResolvedReview | null;
    const livePending: ResolvedReview = { analysisId: "live-id", moveIndex: 0, result: "pending" };
    const stalePending: ResolvedReview = { analysisId: "stale-id", moveIndex: 1, result: "pending" };
    const resolvedPass: ResolvedReview = { analysisId: "x", moveIndex: 2, result: "pass" };
    expect(updater(livePending)).toEqual(livePending); // still pending → kept
    expect(updater(stalePending)).toBeNull();           // resolved while unmounted → cleared
    expect(updater(resolvedPass)).toEqual(resolvedPass); // non-pending overlays untouched
  });

  it("posts nothing for an unknown analysis id", () => {
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
    });

    const host = makeHost();
    renderEffects(host);

    act(() => {
      host.resolved(makeResult({ id: "unknown-analysis", moveIndex: 0 }));
    });

    expect(reviewSrsBlunderMock).not.toHaveBeenCalled();
  });

  // --- leased blunder alert (microtask coalescing) ---

  it("fires exactly one blunder alert (latest) for two same-batch player blunders (H3/I2)", async () => {
    useGameStore.setState({
      sessionId: "s",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
      moveHistory: [
        { san: "e4", fen: "f0", uci: "e2e4" },
        { san: "e5", fen: "f1", uci: "e7e5" },
        { san: "Nf3", fen: "f2", uci: "g1f3" },
        { san: "Nc6", fen: "f3", uci: "b8c6" },
        { san: "Bc4", fen: "f4", uci: "f1c4" },
      ],
    });

    const host = makeHost();
    const setBlunderAlert = vi.fn();
    const setShowFlash = vi.fn();
    renderEffects(host, { setBlunderAlert, setShowFlash });

    await act(async () => {
      host.resolved(makeResult({
        id: "r2", moveIndex: 2, move: "g1f3", bestMove: "d2d4", delta: 200, blunder: true,
      }));
      host.resolved(makeResult({
        id: "r4", moveIndex: 4, move: "f1c4", bestMove: "d2d4", delta: 300, blunder: true,
      }));
      await Promise.resolve();
    });

    expect(setBlunderAlert).toHaveBeenCalledTimes(1);
    expect(setShowFlash).toHaveBeenCalledTimes(1);
    expect(setBlunderAlert.mock.calls[0][0].moveIndex).toBe(4);
  });

  it("coalesces same-turn alerts to one, separate turns to two (J2)", async () => {
    useGameStore.setState({
      sessionId: "s",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
      moveHistory: [
        { san: "e4", fen: "f0", uci: "e2e4" },
        { san: "e5", fen: "f1", uci: "e7e5" },
        { san: "Nf3", fen: "f2", uci: "g1f3" },
      ],
    });

    const host = makeHost();
    const setBlunderAlert = vi.fn();
    renderEffects(host, { setBlunderAlert });

    await act(async () => {
      host.resolved(makeResult({ id: "r0", moveIndex: 0, move: "e2e4", delta: 200, blunder: true }));
      await Promise.resolve();
    });
    await act(async () => {
      host.resolved(makeResult({ id: "r2", moveIndex: 2, move: "g1f3", delta: 200, blunder: true }));
      await Promise.resolve();
    });

    expect(setBlunderAlert).toHaveBeenCalledTimes(2);
  });
});
