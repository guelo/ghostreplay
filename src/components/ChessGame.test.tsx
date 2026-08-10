import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { Chess } from "chess.js";
import { cleanup } from "@testing-library/react";
import { render, screen, fireEvent, waitFor, act, within } from "../test/utils";
import ChessGame from "./ChessGame";
import { useGameStore } from "../stores/useGameStore";
import { STARTING_FEN, MAIA_ELO_BINS, MAIA_BOT_NAMES } from "./chess-game/config";
import { setMatchMedia } from "../test/setup";
import { GAME_MOBILE_QUERY } from "../styles/breakpoints";
import type { AnalysisResult } from "../hooks/useMoveAnalysis";

const startGameMock = vi.fn();
const endGameMock = vi.fn();
const uploadSessionMovesMock = vi.fn();
const getNextOpponentMoveMock = vi.fn();
const continueDrillMock = vi.fn();
const failDrillMock = vi.fn();
const checkDrillRouteMock = vi.fn();
const naturalEndDrillMock = vi.fn();
const abandonDrillMock = vi.fn();
const startDrillMock = vi.fn();
const getOpeningRootsMock = vi.fn();
const recordBlunderMock = vi.fn();
const recordManualBlunderMock = vi.fn();
const reviewSrsBlunderMock = vi.fn();
const fetchCurrentRatingMock = vi.fn();
const getStatsAchievementsMock = vi.fn();
const fetchSessionOpeningsMock = vi.fn();
const pollFreshOpeningDeltaMock = vi.fn();
const audioPlayMock = vi.fn();
const audioCtorSpy = vi.fn();
const captureEventMock = vi.fn();

vi.mock("../analytics/posthog", () => ({
  captureEvent: (...args: unknown[]) => captureEventMock(...args),
}));

// Spread the real module so ApiError/errorCodeOf stay intact — the
// coordinator-owned DecisionOwner (g-2m0p) depends on them for retry
// classification; only the listed endpoints are spied.
vi.mock("../utils/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../utils/api")>();
  return {
    ...actual,
    startGame: (...args: unknown[]) => startGameMock(...args),
    endGame: (...args: unknown[]) => endGameMock(...args),
    uploadSessionMoves: (...args: unknown[]) => uploadSessionMovesMock(...args),
    newClientRequestId: () => "final-request-123",
    getNextOpponentMove: (...args: unknown[]) => getNextOpponentMoveMock(...args),
    continueDrill: (...args: unknown[]) => continueDrillMock(...args),
    failDrill: (...args: unknown[]) => failDrillMock(...args),
    checkDrillRoute: (...args: unknown[]) => checkDrillRouteMock(...args),
    naturalEndDrill: (...args: unknown[]) => naturalEndDrillMock(...args),
    abandonDrill: (...args: unknown[]) => abandonDrillMock(...args),
    startDrill: (...args: unknown[]) => startDrillMock(...args),
    getOpeningRoots: (...args: unknown[]) => getOpeningRootsMock(...args),
    fetchCurrentRating: (...args: unknown[]) => fetchCurrentRatingMock(...args),
    getStatsAchievements: (...args: unknown[]) => getStatsAchievementsMock(...args),
    recordBlunder: (...args: unknown[]) => recordBlunderMock(...args),
    recordManualBlunder: (...args: unknown[]) => recordManualBlunderMock(...args),
    reviewSrsBlunder: (...args: unknown[]) => reviewSrsBlunderMock(...args),
    // The live opening-lineage hook (useSessionOpenings) calls this; without an
    // override it would fall through to the real network helper in every test.
    fetchSessionOpenings: (...args: unknown[]) =>
      fetchSessionOpeningsMock(...args),
  };
});

// The drill accuracy-fail handler fires the reconcile-poll (g-fix-end-latency).
// The api mock above spreads importOriginal, so the real helper would hit the
// network — stub it as a spy and assert it fires with the failing session id.
vi.mock("../utils/openingDeltaPoll", () => ({
  pollFreshOpeningDelta: (...args: unknown[]) =>
    pollFreshOpeningDeltaMock(...args),
  abortOpeningDeltaPolls: () => {},
}));

const evaluatePositionMock = vi.fn();
const lookupOpeningByFenMock = vi.fn();
let stockfishStatus = "ready";

vi.mock("../hooks/useStockfishEngine", () => ({
  useStockfishEngine: () => ({
    status: stockfishStatus,
    error: null,
    info: [],
    isThinking: false,
    evaluatePosition: evaluatePositionMock,
    resetEngine: vi.fn(),
  }),
}));

vi.mock("../openings/openingBook", () => ({
  lookupOpeningByFen: (...args: unknown[]) => lookupOpeningByFenMock(...args),
  prewarmOpeningBook: () => {},
}));

// Mutable router state so individual tests can supply location.state (e.g. the
// "return from drill analysis" marker) and assert replace navigation.
let mockLocation: { state: unknown; pathname: string } = {
  state: null,
  pathname: "/play",
};
const mockNavigate = vi.fn();
vi.mock("react-router-dom", () => ({
  useLocation: () => mockLocation,
  useNavigate: () => mockNavigate,
  // The expanded opening-lineage card renders a "View in Openings" Link footer.
  Link: ({
    to,
    children,
    ...rest
  }: {
    to: string;
    children?: import("react").ReactNode;
    className?: string;
  }) => (
    <a href={to} {...rest}>
      {children}
    </a>
  ),
}));

import { gameAnalysisStore } from "../stores/createAnalysisStore";
import { useDrillAnalysisStore } from "../stores/drillAnalysisStore";
import {
  DecisionOwner,
  type DecisionOwnerGameState,
} from "../services/DecisionOwner";
import type { AnalysisOutcome } from "../services/GameAnalysisCoordinator";
import { gradeDrillMove } from "../workers/analysisUtils";
import { __resetOpeningRootIndexCache } from "../hooks/useLiveOpeningLineage";

// Fresh coordinator-lifetime DecisionOwner per test (g-2m0p). The controller
// registers blunder-context/SRS on this owner; AnalysisEffects leases its UI
// callbacks onto it; the store bridge below routes resolved analyses into it.
const createTestDecisionOwner = () =>
  new DecisionOwner({
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

/** The live FEN after 1.e4 — the position an opponent-reached root confirmation is
 *  about, and what the barrier's staleness guard compares against. */
const E4_FEN = (() => {
  const board = new Chess();
  board.move("e4");
  return board.fen();
})();

/**
 * A served route move that REACHES the drill root. Serving it transitions nothing:
 * the client must apply it and then confirm before the drill is root-reached.
 */
const openingRootPendingResponse = (decisionId: string | null = "decision-1") => ({
  mode: "ghost" as const,
  move: { uci: "e2e4", san: "e4" },
  target_blunder_id: null,
  decision_source: "ghost_path" as const,
  ...(decisionId === null ? {} : { decision_id: decisionId }),
  drill_route: {
    status: "root_pending" as const,
    target_fen: E4_FEN,
    resulting_fen: E4_FEN,
    plies_to_target: 0,
    reaches_root: true,
  },
});

const benignResult = (moveIndex: number, uci: string): AnalysisResult => ({
  id: `auto-${moveIndex}`,
  move: uci,
  bestMove: uci,
  bestLine: null,
  bestEval: 0,
  playedEval: 0,
  currentPositionEval: 0,
  playedEvalMate: null,
  currentPositionEvalMate: null,
  moveIndex,
  delta: 0,
  classification: "good",
  blunder: false,
  recordable: false,
});

const mockAnalyzeMove = vi.fn();
let capturedOutcomeListener: ((o: unknown) => void) | null = null;
let mockUploadCommitSessionId: string | null = null;
let mockUploadCommitRevision = 0;
const mockUploadCommitListeners = new Set<() => void>();

const emitUploadCommit = (sessionId: string) => {
  if (sessionId === mockUploadCommitSessionId) {
    mockUploadCommitRevision += 1;
  }
  for (const listener of mockUploadCommitListeners) listener();
};

const mockCoordinator = {
  analyzeMove: mockAnalyzeMove,
  waitForAnalysis: vi.fn((moveIndex: number) => {
    const analysis = gameAnalysisStore.getState().analysisMap.get(moveIndex);
    return analysis
      ? Promise.resolve(analysis)
      : Promise.reject(new Error("Analysis was not scheduled for this move"));
  }),
  // Default delegates to waitForAnalysis + the REAL gradeDrillMove so existing
  // post-root tests (which mock waitForAnalysis) drive grading unchanged. Tests
  // exercising the position-truth channel override this per call.
  waitForDrillGrade: vi.fn(),
  restartAnalysisWorker: vi.fn(),
  clearAnalysis: vi.fn(),
  // Session changes drive the owner's full reset (in production via emitReset);
  // the mock routes them directly so blunderReserved/frontier reset between games.
  startSession: vi.fn((sessionId: string) => {
    mockUploadCommitSessionId = sessionId;
    mockUploadCommitRevision = 0;
    for (const listener of mockUploadCommitListeners) listener();
    mockCoordinator.decisionOwner.handleReset({ generation: 0, sessionId: null });
  }),
  clearSession: vi.fn(() => {
    mockUploadCommitSessionId = null;
    mockUploadCommitRevision = 0;
    for (const listener of mockUploadCommitListeners) listener();
    mockCoordinator.decisionOwner.handleReset({ generation: 0, sessionId: null });
  }),
  flushPendingUploads: vi.fn().mockResolvedValue(undefined),
  stopSessionUploads: vi.fn(),
  settleWithin: vi.fn().mockResolvedValue(undefined),
  armLateEvaluationRepair: vi.fn().mockReturnValue(false),
  releaseLateEvaluationRepair: vi.fn(),
  cancelLateEvaluationRepair: vi.fn(),
  get sessionId() {
    return mockUploadCommitSessionId;
  },
  getUploadCommitRevision: vi.fn((sessionId: string | null) =>
    sessionId !== null && sessionId === mockUploadCommitSessionId
      ? mockUploadCommitRevision
      : 0,
  ),
  addUploadCommitListener: vi.fn((listener: () => void) => {
    mockUploadCommitListeners.add(listener);
    return () => mockUploadCommitListeners.delete(listener);
  }),
  store: gameAnalysisStore,
  markSkipped: vi.fn(),
  pruneFromMoveIndex: vi.fn((k: number) =>
    mockCoordinator.decisionOwner.handleReset({ generation: 0, sessionId: null, fromMoveIndex: k }),
  ),
  // Tracks the live store session id: the g-2nrn final-upload guard requires the
  // coordinator epoch and the store to agree before it stops uploads, so a
  // hardcoded null here would make every terminal path bail out.
  getEpoch: vi.fn(() => ({
    generation: 0,
    sessionId: useGameStore.getState().sessionId,
  })),
  addAnalysisResetListener: vi.fn(() => () => {}),
  addAnalysisOutcomeListener: vi.fn(() => () => {}),
  decisionOwner: createTestDecisionOwner(),
};

vi.mock("../contexts/useGameAnalysisCoordinator", () => ({
  useGameAnalysisCoordinator: () => mockCoordinator,
}));

// The default waitForDrillGrade: delegate to whatever waitForAnalysis is mocked
// to return for the move and grade it with the real comparator, mirroring the
// coordinator's worker-fallback path. Re-applied in beforeEach so a per-test
// override never leaks.
const defaultWaitForDrillGrade = async (
  moveIndex: number,
  playedMoveUci: string,
  strictnessCp: number,
) => {
  const analysis = await mockCoordinator.waitForAnalysis(moveIndex);
  return {
    grade: gradeDrillMove(analysis.delta, strictnessCp, analysis.bestMove === playedMoveUci),
    bestMove: analysis.bestMove,
    source: "worker" as const,
  };
};

// Production resolveAnalysisResult both writes the store AND emits a `resolved`
// outcome into the coordinator-owned DecisionOwner. These integration tests
// simulate resolution by writing the store directly, so this bridge routes store
// writes into the owner (keyed by the controller's synthetic requestId so the
// BlunderContext lookup matches), draining earlier indices as benign `resolved`
// to advance the recording frontier (the owner treats `skipped`/`failed` as
// provisional-blocking, so terminal resolves are required to advance).
let bridgeLastEmittedIndex = -1;
const bridgeEmittedIndices = new Set<number>();
gameAnalysisStore.subscribe((state, prev) => {
  if (state.lastAnalysis === prev.lastAnalysis) return;
  const r = state.lastAnalysis;
  if (!r || r.moveIndex === null) return;
  const moveIndex = r.moveIndex;
  // Defer so the controller's post-analyzeMove context registration runs first
  // (the mock resolves synchronously inside analyzeMove).
  queueMicrotask(() => {
    const owner = mockCoordinator.decisionOwner;
    if (bridgeEmittedIndices.has(moveIndex)) return;
    bridgeEmittedIndices.add(moveIndex);
    const moveHistory = useGameStore.getState().moveHistory;
    // Mirror the controller's key: `analyzeMove() ?? synthetic`. Find the id the
    // analyzeMove mock returned for this index; fall back to the synthetic id.
    let returned: string | undefined;
    const calls = mockAnalyzeMove.mock.calls;
    for (let i = calls.length - 1; i >= 0; i--) {
      if (calls[i][3] === moveIndex) {
        returned = mockAnalyzeMove.mock.results[i]?.value as string | undefined;
        break;
      }
    }
    const uci = moveHistory[moveIndex]?.uci;
    const requestId =
      returned ?? (uci ? `analysis-${moveIndex}-${uci}` : r.id);
    for (let i = bridgeLastEmittedIndex + 1; i < moveIndex; i++) {
      owner.handleOutcome({
        seq: 0, generation: 0, sessionId: null,
        moveIndex: i, requestId: `auto-${i}`, status: "resolved",
        result: benignResult(i, moveHistory[i]?.uci ?? "0000"),
      });
    }
    owner.handleOutcome({
      seq: 0, generation: 0, sessionId: null,
      moveIndex, requestId, status: "resolved",
      result: { ...r, id: requestId },
    });
    bridgeLastEmittedIndex = Math.max(bridgeLastEmittedIndex, moveIndex);
  });
});

// Capture onPieceDrop from the Chessboard mock so tests can simulate moves
let capturedPieceDrop:
  | ((args: { sourceSquare: string; targetSquare: string }) => boolean)
  | null = null;
let capturedSquareClick: ((args: { square: string }) => void) | null = null;

vi.mock("react-chessboard", () => ({
  defaultPieces: {
    wK: () => <svg data-testid="piece-wK" />,
    bK: () => <svg data-testid="piece-bK" />,
  },
  Chessboard: ({ options }: { options: Record<string, unknown> }) => {
    capturedPieceDrop = options.onPieceDrop as typeof capturedPieceDrop;
    capturedSquareClick = options.onSquareClick as typeof capturedSquareClick;
    return (
      <div
        data-testid="chessboard"
        data-orientation={options.boardOrientation as string}
        data-position={options.position as string}
        data-allow-dragging={String(options.allowDragging)}
        data-arrow-count={String(((options.arrows as unknown[] | undefined) ?? []).length)}
      />
    );
  },
}));

const initialGameStoreState = useGameStore.getInitialState();

beforeEach(() => {
  stockfishStatus = "ready";
  naturalEndDrillMock.mockReset();
  mockLocation = { state: null, pathname: "/play" };
  mockNavigate.mockReset();
  useDrillAnalysisStore.getState().clear();
  useGameStore.setState(initialGameStoreState, true);
  mockUploadCommitSessionId = null;
  mockUploadCommitRevision = 0;
  mockUploadCommitListeners.clear();
  mockCoordinator.getUploadCommitRevision.mockClear();
  mockCoordinator.addUploadCommitListener.mockClear();
  // Isolate persisted drill prefs between tests — a successful drill start
  // writes ghostreplay_drill_prefs, which would otherwise leak into tests whose
  // overlay prefill reads it (e.g. the remount engine-ELO persistence test).
  localStorage.clear();
  // Fresh owner per test so blunderReserved/frontier/outbox don't leak across
  // tests; bind the direct-emit helper used by a few tests to it.
  mockCoordinator.decisionOwner = createTestDecisionOwner();
  capturedOutcomeListener = (o: unknown) =>
    mockCoordinator.decisionOwner.handleOutcome(o as AnalysisOutcome);
  bridgeLastEmittedIndex = -1;
  bridgeEmittedIndices.clear();
  mockCoordinator.addAnalysisOutcomeListener.mockClear();
  mockCoordinator.flushPendingUploads.mockClear();
  mockCoordinator.flushPendingUploads.mockResolvedValue(undefined);
  mockCoordinator.stopSessionUploads.mockClear();
  // Restore the delegating drill-grade default so a per-test override never leaks.
  mockCoordinator.waitForDrillGrade.mockReset();
  mockCoordinator.waitForDrillGrade.mockImplementation(defaultWaitForDrillGrade);
  gameAnalysisStore.getState().clearAll();
  gameAnalysisStore.getState().setStatus("ready");
  class MockAudio {
    preload = "auto";
    currentTime = 0;

    play() {
      return Promise.resolve();
    }
  }
  vi.stubGlobal("Audio", MockAudio);
  fetchCurrentRatingMock.mockReset();
  fetchSessionOpeningsMock.mockReset();
  fetchSessionOpeningsMock.mockResolvedValue({
    player_color: "white",
    lineage: [],
    start_ply: 1,
  });
  captureEventMock.mockReset();
  getStatsAchievementsMock.mockReset();
  getStatsAchievementsMock.mockResolvedValue({
    perfect_streak: { personal_best: 0 },
  });
  fetchCurrentRatingMock.mockResolvedValue({
    current_rating: 1200,
    is_provisional: true,
    games_played: 0,
  });
});

describe("ChessGame start flow", () => {
  beforeEach(() => {
    startGameMock.mockReset();
    endGameMock.mockReset();
    uploadSessionMovesMock.mockReset();
    getNextOpponentMoveMock.mockReset();
    continueDrillMock.mockReset();
    failDrillMock.mockReset();
    checkDrillRouteMock.mockReset();
    mockCoordinator.waitForAnalysis.mockReset();
    mockCoordinator.restartAnalysisWorker.mockReset();
    mockCoordinator.waitForAnalysis.mockImplementation((moveIndex: number) => {
      const analysis = gameAnalysisStore.getState().analysisMap.get(moveIndex);
      return analysis
        ? Promise.resolve(analysis)
        : Promise.reject(new Error("Analysis was not scheduled for this move"));
    });
    startDrillMock.mockReset();
    getOpeningRootsMock.mockReset();
    // The live opening-lineage derivation (g-a5v3) loads the root registry on
    // every mount, so it needs a well-behaved default; tests that care about
    // specific roots override it. The module-level registry cache is dropped
    // too, or the first test's roots would leak into every later test.
    getOpeningRootsMock.mockResolvedValue({ families: [] });
    __resetOpeningRootIndexCache();
    pollFreshOpeningDeltaMock.mockReset();
    recordManualBlunderMock.mockReset();
    reviewSrsBlunderMock.mockReset();
    lookupOpeningByFenMock.mockReset();
    gameAnalysisStore.getState().clearAll();
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 });
    failDrillMock.mockResolvedValue({
      session_id: "session-characterization",
      mode: "drill",
      drill_state: "failed",
      opening_key: "target-fen",
      opening_name: "Target",
      opening_family: "Target",
      eco: null,
      depth: 1,
      player_color: "white",
      engine_elo: 1500,
      strictness: "standard",
      strictness_cp: 25,
      is_rated: false,
      rated_start_ply: null,
      normal_started_at: null,
      converted_at: null,
      terminal_reason: "accuracy",
    });
    // Default: backend returns engine-mode move
    getNextOpponentMoveMock.mockResolvedValue({
      mode: "engine",
      move: { uci: "d7d5", san: "d5" },
      target_blunder_id: null,
      decision_source: "backend_engine",
    });
    lookupOpeningByFenMock.mockResolvedValue(null);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("defaults to random color on Play", async () => {
    // Math.random is called multiple times: sampleEloBin on mount, sampleEloBin
    // when opening the overlay, and once for color resolution in handleNewGame.
    // Return 0.9 for all calls so the color resolves to "black" (0.9 >= 0.5).
    vi.spyOn(Math, "random").mockReturnValue(0.9);
    startGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      engine_elo: 1500,
      player_color: "black",
    });

    render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play random/i }));

    await waitFor(() => {
      expect(startGameMock).toHaveBeenCalledWith(
        expect.any(Number),
        "black",
      );
    });

    await waitFor(() => {
      expect(screen.getByTestId("chessboard")).toHaveAttribute(
        "data-orientation",
        "black",
      );
    });
  });

  it("calls unified opponent-move endpoint when playing as black", async () => {
    const STARTING_FEN =
      "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
    startGameMock.mockResolvedValueOnce({
      session_id: "session-456",
      engine_elo: 1500,
      player_color: "black",
    });

    render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play black/i }));

    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalledWith(
        "session-456",
        STARTING_FEN,
        [],
      );
    });
  });
});

describe("ChessGame characterization safeguards", () => {
  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    startGameMock.mockReset();
    endGameMock.mockReset();
    uploadSessionMovesMock.mockReset();
    getNextOpponentMoveMock.mockReset();
    continueDrillMock.mockReset();
    checkDrillRouteMock.mockReset();
    abandonDrillMock.mockReset();
    // The real post-fix response for a STOPPED drill: the server preserves the
    // terminal outcome across abandon (g-drill-failed-overwrite). The client
    // store's drillState is its own lifecycle sentinel and must still land on
    // "abandoned" — that mapping is what these tests exercise.
    abandonDrillMock.mockResolvedValue({ drill_state: "failed" });
    mockCoordinator.clearSession.mockClear();
    useDrillAnalysisStore.getState().clear();
    startDrillMock.mockReset();
    getOpeningRootsMock.mockReset();
    // The live opening-lineage derivation (g-a5v3) loads the root registry on
    // every mount, so it needs a well-behaved default; tests that care about
    // specific roots override it. The module-level registry cache is dropped
    // too, or the first test's roots would leak into every later test.
    getOpeningRootsMock.mockResolvedValue({ families: [] });
    __resetOpeningRootIndexCache();
    recordBlunderMock.mockReset();
    recordManualBlunderMock.mockReset();
    reviewSrsBlunderMock.mockReset();
    mockAnalyzeMove.mockReset();
    evaluatePositionMock.mockReset();
    lookupOpeningByFenMock.mockReset();
    gameAnalysisStore.getState().clearAll();
    capturedPieceDrop = null;
    capturedSquareClick = null;

    endGameMock.mockResolvedValue({});
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 });
    getNextOpponentMoveMock.mockResolvedValue({
      mode: "engine",
      move: { uci: "d7d5", san: "d5" },
      target_blunder_id: null,
      decision_source: "backend_engine",
    });
    lookupOpeningByFenMock.mockResolvedValue(null);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  const startGameAsWhite = async (
    onOpenHistory?: (options: {
      select: "latest";
      source: "post_game_view_analysis";
    }) => void,
  ) => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-characterization",
      engine_elo: 1500,
      player_color: "white",
    });

    render(<ChessGame onOpenHistory={onOpenHistory} />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(startGameMock).toHaveBeenCalled();
    });
  };

  it("keeps live pieces draggable when analysis engine readiness is degraded", async () => {
    stockfishStatus = "error";
    await startGameAsWhite();

    expect(screen.getByText("Your turn")).toBeInTheDocument();
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-allow-dragging",
      "true",
    );
  });

  it("shows the drilling label while a drill is active or at root, but not once converted", async () => {
    await startGameAsWhite();

    act(() => {
      useGameStore.setState({
        drillOpeningKey: "target-fen",
        drillOpeningName: "Sicilian Defense",
        drillState: "active",
      });
    });
    await waitFor(() => {
      expect(screen.getByText("Drilling:")).toBeInTheDocument();
    });
    expect(screen.getByText("Sicilian Defense")).toBeInTheDocument();

    act(() => {
      useGameStore.setState({ drillState: "root_reached" });
    });
    await waitFor(() => {
      expect(screen.getByText("Drilling:")).toBeInTheDocument();
    });

    // A converted drill is normal rated play — the drilling label must clear.
    act(() => {
      useGameStore.setState({ drillState: "converted" });
    });
    await waitFor(() => {
      expect(screen.queryByText("Drilling:")).not.toBeInTheDocument();
    });
  });

  it("starts live drill play after a player-reached root before requesting the next opponent move", async () => {
    await startGameAsWhite();

    const line = new Chess();
    line.move("e4");
    const rootFen = line.fen();
    act(() => {
      useGameStore.setState({
        drillOpeningKey: rootFen,
        drillState: "active",
      });
    });
    checkDrillRouteMock.mockResolvedValueOnce({
      status: "root_reached",
      current_fen: rootFen,
      target_fen: rootFen,
      suggestions: [],
    });
    getNextOpponentMoveMock.mockReturnValueOnce(new Promise(() => undefined));

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalledWith(
        "session-characterization",
        rootFen,
        ["e2e4"],
      );
    });
    expect(screen.queryByRole("button", { name: /^retry$/i })).not.toBeInTheDocument();
    expect(continueDrillMock).not.toHaveBeenCalled();
    // This call IS the boundary confirmation for a player arrival, and the drill
    // cannot advance until it settles — so it is bounded like the opponent one.
    expect(checkDrillRouteMock).toHaveBeenCalledWith(
      "session-characterization",
      expect.objectContaining({ current_ply: 1, played_uci: "e2e4" }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("starts live drill play after an opponent-reached root without an immediate opponent move", async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-characterization",
      engine_elo: 1500,
      player_color: "black",
    });
    // Hold the opponent response so the drill can be armed before it lands — the
    // confirmation's staleness guard requires a live drill to act on.
    let serveRootReachingMove!: (response: unknown) => void;
    getNextOpponentMoveMock.mockReturnValueOnce(
      new Promise((resolve) => {
        serveRootReachingMove = resolve;
      }),
    );
    render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play black/i }));

    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(1);
    });
    act(() => {
      useGameStore.setState({
        drillOpeningKey: E4_FEN,
        drillState: "active",
      });
    });
    // The serve does not transition; only this confirmation does.
    checkDrillRouteMock.mockResolvedValueOnce({
      status: "root_reached",
      current_fen: E4_FEN,
      target_fen: E4_FEN,
      suggestions: [],
      drill_root_reached_ply: 1,
    });

    await act(async () => {
      serveRootReachingMove(openingRootPendingResponse());
    });

    await waitFor(() => {
      expect(useGameStore.getState().drillState).toBe("root_reached");
    });
    expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(1);
    expect(continueDrillMock).not.toHaveBeenCalled();

    getNextOpponentMoveMock.mockResolvedValueOnce({
      mode: "engine",
      move: { uci: "g1f3", san: "Nf3" },
      target_blunder_id: null,
      decision_source: "backend_engine",
    });
    (mockCoordinator.waitForAnalysis as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      id: "analysis-e5",
      move: "e7e5",
      bestMove: "e7e5",
      bestEval: 10,
      playedEval: 10,
      currentPositionEval: 10,
      playedEvalMate: null,
      currentPositionEvalMate: null,
      moveIndex: 1,
      delta: 0,
      classification: "best",
      blunder: false,
      recordable: false,
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e7", targetSquare: "e5" });
    });

    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(2);
    });
  });

  // --- Two-phase drill root confirmation (g-root-confirm-cutover) -----------
  //
  // Serving a root-reaching route move transitions nothing. The client applies
  // it, then confirms the resulting position; until that succeeds the drill is
  // NOT root-reached and gameplay is barred. These drive the whole barrier
  // through the real component.

  /**
   * Start a drill as black, serve the root-reaching route move, and return once
   * the confirmation this triggers is in flight (or settled, if its mock
   * settles). The drill is armed after the request is issued but before the
   * response lands, which is the only ordering that reaches applyGhostMove with
   * a live drill in this harness.
   */
  const serveRootReachingRouteMove = async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-characterization",
      engine_elo: 1500,
      player_color: "black",
    });
    let serve!: (response: unknown) => void;
    getNextOpponentMoveMock.mockReturnValueOnce(
      new Promise((resolve) => {
        serve = resolve;
      }),
    );

    const view = render(<ChessGame />);
    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play black/i }));
    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(1);
    });

    act(() => {
      useGameStore.setState({
        drillOpeningKey: E4_FEN,
        drillState: "active",
      });
    });

    await act(async () => {
      serve(openingRootPendingResponse());
    });

    return view;
  };

  const rootReachedRouteResponse = {
    status: "root_reached",
    current_fen: E4_FEN,
    target_fen: E4_FEN,
    suggestions: [],
    drill_root_reached_ply: 1,
  };

  /** A promise the test settles by hand, plus its settlers. */
  const deferred = () => {
    let resolve!: (value: unknown) => void;
    let reject!: (reason: unknown) => void;
    const promise = new Promise((res, rej) => {
      resolve = res;
      reject = rej;
    });
    // Attached so an eventual rejection is never an unhandled one.
    promise.catch(() => undefined);
    return { promise, resolve, reject };
  };

  it("confirms the applied root against the served decision, under a bounded signal", async () => {
    checkDrillRouteMock.mockResolvedValueOnce(rootReachedRouteResponse);

    await serveRootReachingRouteMove();

    await waitFor(() => {
      expect(useGameStore.getState().drillState).toBe("root_reached");
    });
    expect(checkDrillRouteMock).toHaveBeenCalledWith(
      "session-characterization",
      {
        current_fen: E4_FEN,
        current_ply: 1,
        decision_id: "decision-1",
      },
      // Unbounded, this POST would block the board indefinitely.
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("bars gameplay while the confirmation is in flight, offering only Abandon", async () => {
    const confirmation = deferred();
    checkDrillRouteMock.mockReturnValueOnce(confirmation.promise);

    await serveRootReachingRouteMove();

    // Applied, but not confirmed: the position is on the board and nothing else
    // has happened to the drill.
    expect(useGameStore.getState().moveHistory).toHaveLength(1);
    expect(useGameStore.getState().drillState).toBe("active");

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e7", targetSquare: "e5" });
    });

    expect(useGameStore.getState().moveHistory).toHaveLength(1);
    expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(1);
    // In flight ⇒ no recovery ⇒ Abandon only. A Retry here would invite a second
    // concurrent confirmation on top of the first.
    expect(await screen.findByRole("button", { name: /^abandon$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^retry$/i })).not.toBeInTheDocument();
  });

  it("retains the board and offers recovery when the confirmation fails, then Retry confirms", async () => {
    checkDrillRouteMock.mockRejectedValueOnce(new Error("network down"));

    await serveRootReachingRouteMove();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^retry$/i })).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /^abandon$/i })).toBeInTheDocument();
    // The applied position survives the failure untouched.
    expect(useGameStore.getState().moveHistory).toHaveLength(1);
    expect(useGameStore.getState().moveHistory[0]?.uci).toBe("e2e4");
    expect(useGameStore.getState().drillState).toBe("active");
    expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(1);

    checkDrillRouteMock.mockResolvedValueOnce(rootReachedRouteResponse);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^retry$/i }));
    });

    await waitFor(() => {
      expect(useGameStore.getState().drillState).toBe("root_reached");
    });
    expect(checkDrillRouteMock).toHaveBeenCalledTimes(2);
  });

  it("hides Retry while a retried confirmation is itself in flight", async () => {
    checkDrillRouteMock.mockRejectedValueOnce(new Error("network down"));
    await serveRootReachingRouteMove();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^retry$/i })).toBeInTheDocument();
    });

    const retried = deferred();
    checkDrillRouteMock.mockReturnValueOnce(retried.promise);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^retry$/i }));
    });

    expect(screen.queryByRole("button", { name: /^retry$/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^abandon$/i })).toBeInTheDocument();
    expect(checkDrillRouteMock).toHaveBeenCalledTimes(2);
  });

  it("routes a confirmation timeout into recovery with the board retained", async () => {
    // The shape AbortSignal.timeout produces.
    checkDrillRouteMock.mockRejectedValueOnce(
      new DOMException("timed out", "TimeoutError"),
    );

    await serveRootReachingRouteMove();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^retry$/i })).toBeInTheDocument();
    });
    expect(useGameStore.getState().moveHistory).toHaveLength(1);
    expect(useGameStore.getState().drillState).toBe("active");
  });

  it("ignores a confirmation that lands after the drill was abandoned", async () => {
    const confirmation = deferred();
    checkDrillRouteMock.mockReturnValueOnce(confirmation.promise);
    await serveRootReachingRouteMove();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^abandon$/i }));
    });
    const resignDialog = screen.getByRole("alertdialog");
    await act(async () => {
      fireEvent.click(within(resignDialog).getByRole("button", { name: /^resign$/i }));
    });

    await act(async () => {
      confirmation.resolve(rootReachedRouteResponse);
    });

    // Session id, history length and "not reverting" all still match here — only
    // an identity guard rejects this.
    expect(useGameStore.getState().drillState).not.toBe("root_reached");
  });

  it("ignores a confirmation that lands after a different branch reached the same ply", async () => {
    const confirmation = deferred();
    checkDrillRouteMock.mockReturnValueOnce(confirmation.promise);
    await serveRootReachingRouteMove();

    // Revert-and-replay: same session, same ply, DIFFERENT move. A guard built on
    // shape rather than identity would stamp root_reached onto this position.
    act(() => {
      useGameStore.setState({
        moveHistory: [{ san: "d4", fen: "other-fen", uci: "d2d4" }],
      });
    });

    await act(async () => {
      confirmation.resolve(rootReachedRouteResponse);
    });

    expect(useGameStore.getState().drillState).not.toBe("root_reached");
  });

  it("keeps the barrier across a remount and resumes the confirmation", async () => {
    // Navigating away and back remounts ChessGame, which rebuilds the board from
    // the store. A barrier that did not survive with it would hand the player an
    // applied-but-unconfirmed root position to move from — straight off-route.
    checkDrillRouteMock.mockRejectedValueOnce(new Error("network down"));
    const view = await serveRootReachingRouteMove();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^retry$/i })).toBeInTheDocument();
    });

    const resumed = deferred();
    checkDrillRouteMock.mockReturnValueOnce(resumed.promise);
    view.unmount();
    await act(async () => {
      render(<ChessGame />);
    });

    // The remount re-issues the confirmation rather than leaving the board frozen
    // behind a barrier whose Retry button unmounted with the old component.
    await waitFor(() => {
      expect(checkDrillRouteMock).toHaveBeenCalledTimes(2);
    });
    expect(useGameStore.getState().drillRootConfirm).not.toBeNull();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e7", targetSquare: "e5" });
    });
    expect(useGameStore.getState().moveHistory).toHaveLength(1);
    expect(useGameStore.getState().drillState).toBe("active");

    await act(async () => {
      resumed.resolve(rootReachedRouteResponse);
    });
    await waitFor(() => {
      expect(useGameStore.getState().drillState).toBe("root_reached");
    });
    expect(useGameStore.getState().drillRootConfirm).toBeNull();
  });

  it("discards a confirmation that no longer owns the barrier", async () => {
    // A/B: the pre-remount confirmation settles AFTER the remount engaged a
    // barrier of its own. Every identity field still matches — same session, ply,
    // move and FEN — so only attempt ownership rejects it. Clearing the barrier
    // here would re-open the board while B is still unresolved.
    const first = deferred();
    checkDrillRouteMock.mockReturnValueOnce(first.promise);
    const view = await serveRootReachingRouteMove();

    const second = deferred();
    checkDrillRouteMock.mockReturnValueOnce(second.promise);
    view.unmount();
    await act(async () => {
      render(<ChessGame />);
    });
    await waitFor(() => {
      expect(checkDrillRouteMock).toHaveBeenCalledTimes(2);
    });

    await act(async () => {
      first.resolve(rootReachedRouteResponse);
    });
    expect(useGameStore.getState().drillState).toBe("active");
    expect(useGameStore.getState().drillRootConfirm).not.toBeNull();

    // The owning attempt still lands normally.
    await act(async () => {
      second.resolve(rootReachedRouteResponse);
    });
    await waitFor(() => {
      expect(useGameStore.getState().drillState).toBe("root_reached");
    });
  });

  /** Arm a white drill whose root is reached by the player's own e4. */
  const armPlayerArrivalDrill = () => {
    useGameStore.setState({
      sessionId: "session-characterization",
      isGameActive: true,
      playerColor: "white",
      boardOrientation: "white",
      liveFen: STARTING_FEN,
    });
    const view = render(<ChessGame />);
    act(() => {
      useGameStore.setState({ drillOpeningKey: E4_FEN, drillState: "active" });
    });
    return view;
  };

  it("drops a player-route retry whose move left live history", async () => {
    // Revert-and-replace: e4 reached the root and its check failed, then the player
    // reverted and played something else. The stale proof must not stay armed — the
    // backend can replay previous_fen + played_uci, prove that arrival, and stamp
    // state and boundary for a move no longer on the board.
    checkDrillRouteMock.mockRejectedValueOnce(new Error("network down"));
    armPlayerArrivalDrill();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^retry$/i })).toBeInTheDocument();
    });
    expect(useGameStore.getState().drillPendingRouteMove).not.toBeNull();

    act(() => {
      useGameStore.setState({
        moveHistory: [{ san: "d4", fen: "other-fen", uci: "d2d4" }],
      });
    });

    expect(useGameStore.getState().drillPendingRouteMove).toBeNull();

    // Retry is still offered — but as ordinary drill steering, not as the stale
    // proof: no second route-check leaves the client.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^retry$/i }));
    });
    expect(checkDrillRouteMock).toHaveBeenCalledTimes(1);
    expect(getNextOpponentMoveMock).toHaveBeenCalled();
  });

  it("never re-issues a route proof for a move that left live history", async () => {
    // A stale pending record found at mount: the player reverted e4 and played d4
    // in a session that never got to clear it. Neither the invariant nor the
    // pre-dispatch check may let that proof reach the backend.
    const afterD4 = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1";
    useGameStore.setState({
      sessionId: "session-characterization",
      isGameActive: true,
      playerColor: "white",
      boardOrientation: "white",
      liveFen: afterD4,
      moveHistory: [{ san: "d4", fen: afterD4, uci: "d2d4" }],
      drillOpeningKey: E4_FEN,
      drillState: "active",
      drillPendingRouteMove: {
        fenAfter: E4_FEN,
        fenBefore: STARTING_FEN,
        uciHistory: ["e2e4"],
        gameOver: false,
        moveIndex: 0,
        moveSan: "e4",
        moveUci: "e2e4",
      },
    });

    render(<ChessGame />);

    await waitFor(() => {
      expect(useGameStore.getState().drillPendingRouteMove).toBeNull();
    });
    expect(checkDrillRouteMock).not.toHaveBeenCalled();
    expect(useGameStore.getState().drillState).toBe("active");
  });

  it("resumes an interrupted player-arrival route-check after a remount", async () => {
    // Without a durable record the drill is stranded here: the move is applied, the
    // opponent is to move, and Retry unmounted with the component — nothing left
    // re-drives the drill (the opponent effect only covers the opening).
    checkDrillRouteMock.mockRejectedValueOnce(new Error("network down"));
    const view = armPlayerArrivalDrill();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^retry$/i })).toBeInTheDocument();
    });

    const resumed = deferred();
    checkDrillRouteMock.mockReturnValueOnce(resumed.promise);
    getNextOpponentMoveMock.mockClear();
    getNextOpponentMoveMock.mockReturnValueOnce(new Promise(() => undefined));
    view.unmount();
    await act(async () => {
      render(<ChessGame />);
    });

    // Re-issued from the durable record — byte-identical to the first attempt.
    await waitFor(() => {
      expect(checkDrillRouteMock).toHaveBeenCalledTimes(2);
    });
    // Same session and same proof body; only the abort signal is a fresh object.
    expect(checkDrillRouteMock.mock.calls[1]?.slice(0, 2)).toEqual(
      checkDrillRouteMock.mock.calls[0]?.slice(0, 2),
    );

    await act(async () => {
      resumed.resolve(rootReachedRouteResponse);
    });
    await waitFor(() => {
      expect(useGameStore.getState().drillState).toBe("root_reached");
    });
    expect(useGameStore.getState().drillPendingRouteMove).toBeNull();
    // The continuation the interrupted pass owed: only now does the drill advance.
    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(1);
    });
  });

  it("discards a player-route response that no longer owns the pending record", async () => {
    // A/B while A is still IN FLIGHT — the case the resume test above cannot reach,
    // because there the first request has already rejected. Every identity field
    // still matches when A lands: same session, ply, move and FEN. Only attempt
    // ownership rejects it. Left ungated, A confirms the root and applies an
    // opponent move through the unmounted component's own Chess instance while B is
    // still unresolved, and both attempts append to the one shared history.
    const first = deferred();
    checkDrillRouteMock.mockReturnValueOnce(first.promise);
    const view = armPlayerArrivalDrill();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    await waitFor(() => {
      expect(useGameStore.getState().drillPendingRouteMove).not.toBeNull();
    });

    const second = deferred();
    checkDrillRouteMock.mockReturnValueOnce(second.promise);
    getNextOpponentMoveMock.mockClear();
    getNextOpponentMoveMock.mockReturnValueOnce(new Promise(() => undefined));
    view.unmount();
    await act(async () => {
      render(<ChessGame />);
    });
    await waitFor(() => {
      expect(checkDrillRouteMock).toHaveBeenCalledTimes(2);
    });

    await act(async () => {
      first.resolve(rootReachedRouteResponse);
    });
    expect(useGameStore.getState().drillState).toBe("active");
    // B's record survives A's settlement, and the board has not advanced.
    expect(useGameStore.getState().drillPendingRouteMove).not.toBeNull();
    expect(getNextOpponentMoveMock).not.toHaveBeenCalled();

    // The owning attempt still lands normally, once.
    await act(async () => {
      second.resolve(rootReachedRouteResponse);
    });
    await waitFor(() => {
      expect(useGameStore.getState().drillState).toBe("root_reached");
    });
    expect(useGameStore.getState().drillPendingRouteMove).toBeNull();
    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(1);
    });
  });

  it("retries a failed player-arrival route-check as itself, not as an opponent request", async () => {
    // A player move INTO the root: this route-check is the boundary stamp, so its
    // failure must be retryable as a route-check. Falling through to an opponent
    // request would advance the drill with the boundary never stamped.
    useGameStore.setState({
      sessionId: "session-characterization",
      isGameActive: true,
      playerColor: "white",
      boardOrientation: "white",
      liveFen: STARTING_FEN,
    });
    render(<ChessGame />);
    act(() => {
      useGameStore.setState({
        drillOpeningKey: E4_FEN,
        drillState: "active",
      });
    });

    checkDrillRouteMock.mockRejectedValueOnce(new Error("network down"));
    getNextOpponentMoveMock.mockClear();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^retry$/i })).toBeInTheDocument();
    });
    expect(useGameStore.getState().moveHistory).toHaveLength(1);
    expect(useGameStore.getState().drillState).toBe("active");
    expect(getNextOpponentMoveMock).not.toHaveBeenCalled();

    checkDrillRouteMock.mockResolvedValueOnce(rootReachedRouteResponse);
    getNextOpponentMoveMock.mockReturnValueOnce(new Promise(() => undefined));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^retry$/i }));
    });

    await waitFor(() => {
      expect(useGameStore.getState().drillState).toBe("root_reached");
    });
    expect(checkDrillRouteMock).toHaveBeenCalledTimes(2);
    // Only after the boundary is stamped does the drill advance.
    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(1);
    });
  });

  it("ends the game only after a retried player-arrival route-check succeeds", async () => {
    // The game-ending variant: the player's mating move is also the root arrival.
    // A failed route-check must not end the game — the boundary is unstamped, so
    // the drill would be finalized with no proof it ever reached its opening.
    // After f3 e5 g4, black mates with Qh4#.
    useGameStore.setState({
      sessionId: "session-characterization",
      isGameActive: true,
      playerColor: "black",
      boardOrientation: "black",
      liveFen: "rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq g3 0 2",
    });
    render(<ChessGame />);
    act(() => {
      useGameStore.setState({ drillOpeningKey: E4_FEN, drillState: "active" });
    });

    checkDrillRouteMock.mockRejectedValueOnce(new Error("network down"));
    naturalEndDrillMock.mockResolvedValue({
      session_id: "session-characterization",
      drill_state: "root_reached",
      terminal_reason: "natural_end",
      opening_score_changes: null,
    });
    getNextOpponentMoveMock.mockClear();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "d8", targetSquare: "h4" });
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^retry$/i })).toBeInTheDocument();
    });
    expect(useGameStore.getState().moveHistory).toHaveLength(1);
    // Unconfirmed root ⇒ the drill is not finalized, mate on the board or not.
    expect(naturalEndDrillMock).not.toHaveBeenCalled();

    checkDrillRouteMock.mockResolvedValueOnce(rootReachedRouteResponse);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^retry$/i }));
    });

    await waitFor(() => {
      expect(naturalEndDrillMock).toHaveBeenCalled();
    });
    expect(useGameStore.getState().drillState).toBe("root_reached");
    // A terminal position never asks for another opponent move.
    expect(getNextOpponentMoveMock).not.toHaveBeenCalled();
  });

  it("seeds the New Game popup difficulty from the rating sample — not the stored drill pref — without mutating the committed store (g-fxrm)", async () => {
    // A conflicting sticky drill pref must NOT seed the popup difficulty.
    localStorage.setItem(
      "ghostreplay_drill_prefs",
      JSON.stringify({ openingKey: "k", engineElo: 2000, strictnessCp: 25, playerColor: "white" }),
    );
    // Known committed store value; idle-mount/New Game sampling must leave it alone.
    useGameStore.setState({ engineElo: 800 });
    // Math.random === 0 → sampleEloBin returns MAIA_ELO_BINS[0].
    vi.spyOn(Math, "random").mockReturnValue(0);

    render(<ChessGame />);

    // Overlay is open on mount (play mode). After the idle rating fetch resolves
    // the panel shows the sampled bot — and the committed store value is NOT
    // mutated (Finding 2: idle mount seeds the panel, not the store).
    await waitFor(() => {
      expect(
        screen.getByText(MAIA_BOT_NAMES[MAIA_ELO_BINS[0]]),
      ).toBeInTheDocument();
    });
    expect(useGameStore.getState().engineElo).toBe(800);

    // Close, then reopen via New Game. This is the closed→open transition that
    // re-runs the prefill AFTER the fresh sample — the case where a clobbering
    // prefill would overwrite the sampled bot with the stored 2000 pref. The
    // panel must still show the sampled bot, not the pref (Finding 1).
    fireEvent.click(screen.getByRole("button", { name: /^close$/i }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    });
    await waitFor(() => {
      expect(
        screen.getByText(MAIA_BOT_NAMES[MAIA_ELO_BINS[0]]),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText(MAIA_BOT_NAMES[2000])).not.toBeInTheDocument();

    localStorage.removeItem("ghostreplay_drill_prefs");
  });

  it("stops a post-root drill when player analysis exceeds strictness", async () => {
    useGameStore.setState({
      sessionId: "session-characterization",
      isGameActive: true,
      playerColor: "white",
      boardOrientation: "white",
      drillStrictnessCp: 25,
      liveFen: STARTING_FEN,
    });

    render(<ChessGame />);
    act(() => {
      useGameStore.setState({
        drillOpeningKey: "target-fen",
        drillState: "root_reached",
        drillStrictnessCp: 25,
      });
    });

    mockCoordinator.waitForAnalysis.mockReset();
    mockCoordinator.waitForAnalysis.mockResolvedValue({
      id: "analysis-e4",
      move: "e2e4",
      bestMove: "d2d4",
      bestEval: 40,
      playedEval: 10,
      currentPositionEval: 10,
      playedEvalMate: null,
      currentPositionEvalMate: null,
      moveIndex: 0,
      delta: 30,
      classification: "mistake",
      blunder: false,
      recordable: false,
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(failDrillMock).toHaveBeenCalledWith("session-characterization", "accuracy");
      expect(useGameStore.getState().drillState).toBe("failed");
      expect(useGameStore.getState().drillTerminalReason).toBe("accuracy");
      expect(useGameStore.getState().viewIndex).toBe(-1);
    });
    // The reconcile-poll fires for the failed drill session (g-fix-end-latency).
    expect(pollFreshOpeningDeltaMock).toHaveBeenCalledWith("session-characterization");
    // The full move history is durably uploaded BEFORE failDrill computes the
    // opening-score delta (g-xanz barrier). The bug was ordering-specific, so
    // assert the upload ran first — not merely that both were called.
    expect(uploadSessionMovesMock).toHaveBeenCalled();
    expect(uploadSessionMovesMock.mock.invocationCallOrder[0]).toBeLessThan(
      failDrillMock.mock.invocationCallOrder[0],
    );
    // g-y90g: the accuracy-fail path goes through uploadFullMoveHistoryBeforeEnd,
    // which stops the incremental uploader BEFORE this final upload (so no stray
    // mid-game upload races it) and flags it for the single opportunity recompute.
    // This is the structural guard for the review's P1 miss (no ChessGame change).
    expect(mockCoordinator.stopSessionUploads).toHaveBeenCalled();
    expect(
      vi.mocked(mockCoordinator.stopSessionUploads).mock.invocationCallOrder[0],
    ).toBeLessThan(uploadSessionMovesMock.mock.invocationCallOrder[0]);
    expect(uploadSessionMovesMock.mock.calls[0][2]).toEqual(
      expect.objectContaining({ recomputeOpportunity: true }),
    );
  });

  it("shows the opening-score badge in the lineage (not DrillStopActions) when a drill fails on accuracy (g-3gmc)", async () => {
    // The played opening lineage (separate from the drill target) hosts the badge.
    fetchSessionOpeningsMock.mockResolvedValue({
      player_color: "white",
      lineage: [
        {
          opening_key: "k1",
          opening_name: "King's Pawn Game",
          opening_family: "King's Pawn",
          eco: "C20",
          depth: 0,
          score: 44,
          confidence: 0.5,
          coverage: 0.5,
          sample_size: 5,
          game_count: 2,
          path: [],
          moves: ["e4"],
        },
      ],
      start_ply: 1,
    });
    // The accuracy stop carries the opening-score delta on the failDrill contract.
    failDrillMock.mockResolvedValueOnce({
      session_id: "session-characterization",
      drill_state: "failed",
      terminal_reason: "accuracy",
      opening_score_changes: [
        {
          opening_key: "k1",
          opening_name: "King's Pawn Game",
          opening_family: "King's Pawn",
          eco: "C20",
          depth: 0,
          before: 41,
          after: 44,
          delta: 3,
          is_new: false,
        },
      ],
    });

    useGameStore.setState({
      sessionId: "session-characterization",
      isGameActive: true,
      playerColor: "white",
      boardOrientation: "white",
      drillStrictnessCp: 25,
      liveFen: STARTING_FEN,
    });

    render(<ChessGame />);
    act(() => {
      useGameStore.setState({
        drillOpeningKey: "target-fen",
        drillState: "root_reached",
        drillStrictnessCp: 25,
      });
    });

    mockCoordinator.waitForAnalysis.mockReset();
    mockCoordinator.waitForAnalysis.mockResolvedValue({
      id: "analysis-e4",
      move: "e2e4",
      bestMove: "d2d4",
      bestEval: 40,
      playedEval: 10,
      currentPositionEval: 10,
      playedEvalMate: null,
      currentPositionEvalMate: null,
      moveIndex: 0,
      delta: 30,
      classification: "mistake",
      blunder: false,
      recordable: false,
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(useGameStore.getState().drillState).toBe("failed");
    });

    // The badge renders next to the played-opening chip in the lineage...
    const region = await screen.findByRole("region", {
      name: "Openings played",
    });
    const badge = within(region).getByText("+3 → 44");
    expect(badge).toHaveClass("game-opening-lineage__delta--up");

    // ...and NOT inside the (now delta-less) drill-stopped actions.
    const drillRegion = screen.getByRole("region", {
      name: /Drill stopped/i,
    });
    expect(within(drillRegion).queryByText("+3 → 44")).not.toBeInTheDocument();
  });

  it("passes a post-root drill move whose loss exactly equals strictness (boundary passes)", async () => {
    useGameStore.setState({
      sessionId: "session-boundary",
      isGameActive: true,
      playerColor: "white",
      boardOrientation: "white",
      drillStrictnessCp: 25,
      liveFen: STARTING_FEN,
    });

    render(<ChessGame />);
    act(() => {
      useGameStore.setState({
        drillOpeningKey: "target-fen",
        drillState: "root_reached",
        drillStrictnessCp: 25,
      });
    });

    failDrillMock.mockClear();
    getNextOpponentMoveMock.mockClear();
    mockCoordinator.waitForAnalysis.mockReset();
    mockCoordinator.waitForAnalysis.mockResolvedValue({
      id: "analysis-e4",
      move: "e2e4",
      bestMove: "d2d4",
      bestEval: 35,
      playedEval: 10,
      currentPositionEval: 10,
      playedEvalMate: null,
      currentPositionEvalMate: null,
      moveIndex: 0,
      delta: 25, // exactly strictness — must PASS (strict `>` comparator)
      classification: "good",
      blunder: false,
      recordable: false,
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalled();
    });
    expect(failDrillMock).not.toHaveBeenCalled();
    expect(useGameStore.getState().drillState).not.toBe("failed");
  });

  it("routes an ungraded post-root move (null delta) to recovery instead of advancing", async () => {
    useGameStore.setState({
      sessionId: "session-unavailable",
      isGameActive: true,
      playerColor: "white",
      boardOrientation: "white",
      drillStrictnessCp: 25,
      liveFen: STARTING_FEN,
    });

    render(<ChessGame />);
    act(() => {
      useGameStore.setState({
        drillOpeningKey: "target-fen",
        drillState: "root_reached",
        drillStrictnessCp: 25,
      });
    });

    failDrillMock.mockClear();
    getNextOpponentMoveMock.mockClear();
    mockCoordinator.waitForAnalysis.mockReset();
    mockCoordinator.waitForAnalysis.mockResolvedValue({
      id: "analysis-e4",
      move: "e2e4",
      bestMove: "d2d4",
      bestEval: null,
      playedEval: null,
      currentPositionEval: null,
      playedEvalMate: null,
      currentPositionEvalMate: null,
      moveIndex: 0,
      delta: null, // unavailable — neither pass nor fail
      classification: null,
      blunder: false,
      recordable: false,
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(screen.getByText(/analysis is unavailable/i)).toBeInTheDocument();
    });
    expect(failDrillMock).not.toHaveBeenCalled();
    expect(getNextOpponentMoveMock).not.toHaveBeenCalled();
  });

  it("post-root fail from the position channel suggests the trusted best move (not the played move)", async () => {
    useGameStore.setState({
      sessionId: "session-position-fail",
      isGameActive: true,
      playerColor: "white",
      boardOrientation: "white",
      drillStrictnessCp: 0,
      liveFen: STARTING_FEN,
    });

    render(<ChessGame />);
    act(() => {
      useGameStore.setState({
        drillOpeningKey: "target-fen",
        drillState: "root_reached",
        drillStrictnessCp: 0,
      });
    });

    failDrillMock.mockClear();
    // Strictness-0 position-truth grade: played e2e4 is not the trusted best c2c4.
    mockCoordinator.waitForDrillGrade.mockResolvedValueOnce({
      grade: "fail",
      bestMove: "c2c4",
      source: "position",
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(failDrillMock).toHaveBeenCalledWith("session-position-fail", "accuracy");
      expect(useGameStore.getState().drillState).toBe("failed");
    });
    expect(mockCoordinator.waitForDrillGrade).toHaveBeenCalledWith(0, "e2e4", 0);
    // Red played-move arrow (e2e4) + green trusted-best suggestion (c2c4): the
    // suggestion is the position best move, never the played move.
    expect(screen.getByTestId("chessboard")).toHaveAttribute("data-arrow-count", "2");
  });

  it("post-root pass from the position channel advances the drill", async () => {
    useGameStore.setState({
      sessionId: "session-position-pass",
      isGameActive: true,
      playerColor: "white",
      boardOrientation: "white",
      drillStrictnessCp: 0,
      liveFen: STARTING_FEN,
    });

    render(<ChessGame />);
    act(() => {
      useGameStore.setState({
        drillOpeningKey: "target-fen",
        drillState: "root_reached",
        drillStrictnessCp: 0,
      });
    });

    failDrillMock.mockClear();
    getNextOpponentMoveMock.mockClear();
    getNextOpponentMoveMock.mockResolvedValueOnce({
      mode: "engine",
      move: { uci: "g8f6", san: "Nf6" },
      target_blunder_id: null,
      decision_source: "backend_engine",
    });
    // Strictness-0 position-truth grade: played c2c4 IS the trusted best move.
    mockCoordinator.waitForDrillGrade.mockResolvedValueOnce({
      grade: "pass",
      bestMove: "c2c4",
      source: "position",
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "c2", targetSquare: "c4" });
    });

    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalled();
    });
    expect(failDrillMock).not.toHaveBeenCalled();
    expect(useGameStore.getState().drillState).not.toBe("failed");
  });

  const driveOffRouteFail = async () => {
    await startGameAsWhite();
    act(() => {
      useGameStore.setState({
        drillOpeningKey: "target-fen",
        drillState: "active",
      });
    });
    checkDrillRouteMock.mockResolvedValueOnce({
      status: "failed",
      current_fen: STARTING_FEN,
      target_fen: "target-fen",
      suggestions: [{ uci: "d2d4" }],
      failure: {
        reason: "off_route",
        played_move_uci: "e2e4",
        correction_fen: STARTING_FEN,
      },
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(useGameStore.getState().drillState).toBe("failed");
    });
  };

  it("Analyze awaits the pending failed-move analysis, then abandons and snapshots the drill", async () => {
    // Off-route failures do not pre-resolve the failed move's analysis, so the
    // targeted barrier must await it before snapshotting.
    mockCoordinator.waitForAnalysis.mockReset();
    mockCoordinator.waitForAnalysis.mockImplementation(async (idx: number) => {
      const result: AnalysisResult = {
        id: "analysis-e4",
        move: "e2e4",
        bestMove: "d2d4",
        bestEval: 40,
        playedEval: 10,
        currentPositionEval: 10,
        playedEvalMate: null,
        currentPositionEvalMate: null,
        moveIndex: idx,
        delta: 30,
        classification: "mistake",
        blunder: false,
        recordable: false,
      };
      gameAnalysisStore.getState().resolveAnalysis(idx, result);
      return result;
    });

    await driveOffRouteFail();

    const analyze = await screen.findByRole("button", { name: "Analyze" });
    await act(async () => {
      fireEvent.click(analyze);
    });

    await waitFor(() => {
      expect(useDrillAnalysisStore.getState().snapshot).not.toBeNull();
    });

    // Targeted barrier ran for the failed move index (0).
    expect(mockCoordinator.waitForAnalysis).toHaveBeenCalledWith(0);
    expect(abandonDrillMock).toHaveBeenCalledWith("session-characterization");
    expect(mockCoordinator.clearSession).toHaveBeenCalled();
    expect(useGameStore.getState().isGameActive).toBe(false);
    // Server says 'failed' (outcome preserved); the client store finalizes to
    // 'abandoned', which is what isReviewedDrillReturnValid and the post-game
    // banner read (g-drill-failed-overwrite).
    expect(useGameStore.getState().drillState).toBe("abandoned");

    const snapshot = useDrillAnalysisStore.getState().snapshot!;
    // The late-resolved failed move's eval is included in the snapshot.
    expect(snapshot.moves[0]).toMatchObject({ move_san: "e4", eval_cp: 10 });
    // First-ply failure (failedMoveIndex 0) resolves one ply earlier, to the
    // starting-position sentinel (-1) (g-eflo).
    expect(snapshot.initialMoveIndex).toBe(-1);
    expect(snapshot.warning).toBeNull();
  });

  it("Analyze then return restores the reviewed drill, with the server preserving 'failed'", async () => {
    // End-to-end guard on the client-lifecycle pin (g-drill-failed-overwrite):
    // the abandon response says drill_state 'failed', and the reviewed-return
    // path — which requires drillState === "abandoned" — must still light up.
    // Driving the REAL Analyze flow is the point; a hand-seeded "abandoned"
    // store cannot prove the transition.
    // handleAnalyzeDrill discards this value — only resolve-vs-reject matters
    // (a reject would set the "Analysis unavailable" warning). Resolve with a
    // typed benign result rather than null, which the signature forbids.
    mockCoordinator.waitForAnalysis.mockResolvedValue(benignResult(0, "e2e4"));
    await driveOffRouteFail();
    act(() => {
      // A real drill carries strictness; the reviewed-return guard requires it.
      useGameStore.setState({ drillStrictness: "standard", drillStrictnessCp: 25 });
    });

    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Analyze" }));
    });
    await waitFor(() => {
      expect(useDrillAnalysisStore.getState().snapshot).not.toBeNull();
    });
    expect(useGameStore.getState().drillState).toBe("abandoned");
    // Drain the abandon/teardown continuations before unmounting, so no state
    // update lands on the outgoing tree outside act() (the pre-push gate rejects
    // "not wrapped in act(" as a hard failure).
    await act(async () => {});

    // Remount /play the way the round trip back from /drill-analysis does.
    cleanup();
    mockLocation = {
      state: {
        returnFromDrillAnalysis: { sourceSessionId: "session-characterization" },
      },
      pathname: "/play",
    };
    // The fresh mount fires its own async effects (rating fetch, opening-root
    // lineage); settle them inside act before asserting.
    await act(async () => {
      render(<ChessGame />);
    });

    // Reviewed-return presentation: drill actions restored, no generic banner.
    expect(screen.getByRole("button", { name: /^again$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /new game/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /play white/i })).toBeNull();
  });

  it("keeps the stopped drill active and surfaces an error when abandon fails", async () => {
    gameAnalysisStore.getState().resolveAnalysis(0, {
      id: "analysis-e4",
      move: "e2e4",
      bestMove: "d2d4",
      bestEval: 40,
      playedEval: 10,
      currentPositionEval: 10,
      playedEvalMate: null,
      currentPositionEvalMate: null,
      moveIndex: 0,
      delta: 30,
      classification: "mistake",
      blunder: false,
      recordable: false,
    });
    abandonDrillMock.mockRejectedValueOnce(new Error("network down"));

    await driveOffRouteFail();

    const analyze = await screen.findByRole("button", { name: "Analyze" });
    await act(async () => {
      fireEvent.click(analyze);
    });

    await waitFor(() => {
      expect(screen.getByText(/couldn't end the drill/i)).toBeInTheDocument();
    });
    // Drill stays active; no snapshot, no session teardown, no navigation.
    expect(useGameStore.getState().isGameActive).toBe(true);
    expect(useDrillAnalysisStore.getState().snapshot).toBeNull();
    expect(mockCoordinator.clearSession).not.toHaveBeenCalled();
  });

  it("restarts analysis worker before retrying a post-root analysis failure", async () => {
    useGameStore.setState({
      sessionId: "session-characterization",
      isGameActive: true,
      playerColor: "white",
      boardOrientation: "white",
      drillStrictnessCp: 25,
      liveFen: STARTING_FEN,
    });

    render(<ChessGame />);
    act(() => {
      useGameStore.setState({
        drillOpeningKey: "target-fen",
        drillState: "root_reached",
        drillStrictnessCp: 25,
      });
    });
    mockCoordinator.waitForAnalysis.mockReset();
    mockCoordinator.waitForAnalysis
      .mockRejectedValueOnce(new Error("Analysis worker unavailable"))
      .mockResolvedValueOnce({
        id: "analysis-e4",
        move: "e2e4",
        bestMove: "e2e4",
        bestEval: 10,
        playedEval: 10,
        currentPositionEval: 10,
        playedEvalMate: null,
        currentPositionEvalMate: null,
        moveIndex: 0,
        delta: 0,
        classification: "best",
        blunder: false,
        recordable: false,
      });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    const retry = await screen.findByRole("button", { name: /^retry$/i });

    await act(async () => {
      fireEvent.click(retry);
    });

    await waitFor(() => {
      expect(mockCoordinator.restartAnalysisWorker).toHaveBeenCalledTimes(1);
      expect(mockAnalyzeMove.mock.calls).toContainEqual([
        STARTING_FEN,
        "e2e4",
        "white",
        0,
      ]);
      expect(getNextOpponentMoveMock).toHaveBeenCalled();
    });
  });

  it("records resignation on revert, then continues in practice mode", async () => {
    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /revert last move/i }));
    expect(
      screen.getByText("Reverting records this game as a resignation"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(
      screen.queryByText("Reverting records this game as a resignation"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/^practice$/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /revert last move/i }));
    endGameMock.mockResolvedValueOnce({
      session_id: "session-characterization",
      result: "resign",
      ended_at: "2026-04-19T00:00:00Z",
      rating: {
        rating_before: 1200,
        rating_after: 1184,
        is_provisional: true,
      },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /revert anyway/i }));
    });

    expect(
      screen.queryByText("Reverting records this game as a resignation"),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/^practice$/i)).toBeInTheDocument();
    expect(screen.getByText(/no moves yet/i)).toBeInTheDocument();
    expect(uploadSessionMovesMock).toHaveBeenCalledWith(
      "session-characterization",
      expect.any(Array),
      // Terminal resign-before-revert flags the single opportunity recompute (g-y90g).
      expect.objectContaining({ recomputeOpportunity: true }),
    );
    expect(endGameMock).toHaveBeenCalledWith(
      "session-characterization",
      "resign",
      expect.any(String),
      true,
    );

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "d2", targetSquare: "d4" });
    });

    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(2);
    });
  });

  it("keeps the revert modal open with an inline error when sealing resignation fails", async () => {
    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    uploadSessionMovesMock.mockRejectedValueOnce(new Error("Network down"));

    fireEvent.click(screen.getByRole("button", { name: /revert last move/i }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /revert anyway/i }));
    });

    expect(
      screen.getByText("Reverting records this game as a resignation"),
    ).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Network down");
    expect(screen.queryByText(/^practice$/i)).not.toBeInTheDocument();
  });

  it("freezes move entry while a rated revert is being sealed", async () => {
    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    let resolveUpload: ((value: { moves_inserted: number }) => void) | null = null;
    uploadSessionMovesMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveUpload = resolve;
      }),
    );
    endGameMock.mockResolvedValueOnce({
      session_id: "session-characterization",
      result: "resign",
      ended_at: "2026-04-19T00:00:00Z",
      rating: {
        rating_before: 1200,
        rating_after: 1184,
        is_provisional: true,
      },
    });

    fireEvent.click(screen.getByRole("button", { name: /revert last move/i }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /revert anyway/i }));
    });

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /recording resignation/i }),
      ).toBeDisabled();
    });
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-allow-dragging",
      "false",
    );

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "d2", targetSquare: "d4" });
    });

    expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();

    await act(async () => {
      resolveUpload?.({ moves_inserted: 2 });
    });

    await waitFor(() => {
      expect(screen.getByText(/^practice$/i)).toBeInTheDocument();
    });
  });

  it("disables move-list actions while a rated revert is being sealed", async () => {
    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    let resolveUpload: ((value: { moves_inserted: number }) => void) | null = null;
    uploadSessionMovesMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveUpload = resolve;
      }),
    );
    endGameMock.mockResolvedValueOnce({
      session_id: "session-characterization",
      result: "resign",
      ended_at: "2026-04-19T00:00:00Z",
      rating: {
        rating_before: 1200,
        rating_after: 1184,
        is_provisional: true,
      },
    });

    fireEvent.click(screen.getByRole("button", { name: /revert last move/i }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /revert anyway/i }));
    });

    await waitFor(() => {
      expect(screen.getByTitle("Reset game")).toBeDisabled();
    });

    expect(screen.getByTitle("Resign")).toBeDisabled();
    expect(screen.getByTitle("Revert last move")).toBeDisabled();
    expect(screen.getByTitle("Flip board")).toBeDisabled();

    await act(async () => {
      resolveUpload?.({ moves_inserted: 2 });
    });
  });

  it("drops an already in-flight opponent reply once revert sealing begins", async () => {
    await startGameAsWhite();

    let resolveOpponentMove!: (value: {
      mode: "engine";
      move: { uci: string; san: string };
      target_blunder_id: null;
      decision_source: "backend_engine";
    }) => void;
    getNextOpponentMoveMock.mockReset();
    getNextOpponentMoveMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOpponentMove = resolve;
      }),
    );

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    fireEvent.click(screen.getByRole("button", { name: /revert last move/i }));

    let resolveUpload!: (value: { moves_inserted: number }) => void;
    uploadSessionMovesMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveUpload = resolve;
      }),
    );
    endGameMock.mockResolvedValueOnce({
      session_id: "session-characterization",
      result: "resign",
      ended_at: "2026-04-19T00:00:00Z",
      rating: {
        rating_before: 1200,
        rating_after: 1184,
        is_provisional: true,
      },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /revert anyway/i }));
    });

    await act(async () => {
      resolveOpponentMove({
        mode: "engine",
        move: { uci: "d7d5", san: "d5" },
        target_blunder_id: null,
        decision_source: "backend_engine",
      });
    });

    expect(screen.queryByRole("button", { name: /d5/i })).not.toBeInTheDocument();

    await act(async () => {
      resolveUpload({ moves_inserted: 2 });
    });

    await waitFor(() => {
      expect(screen.getByText(/^practice$/i)).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: /d5/i })).not.toBeInTheDocument();
  });

  it("applies player move through square-click flow and requests opponent reply", async () => {
    await startGameAsWhite();
    expect(capturedSquareClick).not.toBeNull();

    await act(async () => {
      capturedSquareClick?.({ square: "e2" });
    });

    await act(async () => {
      capturedSquareClick?.({ square: "e4" });
    });

    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalledWith(
        "session-characterization",
        expect.any(String),
        ["e2e4"],
      );
    });
  });

  it("closes ghost info when clicking outside the popover anchor", async () => {
    getNextOpponentMoveMock.mockResolvedValueOnce({
      mode: "ghost",
      move: { uci: "e7e5", san: "e5" },
      target_blunder_id: 42,
      target_blunder_srs: {
        blunder_id: 42,
        pass_count: 2,
        fail_count: 1,
        pass_streak: 1,
        last_reviewed_at: "2026-02-01T12:00:00Z",
        created_at: "2026-01-15T12:00:00Z",
      },
      target_fen:
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2",
      decision_source: "ghost_path",
    });

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /toggle ghost info/i }),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /toggle ghost info/i }));
    expect(
      screen.getByText("Ghost Target Blunder Position"),
    ).toBeInTheDocument();

    fireEvent.mouseDown(document.body);

    await waitFor(() => {
      expect(
        screen.queryByText("Ghost Target Blunder Position"),
      ).not.toBeInTheDocument();
    });
  });

  it("routes post-game View Analysis action to history callback", async () => {
    const onOpenHistory = vi.fn();
    await startGameAsWhite(onOpenHistory);

    fireEvent.click(screen.getByRole("button", { name: /resign/i }));
    await waitFor(() => {
      expect(screen.getByText("Are you sure?")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Resign"));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /view analysis/i }),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /view analysis/i }));

    expect(onOpenHistory).toHaveBeenCalledWith(
      expect.objectContaining({
        select: "latest",
        source: "post_game_view_analysis",
      }),
    );
  });

  // g-e01b: the post-game History button was removed as redundant with View
  // Analysis — both routed to the latest game's history entry.
  it("does not offer a post-game History action", async () => {
    const onOpenHistory = vi.fn();
    await startGameAsWhite(onOpenHistory);

    fireEvent.click(screen.getByRole("button", { name: /resign/i }));
    await waitFor(() => {
      expect(screen.getByText("Are you sure?")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Resign"));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /view analysis/i }),
      ).toBeInTheDocument();
    });

    expect(
      screen.queryByRole("button", { name: /^history$/i }),
    ).not.toBeInTheDocument();
  });

  // ---- Instant "Again" + gear settings on drill end (g-osni) -------------
  const makeDrillResponse = (overrides: Record<string, unknown> = {}) => ({
    session_id: "drill-restart",
    mode: "drill",
    drill_state: "active",
    opening_key: "target-fen",
    opening_name: "Target",
    opening_family: "Target",
    eco: null,
    depth: 1,
    player_color: "white",
    engine_elo: 1500,
    strictness: "lenient",
    strictness_cp: 20,
    is_rated: false,
    rated_start_ply: null,
    normal_started_at: null,
    converted_at: null,
    ...overrides,
  });

  it("instant Again restarts the drill with exact opening/side/strictness (difficulty resampled) and no overlay", async () => {
    await driveOffRouteFail();
    useGameStore.setState({
      playerColor: "white",
      drillStrictness: "lenient",
      drillStrictnessCp: 20,
    });
    mockCoordinator.flushPendingUploads.mockClear();
    mockCoordinator.stopSessionUploads.mockClear();
    startDrillMock.mockResolvedValueOnce(makeDrillResponse());

    const again = await screen.findByRole("button", { name: /^again$/i });
    await act(async () => {
      fireEvent.click(again);
    });

    await waitFor(() => {
      expect(startDrillMock).toHaveBeenCalledWith({
        opening_key: "target-fen",
        player_color: "white",
        // Difficulty is re-randomized (g-ncvm), so any sampled bin is valid.
        engine_elo: expect.any(Number),
        strictness: "lenient",
        // Exact cp preserved — a 20cp drill restarts at 20cp, not a rounded 25.
        strictness_cp: 20,
      });
    });
    expect(mockCoordinator.flushPendingUploads).not.toHaveBeenCalled();
    expect(mockCoordinator.stopSessionUploads).toHaveBeenCalledTimes(1);
    expect(
      screen.queryByRole("button", { name: /start drill/i }),
    ).not.toBeInTheDocument();
  });

  it("instant Again re-randomizes opponent difficulty (g-ncvm)", async () => {
    await driveOffRouteFail();
    act(() => {
      useGameStore.setState({
        playerColor: "white",
        drillStrictness: "lenient",
        drillStrictnessCp: 20,
        engineElo: 1500,
        playerRating: 1500,
      });
    });
    // Math.random() === 0 makes sampleEloBin return MAIA_ELO_BINS[0] regardless
    // of rating, so the resampled bin is deterministic and ≠ the stored 1500.
    vi.spyOn(Math, "random").mockReturnValue(0);
    startDrillMock.mockResolvedValueOnce(makeDrillResponse());

    const again = await screen.findByRole("button", { name: /^again$/i });
    await act(async () => {
      fireEvent.click(again);
    });

    await waitFor(() => {
      expect(startDrillMock).toHaveBeenCalledWith(
        expect.objectContaining({ engine_elo: MAIA_ELO_BINS[0] }),
      );
    });
    // The resampled value drives the avatar/label refresh via the store.
    expect(useGameStore.getState().engineElo).toBe(MAIA_ELO_BINS[0]);
  });

  it("captures drill_again_clicked when Again is pressed", async () => {
    await driveOffRouteFail();
    act(() => {
      useGameStore.setState({
        playerColor: "white",
        drillStrictness: "lenient",
        drillStrictnessCp: 20,
        playerRating: 1500,
      });
    });
    // Mocked Math.random pins the resampled bin so we can assert the exact value.
    vi.spyOn(Math, "random").mockReturnValue(0);
    startDrillMock.mockResolvedValueOnce(makeDrillResponse());

    const again = await screen.findByRole("button", { name: /^again$/i });
    await act(async () => {
      fireEvent.click(again);
    });

    expect(captureEventMock).toHaveBeenCalledWith("drill_again_clicked", {
      opening_key: "target-fen",
      player_color: "white",
      engine_elo: MAIA_ELO_BINS[0],
    });
  });

  it("opens the setup overlay instead of restarting when exact cp is missing", async () => {
    getOpeningRootsMock.mockResolvedValue({ families: [] });
    await driveOffRouteFail();
    useGameStore.setState({
      drillStrictness: "lenient",
      drillStrictnessCp: null,
    });

    const again = await screen.findByRole("button", { name: /^again$/i });
    await act(async () => {
      fireEvent.click(again);
    });

    expect(startDrillMock).not.toHaveBeenCalled();
    expect(
      await screen.findByRole("button", { name: /start drill/i }),
    ).toBeInTheDocument();
  });

  it("a drill restarted via Again still stops on a post-root bad move", async () => {
    await driveOffRouteFail();
    useGameStore.setState({
      playerColor: "white",
      boardOrientation: "white",
      drillStrictness: "standard",
      drillStrictnessCp: 25,
    });
    startDrillMock.mockResolvedValueOnce(
      makeDrillResponse({
        session_id: "drill-restart",
        drill_state: "root_reached",
        strictness_cp: 25,
      }),
    );

    const again = await screen.findByRole("button", { name: /^again$/i });
    await act(async () => {
      fireEvent.click(again);
    });

    await waitFor(() => {
      expect(useGameStore.getState().sessionId).toBe("drill-restart");
      expect(useGameStore.getState().drillState).toBe("root_reached");
      // Exact strictness carried into the restarted drill.
      expect(useGameStore.getState().drillStrictnessCp).toBe(25);
    });

    // A post-root move whose eval loss (delta 30) exceeds the 25cp threshold
    // must still fail the restarted drill.
    mockCoordinator.waitForAnalysis.mockReset();
    mockCoordinator.waitForAnalysis.mockResolvedValue({
      id: "analysis-e4",
      move: "e2e4",
      bestMove: "d2d4",
      bestEval: 40,
      playedEval: 10,
      currentPositionEval: 10,
      playedEvalMate: null,
      currentPositionEvalMate: null,
      moveIndex: 0,
      delta: 30,
      classification: "mistake",
      blunder: false,
      recordable: false,
    });
    failDrillMock.mockResolvedValueOnce({
      session_id: "drill-restart",
      mode: "drill",
      drill_state: "failed",
      opening_key: "target-fen",
      opening_name: "Target",
      opening_family: "Target",
      eco: null,
      depth: 1,
      player_color: "white",
      engine_elo: 1500,
      strictness: "standard",
      strictness_cp: 25,
      is_rated: false,
      rated_start_ply: null,
      normal_started_at: null,
      converted_at: null,
      terminal_reason: "accuracy",
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(failDrillMock).toHaveBeenCalledWith("drill-restart", "accuracy");
      expect(useGameStore.getState().drillState).toBe("failed");
      expect(useGameStore.getState().drillTerminalReason).toBe("accuracy");
    });
  });

  it("disables the restart actions while a new drill is starting", async () => {
    await driveOffRouteFail();
    useGameStore.setState({
      playerColor: "white",
      drillStrictness: "lenient",
      drillStrictnessCp: 20,
    });
    let resolveStart: (value: unknown) => void = () => {};
    startDrillMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveStart = resolve;
      }),
    );

    const again = await screen.findByRole("button", { name: /^again$/i });
    await act(async () => {
      fireEvent.click(again);
    });

    // Successful abandonment immediately finalizes the old drill locally, so
    // the pending replacement is represented by the ended-drill banner rather
    // than the former live stopped-drill controls. Its replacement actions must
    // remain disabled until startDrill settles.
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /^new drill$/i }),
      ).toBeDisabled();
      expect(
        screen.getByRole("button", { name: /change drill settings/i }),
      ).toBeDisabled();
    });

    await act(async () => {
      resolveStart(makeDrillResponse());
    });
  });

  it("applies the opponent move when restarting an opponent-first drill", async () => {
    await driveOffRouteFail();
    act(() => {
      useGameStore.setState({
        playerColor: "black",
        boardOrientation: "black",
        drillStrictness: "lenient",
        drillStrictnessCp: 20,
      });
    });
    startDrillMock.mockResolvedValueOnce(makeDrillResponse({ player_color: "black" }));
    getNextOpponentMoveMock.mockClear();

    const again = await screen.findByRole("button", { name: /^again$/i });
    await act(async () => {
      fireEvent.click(again);
    });

    await waitFor(() => {
      expect(startDrillMock).toHaveBeenCalledWith(
        expect.objectContaining({ player_color: "black" }),
      );
    });
    // The opponent move must target the NEW session, not the abandoned one — the
    // direct call would have captured the pre-restart sessionId.
    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalledWith(
        "drill-restart",
        expect.any(String),
        expect.any(Array),
      );
    });
    expect(getNextOpponentMoveMock).not.toHaveBeenCalledWith(
      "session-characterization",
      expect.anything(),
      expect.anything(),
    );
  });

  it("gear opens the overlay seeded from the store, ignoring conflicting localStorage", async () => {
    getOpeningRootsMock.mockResolvedValue({
      families: [
        {
          family_name: "Target",
          roots: [
            {
              opening_key: "target-fen",
              opening_name: "Target Opening",
              opening_family: "Target",
              eco: null,
              depth: 1,
            },
            {
              opening_key: "other-fen",
              opening_name: "Other Opening",
              opening_family: "Other",
              eco: null,
              depth: 1,
            },
          ],
        },
      ],
    });
    localStorage.setItem(
      "ghostreplay_drill_prefs",
      JSON.stringify({
        openingKey: "other-fen",
        engineElo: 800,
        strictnessCp: 50,
        playerColor: "black",
      }),
    );
    await driveOffRouteFail();
    act(() => {
      useGameStore.setState({
        playerColor: "white",
        engineElo: 1500,
        playerRating: 1500,
        drillStrictness: "lenient",
        drillStrictnessCp: 20,
      });
    });
    // Opening difficulty is resampled (g-ncvm), not seeded from store/localStorage.
    vi.spyOn(Math, "random").mockReturnValue(0);

    const gear = await screen.findByRole("button", {
      name: /change drill settings/i,
    });
    await act(async () => {
      fireEvent.click(gear);
    });

    expect(
      await screen.findByRole("button", { name: /start drill/i }),
    ).toBeInTheDocument();
    // Difficulty is re-randomized to MAIA_ELO_BINS[0] (mocked) and seeds the
    // panel draft only; opening the overlay does NOT mutate the store engineElo
    // (g-fxrm) — it commits on Start.
    expect(useGameStore.getState().engineElo).toBe(1500);
    // Drill side is now local state, decoupled from the store playerColorChoice;
    // the White side king button should be active (from the store's player_color).
    expect(screen.getByRole("button", { name: /^white$/i })).toHaveClass(
      "play-side-button--active",
    );
    // Strictness is force-always (g-09mu): neither the store's exact 20cp nor
    // the localStorage 50 pre-selects a tier — the panel opens unset and Start
    // is gated until the user picks one.
    expect(
      screen.getByText(/pick a strictness to start/i),
    ).toBeInTheDocument();
    for (const name of [/^strict$/i, /^standard$/i, /^lenient$/i]) {
      expect(screen.getByRole("button", { name })).toHaveAttribute(
        "aria-pressed",
        "false",
      );
    }
    expect(screen.getByRole("button", { name: /start drill/i })).toBeDisabled();
    // The store's opening is selected, not the localStorage one (picker trigger
    // shows the selected opening name).
    await waitFor(() => {
      expect(screen.getByRole("combobox")).toHaveTextContent("Target");
    });

    startDrillMock.mockResolvedValueOnce(makeDrillResponse());
    fireEvent.click(screen.getByRole("button", { name: /^standard$/i }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /start drill/i }));
    });
    await waitFor(() => {
      expect(startDrillMock).toHaveBeenCalledWith(
        expect.objectContaining({
          opening_key: "target-fen",
          player_color: "white",
          engine_elo: MAIA_ELO_BINS[0],
          strictness: "standard",
          strictness_cp: 25,
        }),
      );
    });
    localStorage.removeItem("ghostreplay_drill_prefs");
  });

  it("opens the drill setup with no tier selected even when a saved strictnessCp pref exists (g-09mu force-always)", async () => {
    getOpeningRootsMock.mockResolvedValue({ families: [] });
    // A legacy pref with strictnessCp must NOT pre-select a tier.
    localStorage.setItem(
      "ghostreplay_drill_prefs",
      JSON.stringify({ engineElo: 1000, strictnessCp: 50, playerColor: "white" }),
    );

    render(<ChessGame />);

    // Overlay is open on mount (play mode); switch to the Drill tab.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^drill$/i }));
    });

    expect(
      await screen.findByRole("button", { name: /start drill/i }),
    ).toBeDisabled();
    for (const name of [/^strict$/i, /^standard$/i, /^lenient$/i]) {
      expect(screen.getByRole("button", { name })).toHaveAttribute(
        "aria-pressed",
        "false",
      );
    }
    expect(
      screen.getByText(/pick a strictness to start/i),
    ).toBeInTheDocument();

    localStorage.removeItem("ghostreplay_drill_prefs");
  });

  it("clears the prior opening so a failed reload can't start a stale selection", async () => {
    const targetFamily = {
      families: [
        {
          family_name: "Target",
          roots: [
            {
              opening_key: "target-fen",
              opening_name: "Target Opening",
              opening_family: "Target",
              eco: null,
              depth: 1,
            },
          ],
        },
      ],
    };
    // Order-dependent: the live-lineage registry preload (g-a5v3) fires on
    // mount and consumes the FIRST call, before the overlay opens at all.
    // Then: first overlay open succeeds; the reopen's fetch fails.
    getOpeningRootsMock
      .mockResolvedValueOnce({ families: [] }) // registry preload (on mount)
      .mockResolvedValueOnce(targetFamily) // first overlay open
      .mockRejectedValueOnce(new Error("boom")); // reopen

    await driveOffRouteFail();
    useGameStore.setState({ playerColor: "white", drillStrictnessCp: 20 });

    const gear = await screen.findByRole("button", {
      name: /change drill settings/i,
    });
    await act(async () => {
      fireEvent.click(gear);
    });

    // Opening resolves; Start Drill stays gated until a strictness tier is
    // picked (g-09mu force-always), then enables.
    await waitFor(() => {
      expect(screen.getByRole("combobox")).toHaveTextContent("Target");
    });
    expect(screen.getByRole("button", { name: /start drill/i })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /^standard$/i }));
    expect(
      screen.getByRole("button", { name: /start drill/i }),
    ).not.toBeDisabled();

    // Close the overlay, then reopen with a failing fetch.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^close$/i }));
    });
    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: /change drill settings/i }),
      );
    });

    // Failure surfaces on the trigger and the stale selection is cleared, so
    // Start Drill cannot relaunch the previous opening.
    await waitFor(() => {
      expect(screen.getByRole("combobox")).toHaveTextContent(
        /failed to load openings/i,
      );
    });
    expect(screen.getByRole("button", { name: /start drill/i })).toBeDisabled();
  });

  it("ad-hoc card drill survives a getOpeningRoots() failure and sends its line", async () => {
    // A /openings card navigates with the target FEN + full line. The roots
    // fetch fails, but the synthetic selection must survive and Start Drill stay
    // live — the ad-hoc drill carries everything it needs.
    getOpeningRootsMock.mockRejectedValue(new Error("boom"));
    mockLocation = {
      state: {
        drillSetup: {
          targetFen: "target-fen",
          line: ["e2e4", "c7c5"],
          displayName: "Sicilian Defense",
          eco: null,
          playerColor: "white",
        },
      },
      pathname: "/play",
    };

    render(<ChessGame />);

    // The picker shows the synthesized name despite the failed roots fetch, and
    // Start Drill gates only on the selection + a strictness tier pick — not
    // the roots list.
    const start = await screen.findByRole("button", { name: /start drill/i });
    await waitFor(() => {
      expect(screen.getByRole("combobox")).toHaveTextContent("Sicilian Defense");
    });
    expect(start).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /^standard$/i }));
    expect(start).not.toBeDisabled();

    startDrillMock.mockResolvedValueOnce(
      makeDrillResponse({ opening_key: "target-fen", opening_name: "Sicilian Defense" }),
    );
    await act(async () => {
      fireEvent.click(start);
    });

    await waitFor(() => {
      expect(startDrillMock).toHaveBeenCalledWith({
        opening_key: "target-fen",
        player_color: "white",
        engine_elo: expect.any(Number),
        strictness: expect.any(String),
        strictness_cp: expect.any(Number),
        line: ["e2e4", "c7c5"],
      });
    });
  });

  it("switches an ad-hoc card to a registered opening and starts a clean drill, dropping the ad-hoc line (g-fxrm)", async () => {
    // Roots resolve so the panel offers a registered opening to switch to.
    getOpeningRootsMock.mockResolvedValue({
      families: [
        {
          family_name: "Italian",
          roots: [
            {
              opening_key: "italian-key",
              opening_name: "Italian Game",
              opening_family: "Italian",
              eco: "C50",
              depth: 1,
            },
          ],
        },
      ],
    });
    // Ad-hoc card nav: synthetic opening + the full line.
    mockLocation = {
      state: {
        drillSetup: {
          targetFen: "adhoc-fen",
          line: ["e2e4", "c7c5"],
          displayName: "Custom Najdorf",
          eco: null,
          playerColor: "white",
        },
      },
      pathname: "/play",
    };

    render(<ChessGame />);

    // The panel seeds the ad-hoc opening.
    await waitFor(() => {
      expect(screen.getByRole("combobox")).toHaveTextContent("Custom Najdorf");
    });

    // Switch to the registered opening. In the draft model this is local to the
    // panel and drops the ad-hoc line; ChessGame's seed scratch is not touched
    // until Start, where handleStartDrill syncs it from the committed draft.
    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(await screen.findByRole("option", { name: /Italian Game/ }));

    // Pick a strictness tier — Start is gated until one is chosen (g-09mu).
    fireEvent.click(screen.getByRole("button", { name: /^standard$/i }));

    // Start the registered drill — the API must get the registered key with NO
    // ad-hoc line attached (g-fxrm Finding 2): the stale line cannot ride along.
    startDrillMock.mockResolvedValueOnce(
      makeDrillResponse({ opening_key: "italian-key", opening_name: "Italian Game" }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /start drill/i }));
    });
    await waitFor(() => {
      expect(startDrillMock).toHaveBeenCalledWith(
        expect.objectContaining({ opening_key: "italian-key", line: undefined }),
      );
    });
    // The committed draft line is null, so the seed scratch is cleared (the
    // durable store line is likewise null), preventing later resurrection.
    expect(useGameStore.getState().drillLine).toBeNull();
  });

  // Reaches the natural-end PostGameBanner ("Another drill") branch. Resigning is
  // scaffolding for gameResult + showPostGamePrompt; the drillState the branch keys
  // on ("failed") is set the way naturalEndDrill's contract echo sets it
  // (useChessGameLifecycle.ts:436). Resign itself finalizes to "abandoned" and can
  // no longer produce this state (g-drill-failed-overwrite).
  const reachNaturalEndDrillBanner = async () => {
    getOpeningRootsMock.mockResolvedValue({ families: [] });
    await startGameAsWhite();
    act(() => {
      useGameStore.setState({
        drillOpeningKey: "target-fen",
        drillState: "active",
        drillStrictness: "lenient",
        drillStrictnessCp: 20,
        playerColor: "white",
      });
    });

    fireEvent.click(screen.getByRole("button", { name: /resign/i }));
    await waitFor(() => {
      expect(screen.getByText("Are you sure?")).toBeInTheDocument();
    });
    await act(async () => {
      fireEvent.click(screen.getByText("Resign"));
    });
    act(() => {
      useGameStore.getState().setDrillState("failed");
    });

    expect(
      await screen.findByRole("button", { name: /another drill/i }),
    ).toBeInTheDocument();
    expect(useGameStore.getState().gameResult).not.toBeNull();
  };

  it("natural-end Another drill restarts instantly with exact opening/side/strictness (difficulty resampled)", async () => {
    await reachNaturalEndDrillBanner();
    startDrillMock.mockResolvedValueOnce(makeDrillResponse());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /another drill/i }));
    });

    await waitFor(() => {
      expect(startDrillMock).toHaveBeenCalledWith({
        opening_key: "target-fen",
        player_color: "white",
        // Difficulty is re-randomized (g-ncvm), so any sampled bin is valid.
        engine_elo: expect.any(Number),
        strictness: "lenient",
        strictness_cp: 20,
      });
    });
    expect(
      screen.queryByRole("button", { name: /start drill/i }),
    ).not.toBeInTheDocument();
  });

  it("failed natural-end restart opens the overlay, preserves gameResult, and restores the banner on cancel", async () => {
    await reachNaturalEndDrillBanner();
    startDrillMock.mockRejectedValueOnce(new Error("network down"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /another drill/i }));
    });

    // Overlay opens; gameResult preserved (handleNewDrill clears it only on
    // success), and the banner is hidden while the modal is open.
    expect(
      await screen.findByRole("button", { name: /start drill/i }),
    ).toBeInTheDocument();
    expect(useGameStore.getState().gameResult).not.toBeNull();
    expect(
      screen.queryByRole("button", { name: /another drill/i }),
    ).not.toBeInTheDocument();

    // Cancelling the overlay restores the natural-end banner.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /close/i }));
    });
    expect(
      await screen.findByRole("button", { name: /another drill/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /start drill/i }),
    ).not.toBeInTheDocument();
  });

  it("shakes the board and blocks the move on a board CLICK while reviewing a past move (g-1y68 A3)", async () => {
    await startGameAsWhite();

    // Play e4; the beforeEach mock auto-replies d5, giving a 2-ply live game.
    await act(async () => {
      capturedSquareClick?.({ square: "e2" });
    });
    await act(async () => {
      capturedSquareClick?.({ square: "e4" });
    });
    await waitFor(() => {
      expect(useGameStore.getState().moveHistory).toHaveLength(2);
    });

    // Park on the first ply — live game, reviewing a past move.
    act(() => {
      useGameStore.getState().setViewIndex(0);
    });

    const movesBefore = useGameStore.getState().moveHistory.length;
    const opponentCallsBefore = getNextOpponentMoveMock.mock.calls.length;

    await act(async () => {
      capturedSquareClick?.({ square: "e2" });
    });

    // No move attempted, no opponent request, and the board shakes.
    expect(useGameStore.getState().moveHistory).toHaveLength(movesBefore);
    expect(getNextOpponentMoveMock.mock.calls.length).toBe(opponentCallsBefore);
    expect(
      document.querySelector(".chessboard-square-measure--nudge"),
    ).not.toBeNull();
  });

  it("shakes the board and rejects a DRAG while reviewing a past move (g-1y68 A3)", async () => {
    await startGameAsWhite();

    await act(async () => {
      capturedSquareClick?.({ square: "e2" });
    });
    await act(async () => {
      capturedSquareClick?.({ square: "e4" });
    });
    await waitFor(() => {
      expect(useGameStore.getState().moveHistory).toHaveLength(2);
    });

    act(() => {
      useGameStore.getState().setViewIndex(0);
    });

    // The board is draggable while reviewing purely so a drag attempt reaches us.
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-allow-dragging",
      "true",
    );

    const movesBefore = useGameStore.getState().moveHistory.length;
    const opponentCallsBefore = getNextOpponentMoveMock.mock.calls.length;

    let dropResult: boolean | undefined;
    await act(async () => {
      dropResult = capturedPieceDrop?.({
        sourceSquare: "e2",
        targetSquare: "e4",
      });
    });

    // Drop rejected (piece snaps back), no move, no opponent request, shake fires.
    expect(dropResult).toBe(false);
    expect(useGameStore.getState().moveHistory).toHaveLength(movesBefore);
    expect(getNextOpponentMoveMock.mock.calls.length).toBe(opponentCallsBefore);
    expect(
      document.querySelector(".chessboard-square-measure--nudge"),
    ).not.toBeNull();
  });

  it("does not shake on a board click while merely waiting for the opponent (g-1y68 A3)", async () => {
    await startGameAsWhite();

    // Hold the opponent reply pending so the board stays live but not the
    // player's turn — a different situation that must stay a silent no-op.
    getNextOpponentMoveMock.mockReset();
    getNextOpponentMoveMock.mockReturnValueOnce(new Promise(() => {}));

    await act(async () => {
      capturedSquareClick?.({ square: "e2" });
    });
    await act(async () => {
      capturedSquareClick?.({ square: "e4" });
    });
    await waitFor(() => {
      expect(useGameStore.getState().moveHistory).toHaveLength(1);
    });

    await act(async () => {
      capturedSquareClick?.({ square: "d2" });
    });

    expect(useGameStore.getState().moveHistory).toHaveLength(1);
    expect(
      document.querySelector(".chessboard-square-measure--nudge"),
    ).toBeNull();
  });

});

describe("ChessGame eval bar behavior", () => {
  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    startGameMock.mockReset();
    endGameMock.mockReset();
    uploadSessionMovesMock.mockReset();
    getNextOpponentMoveMock.mockReset();
    recordBlunderMock.mockReset();
    recordManualBlunderMock.mockReset();
    reviewSrsBlunderMock.mockReset();
    mockAnalyzeMove.mockReset();
    evaluatePositionMock.mockReset();
    lookupOpeningByFenMock.mockReset();
    gameAnalysisStore.getState().clearAll();
    capturedPieceDrop = null;

    startGameMock.mockResolvedValue({
      session_id: "session-eval",
      engine_elo: 1500,
      player_color: "white",
    });
    getNextOpponentMoveMock.mockResolvedValue({
      mode: "engine",
      move: { uci: "d7d5", san: "d5" },
      target_blunder_id: null,
      decision_source: "backend_engine",
    });
    lookupOpeningByFenMock.mockResolvedValue(null);
  });

  it("keeps prior eval displayed while latest move analysis is pending", async () => {
    render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(startGameMock).toHaveBeenCalled();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    // Only the earlier move has analysis so far.
    act(() => {
      gameAnalysisStore.getState().resolveAnalysis(0, {
        id: "analysis-0",
        move: "e2e4",
        bestMove: "e2e4",
        bestEval: 80,
        playedEval: 80,
        currentPositionEval: 80,
        playedEvalMate: null,
        currentPositionEvalMate: null,
        moveIndex: 0,
        delta: 0,
        classification: "best" as const,
        blunder: false,
        recordable: false,
      });
    });

    await waitFor(() => {
      expect(
        screen.getByRole("img", { name: "Evaluation +0.8" }),
      ).toBeInTheDocument();
    });
  });
});

describe("ChessGame clipboard copy", () => {
  const originalClipboardDescriptor = Object.getOwnPropertyDescriptor(
    navigator,
    "clipboard",
  );

  const setClipboard = (clipboard: Pick<Clipboard, "writeText"> | undefined) => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: clipboard,
    });
  };

  const gameplaySnapshot = () => {
    const { liveFen, moveHistory, viewIndex, isGameActive, gameResult } =
      useGameStore.getState();
    return {
      liveFen,
      moveHistory: moveHistory.map((move) => ({ ...move })),
      viewIndex,
      isGameActive,
      gameResult,
    };
  };

  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    startGameMock.mockReset();
    endGameMock.mockReset();
    uploadSessionMovesMock.mockReset();
    getNextOpponentMoveMock.mockReset();
    recordBlunderMock.mockReset();
    recordManualBlunderMock.mockReset();
    reviewSrsBlunderMock.mockReset();
    mockAnalyzeMove.mockReset();
    evaluatePositionMock.mockReset();
    lookupOpeningByFenMock.mockReset();
    getOpeningRootsMock.mockReset();
    getOpeningRootsMock.mockResolvedValue({ families: [] });
    __resetOpeningRootIndexCache();
    gameAnalysisStore.getState().clearAll();
    capturedPieceDrop = null;

    getNextOpponentMoveMock.mockResolvedValue({
      mode: "engine",
      move: { uci: "d7d5", san: "d5" },
      target_blunder_id: null,
      decision_source: "backend_engine",
    });
    lookupOpeningByFenMock.mockResolvedValue(null);
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 });
    setClipboard(undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
    if (originalClipboardDescriptor) {
      Object.defineProperty(
        navigator,
        "clipboard",
        originalClipboardDescriptor,
      );
    } else {
      Reflect.deleteProperty(navigator, "clipboard");
    }
    vi.restoreAllMocks();
  });

  const startGameAsWhite = async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-clipboard",
      engine_elo: 1500,
      player_color: "white",
    });

    render(<ChessGame />);
    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(startGameMock).toHaveBeenCalled();
    });
  };

  it("copies the FEN displayed for a historical position and confirms success", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard({ writeText });
    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /^e4$/i }));
    await waitFor(() => {
      expect(screen.getByTestId("chessboard")).toHaveAttribute(
        "data-position",
        E4_FEN,
      );
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Copy position FEN" }),
    );

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(E4_FEN);
      expect(screen.getByText("FEN copied")).toBeInTheDocument();
    });
  });

  it("auto-dismisses each success notice after its own full lifetime", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard({ writeText });
    await startGameAsWhite();
    const copyButton = screen.getByRole("button", {
      name: "Copy position FEN",
    });

    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(copyButton);
    });
    expect(screen.getByText("FEN copied")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(1_799));
    expect(screen.getByText("FEN copied")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(1));
    expect(screen.queryByText("FEN copied")).not.toBeInTheDocument();

    await act(async () => {
      fireEvent.click(copyButton);
    });
    act(() => vi.advanceTimersByTime(1_000));

    await act(async () => {
      fireEvent.click(copyButton);
    });
    act(() => vi.advanceTimersByTime(800));
    expect(screen.getByText("FEN copied")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(999));
    expect(screen.getByText("FEN copied")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(1));
    expect(screen.queryByText("FEN copied")).not.toBeInTheDocument();
    expect(writeText).toHaveBeenCalledTimes(3);
  });

  it("reports an unavailable Clipboard API without changing gameplay state", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    await startGameAsWhite();
    const beforeCopy = gameplaySnapshot();

    fireEvent.click(
      screen.getByRole("button", { name: "Copy position FEN" }),
    );

    await waitFor(() => {
      expect(screen.getByText("Couldn't copy FEN")).toBeInTheDocument();
    });
    expect(consoleError).toHaveBeenCalledWith(
      "[Clipboard] Clipboard API is unavailable",
    );
    expect(gameplaySnapshot()).toEqual(beforeCopy);
  });

  it("handles a rejected clipboard write without changing gameplay state", async () => {
    const rejection = new DOMException("Document is not focused", "NotAllowedError");
    const writeText = vi.fn().mockRejectedValue(rejection);
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    setClipboard({ writeText });
    await startGameAsWhite();
    const beforeCopy = gameplaySnapshot();

    fireEvent.click(
      screen.getByRole("button", { name: "Copy position FEN" }),
    );

    await waitFor(() => {
      expect(screen.getByText("Couldn't copy FEN")).toBeInTheDocument();
    });
    expect(writeText).toHaveBeenCalledWith(STARTING_FEN);
    expect(consoleError).toHaveBeenCalledWith(
      "[Clipboard] Failed to copy position FEN:",
      rejection,
    );
    expect(gameplaySnapshot()).toEqual(beforeCopy);
  });
});

describe("ChessGame blunder recording", () => {
  beforeEach(() => {
    // jsdom doesn't implement scrollIntoView
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    startGameMock.mockReset();
    endGameMock.mockReset();
    endGameMock.mockResolvedValue({});
    uploadSessionMovesMock.mockReset();
    getNextOpponentMoveMock.mockReset();
    recordBlunderMock.mockReset();
    recordManualBlunderMock.mockReset();
    reviewSrsBlunderMock.mockReset();
    mockAnalyzeMove.mockReset();
    evaluatePositionMock.mockReset();
    lookupOpeningByFenMock.mockReset();
    gameAnalysisStore.getState().clearAll();
    capturedPieceDrop = null;

    getNextOpponentMoveMock.mockResolvedValue({
      mode: "engine",
      move: { uci: "d7d5", san: "d5" },
      target_blunder_id: null,
      decision_source: "backend_engine",
    });
    lookupOpeningByFenMock.mockResolvedValue(null);
    recordBlunderMock.mockResolvedValue({
      blunder_id: 1,
      position_id: 10,
      positions_created: 3,
      is_new: true,
    });
    recordManualBlunderMock.mockResolvedValue({
      blunder_id: 2,
      position_id: 11,
      positions_created: 1,
      is_new: true,
    });
    reviewSrsBlunderMock.mockResolvedValue({
      blunder_id: 42,
      pass_streak: 1,
      priority: 0,
      next_expected_review: "2026-02-08T00:00:00Z",
    });
    audioPlayMock.mockReset();
    audioPlayMock.mockResolvedValue(undefined);
    audioCtorSpy.mockReset();
    class MockAudio {
      constructor(src: string) {
        audioCtorSpy(src);
      }

      play() {
        return audioPlayMock();
      }
    }
    vi.stubGlobal("Audio", MockAudio);
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const startGameAsWhite = async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-blunder",
      engine_elo: 1500,
      player_color: "white",
    });

    const renderResult = render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(startGameMock).toHaveBeenCalled();
    });

    return renderResult;
  };

  it("calls recordBlunder when analysis detects a blunder after user move", async () => {
    mockAnalyzeMove.mockImplementation(
      (_fen: string, move: string, _color: string, moveIndex: number) => {
        if (moveIndex !== 2) {
          return;
        }
        gameAnalysisStore.getState().resolveAnalysis(moveIndex, {
          id: "test-blunder",
          move,
          bestMove: "c2c4",
          bestEval: 50,
          playedEval: -150,
          currentPositionEval: -150,
          playedEvalMate: null,
          currentPositionEvalMate: null,
          moveIndex,
          delta: 200,
          classification: "blunder" as const,
          blunder: true,
          recordable: true,
        });
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.any(String),
        "g1f3",
        "white",
        2,
        expect.any(Number),
      );
    });

    await waitFor(() => {
      expect(recordBlunderMock).toHaveBeenCalledWith(
        "session-blunder",
        expect.any(String),
        expect.any(String),
        "Nf3",
        "c4",
        50,
        -150,
        expect.any(String),
      );
    });
  });

  it("plays a random bundled blunder audio clip when player blunders", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0);
    mockAnalyzeMove.mockImplementation(
      (_fen: string, move: string, _color: string, moveIndex: number) => {
        if (moveIndex !== 2) {
          return;
        }
        gameAnalysisStore.getState().resolveAnalysis(moveIndex, {
          id: "blunder-audio",
          move,
          bestMove: "c2c4",
          bestEval: 50,
          playedEval: -150,
          currentPositionEval: -150,
          playedEvalMate: null,
          currentPositionEvalMate: null,
          moveIndex,
          delta: 200,
          classification: "blunder" as const,
          blunder: true,
          recordable: true,
        });
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.any(String),
        "g1f3",
        "white",
        2,
        expect.any(Number),
      );
    });

    await waitFor(() => {
      expect(audioCtorSpy).toHaveBeenCalledWith("/audio/blunder1.m4a");
      expect(audioPlayMock).toHaveBeenCalled();
    });
  });

  it("does not call recordBlunder for non-blunder analysis", async () => {
    mockAnalyzeMove.mockImplementation(
      (_fen: string, move: string, _color: string, moveIndex: number) => {
        gameAnalysisStore.getState().setLastAnalysis({
          id: "test-ok",
          move,
          bestMove: move,
          bestEval: 50,
          playedEval: 40,
          currentPositionEval: 40,
          playedEvalMate: null,
          currentPositionEvalMate: null,
          moveIndex,
          delta: 10,
          classification: "excellent" as const,
          blunder: false,
        recordable: false,
        });
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalled();
    });

    // Give effects time to run
    await new Promise((r) => setTimeout(r, 50));

    expect(recordBlunderMock).not.toHaveBeenCalled();
  });

  it("records only the first blunder per session (first-only rule)", async () => {
    mockAnalyzeMove.mockImplementation(
      (_fen: string, move: string, _color: string, moveIndex: number) => {
        if (moveIndex !== 2) {
          return;
        }
        gameAnalysisStore.getState().resolveAnalysis(moveIndex, {
          id: "blunder-1",
          move,
          bestMove: "c2c4",
          bestEval: 50,
          playedEval: -150,
          currentPositionEval: -150,
          playedEvalMate: null,
          currentPositionEvalMate: null,
          moveIndex,
          delta: 200,
          classification: "blunder" as const,
          blunder: true,
          recordable: true,
        });
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.any(String),
        "g1f3",
        "white",
        2,
        expect.any(Number),
      );
    });

    await waitFor(() => {
      expect(recordBlunderMock).toHaveBeenCalledTimes(1);
    });

    // Simulate a second blunder (different analysis object to trigger useEffect)
    act(() => {
      gameAnalysisStore.getState().setLastAnalysis({
        id: "blunder-2",
        move: "g1f3",
        bestMove: "c2c4",
        bestEval: 100,
        playedEval: -200,
        currentPositionEval: -200,
        playedEvalMate: null,
        currentPositionEvalMate: null,
        moveIndex: 2,
        delta: 300,
        classification: "blunder" as const,
        blunder: true,
        recordable: true,
      });
    });

    // Wait for any effects
    await new Promise((r) => setTimeout(r, 50));

    // Should still be exactly 1 call - second blunder NOT recorded
    expect(recordBlunderMock).toHaveBeenCalledTimes(1);
  });

  it("does not call recordBlunder when move UCI does not match analysis", async () => {
    // Analysis is for a different move than what was played
    mockAnalyzeMove.mockImplementation(
      (_fen: string, _move: string, _color: string, moveIndex: number) => {
        gameAnalysisStore.getState().setLastAnalysis({
          id: "test-mismatch",
          move: "g1f3", // Analysis is for Nf3, not e4
          bestMove: "d2d4",
          bestEval: 50,
          playedEval: -150,
          currentPositionEval: -150,
          playedEvalMate: null,
          currentPositionEvalMate: null,
          moveIndex,
          delta: 200,
          classification: "blunder" as const,
          blunder: true,
          recordable: true,
        });
      },
    );

    await startGameAsWhite();

    // User plays e2e4, but analysis will claim it's for g1f3
    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalled();
    });

    await new Promise((r) => setTimeout(r, 50));

    expect(recordBlunderMock).not.toHaveBeenCalled();
  });

  it("does not call recordBlunder when no session is active", async () => {
    // Don't start a game - just render with no session
    render(<ChessGame />);

    // Set lastAnalysis after render so the store exists
    act(() => {
      gameAnalysisStore.getState().setLastAnalysis({
        id: "no-session",
        move: "e2e4",
        bestMove: "d2d4",
        bestEval: 50,
        playedEval: -150,
        currentPositionEval: -150,
        playedEvalMate: null,
        currentPositionEvalMate: null,
        moveIndex: null,
        delta: 200,
        classification: "blunder" as const,
        blunder: true,
        recordable: false,
      });
    });

    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(recordBlunderMock).not.toHaveBeenCalled();
  });

  // HTTP retry/terminal classification for recordBlunder now lives on the
  // coordinator-owned DecisionOwner — see DecisionOwner.test.ts (network
  // TypeError → awaiting_http_retry, non-retryable 4xx → terminal_error). The
  // old React-ref "does not retry" contract no longer applies here.

  it("adds selected player move to ghost library from MoveList", async () => {
    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    fireEvent.click(screen.getByRole("button", { name: /e4/i }));
    fireEvent.click(
      screen.getByRole("button", { name: /add selected move to ghost library/i }),
    );

    await waitFor(() => {
      expect(recordManualBlunderMock).toHaveBeenCalledWith(
        "session-blunder",
        expect.stringContaining("1. e4"),
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "e4",
        "e4",
        0,
        0,
      );
    });
  });

  it("handles duplicate add without rendering status line", async () => {
    recordManualBlunderMock.mockResolvedValueOnce({
      blunder_id: 2,
      position_id: 11,
      positions_created: 0,
      is_new: false,
    });
    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    // Select player move (opponent d5 is now auto-selected as last move)
    fireEvent.click(screen.getByRole("button", { name: /e4/i }));
    fireEvent.click(
      screen.getByRole("button", { name: /add selected move to ghost library/i }),
    );

    await waitFor(() => {
      expect(recordManualBlunderMock).toHaveBeenCalledTimes(1);
    });

    expect(screen.queryByText("Already in library.")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Added to ghost library."),
    ).not.toBeInTheDocument();
  });

  it("allows manual add after game has ended", async () => {
    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    fireEvent.click(screen.getByRole("button", { name: /resign/i }));
    await waitFor(() => {
      expect(screen.getByText("Are you sure?")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Resign"));

    await waitFor(() => {
      expect(screen.getByText("You resigned.")).toBeInTheDocument();
    });

    // Select player move (opponent d5 is auto-selected as last move)
    fireEvent.click(screen.getByRole("button", { name: /e4/i }));
    fireEvent.click(
      screen.getByRole("button", { name: /add selected move to ghost library/i }),
    );

    await waitFor(() => {
      expect(recordManualBlunderMock).toHaveBeenCalledTimes(1);
    });
  });

  it("disables add button when selected move is not a player move", async () => {
    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    fireEvent.click(screen.getByRole("button", { name: /d5/i }));

    expect(
      screen.getByRole("button", { name: /add selected move to ghost library/i }),
    ).toBeDisabled();
  });

  it("shows the end-game fanfare at game end, clears it on start-overlay open, and does not replay on cancel (g-8079)", async () => {
    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    // Resign to reach a genuine end — the single choke point fires the fanfare.
    fireEvent.click(screen.getByRole("button", { name: /resign/i }));
    await waitFor(() => {
      expect(screen.getByText("Are you sure?")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Resign"));
    await waitFor(() => {
      expect(screen.getByText("You resigned.")).toBeInTheDocument();
    });

    // Fanfare shows centered over the board, naming the loss + termination type.
    const fanfare = document.querySelector(".end-game-fanfare");
    expect(fanfare).not.toBeNull();
    expect(fanfare).toHaveClass("end-game-fanfare--loss");
    expect(
      fanfare?.querySelector(".end-game-fanfare__headline")?.textContent,
    ).toBe("Defeat");
    expect(
      fanfare?.querySelector(".end-game-fanfare__reason")?.textContent,
    ).toBe("Resignation");

    // Opening the post-game start overlay ends the terminal display state
    // (showEndedScrim → false); the parent clear effect drops the nonce.
    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    await waitFor(() => {
      expect(document.querySelector(".end-game-fanfare")).toBeNull();
    });

    // Regression: cancelling the overlay restores the ended state
    // (showEndedScrim false→true) but must NOT replay the stale fanfare.
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    await waitFor(() => {
      expect(screen.getByText("You resigned.")).toBeInTheDocument();
    });
    expect(document.querySelector(".end-game-fanfare")).toBeNull();
  });

  it("shows re-hook notification when opponent mode switches from engine to ghost", async () => {
    getNextOpponentMoveMock
      .mockResolvedValueOnce({
        mode: "engine",
        move: { uci: "d7d5", san: "d5" },
        target_blunder_id: null,
        decision_source: "backend_engine",
      })
      .mockResolvedValueOnce({
        mode: "ghost",
        move: { uci: "e7e5", san: "e5" },
        target_blunder_id: 42,
        target_fen: "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2",
        decision_source: "ghost_path",
      });

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    expect(
      screen.queryByText("The haunting resumes"),
    ).not.toBeInTheDocument();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      const notice = document.querySelector(".board-notice--rehook");
      expect(notice?.textContent).toContain("The haunting resumes");
    });

    expect(
      screen.getByRole("button", { name: /toggle ghost info/i }),
    ).toBeInTheDocument();
  });

  it("records SRS pass for review target when eval delta is below 50cp", async () => {
    getNextOpponentMoveMock
      .mockResolvedValueOnce({
        mode: "ghost",
        move: { uci: "e7e5", san: "e5" },
        target_blunder_id: 42,
        target_fen: "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2",
        decision_source: "ghost_path",
      })
      .mockResolvedValue({
        mode: "engine",
        move: { uci: "b8c6", san: "Nc6" },
        target_blunder_id: null,
        decision_source: "backend_engine",
      });

    mockAnalyzeMove.mockImplementation(
      (_fen: string, move: string, _color: string, moveIndex: number) => {
        if (moveIndex === 2) {
          gameAnalysisStore.getState().resolveAnalysis(moveIndex, {
            id: "review-pass",
            move,
            bestMove: "g1f3",
            bestEval: 40,
            playedEval: 20,
            currentPositionEval: 20,
            playedEvalMate: null,
            currentPositionEvalMate: null,
            moveIndex: 2,
            delta: 20,
            classification: "good" as const,
            blunder: false,
            recordable: false,
          });
          return "review-pass";
        }
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.any(String),
        "e7e5",
        "black",
        1,
        expect.any(Number),
      );
    });
    await waitFor(() => {
      expect(document.querySelector(".board-notice--review-warning")).toBeInTheDocument();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      expect(reviewSrsBlunderMock).toHaveBeenCalledWith(
        "session-blunder",
        42,
        true,
        "Nf3",
        20,
        expect.any(String),
      );
    });
    await waitFor(() => {
      expect(
        screen.getByText("Correct! You avoided your past mistake."),
      ).toBeInTheDocument();
    });
  });

  it("records SRS fail for review target when eval delta is 50cp or higher", async () => {
    getNextOpponentMoveMock
      .mockResolvedValueOnce({
        mode: "ghost",
        move: { uci: "e7e5", san: "e5" },
        target_blunder_id: 99,
        target_fen: "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2",
        decision_source: "ghost_path",
      })
      .mockResolvedValue({
        mode: "engine",
        move: { uci: "b8c6", san: "Nc6" },
        target_blunder_id: null,
        decision_source: "backend_engine",
      });

    mockAnalyzeMove.mockImplementation(
      (_fen: string, move: string, _color: string, moveIndex: number) => {
        if (moveIndex === 2) {
          gameAnalysisStore.getState().resolveAnalysis(moveIndex, {
            id: "review-fail",
            move,
            bestMove: "g1f3",
            bestEval: 40,
            playedEval: -10,
            currentPositionEval: -10,
            playedEvalMate: null,
            currentPositionEvalMate: null,
            moveIndex: 2,
            delta: 50,
            classification: "good" as const,
            blunder: false,
            recordable: false,
          });
          return "review-fail";
        }
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    await waitFor(() => {
      expect(document.querySelector(".board-notice--review-warning")).toBeInTheDocument();
    });
    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      expect(reviewSrsBlunderMock).toHaveBeenCalledWith(
        "session-blunder",
        99,
        false,
        "Nf3",
        50,
        expect.any(String),
      );
    });
    expect(
      screen.queryByText("You avoided your past mistake."),
    ).not.toBeInTheDocument();
  });

  it("spotlights a repeat mistake, auto-reveals arrows, then closes leaving the inline fail detail", async () => {
    getNextOpponentMoveMock
      .mockResolvedValueOnce({
        mode: "ghost",
        move: { uci: "e7e5", san: "e5" },
        target_blunder_id: 99,
        target_fen: "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2",
        decision_source: "ghost_path",
      })
      .mockResolvedValue({
        mode: "engine",
        move: { uci: "b8c6", san: "Nc6" },
        target_blunder_id: null,
        decision_source: "backend_engine",
      });

    mockAnalyzeMove.mockImplementation(
      (_fen: string, move: string, _color: string, moveIndex: number) => {
        if (moveIndex === 2) {
          gameAnalysisStore.getState().resolveAnalysis(moveIndex, {
            id: "review-fail-spotlight",
            move,
            bestMove: "g1f3",
            bestEval: 40,
            playedEval: -10,
            currentPositionEval: -10,
            playedEvalMate: null,
            currentPositionEvalMate: null,
            moveIndex: 2,
            delta: 50,
            classification: "good" as const,
            blunder: false,
            recordable: false,
          });
          return "review-fail-spotlight";
        }
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    await waitFor(() => {
      expect(document.querySelector(".board-notice--review-warning")).toBeInTheDocument();
    });
    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    // Spotlight scrim + headline appear, board arrows are auto-revealed, and the
    // inline fail bubble + revealed detail are present.
    await waitFor(() => {
      expect(document.querySelector(".srs-fail-scrim")).toBeInTheDocument();
    });
    expect(
      document.querySelector(".srs-fail-content__headline")?.textContent,
    ).toBe("You made this blunder again!");
    const inlineBubble = document.querySelector(".move-bubble--srs-fail");
    expect(inlineBubble?.textContent).toContain("You made this blunder again!");
    expect(
      screen.getByTestId("chessboard").getAttribute("data-arrow-count"),
    ).toBe("2");

    // Clicking the dimmed scrim skips to the shrink phase; the spotlight then
    // unmounts while the inline fail detail + arrows persist.
    await act(async () => {
      fireEvent.click(document.querySelector(".srs-fail-scrim") as HTMLElement);
    });
    await waitFor(() => {
      expect(document.querySelector(".srs-fail-scrim")).not.toBeInTheDocument();
    });
    const persistedBubble = document.querySelector(".move-bubble--srs-fail");
    expect(persistedBubble?.textContent).toContain(
      "You made this blunder again!",
    );
    expect(
      screen.getByTestId("chessboard").getAttribute("data-arrow-count"),
    ).toBe("2");
  });

  it("shows SRS pass toast even if review submission fails", async () => {
    getNextOpponentMoveMock
      .mockResolvedValueOnce({
        mode: "ghost",
        move: { uci: "e7e5", san: "e5" },
        target_blunder_id: 77,
        target_fen: "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2",
        decision_source: "ghost_path",
      })
      .mockResolvedValue({
        mode: "engine",
        move: { uci: "b8c6", san: "Nc6" },
        target_blunder_id: null,
        decision_source: "backend_engine",
      });

    mockAnalyzeMove.mockImplementation(
      (_fen: string, move: string, _color: string, moveIndex: number) => {
        if (moveIndex === 2) {
          gameAnalysisStore.getState().resolveAnalysis(moveIndex, {
            id: "review-pass-api-error",
            move,
            bestMove: "g1f3",
            bestEval: 40,
            playedEval: 20,
            currentPositionEval: 20,
            playedEvalMate: null,
            currentPositionEvalMate: null,
            moveIndex: 2,
            delta: 20,
            classification: "good" as const,
            blunder: false,
            recordable: false,
          });
          return "review-pass-api-error";
        }
      },
    );

    reviewSrsBlunderMock.mockRejectedValueOnce(
      new Error("Failed to record SRS review"),
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    await waitFor(() => {
      expect(document.querySelector(".board-notice--review-warning")).toBeInTheDocument();
    });
    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      expect(reviewSrsBlunderMock).toHaveBeenCalledWith(
        "session-blunder",
        77,
        true,
        "Nf3",
        20,
        expect.any(String),
      );
    });
    await waitFor(() => {
      expect(
        screen.getByText("Correct! You avoided your past mistake."),
      ).toBeInTheDocument();
    });
  });

  it("shows pass overlay on resolved review toast after analysis returns", async () => {
    getNextOpponentMoveMock
      .mockResolvedValueOnce({
        mode: "ghost",
        move: { uci: "e7e5", san: "e5" },
        target_blunder_id: 42,
        target_fen: "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2",
        decision_source: "ghost_path",
      })
      .mockResolvedValue({
        mode: "engine",
        move: { uci: "b8c6", san: "Nc6" },
        target_blunder_id: null,
        decision_source: "backend_engine",
      });

    mockAnalyzeMove.mockImplementation(
      (_fen: string, move: string, _color: string, moveIndex: number) => {
        if (moveIndex === 2) {
          gameAnalysisStore.getState().resolveAnalysis(moveIndex, {
            id: "review-pass-overlay",
            move,
            bestMove: "g1f3",
            bestEval: 40,
            playedEval: 20,
            currentPositionEval: 20,
            playedEvalMate: null,
            currentPositionEvalMate: null,
            moveIndex: 2,
            delta: 20,
            classification: "good" as const,
            blunder: false,
            recordable: false,
          });
          return "review-pass-overlay";
        }
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    await waitFor(() => {
      expect(document.querySelector(".board-notice--review-warning")).toBeInTheDocument();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      const notice = document.querySelector(".board-notice--pass");
      expect(notice).toBeInTheDocument();
      expect(notice?.querySelector(".board-notice__result-icon")?.textContent).toBe("✓");
    });
  });

  it("shows fail overlay on resolved review toast after analysis returns", async () => {
    getNextOpponentMoveMock
      .mockResolvedValueOnce({
        mode: "ghost",
        move: { uci: "e7e5", san: "e5" },
        target_blunder_id: 99,
        target_fen: "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2",
        decision_source: "ghost_path",
      })
      .mockResolvedValue({
        mode: "engine",
        move: { uci: "b8c6", san: "Nc6" },
        target_blunder_id: null,
        decision_source: "backend_engine",
      });

    mockAnalyzeMove.mockImplementation(
      (_fen: string, move: string, _color: string, moveIndex: number) => {
        if (moveIndex === 2) {
          gameAnalysisStore.getState().resolveAnalysis(moveIndex, {
            id: "review-fail-overlay",
            move,
            bestMove: "g1f3",
            bestEval: 40,
            playedEval: -10,
            currentPositionEval: -10,
            playedEvalMate: null,
            currentPositionEvalMate: null,
            moveIndex: 2,
            delta: 50,
            classification: "good" as const,
            blunder: false,
            recordable: false,
          });
          return "review-fail-overlay";
        }
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    await waitFor(() => {
      expect(document.querySelector(".board-notice--review-warning")).toBeInTheDocument();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      const notice = document.querySelector(".board-notice--fail");
      expect(notice).toBeInTheDocument();
      expect(notice?.querySelector(".board-notice__result-icon")?.textContent).toBe("✗");
    });
  });

  it("auto-dismisses the resolved review result after its short window", async () => {
    getNextOpponentMoveMock
      .mockResolvedValueOnce({
        mode: "ghost",
        move: { uci: "e7e5", san: "e5" },
        target_blunder_id: 42,
        target_fen: "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2",
        decision_source: "ghost_path",
      })
      .mockResolvedValue({
        mode: "engine",
        move: { uci: "b8c6", san: "Nc6" },
        target_blunder_id: null,
        decision_source: "backend_engine",
      });

    mockAnalyzeMove.mockImplementation(
      (_fen: string, move: string, _color: string, moveIndex: number) => {
        if (moveIndex === 2) {
          gameAnalysisStore.getState().resolveAnalysis(moveIndex, {
            id: "review-pass-clear",
            move,
            bestMove: "g1f3",
            bestEval: 40,
            playedEval: 20,
            currentPositionEval: 20,
            playedEvalMate: null,
            currentPositionEvalMate: null,
            moveIndex: 2,
            delta: 20,
            classification: "good" as const,
            blunder: false,
            recordable: false,
          });
          return "review-pass-clear";
        }
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    await waitFor(() => {
      expect(document.querySelector(".board-notice--review-warning")).toBeInTheDocument();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      expect(document.querySelector(".board-notice--pass")).toBeInTheDocument();
    });

    // The result box gets out of the way on its own short (2s) timer — no
    // further move is required.
    await waitFor(
      () => {
        expect(
          document.querySelector(".board-notice--pass"),
        ).not.toBeInTheDocument();
      },
      { timeout: 3000 },
    );
  });
});

describe("ChessGame move analysis", () => {
  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    startGameMock.mockReset();
    endGameMock.mockReset();
    uploadSessionMovesMock.mockReset();
    getNextOpponentMoveMock.mockReset();
    recordBlunderMock.mockReset();
    recordManualBlunderMock.mockReset();
    reviewSrsBlunderMock.mockReset();
    mockAnalyzeMove.mockReset();
    evaluatePositionMock.mockReset();
    lookupOpeningByFenMock.mockReset();
    gameAnalysisStore.getState().clearAll();
    capturedPieceDrop = null;

    getNextOpponentMoveMock.mockResolvedValue({
      mode: "engine",
      move: { uci: "d7d5", san: "d5" },
      target_blunder_id: null,
      decision_source: "backend_engine",
    });
    lookupOpeningByFenMock.mockResolvedValue(null);
    reviewSrsBlunderMock.mockResolvedValue({
      blunder_id: 1,
      pass_streak: 1,
      priority: 0,
      next_expected_review: "2026-02-08T00:00:00Z",
    });
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  const startGameAsWhite = async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-analysis",
      engine_elo: 1500,
      player_color: "white",
    });

    render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(startGameMock).toHaveBeenCalled();
    });
  };

  it("calls analyzeMove for both player and engine moves", async () => {
    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    // Player move analyzed with player color and index 0
    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.stringContaining("rnbqkbnr"),
        "e2e4",
        "white",
        0,
        expect.any(Number),
      );
    });

    // Engine responds with d7d5 — analyzed with opponent color and index 1
    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.any(String),
        "d7d5",
        "black",
        1,
        expect.any(Number),
      );
    });
  });

  it("calls analyzeMove for ghost moves with opponent color", async () => {
    // Ghost returns a move instead of engine
    getNextOpponentMoveMock.mockResolvedValue({
      mode: "ghost",
      move: { uci: "e7e5", san: "e5" },
      target_blunder_id: null,
      decision_source: "ghost_path",
    });

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    // Player move
    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.any(String),
        "e2e4",
        "white",
        0,
        expect.any(Number),
      );
    });

    // Ghost move analyzed with opponent color
    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.any(String),
        "e7e5",
        "black",
        1,
        expect.any(Number),
      );
    });
  });

  it("flushes coordinator uploads and calls endGame on resign", async () => {
    endGameMock.mockResolvedValue({
      session_id: "session-analysis",
      blunders_recorded: 0,
      blunders_reviewed: 0,
    });

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.any(String),
        "d7d5",
        "black",
        1,
        expect.any(Number),
      );
    });

    fireEvent.click(screen.getByRole("button", { name: /resign/i }));
    await waitFor(() => {
      expect(screen.getByText("Are you sure?")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Resign"));

    // Resign now awaits a full move-history upload before endGame so the
    // opening-score delta sees the complete chain (g-xanz).
    await waitFor(() => {
      expect(uploadSessionMovesMock).toHaveBeenCalled();
    });

    await waitFor(() => {
      expect(endGameMock).toHaveBeenCalledWith(
        "session-analysis",
        "resign",
        expect.any(String),
        expect.any(Boolean),
      );
    });
  });
});

describe("ChessGame opening lineage", () => {
  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    startGameMock.mockReset();
    uploadSessionMovesMock.mockReset();
    getNextOpponentMoveMock.mockReset();
    evaluatePositionMock.mockReset();
    gameAnalysisStore.getState().clearAll();
    capturedPieceDrop = null;
    abandonDrillMock.mockReset();
    startDrillMock.mockReset();
    getOpeningRootsMock.mockReset();
    getOpeningRootsMock.mockResolvedValue({ families: [] });
    __resetOpeningRootIndexCache();

    getNextOpponentMoveMock.mockResolvedValue({
      mode: "engine",
      move: { uci: "e7e5", san: "e5" },
      target_blunder_id: null,
      decision_source: "backend_engine",
    });
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 });
  });

  it("renders the live opening lineage while a game is active", async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-lineage",
      engine_elo: 1500,
      player_color: "white",
    });
    fetchSessionOpeningsMock.mockResolvedValue({
      player_color: "white",
      lineage: [
        {
          opening_key: "k1",
          opening_name: "King's Pawn Game",
          opening_family: "King's Pawn",
          eco: "C20",
          depth: 0,
          score: 60,
          confidence: 0.5,
          coverage: 0.5,
          sample_size: 5,
          game_count: 2,
          path: [],
          moves: ["e4"],
        },
      ],
      start_ply: 1,
    });

    render(<ChessGame />);

    // No lineage before a game starts; the legacy "Opening:" line is gone.
    expect(
      screen.queryByRole("region", { name: "Openings played" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/^Opening:/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(
        screen.getByRole("region", { name: "Openings played" }),
      ).toBeInTheDocument();
    });
    const region = screen.getByRole("region", { name: "Openings played" });
    expect(region).toHaveTextContent("King's Pawn Game");

    // Resetting (game no longer active) hides the lineage.
    fireEvent.click(screen.getByRole("button", { name: /reset/i }));
    expect(
      screen.queryByRole("region", { name: "Openings played" }),
    ).not.toBeInTheDocument();
  });

  it("keeps the lineage visible after the game ends", async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-postgame-lineage",
      engine_elo: 1500,
      player_color: "white",
    });
    endGameMock.mockResolvedValue({});
    fetchSessionOpeningsMock.mockResolvedValue({
      player_color: "white",
      lineage: [
        {
          opening_key: "k1",
          opening_name: "King's Pawn Game",
          opening_family: "King's Pawn",
          eco: "C20",
          depth: 0,
          score: 60,
          confidence: 0.5,
          coverage: 0.5,
          sample_size: 5,
          game_count: 2,
          path: [],
          moves: ["e4"],
        },
      ],
      start_ply: 1,
    });

    render(<ChessGame />);
    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(
        screen.getByRole("region", { name: "Openings played" }),
      ).toBeInTheDocument();
    });

    // Resign to end the game (gameResult set, isGameActive false) without reset.
    fireEvent.click(screen.getByRole("button", { name: /resign/i }));
    await waitFor(() => {
      expect(screen.getByText("Are you sure?")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Resign"));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /view analysis/i }),
      ).toBeInTheDocument();
    });

    // The lineage persists post-game (gated on gameResult, not just active).
    expect(
      screen.getByRole("region", { name: "Openings played" }),
    ).toBeInTheDocument();
  });

  // --- Post-game lineage actions on /play (g-d65n, history parity) ----------

  const LINEAGE_FEN_E4 =
    "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1";
  const LINEAGE_FEN_E5 =
    "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2";

  /** A lineage card whose crossing move index is `moves.length - 1`. */
  function lineageCard(
    openingKey: string,
    openingName: string,
    moves: string[],
    depth = 0,
  ) {
    return {
      opening_key: openingKey,
      opening_name: openingName,
      opening_family: openingName,
      eco: "C20",
      depth,
      score: 60,
      confidence: 0.5,
      coverage: 0.5,
      sample_size: 5,
      game_count: 2,
      path: [],
      moves,
    };
  }

  function lineageResponse(openingKey: string) {
    return {
      player_color: "white",
      lineage: [lineageCard(openingKey, "King's Pawn Game", ["e4"])],
      start_ply: 1,
    };
  }

  it("hydrates a locally visible card after the current session upload commits", async () => {
    const openingKey = LINEAGE_FEN_E4.split(" ").slice(0, 4).join(" ");
    getOpeningRootsMock.mockResolvedValue({
      families: [
        {
          family_name: "King's Pawn",
          roots: [
            {
              opening_key: openingKey,
              opening_name: "King's Pawn Game",
              opening_family: "King's Pawn",
              eco: "C20",
              depth: 0,
            },
          ],
        },
      ],
    });
    __resetOpeningRootIndexCache();
    fetchSessionOpeningsMock
      .mockResolvedValueOnce({
        player_color: "white",
        lineage: [],
        start_ply: 1,
        score_status: "ready",
      })
      .mockResolvedValueOnce({
        player_color: "white",
        lineage: [
          {
            ...lineageCard(openingKey, "King's Pawn Game", ["e4"]),
            score: 73,
          },
        ],
        start_ply: 1,
        score_status: "ready",
      });
    useGameStore.setState({
      sessionId: "session-upload-commit",
      isGameActive: true,
      playerColor: "white",
      boardOrientation: "white",
      moveHistory: [{ san: "e4", fen: LINEAGE_FEN_E4, uci: "e2e4" }],
      liveFen: LINEAGE_FEN_E4,
    });

    render(<ChessGame />);
    const region = await screen.findByRole("region", {
      name: "Openings played",
    });
    await waitFor(() =>
      expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(1),
    );
    expect(within(region).queryByText("73")).not.toBeInTheDocument();
    expect(within(region).getByText(/score loading/i)).toBeInTheDocument();
    expect(useGameStore.getState().moveHistory).toHaveLength(1);

    act(() => emitUploadCommit("session-upload-commit"));

    await waitFor(() =>
      expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(2),
    );
    expect(await within(region).findByText("73")).toBeInTheDocument();
    expect(within(region).queryByText(/score loading/i)).not.toBeInTheDocument();
    expect(useGameStore.getState().moveHistory).toHaveLength(1);

    // A notification that does not belong to the source's current session is
    // observable by the hook but leaves its snapshot (and fetch key) unchanged.
    act(() => emitUploadCommit("session-old"));
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(2);
  });

  it("during active play, selecting a card reviews the opening position but offers no Start Drill", async () => {
    fetchSessionOpeningsMock.mockResolvedValue(lineageResponse(LINEAGE_FEN_E4));
    useGameStore.setState({
      sessionId: "session-active-lineage",
      isGameActive: true,
      playerColor: "white",
      boardOrientation: "white",
      moveHistory: [
        { san: "e4", fen: LINEAGE_FEN_E4, uci: "e2e4" },
        { san: "e5", fen: LINEAGE_FEN_E5, uci: "e7e5" },
      ],
      liveFen: LINEAGE_FEN_E5,
    });
    render(<ChessGame />);

    // The board starts at the live position (after 1...e5).
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      LINEAGE_FEN_E5,
    );

    // The board sits on 1...e5, past this lineage's only crossing (1.e4), so no
    // card is expanded (g-m1xc) — the game has left the opening.
    const select = await screen.findByRole("button", {
      name: /Select King's Pawn Game/,
    });
    expect(
      screen.queryByRole("button", { name: /^Collapse .* details$/ }),
    ).not.toBeInTheDocument();
    // Start Drill is not offered during a regular rated live game.
    expect(
      screen.queryByRole("button", { name: /start drill/i }),
    ).not.toBeInTheDocument();

    // Board navigation is wired during play (history parity), so selecting the
    // card reviews the opening's past position.
    fireEvent.click(select);

    // Expanding a regular live-game card still does not expose the destructive
    // replacement action; the absence above was not merely compact-card UI.
    await screen.findByRole("button", {
      name: /Collapse King's Pawn Game details/,
    });
    expect(
      screen.queryByRole("button", { name: /start drill/i }),
    ).not.toBeInTheDocument();

    // Selecting reviews the opening's past position without disturbing the live
    // game (the store's live position is untouched — only viewIndex moves).
    await waitFor(() =>
      expect(screen.getByTestId("chessboard")).toHaveAttribute(
        "data-position",
        LINEAGE_FEN_E4,
      ),
    );
    expect(useGameStore.getState().liveFen).toBe(LINEAGE_FEN_E5);
  });

  it("replaces an active drill from a lineage card", async () => {
    const targetOpeningKey = LINEAGE_FEN_E4.split(" ").slice(0, 4).join(" ");
    getOpeningRootsMock.mockReset();
    getOpeningRootsMock.mockResolvedValue({
      families: [
        {
          family_name: "King's Pawn",
          roots: [
            {
              opening_key: targetOpeningKey,
              opening_name: "King's Pawn Game",
              opening_family: "King's Pawn",
              eco: "C20",
              depth: 0,
            },
          ],
        },
      ],
    });
    __resetOpeningRootIndexCache();
    fetchSessionOpeningsMock.mockResolvedValue(
      lineageResponse(targetOpeningKey),
    );
    abandonDrillMock.mockReset();
    abandonDrillMock.mockResolvedValue({ drill_state: "abandoned" });
    startDrillMock.mockReset();
    startDrillMock.mockResolvedValue({
      session_id: "session-replacement-drill",
      drill_state: "active",
      opening_name: "King's Pawn Game",
      strictness_cp: 25,
    });
    useGameStore.setState({
      sessionId: "session-active-drill",
      isGameActive: true,
      isRated: false,
      playerColor: "white",
      boardOrientation: "white",
      moveHistory: [{ san: "e4", fen: LINEAGE_FEN_E4, uci: "e2e4" }],
      liveFen: LINEAGE_FEN_E4,
      drillOpeningKey: "old-target",
      drillOpeningName: "Old target",
      drillState: "active",
      drillStrictness: "standard",
      drillStrictnessCp: 25,
    });

    render(<ChessGame />);

    // The active drill sits on this card's crossing, so it is expanded and the
    // unrated-session replacement action is available.
    await screen.findByRole("button", {
      name: /Collapse King's Pawn Game details/,
    });
    fireEvent.click(
      await screen.findByRole("button", { name: /start drill/i }),
    );

    const close = await screen.findByRole("button", { name: /close/i });
    const overlay = close.closest<HTMLElement>(".chessboard-overlay");
    expect(overlay).not.toBeNull();
    await waitFor(() =>
      expect(within(overlay!).getByRole("combobox")).toHaveTextContent(
        "King's Pawn Game",
      ),
    );

    // Opening the panel is non-destructive. The old drill is abandoned only
    // after the player selects the required tier and submits the replacement.
    expect(abandonDrillMock).not.toHaveBeenCalled();
    fireEvent.click(
      within(overlay!).getByRole("button", { name: /^standard$/i }),
    );
    await act(async () => {
      fireEvent.click(
        within(overlay!).getByRole("button", { name: /^start drill$/i }),
      );
    });

    await waitFor(() =>
      expect(abandonDrillMock).toHaveBeenCalledWith("session-active-drill"),
    );
    expect(startDrillMock).toHaveBeenCalledWith(
      expect.objectContaining({
        opening_key: targetOpeningKey,
        player_color: "white",
        strictness: "standard",
        strictness_cp: 25,
      }),
    );
    expect(abandonDrillMock.mock.invocationCallOrder[0]).toBeLessThan(
      startDrillMock.mock.invocationCallOrder[0],
    );
    expect(useGameStore.getState()).toEqual(
      expect.objectContaining({
        sessionId: "session-replacement-drill",
        drillOpeningKey: targetOpeningKey,
        drillState: "active",
        isGameActive: true,
        isRated: false,
      }),
    );
  });

  it("blocks a retained same-mount ad-hoc selection until the lineage target resolves", async () => {
    const targetOpeningKey = LINEAGE_FEN_E4.split(" ").slice(0, 4).join(" ");
    const rootsResponse = {
      families: [
        {
          family_name: "King's Pawn",
          roots: [
            {
              opening_key: targetOpeningKey,
              opening_name: "King's Pawn Game",
              opening_family: "King's Pawn",
              eco: "C20",
              depth: 0,
            },
          ],
        },
      ],
    };
    getOpeningRootsMock.mockReset();
    getOpeningRootsMock.mockResolvedValue(rootsResponse);
    __resetOpeningRootIndexCache();
    fetchSessionOpeningsMock.mockResolvedValue(
      lineageResponse(targetOpeningKey),
    );
    mockLocation = {
      state: {
        drillSetup: {
          targetFen: "stale-ad-hoc-target",
          line: ["d2d4", "d7d5"],
          displayName: "Stale custom line",
          eco: null,
          playerColor: "white",
        },
      },
      pathname: "/play",
    };
    startDrillMock
      .mockResolvedValueOnce({
        session_id: "session-mounted-drill",
        drill_state: "active",
        opening_name: "Stale custom line",
        strictness_cp: 25,
      })
      .mockResolvedValueOnce({
        session_id: "session-lineage-replacement",
        drill_state: "active",
        opening_name: "King's Pawn Game",
        strictness_cp: 25,
      });
    abandonDrillMock.mockResolvedValue({ drill_state: "abandoned" });

    render(<ChessGame />);

    // Start an ad-hoc drill so this mounted ChessGame genuinely retains both
    // its synthetic opening selection and line after the overlay unmounts.
    await waitFor(() =>
      expect(screen.getByRole("combobox")).toHaveTextContent(
        "Stale custom line",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: /^standard$/i }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^start drill$/i }));
    });
    await waitFor(() =>
      expect(useGameStore.getState().sessionId).toBe("session-mounted-drill"),
    );
    expect(startDrillMock).toHaveBeenCalledTimes(1);
    expect(startDrillMock).toHaveBeenLastCalledWith(
      expect.objectContaining({
        opening_key: "stale-ad-hoc-target",
        line: ["d2d4", "d7d5"],
      }),
    );

    // Give that same live drill a lineage crossing, still without remounting.
    act(() => {
      useGameStore.setState({
        moveHistory: [{ san: "e4", fen: LINEAGE_FEN_E4, uci: "e2e4" }],
        liveFen: LINEAGE_FEN_E4,
      });
    });
    await screen.findByRole("button", {
      name: /Collapse King's Pawn Game details/,
    });

    let resolveReplacementRoots!: (value: typeof rootsResponse) => void;
    getOpeningRootsMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveReplacementRoots = resolve;
        }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: /start drill/i }),
    );

    const close = await screen.findByRole("button", { name: /close/i });
    const overlay = close.closest<HTMLElement>(".chessboard-overlay");
    expect(overlay).not.toBeNull();
    const setup = within(overlay!);
    fireEvent.click(setup.getByRole("button", { name: /^standard$/i }));

    // The old synthetic selection/line is gone before roots resolve. Choosing
    // strictness cannot enable or submit the stale ad-hoc draft during the gap.
    const start = setup.getByRole("button", { name: /^start drill$/i });
    expect(start).toBeDisabled();
    fireEvent.click(start);
    expect(startDrillMock).toHaveBeenCalledTimes(1);
    expect(abandonDrillMock).not.toHaveBeenCalled();

    await act(async () => {
      resolveReplacementRoots({
        families: rootsResponse.families.map((family) => ({
          ...family,
          roots: [...family.roots],
        })),
      });
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(setup.getByRole("combobox")).toHaveTextContent(
        "King's Pawn Game",
      ),
    );
    expect(start).toBeEnabled();
    await act(async () => {
      fireEvent.click(start);
    });

    await waitFor(() => expect(startDrillMock).toHaveBeenCalledTimes(2));
    expect(abandonDrillMock).toHaveBeenCalledWith("session-mounted-drill");
    expect(startDrillMock).toHaveBeenLastCalledWith(
      expect.objectContaining({
        opening_key: targetOpeningKey,
        line: undefined,
      }),
    );
  });

  it("post-game lineage offers Start Drill that opens the drill setup (route-state intercept)", async () => {
    getOpeningRootsMock.mockResolvedValue({ families: [] });
    fetchSessionOpeningsMock.mockResolvedValue(lineageResponse("k1"));
    useGameStore.setState({
      sessionId: "session-postgame-drill",
      isGameActive: false,
      playerColor: "white",
      boardOrientation: "white",
      moveHistory: [{ san: "e4", fen: LINEAGE_FEN_E4, uci: "e2e4" }],
      gameResult: { type: "resign", message: "Resigned." },
      liveFen: LINEAGE_FEN_E4,
    });
    render(<ChessGame />);

    // The board sits on the crossing move, so the card is already expanded
    // (g-m1xc) and offers Start Drill (onStartDrill wired once gameResult !== null).
    await screen.findByRole("button", {
      name: /Collapse King's Pawn Game details/,
    });

    const drill = await screen.findByRole("button", { name: /start drill/i });
    // Opening the drill setup fetches the opening roots (overlay opened in drill
    // mode) — mirroring the /openings route-state intercept flow, not a direct
    // openingFamilies resolution (which is null post-game until the overlay).
    // Clear first: this describe doesn't reset the mock between tests, so assert
    // the Start Drill CLICK specifically is what triggers the roots fetch.
    getOpeningRootsMock.mockClear();
    fireEvent.click(drill);
    await waitFor(() => expect(getOpeningRootsMock).toHaveBeenCalled());
  });

  it("post-game lineage select jumps the board to the opening's position", async () => {
    // opening_key matches the first move's FEN, so selecting jumps to move 0.
    fetchSessionOpeningsMock.mockResolvedValue(lineageResponse(LINEAGE_FEN_E4));
    useGameStore.setState({
      sessionId: "session-postgame-nav",
      isGameActive: false,
      playerColor: "white",
      boardOrientation: "white",
      moveHistory: [
        { san: "e4", fen: LINEAGE_FEN_E4, uci: "e2e4" },
        { san: "e5", fen: LINEAGE_FEN_E5, uci: "e7e5" },
      ],
      gameResult: { type: "resign", message: "Resigned." },
      liveFen: LINEAGE_FEN_E5,
    });
    render(<ChessGame />);

    // The board starts at the live/final position (after 1...e5).
    const board = screen.getByTestId("chessboard");
    expect(board).toHaveAttribute("data-position", LINEAGE_FEN_E5);

    // The final position is past the crossing, so the card is compact (g-m1xc).
    fireEvent.click(
      await screen.findByRole("button", {
        name: /Select King's Pawn Game/,
      }),
    );

    // Selecting the card jumps the board back to the King's Pawn position (move 0).
    await waitFor(() =>
      expect(screen.getByTestId("chessboard")).toHaveAttribute(
        "data-position",
        LINEAGE_FEN_E4,
      ),
    );
  });

  it("keeps the expanded card in sync with the displayed move (g-m1xc)", async () => {
    // Two crossings: King's Pawn at move 0, Petrov at move 3.
    const FEN_NF3 =
      "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2";
    const FEN_NF6 =
      "rnbqkb1r/pppp1ppp/5n2/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3";
    getOpeningRootsMock.mockResolvedValue({ families: [] });
    __resetOpeningRootIndexCache();
    fetchSessionOpeningsMock.mockResolvedValue({
      player_color: "white",
      lineage: [
        lineageCard("k1", "King's Pawn Game", ["e4"]),
        lineageCard("k2", "Petrov's Defense", ["e4", "e5", "Nf3", "Nf6"], 1),
      ],
      start_ply: 1,
    });
    useGameStore.setState({
      sessionId: "session-lineage-sync",
      isGameActive: false,
      playerColor: "white",
      boardOrientation: "white",
      moveHistory: [
        { san: "e4", fen: LINEAGE_FEN_E4, uci: "e2e4" },
        { san: "e5", fen: LINEAGE_FEN_E5, uci: "e7e5" },
        { san: "Nf3", fen: FEN_NF3, uci: "g1f3" },
        { san: "Nf6", fen: FEN_NF6, uci: "g8f6" },
      ],
      gameResult: { type: "resign", message: "Resigned." },
      liveFen: FEN_NF6,
    });
    render(<ChessGame />);

    // Latest move (index 3) → the deepest crossing reached is expanded.
    await screen.findByRole("button", {
      name: /Collapse Petrov's Defense details/,
    });
    expect(
      screen.getByRole("button", { name: /Select King's Pawn Game/ }),
    ).toBeInTheDocument();

    // Rewinding to a move before the Petrov crossing falls back to the opening
    // that was most recently crossed at that point.
    act(() => {
      useGameStore.getState().setViewIndex(1);
    });
    expect(
      screen.getByRole("button", { name: /Collapse King's Pawn Game details/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Select Petrov's Defense/ }),
    ).toBeInTheDocument();

    // Before the first crossing (the starting position) nothing is expanded.
    act(() => {
      useGameStore.getState().setViewIndex(-1);
    });
    expect(
      screen.queryByRole("button", { name: /^Collapse .* details$/ }),
    ).not.toBeInTheDocument();

    // Returning to the live/latest position restores the deepest opening.
    act(() => {
      useGameStore.getState().setViewIndex(null);
    });
    expect(
      screen.getByRole("button", { name: /Collapse Petrov's Defense details/ }),
    ).toBeInTheDocument();
  });

  it("refetches the lineage at terminal and shows the inline score-diff badge after resign (g-3gmc)", async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-postgame-delta",
      engine_elo: 1500,
      player_color: "white",
    });
    // The opening-score delta lands at the terminal endGame call (g-xanz)...
    endGameMock.mockResolvedValue({
      opening_score_changes: [
        {
          opening_key: "k1",
          opening_name: "King's Pawn Game",
          opening_family: "King's Pawn",
          eco: "C20",
          depth: 0,
          before: 41,
          after: 44,
          delta: 3,
          is_new: false,
        },
      ],
    });
    // ...but the lineage is EMPTY during play (a resign adds no move and polling
    // is already off), so the card the badge attaches to only exists after the
    // forced terminal refetch — exactly the "deltas arrive before lineage" gap.
    // Global beforeEach defaults fetchSessionOpenings to an empty lineage.

    render(<ChessGame />);
    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(fetchSessionOpeningsMock).toHaveBeenCalled();
    });
    // Empty lineage during play -> no region, no badge yet.
    expect(
      screen.queryByRole("region", { name: "Openings played" }),
    ).not.toBeInTheDocument();

    const callsBeforeResign = fetchSessionOpeningsMock.mock.calls.length;
    // The terminal refetch returns the played opening so the badge has a card.
    fetchSessionOpeningsMock.mockResolvedValue({
      player_color: "white",
      lineage: [
        {
          opening_key: "k1",
          opening_name: "King's Pawn Game",
          opening_family: "King's Pawn",
          eco: "C20",
          depth: 0,
          score: 44,
          confidence: 0.5,
          coverage: 0.5,
          sample_size: 5,
          game_count: 2,
          path: [],
          moves: ["e4"],
        },
      ],
      start_ply: 1,
    });

    fireEvent.click(screen.getByRole("button", { name: /resign/i }));
    await waitFor(() => {
      expect(screen.getByText("Are you sure?")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Resign"));

    // The terminal openingScoreChanges bumps refetchKey -> one more fetch, which
    // loads the card; the inline badge then renders next to the chip.
    const region = await screen.findByRole("region", {
      name: "Openings played",
    });
    expect(fetchSessionOpeningsMock.mock.calls.length).toBeGreaterThan(
      callsBeforeResign,
    );
    const badge = within(region).getByText("+3 → 44");
    expect(badge).toHaveClass("game-opening-lineage__delta--up");
  });

  it("updates a same-session card to the reconciled after-score when no baseline exists", async () => {
    const sessionId = "session-reconciled-no-baseline";
    getOpeningRootsMock.mockResolvedValue({ families: [] });
    __resetOpeningRootIndexCache();
    // Keep every lineage response deliberately stale. The final score must come
    // from the reconciled store envelope, not a navigation or lineage refetch.
    fetchSessionOpeningsMock.mockResolvedValue({
      player_color: "white",
      lineage: [
        {
          ...lineageCard("k1", "King's Pawn Game", ["e4"]),
          score: 60,
        },
      ],
      start_ply: 1,
    });
    useGameStore.setState({
      sessionId,
      isGameActive: false,
      playerColor: "white",
      boardOrientation: "white",
      moveHistory: [{ san: "e4", fen: LINEAGE_FEN_E4, uci: "e2e4" }],
      gameResult: { type: "resign", message: "Resigned." },
      liveFen: LINEAGE_FEN_E4,
      viewIndex: null,
    });

    render(<ChessGame />);
    const region = await screen.findByRole("region", {
      name: "Openings played",
    });
    expect(within(region).getByText("60")).toBeInTheDocument();
    const callsBeforeTerminal = fetchSessionOpeningsMock.mock.calls.length;

    act(() => {
      useGameStore.getState().setTerminalOpeningDelta(sessionId, [
        {
          opening_key: "k1",
          opening_name: "King's Pawn Game",
          opening_family: "King's Pawn",
          eco: "C20",
          depth: 0,
          before: null,
          after: 61,
          delta: null,
          is_new: false,
        },
      ]);
    });

    // The first non-null terminal envelope changes the Boolean refetch key once.
    await waitFor(() =>
      expect(fetchSessionOpeningsMock.mock.calls.length).toBeGreaterThan(
        callsBeforeTerminal,
      ),
    );
    expect(within(region).getByText("61")).toBeInTheDocument();
    expect(within(region).queryByText(/→/)).not.toBeInTheDocument();

    const callsAfterTerminalRefetch = fetchSessionOpeningsMock.mock.calls.length;
    const stateBeforeReconcile = useGameStore.getState();
    const moveHistoryBeforeReconcile = stateBeforeReconcile.moveHistory;

    act(() => {
      useGameStore.getState().applyPolledOpeningDelta(
        sessionId,
        [
          {
            opening_key: "k1",
            opening_name: "King's Pawn Game",
            opening_family: "King's Pawn",
            eco: "C20",
            depth: 0,
            before: null,
            after: 72,
            delta: null,
            is_new: false,
          },
        ],
        stateBeforeReconcile.openingDeltaPollToken,
      );
    });

    await waitFor(() =>
      expect(within(region).getByText("72")).toBeInTheDocument(),
    );
    expect(within(region).queryByText("60")).not.toBeInTheDocument();
    expect(within(region).queryByText("61")).not.toBeInTheDocument();
    expect(within(region).queryByText(/→/)).not.toBeInTheDocument();
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(
      callsAfterTerminalRefetch,
    );

    const stateAfterReconcile = useGameStore.getState();
    expect(stateAfterReconcile.sessionId).toBe(sessionId);
    expect(stateAfterReconcile.viewIndex).toBe(stateBeforeReconcile.viewIndex);
    expect(stateAfterReconcile.liveFen).toBe(stateBeforeReconcile.liveFen);
    expect(stateAfterReconcile.moveHistory).toBe(moveHistoryBeforeReconcile);
  });

  it("renders no lineage when the session has no openings yet", async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-empty-lineage",
      engine_elo: 1500,
      player_color: "white",
    });
    // Global beforeEach already defaults fetchSessionOpenings to an empty lineage.

    render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(fetchSessionOpeningsMock).toHaveBeenCalled();
    });
    expect(
      screen.queryByRole("region", { name: "Openings played" }),
    ).not.toBeInTheDocument();
  });
});

describe("ChessGame remount persistence", () => {
  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    startGameMock.mockReset();
    endGameMock.mockReset();
    endGameMock.mockResolvedValue({});
    uploadSessionMovesMock.mockReset();
    getNextOpponentMoveMock.mockReset();
    recordBlunderMock.mockReset();
    mockAnalyzeMove.mockReset();
    evaluatePositionMock.mockReset();
    lookupOpeningByFenMock.mockReset();
    gameAnalysisStore.getState().clearAll();
    capturedPieceDrop = null;

    getNextOpponentMoveMock.mockResolvedValue({
      mode: "engine",
      move: { uci: "d7d5", san: "d5" },
      target_blunder_id: null,
      decision_source: "backend_engine",
    });
    lookupOpeningByFenMock.mockResolvedValue(null);
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("preserves analysis data across unmount/remount, flushes coordinator on resign, and never shows the start popup at game end or on an ended-session remount (g-yuvr)", async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-remount",
      engine_elo: 1500,
      player_color: "white",
    });
    endGameMock.mockResolvedValue({});

    const { unmount } = render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(startGameMock).toHaveBeenCalled();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    // Populate analysis for both moves
    act(() => {
      gameAnalysisStore.getState().resolveAnalysis(0, {
        id: "analysis-0",
        move: "e2e4",
        bestMove: "e2e4",
        bestEval: 30,
        playedEval: 30,
        currentPositionEval: 30,
        playedEvalMate: null,
        currentPositionEvalMate: null,
        moveIndex: 0,
        delta: 0,
        classification: "best" as const,
        blunder: false,
        recordable: false,
      });
      gameAnalysisStore.getState().resolveAnalysis(1, {
        id: "analysis-1",
        move: "d7d5",
        bestMove: "d7d5",
        bestEval: 20,
        playedEval: 20,
        currentPositionEval: 20,
        playedEvalMate: null,
        currentPositionEvalMate: null,
        moveIndex: 1,
        delta: 0,
        classification: "best" as const,
        blunder: false,
        recordable: false,
      });
    });

    // Verify analysis data is present before unmount
    expect(gameAnalysisStore.getState().analysisMap.size).toBe(2);

    // Unmount (simulates navigating away from /game)
    unmount();

    // Analysis data survives in the singleton store
    expect(gameAnalysisStore.getState().analysisMap.size).toBe(2);

    // Remount (simulates navigating back to /game)
    const { unmount: unmountEnded } = render(<ChessGame />);

    // Move list should still show both moves (game store persists moveHistory)
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /e4/i })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    // Resign — coordinator should durably upload the full move history first
    fireEvent.click(screen.getByRole("button", { name: /resign/i }));
    await waitFor(() => {
      expect(screen.getByText("Are you sure?")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Resign"));

    await waitFor(() => {
      expect(uploadSessionMovesMock).toHaveBeenCalled();
    });

    await waitFor(() => {
      expect(endGameMock).toHaveBeenCalledWith(
        "session-remount",
        "resign",
        expect.any(String),
        expect.any(Boolean),
      );
    });

    // (a) No new-game popup at game end. endGameMock is awaited before
    // finishLocalGame flips isGameActive→false and renders the terminal UI, so
    // waiting on endGameMock alone can pass before the overlay would ever render.
    // Gate on the post-game banner (finishLocalGame renders role="region"
    // "Post-game options") so the assertion runs after the terminal UI is live;
    // on the bug the banner and the erroneous StartPanel coexist and this fails.
    await screen.findByRole("region", { name: "Post-game options" });
    // StartPanel is identified by its "Play White" control.
    expect(
      screen.queryByRole("button", { name: /play white/i }),
    ).not.toBeInTheDocument();

    // (b) No popup on an ended-session remount — covers the initializer's
    // "already-ended session" branch (sessionId non-null, isGameActive=false,
    // gameResult set). Without the fix, showStartOverlay re-seeds true and the
    // StartPanel renders; with the fix it seeds false (sessionId non-null).
    unmountEnded();
    render(<ChessGame />);
    // moveHistory persists across remount, so the move list settles the render.
    await screen.findByRole("button", { name: /e4/i });
    expect(
      screen.queryByRole("button", { name: /play white/i }),
    ).not.toBeInTheDocument();
  });

  it("does not overwrite engine ELO on remount when a game is active", async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-elo",
      engine_elo: 1500,
      player_color: "white",
    });

    const { unmount } = render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(startGameMock).toHaveBeenCalled();
    });

    const eloAfterStart = useGameStore.getState().engineElo;

    // Unmount + remount while game is active
    unmount();

    // fetchCurrentRating returns a different rating on remount
    fetchCurrentRatingMock.mockResolvedValue({
      current_rating: 1800,
      is_provisional: false,
      games_played: 50,
    });

    render(<ChessGame />);

    // Wait for the fetchCurrentRating effect to complete
    await waitFor(() => {
      expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(2);
    });

    // Engine ELO should NOT have been resampled
    expect(useGameStore.getState().engineElo).toBe(eloAfterStart);
    // But player rating should still be updated
    expect(useGameStore.getState().playerRating).toBe(1800);
  });
});

describe("ChessGame blunder board rewind", () => {
  const reachDelayedPlayerBlunder = async () => {
    const line = new Chess();
    line.move("e4");
    line.move("d5");
    const sourceFenBeforeBlunder = line.fen();
    line.move("Nf3");
    const fenAfterBlunder = line.fen();
    line.move("Nc6");
    const liveFenAfterReply = line.fen();

    startGameMock.mockResolvedValueOnce({
      session_id: "session-rewind",
      engine_elo: 1500,
      player_color: "white",
    });
    getNextOpponentMoveMock.mockReset();
    getNextOpponentMoveMock
      .mockResolvedValueOnce({
        mode: "engine",
        move: { uci: "d7d5", san: "d5" },
        target_blunder_id: null,
        decision_source: "backend_engine",
      })
      .mockResolvedValueOnce({
        mode: "engine",
        move: { uci: "b8c6", san: "Nc6" },
        target_blunder_id: null,
        decision_source: "backend_engine",
      });

    render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(startGameMock).toHaveBeenCalled();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /nc6/i })).toBeInTheDocument();
    });

    return {
      sourceFenBeforeBlunder,
      fenAfterBlunder,
      liveFenAfterReply,
    };
  };

  const resolveMoveTwoAsBlunder = async () => {
    const result = {
      id: "analysis-2-g1f3",
      move: "g1f3",
      bestMove: "d2d4",
      bestEval: 50,
      playedEval: -150,
      currentPositionEval: -150,
      playedEvalMate: null,
      currentPositionEvalMate: null,
      moveIndex: 2,
      delta: 200,
      classification: "blunder" as const,
      blunder: true,
      recordable: true,
    };
    // Drive the blunder alert through the outcome channel and flush the
    // consumer's coalescing microtask so the board-wash fires before assertions.
    // Dedup so the deferred bridge does not re-fire index 2.
    await act(async () => {
      gameAnalysisStore.getState().resolveAnalysis(2, result);
      bridgeEmittedIndices.add(2);
      capturedOutcomeListener?.({
        seq: 0, generation: 0, sessionId: null,
        moveIndex: 2, requestId: result.id, status: "resolved", result,
      });
      await Promise.resolve();
    });
  };

  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    startGameMock.mockReset();
    endGameMock.mockReset();
    uploadSessionMovesMock.mockReset();
    getNextOpponentMoveMock.mockReset();
    recordBlunderMock.mockReset();
    recordManualBlunderMock.mockReset();
    reviewSrsBlunderMock.mockReset();
    mockAnalyzeMove.mockReset();
    evaluatePositionMock.mockReset();
    lookupOpeningByFenMock.mockReset();
    gameAnalysisStore.getState().clearAll();
    capturedPieceDrop = null;
    capturedSquareClick = null;

    endGameMock.mockResolvedValue({});
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 });
    lookupOpeningByFenMock.mockResolvedValue(null);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("rewinds one ply at a time and stays on the pre-blunder position", async () => {
    const { sourceFenBeforeBlunder, fenAfterBlunder, liveFenAfterReply } =
      await reachDelayedPlayerBlunder();

    vi.useFakeTimers();
    await resolveMoveTwoAsBlunder();

    expect(useGameStore.getState().viewIndex).toBe(3);
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      liveFenAfterReply,
    );

    act(() => {
      vi.advanceTimersByTime(124);
    });
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      liveFenAfterReply,
    );

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(useGameStore.getState().viewIndex).toBe(2);
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      fenAfterBlunder,
    );

    act(() => {
      vi.advanceTimersByTime(239);
    });
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      fenAfterBlunder,
    );

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(useGameStore.getState().viewIndex).toBe(1);
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      sourceFenBeforeBlunder,
    );

    act(() => {
      vi.advanceTimersByTime(10000);
    });
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      sourceFenBeforeBlunder,
    );
  });

  it("uses stored source fen when analysis resolves after later moves are already on the live board", async () => {
    const { sourceFenBeforeBlunder, liveFenAfterReply } =
      await reachDelayedPlayerBlunder();

    expect(liveFenAfterReply).not.toBe(sourceFenBeforeBlunder);

    vi.useFakeTimers();
    await resolveMoveTwoAsBlunder();

    act(() => {
      vi.advanceTimersByTime(365);
    });

    expect(useGameStore.getState().viewIndex).toBe(1);
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      sourceFenBeforeBlunder,
    );
  });

  it("shakes but blocks the move on a board click during the blunder rewind override (g-1y68 A3)", async () => {
    const { sourceFenBeforeBlunder } = await reachDelayedPlayerBlunder();

    vi.useFakeTimers();
    await resolveMoveTwoAsBlunder();

    act(() => {
      vi.advanceTimersByTime(365);
    });

    const moveCountBeforeClick = useGameStore.getState().moveHistory.length;
    act(() => {
      capturedSquareClick?.({ square: "e2" });
      capturedSquareClick?.({ square: "e4" });
    });

    // No move applied and no opponent request, but the blunder rewind IS the core
    // "why can't I move?" moment — so the board now shakes toward the pill.
    expect(useGameStore.getState().moveHistory).toHaveLength(moveCountBeforeClick);
    expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      sourceFenBeforeBlunder,
    );
    expect(
      document.querySelector(".chessboard-square-measure--nudge"),
    ).not.toBeNull();
    // Dragging is enabled during review only so a drag attempt can be caught and
    // rejected (handleDropPiece), not so a move can land.
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-allow-dragging",
      "true",
    );
  });

  it("clears pending rewind timers during reset so stale fen does not reapply afterward", async () => {
    await reachDelayedPlayerBlunder();

    vi.useFakeTimers();
    await resolveMoveTwoAsBlunder();

    fireEvent.click(screen.getByRole("button", { name: /reset game/i }));
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      STARTING_FEN,
    );

    act(() => {
      vi.advanceTimersByTime(5000);
    });

    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      STARTING_FEN,
    );
  });

  it("shows blunder arrows on historical navigation without rewinding the selected move", async () => {
    const { sourceFenBeforeBlunder, fenAfterBlunder, liveFenAfterReply } =
      await reachDelayedPlayerBlunder();

    vi.useFakeTimers();
    await resolveMoveTwoAsBlunder();

    act(() => {
      vi.advanceTimersByTime(365);
    });
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      sourceFenBeforeBlunder,
    );

    fireEvent.click(screen.getByRole("button", { name: /nc6/i }));
    expect(useGameStore.getState().viewIndex).toBeNull();
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      liveFenAfterReply,
    );

    const blunderMoveButton = screen.getByRole("button", { name: /nf3/i });
    fireEvent.click(blunderMoveButton);

    expect(useGameStore.getState().viewIndex).toBe(2);
    expect(blunderMoveButton.className).toContain("selected");
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      fenAfterBlunder,
    );

    act(() => {
      vi.advanceTimersByTime(500);
    });

    expect(useGameStore.getState().viewIndex).toBe(2);
    expect(blunderMoveButton.className).toContain("selected");
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      fenAfterBlunder,
    );
    expect(screen.getByTestId("chessboard")).toHaveAttribute("data-arrow-count", "2");
  });

  // g-i9v8: the failing ordering the other tests never exercise — analysis
  // resolves the blunder BEFORE the opponent reply lands. Same line as
  // reachDelayedPlayerBlunder, but the second opponent reply (Nc6) is held
  // unresolved so the rewind runs first and the reply commits late.
  const reachDelayedPlayerBlunderDeferredReply = async () => {
    const line = new Chess();
    line.move("e4");
    line.move("d5");
    const sourceFenBeforeBlunder = line.fen();
    line.move("Nf3");
    const fenAfterBlunder = line.fen();
    line.move("Nc6");
    const liveFenAfterReply = line.fen();

    startGameMock.mockResolvedValueOnce({
      session_id: "session-rewind",
      engine_elo: 1500,
      player_color: "white",
    });
    getNextOpponentMoveMock.mockReset();

    let resolveSecondReply!: (value: {
      mode: "engine";
      move: { uci: string; san: string };
      target_blunder_id: null;
      decision_source: "backend_engine";
    }) => void;

    getNextOpponentMoveMock
      .mockResolvedValueOnce({
        mode: "engine",
        move: { uci: "d7d5", san: "d5" },
        target_blunder_id: null,
        decision_source: "backend_engine",
      })
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveSecondReply = resolve;
        }),
      );

    render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(startGameMock).toHaveBeenCalled();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });
    // The second opponent reply stays in flight; assert it was requested but do
    // not resolve it yet so analysis can win the race.
    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(2);
    });
    expect(screen.queryByRole("button", { name: /nc6/i })).not.toBeInTheDocument();

    return {
      sourceFenBeforeBlunder,
      fenAfterBlunder,
      liveFenAfterReply,
      resolveSecondReply: () =>
        resolveSecondReply({
          mode: "engine",
          move: { uci: "b8c6", san: "Nc6" },
          target_blunder_id: null,
          decision_source: "backend_engine",
        }),
    };
  };

  it("keeps the blunder rewind when the opponent reply lands late, returning to live only on navigation (g-i9v8)", async () => {
    const { sourceFenBeforeBlunder, liveFenAfterReply, resolveSecondReply } =
      await reachDelayedPlayerBlunderDeferredReply();

    vi.useFakeTimers();
    await resolveMoveTwoAsBlunder();

    // Rewind settles on the pre-blunder position (one ply before Nf3).
    act(() => {
      vi.advanceTimersByTime(365);
    });
    expect(useGameStore.getState().viewIndex).toBe(1);
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      sourceFenBeforeBlunder,
    );

    // The late opponent reply commits. It must be absorbed into
    // liveFen/moveHistory WITHOUT snapping the board off the rewound position —
    // the regression: previously commitAppliedMove forced viewIndex null,
    // washing the board to live while leaving the alert (and its arrows) active
    // with move entry blocked.
    await act(async () => {
      resolveSecondReply();
    });

    expect(useGameStore.getState().viewIndex).toBe(1);
    expect(useGameStore.getState().liveFen).toBe(liveFenAfterReply);
    expect(useGameStore.getState().moveHistory.map((m) => m.san)).toEqual([
      "e4",
      "d5",
      "Nf3",
      "Nc6",
    ]);
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      sourceFenBeforeBlunder,
    );
    // Arrows stay up during review, and the reply is now navigable in the list.
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-arrow-count",
      "2",
    );
    expect(
      screen.getByRole("button", { name: /nc6/i }),
    ).toBeInTheDocument();

    // Navigating to the latest ply returns to live and clears the alert.
    fireEvent.click(screen.getByRole("button", { name: /nc6/i }));
    expect(useGameStore.getState().viewIndex).toBeNull();
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      liveFenAfterReply,
    );
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-arrow-count",
      "0",
    );

    // The board is playable again: a legal player move now lands.
    vi.useRealTimers();
    getNextOpponentMoveMock.mockReturnValueOnce(new Promise(() => undefined));
    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "d2", targetSquare: "d4" });
    });
    expect(useGameStore.getState().moveHistory.map((m) => m.san)).toEqual([
      "e4",
      "d5",
      "Nf3",
      "Nc6",
      "d4",
    ]);
  });
});

describe("ChessGame mobile auto-scroll on graph appearance", () => {
  let scrollSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    scrollSpy = vi.fn();
    window.HTMLElement.prototype.scrollIntoView =
      scrollSpy as unknown as typeof window.HTMLElement.prototype.scrollIntoView;
    startGameMock.mockReset();
    getNextOpponentMoveMock.mockReset();
    mockAnalyzeMove.mockReset();
    evaluatePositionMock.mockReset();
    lookupOpeningByFenMock.mockReset();
    gameAnalysisStore.getState().clearAll();
    capturedPieceDrop = null;

    startGameMock.mockResolvedValue({
      session_id: "session-scroll",
      engine_elo: 1500,
      player_color: "white",
    });
    getNextOpponentMoveMock.mockResolvedValue({
      mode: "engine",
      move: { uci: "d7d5", san: "d5" },
      target_blunder_id: null,
      decision_source: "backend_engine",
    });
    lookupOpeningByFenMock.mockResolvedValue(null);
  });

  async function startAndPlayFirstMove() {
    render(<ChessGame />);
    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));
    await waitFor(() => expect(startGameMock).toHaveBeenCalled());
    // Clear scrolls from the start flow so we measure only the graph transition.
    scrollSpy.mockClear();
    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument(),
    );
  }

  const graphScrolls = (): ScrollIntoViewOptions[] =>
    scrollSpy.mock.calls
      .map(([opts]) => opts as ScrollIntoViewOptions)
      .filter((opts) => opts && opts.block === "start");

  it("scrolls the section into view once (smooth) when the graph first appears (narrow)", async () => {
    setMatchMedia(GAME_MOBILE_QUERY, true);
    await startAndPlayFirstMove();
    const scrolls = graphScrolls();
    expect(scrolls).toHaveLength(1);
    expect(scrolls[0].behavior).toBe("smooth");
  });

  it("does not auto-scroll when not narrow", async () => {
    setMatchMedia(GAME_MOBILE_QUERY, false);
    await startAndPlayFirstMove();
    expect(graphScrolls()).toHaveLength(0);
  });

  it("does not auto-scroll on an initial mount that already has below-board content", async () => {
    // The game starts inactive, so hasBelowBoardContent is already true at
    // mount — the effect must seed without a false→true transition (no jump).
    setMatchMedia(GAME_MOBILE_QUERY, true);
    render(<ChessGame />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /new game/i })).toBeInTheDocument(),
    );
    expect(graphScrolls()).toHaveLength(0);
  });

  it("uses behavior:auto when prefers-reduced-motion is set", async () => {
    setMatchMedia(GAME_MOBILE_QUERY, true);
    setMatchMedia("(prefers-reduced-motion: reduce)", true);
    await startAndPlayFirstMove();
    const scrolls = graphScrolls();
    expect(scrolls).toHaveLength(1);
    expect(scrolls[0].behavior).toBe("auto");
  });
});

// ---------------------------------------------------------------------------
// AC4 — post-root drill pass/fail is stable across the strictness threshold
// matrix. This drives the REAL ChessGame post-root drill flow (gradeDrillMove
// inside the controller) across every threshold/boundary, not a test-local
// comparator.
//
// Order-invariance is COMPOSITIONAL, not re-tested here: (1) the coordinator
// yields one settled AnalysisResult whose delta is identical regardless of cache/
// worker completion order — proven by GameAnalysisCoordinator.test.ts, including
// the "resolved delta is identical whether the worker or the cache wins (AC4
// anchor)" case that pins the exact delta the drill reads; (2) gradeDrillMove is
// a pure function of that delta (analysisUtils.test.ts). This suite covers the
// remaining link: the controller grades the settled result and acts on it.
// Matrix: delta at threshold-1 / threshold / threshold+1 for strict 15,
// standard 35, lenient 50, plus custom {0,25,40}. failsDrill uses strict `>` so
// the boundary value PASSES.
// ---------------------------------------------------------------------------
describe("ChessGame post-root drill outcome stability (AC4)", () => {
  const STRICT_CP = 15;
  const STANDARD_CP = 35;
  const LENIENT_CP = 50;
  const CUSTOM_CPS = [0, 25, 40];
  const ALL_THRESHOLDS = [STRICT_CP, STANDARD_CP, LENIENT_CP, ...CUSTOM_CPS];
  const SCENARIOS: Array<[number, number]> = ALL_THRESHOLDS.flatMap((t) =>
    [t - 1, t, t + 1].filter((d) => d >= 0).map((d) => [t, d] as [number, number]),
  );

  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    failDrillMock.mockReset();
    getNextOpponentMoveMock.mockReset();
    mockAnalyzeMove.mockReset();
    mockCoordinator.waitForAnalysis.mockReset();
    gameAnalysisStore.getState().clearAll();
    capturedPieceDrop = null;
    failDrillMock.mockResolvedValue({
      session_id: "drill-matrix",
      mode: "drill",
      drill_state: "failed",
      opening_key: "target-fen",
      opening_name: "Target",
      opening_family: "Target",
      eco: null,
      depth: 1,
      player_color: "white",
      engine_elo: 1500,
      strictness: "standard",
      strictness_cp: 25,
      is_rated: false,
      rated_start_ply: null,
      normal_started_at: null,
      converted_at: null,
      terminal_reason: "accuracy",
    });
    getNextOpponentMoveMock.mockResolvedValue({
      mode: "engine",
      move: { uci: "d7d5", san: "d5" },
      target_blunder_id: null,
      decision_source: "backend_engine",
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each(SCENARIOS)(
    "strictness=%i, delta=%i grades deterministically",
    async (threshold, delta) => {
      // `: string` widens the literals so `bestMove !== playedMove` isn't a
      // compile-time-constant comparison TS rejects as having no type overlap.
      const playedMove: string = "e2e4";
      const bestMove: string = "d2d4";
      const shouldFail =
        (threshold <= 0 && bestMove !== playedMove) ||
        delta > threshold; // nonzero thresholds use failsDrill: strict `>`

      useGameStore.setState({
        sessionId: "drill-matrix",
        isGameActive: true,
        playerColor: "white",
        boardOrientation: "white",
        drillStrictnessCp: threshold,
        liveFen: STARTING_FEN,
      });

      render(<ChessGame />);
      act(() => {
        useGameStore.setState({
          drillOpeningKey: "target-fen",
          drillState: "root_reached",
          drillStrictnessCp: threshold,
        });
      });

      mockCoordinator.waitForAnalysis.mockResolvedValue({
        id: "analysis-e4",
        move: playedMove,
        bestMove,
        bestEval: delta,
        playedEval: 0,
        currentPositionEval: 0,
        playedEvalMate: null,
        currentPositionEvalMate: null,
        moveIndex: 0,
        delta,
        classification: shouldFail ? "mistake" : "good",
        blunder: false,
        recordable: false,
      });

      await act(async () => {
        capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
      });

      if (shouldFail) {
        await waitFor(() => {
          expect(failDrillMock).toHaveBeenCalledWith("drill-matrix", "accuracy");
        });
        expect(useGameStore.getState().drillState).toBe("failed");
      } else {
        await waitFor(() => {
          expect(getNextOpponentMoveMock).toHaveBeenCalled();
        });
        expect(failDrillMock).not.toHaveBeenCalled();
        expect(useGameStore.getState().drillState).not.toBe("failed");
      }
    },
  );

  it("allows the exact best move at 0cp strictness", async () => {
    useGameStore.setState({
      sessionId: "drill-matrix",
      isGameActive: true,
      playerColor: "white",
      boardOrientation: "white",
      drillStrictnessCp: 0,
      liveFen: STARTING_FEN,
    });

    render(<ChessGame />);
    act(() => {
      useGameStore.setState({
        drillOpeningKey: "target-fen",
        drillState: "root_reached",
        drillStrictnessCp: 0,
      });
    });

    mockCoordinator.waitForAnalysis.mockResolvedValue({
      id: "analysis-e4",
      move: "e2e4",
      bestMove: "e2e4",
      bestEval: 0,
      playedEval: 0,
      currentPositionEval: 0,
      playedEvalMate: null,
      currentPositionEvalMate: null,
      moveIndex: 0,
      delta: 0,
      classification: "best",
      blunder: false,
      recordable: false,
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(getNextOpponentMoveMock).toHaveBeenCalled();
    });
    expect(failDrillMock).not.toHaveBeenCalled();
    expect(useGameStore.getState().drillState).not.toBe("failed");
  });
});

describe("ChessGame return to drill after analyze (g-65ve)", () => {
  const FEN_AFTER_E4 =
    "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1";

  const seedAbandonedDrillStore = (sessionId = "drill-1") => {
    useGameStore.setState({
      sessionId,
      isGameActive: false,
      drillOpeningKey: "ruy-lopez",
      drillOpeningName: "Ruy Lopez",
      drillState: "abandoned",
      drillStrictness: "standard",
      drillStrictnessCp: 25,
      playerColor: "white",
      boardOrientation: "white",
      engineElo: 1500,
      isRated: false,
      moveHistory: [{ san: "e4", fen: FEN_AFTER_E4, uci: "e2e4" }],
      gameResult: { type: "resign", message: "Drill abandoned." },
    });
  };

  const snapshotFor = (sessionId: string) => ({
    moves: [
      {
        move_number: 1,
        color: "white" as const,
        move_san: "e4",
        fen_after: FEN_AFTER_E4,
        eval_cp: 20,
        eval_mate: null,
        best_move_san: "d4",
        best_move_eval_cp: 30,
        eval_delta: 10,
        classification: "good" as const,
      },
    ],
    positionAnalysis: {},
    playerColor: "white" as const,
    initialMoveIndex: 0,
    sourceSessionId: sessionId,
  });

  const setReturnMarker = (sessionId: string) => {
    mockLocation = {
      state: { returnFromDrillAnalysis: { sourceSessionId: sessionId } },
      pathname: "/play",
    };
  };

  beforeEach(() => {
    startDrillMock.mockReset();
    getNextOpponentMoveMock.mockReset();
    startDrillMock.mockResolvedValue({
      session_id: "drill-2",
      mode: "drill",
      drill_state: "active",
      opening_key: "ruy-lopez",
      opening_name: "Ruy Lopez",
      opening_family: "Ruy Lopez",
      eco: null,
      depth: 1,
      player_color: "white",
      engine_elo: 1500,
      strictness: "standard",
      strictness_cp: 25,
      is_rated: false,
      rated_start_ply: null,
      normal_started_at: null,
      converted_at: null,
      terminal_reason: null,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("restores the drill with Again ready, no popup, board disabled, moves retained", async () => {
    seedAbandonedDrillStore();
    useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
    setReturnMarker("drill-1");

    render(<ChessGame />);

    // No setup popup and no generic "New game" action — only "Again" + gear.
    expect(screen.getByRole("button", { name: /^again$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /play white/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /new game/i })).toBeNull();
    // Analyze keeps its original label but is re-wired to safely re-open the
    // saved snapshot rather than rebuild it.
    expect(
      screen.getByRole("button", { name: /^analyze$/i }),
    ).toBeInTheDocument();

    // Retained position/moves and a disabled (ended) board.
    expect(screen.getByRole("button", { name: /e4/i })).toBeInTheDocument();
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-allow-dragging",
      "false",
    );

    // One-shot marker consumed via replace navigation, but presentation stays.
    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith("/play", {
        replace: true,
        state: null,
      });
    });
    expect(screen.getByRole("button", { name: /^again$/i })).toBeInTheDocument();
  });

  it("Again restarts with the preserved opening/side/strictness (difficulty resampled) and no stale-session traffic", async () => {
    seedAbandonedDrillStore();
    useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
    setReturnMarker("drill-1");
    // Difficulty is re-randomized (g-ncvm); mock pins it to MAIA_ELO_BINS[0].
    vi.spyOn(Math, "random").mockReturnValue(0);

    render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /^again$/i }));

    await waitFor(() => {
      expect(startDrillMock).toHaveBeenCalledWith({
        opening_key: "ruy-lopez",
        player_color: "white",
        engine_elo: MAIA_ELO_BINS[0],
        strictness: "standard",
        strictness_cp: 25,
      });
    });
    // White-to-move restart: no opponent move fired against the abandoned drill.
    expect(getNextOpponentMoveMock).not.toHaveBeenCalled();
  });

  it("reviewed-return Again replays an ad-hoc drill with its line from the durable store", async () => {
    // The /drill-analysis round trip remounts ChessGame, wiping the component
    // ref. The line lives in the durable store, so Again can still resend it —
    // without it the backend 404s a non-root target FEN.
    useGameStore.setState({
      sessionId: "drill-1",
      isGameActive: false,
      drillOpeningKey: "target-fen",
      drillLine: ["e2e4", "c7c5"],
      drillOpeningName: "Sicilian Defense",
      drillState: "abandoned",
      drillStrictness: "standard",
      drillStrictnessCp: 25,
      playerColor: "white",
      boardOrientation: "white",
      engineElo: 1500,
      isRated: false,
      moveHistory: [{ san: "e4", fen: FEN_AFTER_E4, uci: "e2e4" }],
      gameResult: { type: "resign", message: "Drill abandoned." },
    });
    useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
    setReturnMarker("drill-1");
    // Difficulty is re-randomized (g-ncvm); mock pins it to MAIA_ELO_BINS[0].
    vi.spyOn(Math, "random").mockReturnValue(0);

    render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /^again$/i }));

    await waitFor(() => {
      expect(startDrillMock).toHaveBeenCalledWith({
        opening_key: "target-fen",
        player_color: "white",
        engine_elo: MAIA_ELO_BINS[0],
        strictness: "standard",
        strictness_cp: 25,
        line: ["e2e4", "c7c5"],
      });
    });
  });

  it("Analyze re-opens the saved snapshot without rebuilding it", async () => {
    seedAbandonedDrillStore();
    const snapshot = snapshotFor("drill-1");
    useDrillAnalysisStore.getState().setSnapshot(snapshot);
    setReturnMarker("drill-1");

    render(<ChessGame />);
    mockNavigate.mockClear();

    fireEvent.click(screen.getByRole("button", { name: /^analyze$/i }));

    expect(mockNavigate).toHaveBeenCalledWith("/drill-analysis");
    // Snapshot is re-used as-is, never rebuilt/overwritten on return.
    expect(useDrillAnalysisStore.getState().snapshot).toBe(snapshot);

    // Flush the mount-time getStatsAchievements() resolution inside act() so its
    // state update doesn't leak past this test.
    await act(async () => {
      await Promise.resolve();
    });
  });

  it("retains the engine Elo on the mount resample but resamples it on Again", async () => {
    // Two behaviors in one test: the on-mount rating resample is SKIPPED for a
    // drill-in-store (so the post-drill UI keeps showing 1500), while clicking
    // Again deliberately re-randomizes the difficulty (g-ncvm).
    seedAbandonedDrillStore();
    useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
    setReturnMarker("drill-1");
    // A rating that maps to a different Maia bin — if the mount resample fired it
    // would clobber the retained 1500 before "Again" reads it.
    fetchCurrentRatingMock.mockResolvedValue({
      current_rating: 900,
      is_provisional: false,
      games_played: 50,
    });

    render(<ChessGame />);

    // Let the on-mount fetchCurrentRating().then(...) settle.
    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalled());
    await act(async () => {
      await Promise.resolve();
    });

    // Mount resample skipped: the just-played Elo is retained.
    expect(useGameStore.getState().engineElo).toBe(1500);

    // Again resamples; mock pins the fresh bin to MAIA_ELO_BINS[0].
    vi.spyOn(Math, "random").mockReturnValue(0);
    fireEvent.click(screen.getByRole("button", { name: /^again$/i }));
    await waitFor(() => {
      expect(startDrillMock).toHaveBeenCalledWith(
        expect.objectContaining({ engine_elo: MAIA_ELO_BINS[0] }),
      );
    });
  });

  it("gear clears the reviewed presentation and opens the drill setup overlay", async () => {
    seedAbandonedDrillStore();
    useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
    setReturnMarker("drill-1");
    getOpeningRootsMock.mockResolvedValue({ families: [] });

    render(<ChessGame />);

    fireEvent.click(
      screen.getByRole("button", { name: /change drill settings/i }),
    );

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /^again$/i })).toBeNull();
    });
    // Setup overlay opened (drill mode start controls present).
    expect(
      screen.getByRole("button", { name: /start drill/i }),
    ).toBeInTheDocument();
  });

  describe.each([
    {
      name: "snapshot present but no marker",
      endedRecovery: true,
      setup: () => {
        seedAbandonedDrillStore();
        useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
      },
    },
    {
      name: "marker/snapshot/store session mismatch",
      endedRecovery: true,
      setup: () => {
        seedAbandonedDrillStore("drill-1");
        useDrillAnalysisStore.getState().setSnapshot(snapshotFor("other"));
        setReturnMarker("other");
      },
    },
    {
      name: "marker present but game still active",
      endedRecovery: false,
      setup: () => {
        seedAbandonedDrillStore();
        useGameStore.setState({ isGameActive: true });
        useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
        setReturnMarker("drill-1");
      },
    },
    {
      name: "drill not abandoned",
      endedRecovery: true,
      setup: () => {
        seedAbandonedDrillStore();
        useGameStore.setState({ drillState: "failed" });
        useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
        setReturnMarker("drill-1");
      },
    },
    {
      name: "incomplete restart settings",
      endedRecovery: true,
      setup: () => {
        seedAbandonedDrillStore();
        useGameStore.setState({ drillStrictnessCp: null });
        useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
        setReturnMarker("drill-1");
      },
    },
  ])(
    "falls back to ordinary /play: $name",
    ({ setup, endedRecovery }) => {
      it("surfaces the ended-session recovery banner (not the auto-opened start popup) and no reviewed Again action", async () => {
        setup();
        render(<ChessGame />);
        // Flush the mount-time getStatsAchievements() resolution inside act() so
        // its state update doesn't leak past this synchronous assertion block.
        await act(async () => {});

        // No reviewed "Again" action exposed.
        expect(screen.queryByRole("button", { name: /^again$/i })).toBeNull();
        // The stale start popup must not auto-open (g-yuvr): with sessionId
        // non-null the overlay seeds hidden, so StartPanel's "Play White" is absent.
        expect(
          screen.queryByRole("button", { name: /play white/i }),
        ).not.toBeInTheDocument();

        if (endedRecovery) {
          // For an ended, non-null session the PostGameBanner inactive branch
          // renders a "New game" recovery action, so the user is never stranded.
          expect(
            screen.getByRole("button", { name: /new game/i }),
          ).toBeInTheDocument();
        }
      });
    },
  );
});
