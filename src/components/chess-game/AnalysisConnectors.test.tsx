import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, act } from "../../test/utils";
import { setMatchMedia } from "../../test/setup";
import { ConnectedAnalysisGraph, ConnectedEvalBar, ConnectedMoveList } from "./AnalysisConnectors";
import { useGameStore } from "../../stores/useGameStore";
import {
  AnalysisStoreProvider,
  createAnalysisStore,
} from "../../stores/createAnalysisStore";
import type { MoveRecord } from "./domain/movePresentation";
import type { AnalysisResult } from "../../hooks/useMoveAnalysis";
import { GAME_MOBILE_QUERY } from "../../styles/breakpoints";

const makeAnalysis = (overrides: Partial<AnalysisResult> & Pick<AnalysisResult, 'playedEval' | 'bestEval' | 'bestMove' | 'delta' | 'classification' | 'blunder'>): AnalysisResult => ({
  id: crypto.randomUUID(),
  move: 'e2e4',
  currentPositionEval: 0,
  playedEvalMate: null,
  currentPositionEvalMate: null,
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

// Capture props forwarded to EvalBar
let capturedEvalBarProps: Record<string, unknown> = {};

vi.mock("../EvalBar", () => ({
  default: (props: Record<string, unknown>) => {
    capturedEvalBarProps = props;
    return <div data-testid="eval-bar" />;
  },
}));

// Fool's mate (white to move, mated): black delivers mate at an ODD ply.
const CHECKMATE_FEN =
  "rnb1kbnr/pppp1ppp/4p3/8/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3";
// Scholar's mate (black to move, mated): white delivers mate at an EVEN ply.
const SCHOLARS_MATE_FEN =
  "r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4";
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

  it("forwards a white-perspective evalMate for a mate-only analysis", () => {
    const moves: MoveRecord[] = [makeMoveRecord(NORMAL_FEN)];
    useGameStore.setState({
      moveHistory: moves,
      viewIndex: 0,
      playerColor: "white",
    });

    // Mate-only entry at move index 0 (white move): playedEval null, mate set.
    store.setState({
      analysisMap: new Map([
        [0, makeAnalysis({ playedEval: null, playedEvalMate: 3, bestEval: null, bestMove: "Qh5", delta: 0, classification: "best", blunder: false })],
      ]),
    });

    renderConnected();

    // index 0 is even → white perspective unchanged
    expect(capturedProps.evalMate).toBe(3);
    // cp falls back to a mate-derived value so the badge can position itself
    expect(capturedProps.evalCp).not.toBeNull();
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
// ConnectedAnalysisGraph — terminal checkmate synthesis (g-j7br)
// ---------------------------------------------------------------------------

describe("ConnectedAnalysisGraph — terminal checkmate synthesis", () => {
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

  it("pegs an absent terminal mate (white wins) to the mover and excludes it from pending", () => {
    // 3 plies; the mating move is index 2 (even → white). It is ABSENT from
    // analysisMap (the live persistence race), but its FEN is checkmate.
    const moves: MoveRecord[] = [
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(SCHOLARS_MATE_FEN),
    ];
    useGameStore.setState({ moveHistory: moves, viewIndex: null, playerColor: "white" });
    store.setState({
      analysisMap: new Map([
        [0, makeAnalysis({ playedEval: 0, bestEval: 0, bestMove: "e4", delta: 0, classification: "best", blunder: false })],
        [1, makeAnalysis({ playedEval: 50, bestEval: 50, bestMove: "Nc6", delta: 0, classification: "best", blunder: false })],
        // index 2 (the checkmate) intentionally absent
      ]),
    });

    renderConnected();

    const evals = capturedProps.evals as (number | null)[];
    expect(evals).toHaveLength(3);
    expect(evals[2]).toBe(10000); // pegged to white (even ply)

    // finding #2: the synthesized terminal point is NOT re-drawn as a pending dot.
    expect(capturedProps.pendingIndices).not.toContain(2);

    // finding #1: badge/bar peg to the winner, not the previous move's eval.
    expect(capturedProps.isCheckmate).toBe(true);
    expect(capturedProps.evalCp).toBe(10000);
    expect(capturedProps.evalMate).toBe(0);
  });

  it("pegs an absent terminal mate (black wins) to the mover", () => {
    // 4 plies; the mating move is index 3 (odd → black).
    const moves: MoveRecord[] = [
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(CHECKMATE_FEN),
    ];
    useGameStore.setState({ moveHistory: moves, viewIndex: null, playerColor: "white" });
    store.setState({
      analysisMap: new Map([
        [0, makeAnalysis({ playedEval: 0, bestEval: 0, bestMove: "f3", delta: 0, classification: "good", blunder: false })],
        [1, makeAnalysis({ playedEval: 20, bestEval: 20, bestMove: "e5", delta: 0, classification: "best", blunder: false })],
        [2, makeAnalysis({ playedEval: 0, bestEval: 0, bestMove: "g4", delta: 0, classification: "blunder", blunder: true })],
        // index 3 (the checkmate) intentionally absent
      ]),
    });

    renderConnected();

    const evals = capturedProps.evals as (number | null)[];
    expect(evals).toHaveLength(4);
    expect(evals[3]).toBe(-10000); // pegged to black (odd ply)
    expect(capturedProps.pendingIndices).not.toContain(3);
    expect(capturedProps.evalCp).toBe(-10000);
    expect(capturedProps.evalMate).toBe(0);
  });

  it("trims and keeps pending a non-mate absent terminal move", () => {
    const moves: MoveRecord[] = [
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN), // not checkmate, absent from map
    ];
    useGameStore.setState({ moveHistory: moves, viewIndex: 1, playerColor: "white" });
    store.setState({
      analysisMap: new Map([
        [0, makeAnalysis({ playedEval: 0, bestEval: 0, bestMove: "e4", delta: 0, classification: "best", blunder: false })],
        [1, makeAnalysis({ playedEval: 50, bestEval: 50, bestMove: "e5", delta: 0, classification: "best", blunder: false })],
        // index 2 absent, non-checkmate FEN → not synthesized
      ]),
    });

    renderConnected();

    const evals = capturedProps.evals as (number | null)[];
    // Trailing null trimmed away (no synthesis for a non-mate terminal move).
    expect(evals).toHaveLength(2);
    // Still pending-marked so it shows the hollow zero-line marker.
    expect(capturedProps.pendingIndices).toContain(2);
  });

  it("pegs the badge to the winner for a resolved-but-eval-less terminal mate row", () => {
    // The terminal ply IS in analysisMap but both eval channels are null (a
    // resolved row that never captured the mate eval). selectedEvalFromMap must
    // short-circuit to the FEN winner instead of walking back to move 1's eval.
    const moves: MoveRecord[] = [
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(SCHOLARS_MATE_FEN),
    ];
    useGameStore.setState({ moveHistory: moves, viewIndex: 2, playerColor: "white" });
    store.setState({
      analysisMap: new Map([
        [0, makeAnalysis({ playedEval: 0, bestEval: 0, bestMove: "e4", delta: 0, classification: "best", blunder: false })],
        [1, makeAnalysis({ playedEval: -50, bestEval: -50, bestMove: "Nc6", delta: 0, classification: "best", blunder: false })],
        [2, makeAnalysis({ playedEval: null, playedEvalMate: null, bestEval: null, bestMove: "Qxf7", delta: 0, classification: "best", blunder: false })],
      ]),
    });

    renderConnected();

    // Not the previous move's −50; pegged to the mating side.
    expect(capturedProps.evalCp).toBe(10000);
    expect(capturedProps.evalMate).toBe(0);
    expect(capturedProps.isCheckmate).toBe(true);
  });
});

describe("ConnectedEvalBar — terminal checkmate synthesis (g-j7br)", () => {
  let store: ReturnType<typeof createAnalysisStore>;

  beforeEach(() => {
    capturedEvalBarProps = {};
    useGameStore.setState(initialGameState, true);
    store = createAnalysisStore();
  });

  it("fills the bar toward the winner for an absent terminal mate", () => {
    const moves: MoveRecord[] = [
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(SCHOLARS_MATE_FEN),
    ];
    useGameStore.setState({ moveHistory: moves, viewIndex: null, playerColor: "white" });
    store.setState({
      analysisMap: new Map([
        [0, makeAnalysis({ playedEval: 0, bestEval: 0, bestMove: "e4", delta: 0, classification: "best", blunder: false })],
        [1, makeAnalysis({ playedEval: -50, bestEval: -50, bestMove: "Nc6", delta: 0, classification: "best", blunder: false })],
        // index 2 absent → selectedEvalFromMap short-circuits on the checkmate FEN
      ]),
    });

    render(
      <AnalysisStoreProvider value={store}>
        <ConnectedEvalBar />
      </AnalysisStoreProvider>,
    );

    // Extreme +cp + mate 0 → EvalBar fills to the winner and labels "#".
    expect(capturedEvalBarProps.whitePerspectiveCp).toBe(10000);
    expect(capturedEvalBarProps.whitePerspectiveMate).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// ConnectedAnalysisGraph — latest-move normalization
// ---------------------------------------------------------------------------

describe("ConnectedAnalysisGraph — latest-move normalization", () => {
  let store: ReturnType<typeof createAnalysisStore>;

  beforeEach(() => {
    capturedProps = {};
    useGameStore.setState(initialGameState, true);
    store = createAnalysisStore();
  });

  function renderConnected(onSelectMove: (index: number | null) => void) {
    return render(
      <AnalysisStoreProvider value={store}>
        <ConnectedAnalysisGraph onSelectMove={onSelectMove} />
      </AnalysisStoreProvider>,
    );
  }

  it("normalizes a click on the latest fully-analyzed ply to null", () => {
    const moves: MoveRecord[] = [
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN),
    ];
    useGameStore.setState({ moveHistory: moves, viewIndex: null, playerColor: "white" });
    store.setState({
      analysisMap: new Map([
        [0, makeAnalysis({ playedEval: 0, bestEval: 0, bestMove: "e4", delta: 0, classification: "best", blunder: false })],
        [1, makeAnalysis({ playedEval: 50, bestEval: 50, bestMove: "e5", delta: 0, classification: "best", blunder: false })],
        [2, makeAnalysis({ playedEval: 30, bestEval: 30, bestMove: "Nf3", delta: 0, classification: "best", blunder: false })],
      ]),
    });

    const onSelectMove = vi.fn();
    renderConnected(onSelectMove);

    const forwarded = capturedProps.onSelectMove as (index: number) => void;
    act(() => forwarded(2));
    expect(onSelectMove).toHaveBeenCalledWith(null);
  });

  it("forwards a numeric index for earlier plotted points", () => {
    const moves: MoveRecord[] = [
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN),
    ];
    useGameStore.setState({ moveHistory: moves, viewIndex: null, playerColor: "white" });
    store.setState({
      analysisMap: new Map([
        [0, makeAnalysis({ playedEval: 0, bestEval: 0, bestMove: "e4", delta: 0, classification: "best", blunder: false })],
        [1, makeAnalysis({ playedEval: 50, bestEval: 50, bestMove: "e5", delta: 0, classification: "best", blunder: false })],
        [2, makeAnalysis({ playedEval: 30, bestEval: 30, bestMove: "Nf3", delta: 0, classification: "best", blunder: false })],
      ]),
    });

    const onSelectMove = vi.fn();
    renderConnected(onSelectMove);

    const forwarded = capturedProps.onSelectMove as (index: number) => void;
    act(() => forwarded(1));
    expect(onSelectMove).toHaveBeenCalledWith(1);
    act(() => forwarded(0));
    expect(onSelectMove).toHaveBeenCalledWith(0);
  });

  it("keeps the rightmost plotted point historical when later moves are unanalyzed", () => {
    const moves: MoveRecord[] = [
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN),
      makeMoveRecord(NORMAL_FEN),
    ];
    useGameStore.setState({ moveHistory: moves, viewIndex: null, playerColor: "white" });
    // Only indices 0-2 analyzed; index 3 absent → trimmed from evals.
    store.setState({
      analysisMap: new Map([
        [0, makeAnalysis({ playedEval: 0, bestEval: 0, bestMove: "e4", delta: 0, classification: "best", blunder: false })],
        [1, makeAnalysis({ playedEval: 50, bestEval: 50, bestMove: "e5", delta: 0, classification: "best", blunder: false })],
        [2, makeAnalysis({ playedEval: 30, bestEval: 30, bestMove: "Nf3", delta: 0, classification: "best", blunder: false })],
      ]),
    });

    const onSelectMove = vi.fn();
    renderConnected(onSelectMove);

    // The trim leaves index 2 as the rightmost selectable resolved point.
    expect((capturedProps.evals as unknown[]).length).toBe(3);

    const forwarded = capturedProps.onSelectMove as (index: number) => void;
    act(() => forwarded(2));
    // 2 !== moveHistory.length - 1 (=3), so it stays historical, not null.
    expect(onSelectMove).toHaveBeenCalledWith(2);
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

vi.mock("../HorizontalMoveList", () => ({
  default: (props: Record<string, unknown>) => {
    capturedMoveListProps = props;
    return <div data-testid="h-move-list" />;
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

  it("rerenders the annotated row when only the mate count changes", () => {
    useGameStore.setState({
      moveHistory: [makeMoveRecord(NORMAL_FEN)],
      viewIndex: null,
      playerColor: "white",
    });

    renderConnected();

    act(() => {
      store.getState().resolveAnalysis(
        0,
        makeAnalysis({ moveIndex: 0, playedEval: 30, playedEvalMate: null, bestEval: 30, bestMove: "e4", delta: 0, classification: "good", blunder: false }),
      );
    });

    const before = (capturedMoveListProps.moves as { evalMate: number | null }[])[0];
    expect(before.evalMate).toBeNull();

    // Same eval/classification, mate count newly present: the stability memo
    // must yield a fresh row object instead of reusing the old one.
    act(() => {
      store.setState({
        analysisMap: new Map([
          [0, makeAnalysis({ moveIndex: 0, playedEval: 30, playedEvalMate: 2, bestEval: 30, bestMove: "e4", delta: 0, classification: "good", blunder: false })],
        ]),
      });
    });

    const after = (capturedMoveListProps.moves as { evalMate: number | null }[])[0];
    expect(after.evalMate).toBe(2);
    expect(after).not.toBe(before);
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

  it("renders HorizontalMoveList below the game's mobile breakpoint", () => {
    useGameStore.setState({ moveHistory: [], viewIndex: null, playerColor: "white" });
    setMatchMedia(GAME_MOBILE_QUERY, true);
    const { queryByTestId } = renderConnected();
    expect(queryByTestId("h-move-list")).not.toBeNull();
    expect(queryByTestId("move-list")).toBeNull();
    expect(capturedMoveListProps.showSoundToggle).toBe(true);
  });

  it("forwards materialFen + playerColor-derived perspective to the list", () => {
    useGameStore.setState({ moveHistory: [], viewIndex: null, playerColor: "black" });
    const onCopyPosition = vi.fn();
    render(
      <AnalysisStoreProvider value={store}>
        <ConnectedMoveList
          onNavigate={vi.fn()}
          messages={new Map()}
          onRevealSrsFail={vi.fn()}
          revealedSrsFailIndex={null}
          materialFen={NORMAL_FEN}
          onCopyPosition={onCopyPosition}
        />
      </AnalysisStoreProvider>,
    );
    expect(capturedMoveListProps.materialFen).toBe(NORMAL_FEN);
    expect(capturedMoveListProps.materialPerspective).toBe("black");
    expect(capturedMoveListProps.onCopyPosition).toBe(onCopyPosition);
  });

  it("renders the vertical MoveList above the game's mobile breakpoint", () => {
    useGameStore.setState({ moveHistory: [], viewIndex: null, playerColor: "white" });
    setMatchMedia(GAME_MOBILE_QUERY, false);
    const { queryByTestId } = renderConnected();
    expect(queryByTestId("move-list")).not.toBeNull();
    expect(queryByTestId("h-move-list")).toBeNull();
    expect(capturedMoveListProps.showSoundToggle).toBe(false);
  });
});
