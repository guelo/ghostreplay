/**
 * Table-driven outcome-stability regressions (g-repair-drill-cache, AC5-7,
 * frontend half).
 *
 * The GameAnalysisCoordinator already guarantees that every analysis index
 * reaches exactly ONE terminal outcome regardless of cache/worker completion
 * order (see GameAnalysisCoordinator.test.ts "cache-first authoritative
 * resolution"). This suite picks up at the next seam — AnalysisEffects, the
 * single outcome-channel consumer that turns a resolved outcome into an SRS
 * review, a blunder recording, and the displayed move message — and asserts
 * those decisions are:
 *
 *   - deterministic across the recordable boundary (eval loss at 50-1 / 50 /
 *     50+1 and representative values either side), and
 *   - applied EXACTLY ONCE and order-invariant: a redundant late `resolved`
 *     (the losing side of a cache/worker race) for the same index must not
 *     produce a second SRS request or a second recording.
 *
 * SRS and recording both grade on the recordable comparator (eval loss >= 50
 * fails) — independent of drill strictness, so the strictness axis does NOT
 * belong here; post-root drill pass/fail across the strictness matrix is covered
 * by the real drill flow in ChessGame.test.tsx ("post-root drill outcome
 * stability (AC4)"). Persistence-side exactly-once (DB rows, counters) is in
 * backend/test_analysis_outcome_stability.py.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, act, waitFor } from "../../test/utils";
import { createRef } from "react";
import AnalysisEffects from "./AnalysisEffects";
import { useGameStore } from "../../stores/useGameStore";
import {
  AnalysisStoreProvider,
  createAnalysisStore,
} from "../../stores/createAnalysisStore";
import type { AnalysisResult } from "../../hooks/useMoveAnalysis";
import {
  RECORDABLE_FAILURE_THRESHOLD_CP,
  isRecordableFailure,
} from "../../workers/analysisUtils";

const recordBlunderMock = vi.fn();
const reviewSrsBlunderMock = vi.fn();
vi.mock("../../utils/blingSound", () => ({ playBling: () => {} }));
vi.mock("../../utils/buzzerSound", () => ({ playBuzzer: () => {} }));
vi.mock("./blunderAudio", () => ({ playRandomBlunderAudio: () => {} }));
vi.mock("../../utils/api", () => ({
  recordBlunder: (...args: unknown[]) => recordBlunderMock(...args),
  reviewSrsBlunder: (...args: unknown[]) => reviewSrsBlunderMock(...args),
}));

// SRS/recording grade on the recordable comparator (>= 50). Sweep deltas
// straddling that boundary plus representative values on either side.
const RECORDABLE_DELTAS = [0, 25, 40, 49, 50, 51, 120, 250];

const makeResult = (overrides: Partial<AnalysisResult> = {}): AnalysisResult => ({
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
    scheduled(moveIndex: number, requestId: string) {
      this.emit({ moveIndex, requestId, status: "scheduled" });
    },
    resolved(result: AnalysisResult, requestId = result.id) {
      this.emit({ moveIndex: result.moveIndex, requestId, status: "resolved", result });
    },
  };
}

describe("AnalysisEffects — outcome stability matrix", () => {
  beforeEach(() => {
    recordBlunderMock.mockReset();
    reviewSrsBlunderMock.mockReset();
    recordBlunderMock.mockResolvedValue({});
    reviewSrsBlunderMock.mockResolvedValue({});
    useGameStore.setState(initialGameState, true);
  });

  function renderSrs(
    store: ReturnType<typeof createAnalysisStore>,
    channel: ReturnType<typeof createChannel>,
    pendingSrsReviewRef: { current: Map<string, any> },
    appendMoveMessage = vi.fn(),
  ) {
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
    return appendMoveMessage;
  }

  describe.each(RECORDABLE_DELTAS)(
    "SRS review at delta=%i",
    (delta) => {
      const shouldPass = !isRecordableFailure(delta);

      it.each([
        ["resolved only", false, false],
        ["scheduled then resolved", true, false],
        ["resolved then duplicate late resolved", false, true],
      ])(
        "fires exactly one review with passed=%s-correct (%s)",
        async (_label, withScheduled, withDuplicate) => {
          useGameStore.setState({
            sessionId: "session-x",
            playerColor: "white",
            isGameActive: false,
            isPracticeContinuation: false,
            moveHistory: [{ san: "Nf3", fen: "fen-0", uci: "g1f3" }],
          });
          const store = createAnalysisStore();
          const channel = createChannel();
          const analysisId = "srs-analysis";
          const pendingSrsReviewRef = {
            current: new Map([
              [analysisId, {
                sessionId: "session-x",
                analysisId,
                blunderId: 42,
                moveIndex: 0,
                userMoveSan: "Nf3",
                srs: null,
              }],
            ]),
          };
          const appendMoveMessage = renderSrs(store, channel, pendingSrsReviewRef);

          const result = makeResult({
            id: analysisId,
            moveIndex: 0,
            move: "g1f3",
            delta,
            recordable: isRecordableFailure(delta),
          });

          act(() => {
            if (withScheduled) channel.scheduled(0, analysisId);
            channel.resolved(result);
            if (withDuplicate) channel.resolved({ ...result }, analysisId);
          });

          await waitFor(() =>
            expect(reviewSrsBlunderMock).toHaveBeenCalledWith(
              "session-x", 42, shouldPass, "Nf3", delta,
            ),
          );
          // Exactly once regardless of completion order / duplicate emission.
          expect(reviewSrsBlunderMock).toHaveBeenCalledTimes(1);
          expect(pendingSrsReviewRef.current.size).toBe(0);
          expect(appendMoveMessage).toHaveBeenCalledWith(
            0,
            expect.objectContaining({ variant: shouldPass ? "srs-pass" : "srs-fail" }),
          );
        },
      );
    },
  );

  describe.each([14, 15, 49, 50, 51, 250])(
    "auto-recording at delta=%i",
    (delta) => {
      const recordable = delta >= RECORDABLE_FAILURE_THRESHOLD_CP;

      it("records iff recordable, exactly once, ignoring a duplicate late resolve", async () => {
        useGameStore.setState({
          sessionId: "session-r",
          playerColor: "white",
          isGameActive: true,
          isPracticeContinuation: false,
          moveHistory: [{ san: "Qh5", fen: "fen-0", uci: "d1h5" }],
        });
        const store = createAnalysisStore();
        const channel = createChannel();
        const pendingAnalysisContextRef = {
          current: new Map([
            ["rec-0", {
              fen: "fen-0", pgn: "1. e4 e5 2. Qh5", moveSan: "Qh5",
              moveUci: "d1h5", moveIndex: 2,
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
              pendingSrsReviewRef={{ current: new Map() } as any}
              appendMoveMessage={vi.fn()}
              setBlunderAlert={vi.fn()}
              setShowFlash={vi.fn()}
              setResolvedReview={vi.fn()}
              onSrsFail={vi.fn()}
              coordinator={channel as any}
            />
          </AnalysisStoreProvider>,
        );

        const result = makeResult({
          id: "rec-0",
          moveIndex: 2,
          move: "d1h5",
          delta,
          classification: recordable ? "blunder" : "inaccuracy",
          blunder: recordable,
          recordable,
        });

        act(() => {
          // Drain the frontier so index 2 is the recording frontier.
          channel.scheduled(0, "s0");
          channel.scheduled(1, "s1");
          (channel as any).emit({ moveIndex: 0, requestId: "s0", status: "skipped" });
          (channel as any).emit({ moveIndex: 1, requestId: "s1", status: "skipped" });
          channel.resolved(result);
          channel.resolved({ ...result }, "rec-0"); // duplicate late resolve
        });

        if (recordable) {
          await waitFor(() => expect(recordBlunderMock).toHaveBeenCalledTimes(1));
        } else {
          // Give any async path a tick, then assert it never recorded.
          await act(async () => { await Promise.resolve(); });
          expect(recordBlunderMock).not.toHaveBeenCalled();
        }
      });
    },
  );
});
