import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { Chess } from "chess.js";
import { render, screen, fireEvent, waitFor, act } from "../test/utils";
import ChessGame from "./ChessGame";
import { useGameStore } from "../stores/useGameStore";
import { STARTING_FEN } from "./chess-game/config";

const startGameMock = vi.fn();
const endGameMock = vi.fn();
const uploadSessionMovesMock = vi.fn();
const getNextOpponentMoveMock = vi.fn();
const recordBlunderMock = vi.fn();
const recordManualBlunderMock = vi.fn();
const reviewSrsBlunderMock = vi.fn();
const fetchCurrentRatingMock = vi.fn();
const audioPlayMock = vi.fn();
const audioCtorSpy = vi.fn();

vi.mock("../utils/api", () => ({
  startGame: (...args: unknown[]) => startGameMock(...args),
  endGame: (...args: unknown[]) => endGameMock(...args),
  uploadSessionMoves: (...args: unknown[]) => uploadSessionMovesMock(...args),
  getNextOpponentMove: (...args: unknown[]) => getNextOpponentMoveMock(...args),
  fetchCurrentRating: (...args: unknown[]) => fetchCurrentRatingMock(...args),
  recordBlunder: (...args: unknown[]) => recordBlunderMock(...args),
  recordManualBlunder: (...args: unknown[]) => recordManualBlunderMock(...args),
  reviewSrsBlunder: (...args: unknown[]) => reviewSrsBlunderMock(...args),
}));

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
}));

import { gameAnalysisStore } from "../stores/createAnalysisStore";

const mockAnalyzeMove = vi.fn();

const mockCoordinator = {
  analyzeMove: mockAnalyzeMove,
  clearAnalysis: vi.fn(),
  startSession: vi.fn(),
  clearSession: vi.fn(),
  flushPendingUploads: vi.fn().mockResolvedValue(undefined),
  sessionId: null,
  store: gameAnalysisStore,
  setOnAnalysisResolved: vi.fn(),
};

vi.mock("../contexts/GameAnalysisCoordinatorContext", () => ({
  useGameAnalysisCoordinator: () => mockCoordinator,
}));

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
  useGameStore.setState(initialGameStoreState, true);
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
    recordManualBlunderMock.mockReset();
    reviewSrsBlunderMock.mockReset();
    lookupOpeningByFenMock.mockReset();
    gameAnalysisStore.getState().clearAll();
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 });
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

  it("shows a warning before revert and only marks game unrated after confirm", async () => {
    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /d5/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /revert last move/i }));
    expect(
      screen.getByText("This game will not be rated"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(
      screen.queryByText("This game will not be rated"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/^unrated$/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /revert last move/i }));
    fireEvent.click(screen.getByRole("button", { name: /revert anyway/i }));

    expect(
      screen.queryByText("This game will not be rated"),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/^unrated$/i)).toBeInTheDocument();
    expect(screen.getByText(/no moves yet/i)).toBeInTheDocument();
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
    const { rerender: _rerender } = render(<ChessGame />);

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
        "c2c4",
        50,
        -150,
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

  it("does not retry recordBlunder on API failure", async () => {
    recordBlunderMock.mockRejectedValueOnce(new Error("Network error"));

    mockAnalyzeMove.mockImplementation(
      (_fen: string, move: string, _color: string, moveIndex: number) => {
        if (moveIndex !== 2) {
          return;
        }
        gameAnalysisStore.getState().resolveAnalysis(moveIndex, {
          id: "fail-test",
          move,
          bestMove: "c2c4",
          bestEval: 50,
          playedEval: -150,
          currentPositionEval: -150,
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

    // Even after a second analysis result, no retry since blunderRecordedRef is true
    act(() => {
      gameAnalysisStore.getState().setLastAnalysis({
        id: "fail-test-2",
        move: "g1f3",
        bestMove: "c2c4",
        bestEval: 50,
        playedEval: -150,
        currentPositionEval: -150,
        moveIndex: 2,
        delta: 200,
        classification: "blunder" as const,
        blunder: true,
        recordable: true,
      });
    });

    await new Promise((r) => setTimeout(r, 50));

    expect(recordBlunderMock).toHaveBeenCalledTimes(1);
  });

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
      screen.queryByText("Ghost reactivated: steering to past mistake"),
    ).not.toBeInTheDocument();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "g1", targetSquare: "f3" });
    });

    await waitFor(() => {
      expect(
        screen.getByText("Ghost reactivated: steering to past mistake"),
      ).toBeInTheDocument();
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
            moveIndex: 2,
            delta: 20,
            classification: "good" as const,
            blunder: false,
        recordable: false,
          });
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
            moveIndex: 2,
            delta: 50,
            classification: "good" as const,
            blunder: false,
        recordable: false,
          });
        }
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
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
      );
    });
    expect(
      screen.queryByText("You avoided your past mistake."),
    ).not.toBeInTheDocument();
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
            moveIndex: 2,
            delta: 20,
            classification: "good" as const,
            blunder: false,
        recordable: false,
          });
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
            moveIndex: 2,
            delta: 20,
            classification: "good" as const,
            blunder: false,
            recordable: false,
          });
        }
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
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
            moveIndex: 2,
            delta: 50,
            classification: "good" as const,
            blunder: false,
            recordable: false,
          });
        }
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
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
            moveIndex: 2,
            delta: 20,
            classification: "good" as const,
            blunder: false,
            recordable: false,
          });
        }
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
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
            moveIndex: 2,
            delta: 20,
            classification: "good" as const,
            blunder: false,
            recordable: false,
          });
        }
      },
    );

    await startGameAsWhite();

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: "e2", targetSquare: "e4" });
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

  const resolveMoveTwoAsBlunder = () => {
    act(() => {
      gameAnalysisStore.getState().resolveAnalysis(2, {
        id: "rewind-blunder",
        move: "g1f3",
        bestMove: "d2d4",
        bestEval: 50,
        playedEval: -150,
        currentPositionEval: -150,
        moveIndex: 2,
        delta: 200,
        classification: "blunder" as const,
        blunder: true,
        recordable: true,
      });
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
    resolveMoveTwoAsBlunder();

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
    resolveMoveTwoAsBlunder();

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
    resolveMoveTwoAsBlunder();

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
    resolveMoveTwoAsBlunder();

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
    resolveMoveTwoAsBlunder();

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
