import { describe, expect, it, vi, beforeEach } from "vitest";
import { render } from "../../test/utils";
import { ConnectedAnalysisGraph } from "./AnalysisConnectors";
import { useGameStore } from "../../stores/useGameStore";
import {
  AnalysisStoreProvider,
  createAnalysisStore,
} from "../../stores/createAnalysisStore";
import type { MoveRecord } from "./domain/movePresentation";

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
        [0, { playedEval: 0, bestEval: 0, bestMove: "e4", delta: 0, classification: "best", blunder: false }],
        [1, { playedEval: 50, bestEval: 50, bestMove: "d4", delta: 0, classification: "best", blunder: false }],
        [2, { playedEval: 9990, bestEval: 9990, bestMove: "Qh4", delta: 0, classification: "best", blunder: false }],
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
        [0, { playedEval: 30, bestEval: 30, bestMove: "e4", delta: 0, classification: "best", blunder: false }],
      ]),
    });

    renderConnected();

    expect(capturedProps.isCheckmate).toBe(false);
  });
});
