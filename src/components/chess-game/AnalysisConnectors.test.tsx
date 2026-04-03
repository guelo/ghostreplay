import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, act } from "../../test/utils";
import { ConnectedAnalysisGraph, ConnectedMoveList } from "./AnalysisConnectors";
import { useGameStore } from "../../stores/useGameStore";
import {
  AnalysisStoreProvider,
  createAnalysisStore,
} from "../../stores/createAnalysisStore";
import type { MoveRecord } from "./domain/movePresentation";
import type { AnalysisResult } from "../../hooks/useMoveAnalysis";

const makeAnalysis = (overrides: Partial<AnalysisResult> & Pick<AnalysisResult, 'playedEval' | 'bestEval' | 'bestMove' | 'delta' | 'classification' | 'blunder'>): AnalysisResult => ({
  id: crypto.randomUUID(),
  move: 'e2e4',
  currentPositionEval: 0,
  moveIndex: null,
  recordable: false,
  ...overrides,
});

// Capture props forwarded to AnalysisGraph
let capturedProps: Record<string, unknown> = {};

vi.mock("../AnalysisGraph", () => ({
  default: (props: Record<string, unknown>) => {
    capturedProps = props;
    return <div data-testid="analysis-graph" />;
  },
}));

const CHECKMATE_FEN =
  "rnb1kbnr/pppp1ppp/4p3/8/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3";
const NORMAL_FEN =
  "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1";

const initialGameState = useGameStore.getInitialState();

function makeMoveRecord(fen: string): MoveRecord {
  return { san: "e4", fen, uci: "e2e4" };
}

describe("ConnectedAnalysisGraph — isCheckmate prop", () => {
  let store: ReturnType<typeof createAnalysisStore>;

  beforeEach(() => {
    capturedProps = {};
    useGameStore.setState(initialGameState, true);
    store = createAnalysisStore();
  });

  function renderConnected() {
    return render(
      <AnalysisStoreProvider value={store}>
        <ConnectedAnalysisGraph onSelectMove={vi.fn()} />
      </AnalysisStoreProvider>,
    );
  }

  it("forwards isCheckmate=true when selected move FEN is checkmate", () => {
    const moves: MoveRecord[] = [
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(CHECKMATE_FEN),
    ];
    useGameStore.setState({
      moveHistory: moves,
      viewIndex: 2,
      playerColor: "white",
    });

    // Provide evals so the graph renders
    store.setState({
      analysisMap: new Map([
        [0, makeAnalysis({ playedEval: 0, bestEval: 0, bestMove: "e4", delta: 0, classification: "best", blunder: false })],
        [1, makeAnalysis({ playedEval: 50, bestEval: 50, bestMove: "d4", delta: 0, classification: "best", blunder: false })],
        [2, makeAnalysis({ playedEval: 9990, bestEval: 9990, bestMove: "Qh4", delta: 0, classification: "best", blunder: false })],
      ]),
    });

    renderConnected();

    expect(capturedProps.isCheckmate).toBe(true);
  });

  it("forwards isCheckmate=false for a non-checkmate position", () => {
    const moves: MoveRecord[] = [makeMoveRecord(NORMAL_FEN)];
    useGameStore.setState({
      moveHistory: moves,
      viewIndex: 0,
      playerColor: "white",
    });

    store.setState({
      analysisMap: new Map([
        [0, makeAnalysis({ playedEval: 30, bestEval: 30, bestMove: "e4", delta: 0, classification: "best", blunder: false })],
      ]),
    });

    renderConnected();

    expect(capturedProps.isCheckmate).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// ConnectedMoveList — freshlyResolved filtering
// ---------------------------------------------------------------------------

let capturedMoveListProps: Record<string, unknown> = {};

vi.mock("../MoveList", () => ({
  default: (props: Record<string, unknown>) => {
    capturedMoveListProps = props;
    return <div data-testid="move-list" />;
  },
}));

describe("ConnectedMoveList — freshlyResolvedIndices", () => {
  let store: ReturnType<typeof createAnalysisStore>;

  beforeEach(() => {
    capturedMoveListProps = {};
    useGameStore.setState(initialGameState, true);
    store = createAnalysisStore();
  });

  function renderConnected() {
    return render(
      <AnalysisStoreProvider value={store}>
        <ConnectedMoveList
          onNavigate={vi.fn()}
          messages={new Map()}
          onRevealSrsFail={vi.fn()}
          revealedSrsFailIndex={null}
        />
      </AnalysisStoreProvider>,
    );
  }

  it("marks only player-move indices via subscribe on resolveAnalysis", () => {
    const moves: MoveRecord[] = [
      makeMoveRecord(NORMAL_FEN), // 0: white (player)
      makeMoveRecord(NORMAL_FEN), // 1: black (engine)
    ];
    useGameStore.setState({
      moveHistory: moves,
      viewIndex: null,
      playerColor: "white",
    });

    renderConnected();

    // Resolve both moves — subscribe should only mark player move (index 0)
    act(() => {
      store.getState().resolveAnalysis(0, makeAnalysis({ moveIndex: 0, playedEval: 30, bestEval: 30, bestMove: "e4", delta: 0, classification: "good", blunder: false }));
      store.getState().resolveAnalysis(1, makeAnalysis({ moveIndex: 1, playedEval: -10, bestEval: -10, bestMove: "e5", delta: 0, classification: "good", blunder: false }));
    });

    const fresh = capturedMoveListProps.freshlyResolvedIndices as ReadonlySet<number>;
    expect(fresh.has(0)).toBe(true); // player move marked
    expect(fresh.has(1)).toBe(false); // engine move not marked
  });

  it("does not include indices after resetTransient", () => {
    useGameStore.setState({
      moveHistory: [makeMoveRecord(NORMAL_FEN)],
      viewIndex: null,
      playerColor: "white",
    });

    store.setState({
      analysisMap: new Map([
        [0, makeAnalysis({ moveIndex: 0, playedEval: 30, bestEval: 30, bestMove: "e4", delta: 0, classification: "best", blunder: false })],
      ]),
      freshlyResolved: new Set([0]),
    });

    renderConnected();

    act(() => {
      store.getState().resetTransient();
    });

    const fresh = capturedMoveListProps.freshlyResolvedIndices as ReadonlySet<number>;
    expect(fresh.size).toBe(0);
  });
});
