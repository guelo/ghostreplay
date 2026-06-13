import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, act, waitFor } from "../../test/utils";
import AnalysisEffects from "./AnalysisEffects";
import { useGameStore } from "../../stores/useGameStore";
import {
  AnalysisStoreProvider,
  createAnalysisStore,
} from "../../stores/createAnalysisStore";
import type { AnalysisResult } from "../../hooks/useMoveAnalysis";
import { createRef } from "react";

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
vi.mock("../../utils/api", () => ({
  recordBlunder: (...args: unknown[]) => recordBlunderMock(...args),
  reviewSrsBlunder: (...args: unknown[]) => reviewSrsBlunderMock(...args),
}));

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

describe("AnalysisEffects — best-move bling", () => {
  let store: ReturnType<typeof createAnalysisStore>;

  beforeEach(() => {
    mockPlayBling.mockClear();
    mockPlayBuzzer.mockClear();
    recordBlunderMock.mockReset();
    reviewSrsBlunderMock.mockReset();
    recordBlunderMock.mockResolvedValue({});
    reviewSrsBlunderMock.mockResolvedValue({});
    useGameStore.setState(initialGameState, true);
    store = createAnalysisStore();
  });

  function createPendingSrsReviewRef(entries: Array<[string, any]> = []) {
    return { current: new Map(entries) };
  }

  // Controllable in-memory outcome channel, injected via the coordinator prop so
  // tests drive recording/SRS/alert through the real AnalysisOutcome consumer.
  function createChannel() {
    const outcomeListeners = new Set<(o: any) => void>();
    const resetListeners = new Set<(i: any) => void>();
    let seq = 0;
    return {
      getEpoch: () => ({ generation: 0, sessionId: null }),
      addAnalysisOutcomeListener: (cb: (o: any) => void) => {
        outcomeListeners.add(cb);
        return () => outcomeListeners.delete(cb);
      },
      addAnalysisResetListener: (cb: (i: any) => void) => {
        resetListeners.add(cb);
        return () => resetListeners.delete(cb);
      },
      emit(o: any) {
        const outcome = { seq: seq++, generation: 0, sessionId: null, ...o };
        for (const cb of outcomeListeners) cb(outcome);
      },
      scheduled(moveIndex: number, requestId: string, previousRequestId?: string) {
        this.emit({ moveIndex, requestId, status: "scheduled", previousRequestId });
      },
      resolved(result: AnalysisResult, requestId = result.id) {
        this.emit({ moveIndex: result.moveIndex, requestId, status: "resolved", result });
      },
      skipped(moveIndex: number, requestId = `skip-${moveIndex}`) {
        this.emit({ moveIndex, requestId, status: "skipped" });
      },
      failed(moveIndex: number, requestId = `fail-${moveIndex}`) {
        this.emit({ moveIndex, requestId, status: "failed" });
      },
      reset(info: any = {}) {
        const payload = { generation: 0, sessionId: null, ...info };
        for (const cb of resetListeners) cb(payload);
      },
    };
  }

  function renderEffects() {
    return render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={{ current: new Map() } as any}
          blunderRecordedRef={createRef() as any}
          pendingSrsReviewRef={createPendingSrsReviewRef() as any}
          appendMoveMessage={vi.fn()}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          setResolvedReview={vi.fn()}
          onSrsFail={vi.fn()}
          coordinator={createChannel() as any}
        />
      </AnalysisStoreProvider>,
    );
  }

  it("plays bling when best-move analysis arrives for a player move", () => {
    useGameStore.setState({ playerColor: "white", isGameActive: true });
    renderEffects();

    // Player's move (index 0 = white = player)
    act(() => {
      store.getState().resolveAnalysis(0, makeResult({
        moveIndex: 0,
        classification: "best",
      }));
    });

    expect(mockPlayBling).toHaveBeenCalledTimes(1);
  });

  it("plays bling when best-move analysis arrives for a black player move", () => {
    useGameStore.setState({ playerColor: "black", isGameActive: true });
    renderEffects();

    // Player's move (index 1 = black = player)
    act(() => {
      store.getState().resolveAnalysis(1, makeResult({
        moveIndex: 1,
        classification: "best",
      }));
    });

    expect(mockPlayBling).toHaveBeenCalledTimes(1);
  });

  it("does NOT play bling for engine moves with best classification", () => {
    useGameStore.setState({ playerColor: "white", isGameActive: true });
    renderEffects();

    // Engine move (index 1 = black = engine when player is white)
    act(() => {
      store.getState().resolveAnalysis(1, makeResult({
        moveIndex: 1,
        classification: "best",
      }));
    });

    expect(mockPlayBling).not.toHaveBeenCalled();
  });

  it("does NOT play bling for non-best player moves", () => {
    useGameStore.setState({ playerColor: "white", isGameActive: true });
    renderEffects();

    act(() => {
      store.getState().resolveAnalysis(0, makeResult({
        moveIndex: 0,
        classification: "good",
      }));
    });

    expect(mockPlayBling).not.toHaveBeenCalled();
  });


  it("does not record blunders during practice continuation", () => {
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: true,
    });

    const channel = createChannel();
    const pendingAnalysisContextRef = {
      current: new Map([
        ["req-1", {
          fen: "fen-before",
          pgn: "1. e4",
          moveSan: "e4",
          moveUci: "e2e4",
          moveIndex: 1,
        }],
      ]),
    };
    const blunderRecordedRef = createRef<any>();
    blunderRecordedRef.current = false;

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={pendingAnalysisContextRef as any}
          blunderRecordedRef={blunderRecordedRef}
          pendingSrsReviewRef={createPendingSrsReviewRef() as any}
          appendMoveMessage={vi.fn()}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          setResolvedReview={vi.fn()}
          onSrsFail={vi.fn()}
          coordinator={channel as any}
        />
      </AnalysisStoreProvider>,
    );

    act(() => {
      channel.skipped(0);
      channel.resolved(makeResult({
        id: "req-1",
        moveIndex: 1,
        move: "e2e4",
        bestMove: "d2d4",
        delta: 250,
        classification: "blunder",
        blunder: true,
        recordable: true,
      }));
    });

    expect(recordBlunderMock).not.toHaveBeenCalled();
  });

  it("records the first recordable blunder via the outcome channel (Finding F4)", async () => {
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
    });

    const channel = createChannel();
    const pendingAnalysisContextRef = {
      current: new Map([
        ["req-1", {
          fen: "fen-before",
          pgn: "1. e4 e5 2. Nf3",
          moveSan: "Nf3",
          moveUci: "g1f3",
          moveIndex: 2,
        }],
      ]),
    };
    const blunderRecordedRef = createRef<any>();
    blunderRecordedRef.current = false;

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={pendingAnalysisContextRef as any}
          blunderRecordedRef={blunderRecordedRef}
          pendingSrsReviewRef={createPendingSrsReviewRef() as any}
          appendMoveMessage={vi.fn()}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          setResolvedReview={vi.fn()}
          onSrsFail={vi.fn()}
          coordinator={channel as any}
        />
      </AnalysisStoreProvider>,
    );

    // A single batch resolves index 2 (recordable) only after index 0,1 terminate.
    act(() => {
      channel.skipped(0);
      channel.skipped(1);
      channel.resolved(makeResult({
        id: "req-1",
        moveIndex: 2,
        move: "g1f3",
        bestMove: "d2d4",
        delta: 250,
        classification: "blunder",
        blunder: true,
        recordable: true,
      }));
    });

    await waitFor(() => {
      expect(recordBlunderMock).toHaveBeenCalledTimes(1);
    });
    expect(blunderRecordedRef.current).toBe(true);
  });

  it("blocks recording behind a still-pending earlier scheduled slot, then drains in order (P0)", async () => {
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
    });

    const channel = createChannel();
    const pendingAnalysisContextRef = {
      current: new Map<string, any>([
        ["req-2", {
          fen: "fen-2", pgn: "p2", moveSan: "Nf3", moveUci: "g1f3", moveIndex: 2,
        }],
      ]),
    };
    const blunderRecordedRef = createRef<any>();
    blunderRecordedRef.current = false;

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={pendingAnalysisContextRef as any}
          blunderRecordedRef={blunderRecordedRef}
          pendingSrsReviewRef={createPendingSrsReviewRef() as any}
          appendMoveMessage={vi.fn()}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          setResolvedReview={vi.fn()}
          onSrsFail={vi.fn()}
          coordinator={channel as any}
        />
      </AnalysisStoreProvider>,
    );

    // Index 1 is still `pending` (scheduled, not yet terminal); index 2 resolves
    // recordable but MUST NOT record while the frontier is blocked at 1.
    await act(async () => {
      channel.skipped(0);
      channel.scheduled(1, "req-1");
      channel.resolved(makeResult({
        id: "req-2", moveIndex: 2, move: "g1f3", bestMove: "d2d4",
        delta: 250, classification: "blunder", blunder: true, recordable: true,
      }));
      await Promise.resolve();
    });
    expect(recordBlunderMock).not.toHaveBeenCalled();

    // Index 1 terminates → frontier drains → index 2 records exactly once.
    act(() => {
      channel.resolved(makeResult({
        id: "req-1", moveIndex: 1, move: "g8f6", delta: 0, classification: "good",
      }));
    });
    await waitFor(() => expect(recordBlunderMock).toHaveBeenCalledTimes(1));
  });

  it("records the EARLIER blunder when two indices resolve in one batch (F4 context-correct)", async () => {
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
    });

    const channel = createChannel();
    const pendingAnalysisContextRef = {
      current: new Map<string, any>([
        ["req-2", {
          fen: "fen-2", pgn: "p2", moveSan: "Nf3", moveUci: "g1f3", moveIndex: 2,
        }],
        ["req-4", {
          fen: "fen-4", pgn: "p4", moveSan: "Bc4", moveUci: "f1c4", moveIndex: 4,
        }],
      ]),
    };
    const blunderRecordedRef = createRef<any>();
    blunderRecordedRef.current = false;

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={pendingAnalysisContextRef as any}
          blunderRecordedRef={blunderRecordedRef}
          pendingSrsReviewRef={createPendingSrsReviewRef() as any}
          appendMoveMessage={vi.fn()}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          setResolvedReview={vi.fn()}
          onSrsFail={vi.fn()}
          coordinator={channel as any}
        />
      </AnalysisStoreProvider>,
    );

    // Indices 0,1,3 terminate non-recordably; 2 and 4 both resolve recordable in
    // the same synchronous batch — only the earlier (index 2) is recorded.
    act(() => {
      channel.skipped(0);
      channel.skipped(1);
      channel.skipped(3);
      channel.resolved(makeResult({
        id: "req-4", moveIndex: 4, move: "f1c4", bestMove: "d2d4",
        delta: 300, classification: "blunder", blunder: true, recordable: true,
      }));
      channel.resolved(makeResult({
        id: "req-2", moveIndex: 2, move: "g1f3", bestMove: "d2d4",
        delta: 250, classification: "blunder", blunder: true, recordable: true,
      }));
    });

    await waitFor(() => {
      expect(recordBlunderMock).toHaveBeenCalledTimes(1);
    });
    // Recorded with index-2's own move context.
    expect(recordBlunderMock).toHaveBeenCalledWith(
      "session-1", "p2", "fen-2", "Nf3", expect.any(String), expect.any(Number), expect.any(Number),
    );
  });

  it("submits an armed SRS review immediately on a resolved outcome", async () => {
    const analysisId = "analysis-terminal";
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: false,
      isPracticeContinuation: false,
      moveHistory: [{ san: "Qg7#", fen: "mate-fen", uci: "g6g7" }],
    });

    const channel = createChannel();
    const pendingSrsReviewRef = createPendingSrsReviewRef([
      [analysisId, {
        sessionId: "session-1", analysisId, blunderId: 42, moveIndex: 0,
        userMoveSan: "Qg7#",
        srs: { due_at: "2026-04-28T00:00:00Z", fail_count: 1, interval_days: 1, pass_count: 2, pass_streak: 1, state: "due" },
      }],
    ]);
    const appendMoveMessage = vi.fn();
    const setResolvedReview = vi.fn((updater) => {
      if (typeof updater === "function") {
        updater({ analysisId, moveIndex: 0, result: "pending" });
      }
    });

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={{ current: new Map() } as any}
          blunderRecordedRef={createRef() as any}
          pendingSrsReviewRef={pendingSrsReviewRef}
          appendMoveMessage={appendMoveMessage}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          setResolvedReview={setResolvedReview}
          onSrsFail={vi.fn()}
          coordinator={channel as any}
        />
      </AnalysisStoreProvider>,
    );

    act(() => {
      channel.resolved(makeResult({
        id: analysisId, move: "g6g7", bestMove: "g6e8", moveIndex: 0, delta: 0,
        classification: "excellent",
      }));
    });

    await waitFor(() => {
      expect(reviewSrsBlunderMock).toHaveBeenCalledWith("session-1", 42, true, "Qg7#", 0);
    });
    expect(pendingSrsReviewRef.current.size).toBe(0);
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

    const channel = createChannel();
    const pendingSrsReviewRef = createPendingSrsReviewRef([
      [analysisId, {
        sessionId: "session-fail", analysisId, blunderId: 7, moveIndex: 1,
        userMoveSan: "Nf3", srs: null,
      }],
    ]);
    const appendMoveMessage = vi.fn();
    const onSrsFail = vi.fn();

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={{ current: new Map() } as any}
          blunderRecordedRef={createRef() as any}
          pendingSrsReviewRef={pendingSrsReviewRef as any}
          appendMoveMessage={appendMoveMessage}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          setResolvedReview={vi.fn()}
          onSrsFail={onSrsFail}
          coordinator={channel as any}
        />
      </AnalysisStoreProvider>,
    );

    act(() => {
      channel.resolved(makeResult({
        id: analysisId, move: "g1f3", bestMove: "f1b5", moveIndex: 1, delta: 250,
      }));
    });

    await waitFor(() => {
      expect(reviewSrsBlunderMock).toHaveBeenCalledWith("session-fail", 7, false, "Nf3", 250);
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

    const channel = createChannel();
    const pendingSrsReviewRef = createPendingSrsReviewRef([
      ["analysis-two", {
        sessionId: "session-two", analysisId: "analysis-two", blunderId: 99,
        moveIndex: 1, userMoveSan: "Nf6", srs: null,
      }],
    ]);
    const appendMoveMessage = vi.fn();

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={{ current: new Map() } as any}
          blunderRecordedRef={createRef() as any}
          pendingSrsReviewRef={pendingSrsReviewRef as any}
          appendMoveMessage={appendMoveMessage}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          setResolvedReview={vi.fn()}
          onSrsFail={vi.fn()}
          coordinator={channel as any}
        />
      </AnalysisStoreProvider>,
    );

    // Index 0 still pending (only scheduled); index 1 resolves → SRS fires now.
    act(() => {
      channel.scheduled(0, "req-0");
      channel.resolved(makeResult({
        id: "analysis-two", move: "g8f6", moveIndex: 1, delta: 0,
      }));
    });

    await waitFor(() => {
      expect(reviewSrsBlunderMock).toHaveBeenCalledTimes(1);
    });
    expect(reviewSrsBlunderMock).toHaveBeenCalledWith("session-two", 99, true, "Nf6", 0);
  });

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

    const channel = createChannel();
    const setBlunderAlert = vi.fn();
    const setShowFlash = vi.fn();

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={{ current: new Map() } as any}
          blunderRecordedRef={createRef() as any}
          pendingSrsReviewRef={createPendingSrsReviewRef() as any}
          appendMoveMessage={vi.fn()}
          setBlunderAlert={setBlunderAlert}
          setShowFlash={setShowFlash}
          setResolvedReview={vi.fn()}
          onSrsFail={vi.fn()}
          coordinator={channel as any}
        />
      </AnalysisStoreProvider>,
    );

    // Two player blunders (index 2 and 4) resolve in one synchronous turn.
    await act(async () => {
      channel.resolved(makeResult({
        id: "r2", moveIndex: 2, move: "g1f3", bestMove: "d2d4", delta: 200,
        blunder: true,
      }));
      channel.resolved(makeResult({
        id: "r4", moveIndex: 4, move: "f1c4", bestMove: "d2d4", delta: 300,
        blunder: true,
      }));
      await Promise.resolve();
    });

    // One coalesced alert, for the highest index (4).
    expect(setBlunderAlert).toHaveBeenCalledTimes(1);
    expect(setShowFlash).toHaveBeenCalledTimes(1);
    const alertArg = setBlunderAlert.mock.calls[0][0];
    expect(alertArg.moveIndex).toBe(4);
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

    const channel = createChannel();
    const setBlunderAlert = vi.fn();

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={{ current: new Map() } as any}
          blunderRecordedRef={createRef() as any}
          pendingSrsReviewRef={createPendingSrsReviewRef() as any}
          appendMoveMessage={vi.fn()}
          setBlunderAlert={setBlunderAlert}
          setShowFlash={vi.fn()}
          setResolvedReview={vi.fn()}
          onSrsFail={vi.fn()}
          coordinator={channel as any}
        />
      </AnalysisStoreProvider>,
    );

    await act(async () => {
      channel.resolved(makeResult({ id: "r0", moveIndex: 0, move: "e2e4", delta: 200, blunder: true }));
      await Promise.resolve();
    });
    await act(async () => {
      channel.resolved(makeResult({ id: "r2", moveIndex: 2, move: "g1f3", delta: 200, blunder: true }));
      await Promise.resolve();
    });

    expect(setBlunderAlert).toHaveBeenCalledTimes(2);
  });

  it("does not re-record an earlier retried index past the committed boundary (M2)", async () => {
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
    });

    const channel = createChannel();
    const pendingAnalysisContextRef = {
      current: new Map<string, any>([
        ["req-3", {
          fen: "fen-3", pgn: "p3", moveSan: "Bc4", moveUci: "f1c4", moveIndex: 3,
        }],
      ]),
    };
    const blunderRecordedRef = createRef<any>();
    blunderRecordedRef.current = false;

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={pendingAnalysisContextRef as any}
          blunderRecordedRef={blunderRecordedRef}
          pendingSrsReviewRef={createPendingSrsReviewRef() as any}
          appendMoveMessage={vi.fn()}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          setResolvedReview={vi.fn()}
          onSrsFail={vi.fn()}
          coordinator={channel as any}
        />
      </AnalysisStoreProvider>,
    );

    // Index 1 fails, index 3 recorded → committedDecisionIndex advances past 3.
    act(() => {
      channel.scheduled(0, "req-0");
      channel.failed(0, "req-0");
      channel.scheduled(1, "req-1");
      channel.failed(1, "req-1");
      channel.skipped(2);
      channel.resolved(makeResult({
        id: "req-3", moveIndex: 3, move: "f1c4", bestMove: "d2d4",
        delta: 300, classification: "blunder", blunder: true, recordable: true,
      }));
    });

    await waitFor(() => expect(recordBlunderMock).toHaveBeenCalledTimes(1));
    recordBlunderMock.mockClear();
    blunderRecordedRef.current = false; // even if the flag were reset...

    // Index 1 retried (scheduled with previousRequestId) then resolves recordable.
    pendingAnalysisContextRef.current.set("req-1b", {
      fen: "fen-1", pgn: "p1", moveSan: "Nf3", moveUci: "g1f3", moveIndex: 1,
    });
    act(() => {
      channel.scheduled(1, "req-1b", "req-1");
      channel.resolved(makeResult({
        id: "req-1b", moveIndex: 1, move: "g1f3", bestMove: "d2d4",
        delta: 250, classification: "blunder", blunder: true, recordable: true,
      }));
    });

    // Monotonic boundary: no new recording for the reverted-past index.
    await Promise.resolve();
    expect(recordBlunderMock).not.toHaveBeenCalled();
  });

  it("migrates pending SRS old->new id on a scheduled retry (N2)", async () => {
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

    const channel = createChannel();
    const pendingSrsReviewRef = createPendingSrsReviewRef([
      ["old-id", {
        sessionId: "s", analysisId: "old-id", blunderId: 7, moveIndex: 1,
        userMoveSan: "Nf3", srs: null, srsDecisionId: "decision-x",
      }],
    ]);

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={{ current: new Map() } as any}
          blunderRecordedRef={createRef() as any}
          pendingSrsReviewRef={pendingSrsReviewRef as any}
          appendMoveMessage={vi.fn()}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          setResolvedReview={vi.fn()}
          onSrsFail={vi.fn()}
          coordinator={channel as any}
        />
      </AnalysisStoreProvider>,
    );

    act(() => {
      channel.scheduled(1, "new-id", "old-id");
    });
    // Re-keyed old->new, decision id preserved.
    expect(pendingSrsReviewRef.current.has("old-id")).toBe(false);
    expect(pendingSrsReviewRef.current.get("new-id")?.srsDecisionId).toBe("decision-x");

    act(() => {
      channel.resolved(makeResult({
        id: "new-id", move: "g1f3", moveIndex: 1, delta: 0,
      }));
    });

    await waitFor(() => expect(reviewSrsBlunderMock).toHaveBeenCalledTimes(1));
  });

  it("retains the pending entry and posts nothing when the SRS grade is unavailable (null delta)", () => {
    const analysisId = "analysis-unavailable";
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
    });

    const channel = createChannel();
    const pendingSrsReviewRef = createPendingSrsReviewRef([
      [analysisId, {
        sessionId: "session-one", analysisId, blunderId: 42, moveIndex: 0,
        userMoveSan: "e4", srs: null,
      }],
    ]);
    const setResolvedReview = vi.fn();

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={{ current: new Map() } as any}
          blunderRecordedRef={createRef() as any}
          pendingSrsReviewRef={pendingSrsReviewRef as any}
          appendMoveMessage={vi.fn()}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          setResolvedReview={setResolvedReview}
          onSrsFail={vi.fn()}
          coordinator={channel as any}
        />
      </AnalysisStoreProvider>,
    );

    act(() => {
      channel.resolved(makeResult({ id: analysisId, moveIndex: 0, delta: null }));
    });

    expect(pendingSrsReviewRef.current.has(analysisId)).toBe(true);
    expect(reviewSrsBlunderMock).not.toHaveBeenCalled();
    expect(setResolvedReview).not.toHaveBeenCalled();
  });

  it("keeps pending SRS entries for unknown analysis ids and move-index mismatches", () => {
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: false,
    });

    const channel = createChannel();
    const pendingSrsReviewRef = createPendingSrsReviewRef([
      ["analysis-one", {
        sessionId: "session-one", analysisId: "analysis-one", blunderId: 42,
        moveIndex: 0, userMoveSan: "e4", srs: null,
      }],
    ]);

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={{ current: new Map() } as any}
          blunderRecordedRef={createRef() as any}
          pendingSrsReviewRef={pendingSrsReviewRef as any}
          appendMoveMessage={vi.fn()}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          setResolvedReview={vi.fn()}
          onSrsFail={vi.fn()}
          coordinator={channel as any}
        />
      </AnalysisStoreProvider>,
    );

    act(() => {
      channel.resolved(makeResult({ id: "unknown-analysis", moveIndex: 0 }));
      channel.resolved(makeResult({ id: "analysis-one", moveIndex: 1 }));
    });

    expect(Array.from(pendingSrsReviewRef.current.keys())).toEqual(["analysis-one"]);
    expect(reviewSrsBlunderMock).not.toHaveBeenCalled();
  });
});
