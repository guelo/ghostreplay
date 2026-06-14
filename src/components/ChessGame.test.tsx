import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { Chess } from "chess.js";
import { render, screen, fireEvent, waitFor, act } from "../test/utils";
import ChessGame from "./ChessGame";
import { useGameStore } from "../stores/useGameStore";
import { STARTING_FEN } from "./chess-game/config";
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
const abandonDrillMock = vi.fn();
const startDrillMock = vi.fn();
const getOpeningRootsMock = vi.fn();
const recordBlunderMock = vi.fn();
const recordManualBlunderMock = vi.fn();
const reviewSrsBlunderMock = vi.fn();
const fetchCurrentRatingMock = vi.fn();
const getStatsAchievementsMock = vi.fn();
const audioPlayMock = vi.fn();
const audioCtorSpy = vi.fn();

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
    getNextOpponentMove: (...args: unknown[]) => getNextOpponentMoveMock(...args),
    continueDrill: (...args: unknown[]) => continueDrillMock(...args),
    failDrill: (...args: unknown[]) => failDrillMock(...args),
    checkDrillRoute: (...args: unknown[]) => checkDrillRouteMock(...args),
    abandonDrill: (...args: unknown[]) => abandonDrillMock(...args),
    startDrill: (...args: unknown[]) => startDrillMock(...args),
    getOpeningRoots: (...args: unknown[]) => getOpeningRootsMock(...args),
    fetchCurrentRating: (...args: unknown[]) => fetchCurrentRatingMock(...args),
    getStatsAchievements: (...args: unknown[]) => getStatsAchievementsMock(...args),
    recordBlunder: (...args: unknown[]) => recordBlunderMock(...args),
    recordManualBlunder: (...args: unknown[]) => recordManualBlunderMock(...args),
    reviewSrsBlunder: (...args: unknown[]) => reviewSrsBlunderMock(...args),
  };
});

const evaluatePositionMock = vi.fn();
const lookupOpeningByFenMock = vi.fn();

vi.mock("../hooks/useStockfishEngine", () => ({
  useStockfishEngine: () => ({
    status: "ready",
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
}));

import { gameAnalysisStore } from "../stores/createAnalysisStore";
import { useDrillAnalysisStore } from "../stores/drillAnalysisStore";
import {
  DecisionOwner,
  type DecisionOwnerGameState,
} from "../services/DecisionOwner";
import type { AnalysisOutcome } from "../services/GameAnalysisCoordinator";

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

const mockCoordinator = {
  analyzeMove: mockAnalyzeMove,
  waitForAnalysis: vi.fn((moveIndex: number) => {
    const analysis = gameAnalysisStore.getState().analysisMap.get(moveIndex);
    return analysis
      ? Promise.resolve(analysis)
      : Promise.reject(new Error("Analysis was not scheduled for this move"));
  }),
  restartAnalysisWorker: vi.fn(),
  clearAnalysis: vi.fn(),
  // Session changes drive the owner's full reset (in production via emitReset);
  // the mock routes them directly so blunderReserved/frontier reset between games.
  startSession: vi.fn(() =>
    mockCoordinator.decisionOwner.handleReset({ generation: 0, sessionId: null }),
  ),
  clearSession: vi.fn(() =>
    mockCoordinator.decisionOwner.handleReset({ generation: 0, sessionId: null }),
  ),
  flushPendingUploads: vi.fn().mockResolvedValue(undefined),
  stopSessionUploads: vi.fn(),
  sessionId: null,
  store: gameAnalysisStore,
  markSkipped: vi.fn(),
  pruneFromMoveIndex: vi.fn((k: number) =>
    mockCoordinator.decisionOwner.handleReset({ generation: 0, sessionId: null, fromMoveIndex: k }),
  ),
  getEpoch: vi.fn(() => ({ generation: 0, sessionId: null })),
  addAnalysisResetListener: vi.fn(() => () => {}),
  addAnalysisOutcomeListener: vi.fn(() => () => {}),
  decisionOwner: createTestDecisionOwner(),
};

vi.mock("../contexts/useGameAnalysisCoordinator", () => ({
  useGameAnalysisCoordinator: () => mockCoordinator,
}));

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
  mockLocation = { state: null, pathname: "/play" };
  mockNavigate.mockReset();
  useDrillAnalysisStore.getState().clear();
  useGameStore.setState(initialGameStoreState, true);
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
    abandonDrillMock.mockResolvedValue({ drill_state: "abandoned" });
    mockCoordinator.clearSession.mockClear();
    useDrillAnalysisStore.getState().clear();
    startDrillMock.mockReset();
    getOpeningRootsMock.mockReset();
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
      source: "post_game_view_analysis" | "post_game_history";
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
    useGameStore.setState({
      drillOpeningKey: rootFen,
      drillState: "active",
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
  });

  it("starts live drill play after an opponent-reached root without an immediate opponent move", async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-characterization",
      engine_elo: 1500,
      player_color: "black",
    });
    getNextOpponentMoveMock.mockResolvedValueOnce({
      mode: "ghost",
      move: { uci: "e2e4", san: "e4" },
      target_blunder_id: null,
      decision_source: "ghost_path",
      drill_route: {
        status: "root_reached",
        target_fen: "target-fen",
        resulting_fen: "target-fen",
        plies_to_target: 0,
      },
    });
    render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play black/i }));

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
    useGameStore.setState({
      drillOpeningKey: "target-fen",
      drillState: "root_reached",
      drillStrictnessCp: 25,
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
    useGameStore.setState({
      drillOpeningKey: "target-fen",
      drillState: "root_reached",
      drillStrictnessCp: 25,
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
    useGameStore.setState({
      drillOpeningKey: "target-fen",
      drillState: "root_reached",
      drillStrictnessCp: 25,
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

  const driveOffRouteFail = async () => {
    await startGameAsWhite();
    useGameStore.setState({
      drillOpeningKey: "target-fen",
      drillState: "active",
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

    const snapshot = useDrillAnalysisStore.getState().snapshot!;
    // The late-resolved failed move's eval is included in the snapshot.
    expect(snapshot.moves[0]).toMatchObject({ move_san: "e4", eval_cp: 10 });
    expect(snapshot.initialMoveIndex).toBe(0);
    expect(snapshot.warning).toBeNull();
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
    useGameStore.setState({
      drillOpeningKey: "target-fen",
      drillState: "root_reached",
      drillStrictnessCp: 25,
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

  it("routes post-game History action to history callback", async () => {
    const onOpenHistory = vi.fn();
    await startGameAsWhite(onOpenHistory);

    fireEvent.click(screen.getByRole("button", { name: /resign/i }));
    await waitFor(() => {
      expect(screen.getByText("Are you sure?")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Resign"));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^history$/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /^history$/i }));

    expect(onOpenHistory).toHaveBeenCalledWith(
      expect.objectContaining({
        select: "latest",
        source: "post_game_history",
      }),
    );
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

  it("instant Again restarts the drill with exact stored settings and no overlay", async () => {
    await driveOffRouteFail();
    useGameStore.setState({
      playerColor: "white",
      drillStrictness: "lenient",
      drillStrictnessCp: 20,
    });
    startDrillMock.mockResolvedValueOnce(makeDrillResponse());

    const again = await screen.findByRole("button", { name: /^again$/i });
    await act(async () => {
      fireEvent.click(again);
    });

    await waitFor(() => {
      expect(startDrillMock).toHaveBeenCalledWith({
        opening_key: "target-fen",
        player_color: "white",
        engine_elo: expect.any(Number),
        strictness: "lenient",
        // Exact cp preserved — a 20cp drill restarts at 20cp, not a rounded 25.
        strictness_cp: 20,
      });
    });
    expect(
      screen.queryByRole("button", { name: /start drill/i }),
    ).not.toBeInTheDocument();
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

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^again$/i })).toBeDisabled();
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
    useGameStore.setState({
      playerColor: "black",
      boardOrientation: "black",
      drillStrictness: "lenient",
      drillStrictnessCp: 20,
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
    useGameStore.setState({
      playerColor: "white",
      engineElo: 1500,
      drillStrictness: "lenient",
      drillStrictnessCp: 20,
    });

    const gear = await screen.findByRole("button", {
      name: /change drill settings/i,
    });
    await act(async () => {
      fireEvent.click(gear);
    });

    expect(
      await screen.findByRole("button", { name: /start drill/i }),
    ).toBeInTheDocument();
    // Store values win over localStorage: engine 1500 (not 800), white (not black).
    expect(useGameStore.getState().engineElo).toBe(1500);
    // Drill side is now local state, decoupled from the store playerColorChoice;
    // the White side toggle should be active (from the store's player_color).
    expect(screen.getByRole("button", { name: /^white$/i })).toHaveClass("active");
    // Exact 20cp strictness from the store, not the rounded 50 from localStorage.
    await waitFor(() => {
      expect(screen.getByText(/20 cp loss allowed/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/50 cp loss allowed/i)).not.toBeInTheDocument();
    // The store's opening is selected, not the localStorage one (picker trigger
    // shows the selected opening name).
    await waitFor(() => {
      expect(screen.getByRole("combobox")).toHaveTextContent("Target");
    });
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
    // First overlay open succeeds; the reopen's fetch fails.
    getOpeningRootsMock
      .mockResolvedValueOnce(targetFamily)
      .mockRejectedValueOnce(new Error("boom"));

    await driveOffRouteFail();
    useGameStore.setState({ playerColor: "white", drillStrictnessCp: 20 });

    const gear = await screen.findByRole("button", {
      name: /change drill settings/i,
    });
    await act(async () => {
      fireEvent.click(gear);
    });

    // Opening resolves and Start Drill becomes enabled.
    await waitFor(() => {
      expect(screen.getByRole("combobox")).toHaveTextContent("Target");
    });
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

  // Reaches the natural-end PostGameBanner ("Another drill") branch by resigning
  // a drill (abandonDrill -> drillState "failed", finishLocalGame sets
  // gameResult + showPostGamePrompt).
  const reachNaturalEndDrillBanner = async () => {
    getOpeningRootsMock.mockResolvedValue({ families: [] });
    abandonDrillMock.mockResolvedValueOnce({ drill_state: "failed" });
    await startGameAsWhite();
    useGameStore.setState({
      drillOpeningKey: "target-fen",
      drillState: "active",
      drillStrictness: "lenient",
      drillStrictnessCp: 20,
      playerColor: "white",
    });

    fireEvent.click(screen.getByRole("button", { name: /resign/i }));
    await waitFor(() => {
      expect(screen.getByText("Are you sure?")).toBeInTheDocument();
    });
    await act(async () => {
      fireEvent.click(screen.getByText("Resign"));
    });

    expect(
      await screen.findByRole("button", { name: /another drill/i }),
    ).toBeInTheDocument();
    expect(useGameStore.getState().gameResult).not.toBeNull();
  };

  it("natural-end Another drill restarts instantly with exact stored settings", async () => {
    await reachNaturalEndDrillBanner();
    startDrillMock.mockResolvedValueOnce(makeDrillResponse());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /another drill/i }));
    });

    await waitFor(() => {
      expect(startDrillMock).toHaveBeenCalledWith({
        opening_key: "target-fen",
        player_color: "white",
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

    await new Promise((r) => setTimeout(r, 50));

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
      const rehookToasts = Array.from(
        document.querySelectorAll(".chess-warning-stack .rehook-toast"),
      );
      expect(
        rehookToasts.some((toast) =>
          toast.textContent?.includes("The haunting resumes"),
        ),
      ).toBe(true);
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
      expect(document.querySelector(".review-warning-toast")).toBeInTheDocument();
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
      expect(document.querySelector(".review-warning-toast")).toBeInTheDocument();
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
      expect(document.querySelector(".review-warning-toast")).toBeInTheDocument();
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
    ).toBe("You made this mistake again!");
    const inlineBubble = document.querySelector(".move-bubble--srs-fail");
    expect(inlineBubble?.textContent).toContain("You made this mistake again!");
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
      "You made this mistake again!",
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
      expect(document.querySelector(".review-warning-toast")).toBeInTheDocument();
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
      expect(document.querySelector(".review-warning-toast")).toBeInTheDocument();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      const toast = document.querySelector(".review-warning-toast--pass");
      expect(toast).toBeInTheDocument();
      expect(toast?.querySelector(".review-warning-toast__overlay-icon")?.textContent).toBe("✓");
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
      expect(document.querySelector(".review-warning-toast")).toBeInTheDocument();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      const toast = document.querySelector(".review-warning-toast--fail");
      expect(toast).toBeInTheDocument();
      expect(toast?.querySelector(".review-warning-toast__overlay-icon")?.textContent).toBe("✗");
    });
  });

  it("clears resolved review overlay on next move", async () => {
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
      expect(document.querySelector(".review-warning-toast")).toBeInTheDocument();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      expect(document.querySelector(".review-warning-toast--pass")).toBeInTheDocument();
    });

    // Make another move — overlay should clear
    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "d2", targetSquare: "d4" });
    });

    await waitFor(() => {
      expect(document.querySelector(".review-warning-toast--pass")).not.toBeInTheDocument();
    });
  });

  it("clears resolved review overlay on revert", async () => {
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
            id: "review-pass-revert",
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
          return "review-pass-revert";
        }
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });
    await waitFor(() => {
      expect(document.querySelector(".review-warning-toast")).toBeInTheDocument();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      expect(document.querySelector(".review-warning-toast--pass")).toBeInTheDocument();
    });

    // Trigger revert (unrated, so no warning dialog)
    useGameStore.getState().setIsRated(false);
    fireEvent.click(screen.getByTitle("Revert last move"));

    await waitFor(() => {
      expect(document.querySelector(".review-warning-toast--pass")).not.toBeInTheDocument();
    });
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

    await waitFor(() => {
      expect(mockCoordinator.flushPendingUploads).toHaveBeenCalled();
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

describe("ChessGame opening display", () => {
  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    startGameMock.mockReset();
    uploadSessionMovesMock.mockReset();
    getNextOpponentMoveMock.mockReset();
    evaluatePositionMock.mockReset();
    lookupOpeningByFenMock.mockReset();
    gameAnalysisStore.getState().clearAll();
    capturedPieceDrop = null;

    getNextOpponentMoveMock.mockResolvedValue({
      mode: "engine",
      move: { uci: "e7e5", san: "e5" },
      target_blunder_id: null,
      decision_source: "backend_engine",
    });
    lookupOpeningByFenMock.mockResolvedValue({
      eco: "C20",
      name: "King's Pawn Game",
      source: "eco",
    });
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 });
  });

  it("shows opening only during an active game", async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-opening",
      engine_elo: 1500,
      player_color: "white",
    });

    render(<ChessGame />);

    expect(screen.queryByText(/^Opening:/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(screen.getByText("Opening:")).toBeInTheDocument();
      expect(screen.getByText("C20 King's Pawn Game")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /reset/i }));
    expect(screen.queryByText(/^Opening:/i)).not.toBeInTheDocument();
  });

  it("keeps opening tied to live position while navigating history", async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-live-opening",
      engine_elo: 1500,
      player_color: "white",
    });
    lookupOpeningByFenMock.mockResolvedValue({
      eco: "C50",
      name: "Italian Game",
      source: "eco",
    });

    render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(lookupOpeningByFenMock).toHaveBeenCalled();
    });

    const initialLookupCount = lookupOpeningByFenMock.mock.calls.length;

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(lookupOpeningByFenMock.mock.calls.length).toBeGreaterThan(
        initialLookupCount,
      );
      expect(screen.getByText("C50 Italian Game")).toBeInTheDocument();
    });

    const afterMoveLookupCount = lookupOpeningByFenMock.mock.calls.length;
    fireEvent.click(screen.getByTitle(/previous move/i));

    expect(screen.getByText("C50 Italian Game")).toBeInTheDocument();
    expect(lookupOpeningByFenMock.mock.calls.length).toBe(afterMoveLookupCount);
  });

  it("keeps last known opening after leaving the opening book", async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: "session-sticky-opening",
      engine_elo: 1500,
      player_color: "white",
    });
    lookupOpeningByFenMock
      .mockResolvedValueOnce({
        eco: "C20",
        name: "King's Pawn Game",
        source: "eco",
      })
      .mockResolvedValue(null);

    render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /new game/i }));
    fireEvent.click(screen.getByRole("button", { name: /play white/i }));

    await waitFor(() => {
      expect(screen.getByText("C20 King's Pawn Game")).toBeInTheDocument();
    });

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(lookupOpeningByFenMock.mock.calls.length).toBeGreaterThanOrEqual(
        2,
      );
    });

    // Should retain the last known opening, not show "Unknown"
    expect(screen.getByText("C20 King's Pawn Game")).toBeInTheDocument();
    expect(screen.queryByText("Unknown")).not.toBeInTheDocument();
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

  it("preserves analysis data across unmount/remount and flushes coordinator on resign", async () => {
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
    render(<ChessGame />);

    // Move list should still show both moves (game store persists moveHistory)
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /e4/i })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    // Resign — coordinator should flush pending uploads
    fireEvent.click(screen.getByRole("button", { name: /resign/i }));
    await waitFor(() => {
      expect(screen.getByText("Are you sure?")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Resign"));

    await waitFor(() => {
      expect(mockCoordinator.flushPendingUploads).toHaveBeenCalled();
    });

    await waitFor(() => {
      expect(endGameMock).toHaveBeenCalledWith(
        "session-remount",
        "resign",
        expect.any(String),
        expect.any(Boolean),
      );
    });
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

  it("ignores square-click interaction while the blunder rewind override is active", async () => {
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

    expect(useGameStore.getState().moveHistory).toHaveLength(moveCountBeforeClick);
    expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-position",
      sourceFenBeforeBlunder,
    );
    expect(screen.getByTestId("chessboard")).toHaveAttribute(
      "data-allow-dragging",
      "false",
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
      useGameStore.setState({
        drillOpeningKey: "target-fen",
        drillState: "root_reached",
        drillStrictnessCp: threshold,
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
    useGameStore.setState({
      drillOpeningKey: "target-fen",
      drillState: "root_reached",
      drillStrictnessCp: 0,
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

  it("Again restarts with the preserved exact settings and no stale-session traffic", async () => {
    seedAbandonedDrillStore();
    useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
    setReturnMarker("drill-1");

    render(<ChessGame />);

    fireEvent.click(screen.getByRole("button", { name: /^again$/i }));

    await waitFor(() => {
      expect(startDrillMock).toHaveBeenCalledWith({
        opening_key: "ruy-lopez",
        player_color: "white",
        engine_elo: 1500,
        strictness: "standard",
        strictness_cp: 25,
      });
    });
    // White-to-move restart: no opponent move fired against the abandoned drill.
    expect(getNextOpponentMoveMock).not.toHaveBeenCalled();
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
  });

  it("preserves the retained engine Elo against the on-mount rating resample", async () => {
    seedAbandonedDrillStore();
    useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
    setReturnMarker("drill-1");
    // A rating that maps to a different Maia bin — if the resample fired it would
    // clobber the retained 1500 before "Again" reads it.
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

    expect(useGameStore.getState().engineElo).toBe(1500);

    fireEvent.click(screen.getByRole("button", { name: /^again$/i }));
    await waitFor(() => {
      expect(startDrillMock).toHaveBeenCalledWith(
        expect.objectContaining({ engine_elo: 1500 }),
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
      setup: () => {
        seedAbandonedDrillStore();
        useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
      },
    },
    {
      name: "marker/snapshot/store session mismatch",
      setup: () => {
        seedAbandonedDrillStore("drill-1");
        useDrillAnalysisStore.getState().setSnapshot(snapshotFor("other"));
        setReturnMarker("other");
      },
    },
    {
      name: "marker present but game still active",
      setup: () => {
        seedAbandonedDrillStore();
        useGameStore.setState({ isGameActive: true });
        useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
        setReturnMarker("drill-1");
      },
    },
    {
      name: "drill not abandoned",
      setup: () => {
        seedAbandonedDrillStore();
        useGameStore.setState({ drillState: "failed" });
        useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
        setReturnMarker("drill-1");
      },
    },
    {
      name: "incomplete restart settings",
      setup: () => {
        seedAbandonedDrillStore();
        useGameStore.setState({ drillStrictnessCp: null });
        useDrillAnalysisStore.getState().setSnapshot(snapshotFor("drill-1"));
        setReturnMarker("drill-1");
      },
    },
  ])("falls back to ordinary /play: $name", ({ setup }) => {
    it("shows ordinary start UI and no reviewed Again action", () => {
      setup();
      render(<ChessGame />);

      // No reviewed "Again" action exposed.
      expect(screen.queryByRole("button", { name: /^again$/i })).toBeNull();
    });
  });
});
