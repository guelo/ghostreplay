import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import type { TreeColumn, TreeNode, TreeResponse } from "../utils/api";

const getOpeningTreeMock = vi.fn();
const getOpeningTreeStatusMock = vi.fn();
const captureEventMock = vi.fn();

vi.mock("../analytics/posthog", () => ({
  captureEvent: (...args: unknown[]) => captureEventMock(...args),
}));

// Capture the latest Chessboard options so tests can simulate board drops and
// read the rendered position/orientation.
let boardOptions: Record<string, unknown> = {};

vi.mock("../contexts/useAuth", () => ({
  useAuth: () => ({
    user: { id: 1, username: "tester", isAnonymous: false },
    logout: vi.fn(),
  }),
}));

vi.mock("../utils/api", async () => {
  const actual =
    await vi.importActual<typeof import("../utils/api")>("../utils/api");
  return {
    ...actual,
    getOpeningTree: (...args: Parameters<typeof actual.getOpeningTree>) =>
      getOpeningTreeMock(...args),
    getOpeningTreeStatus: (
      ...args: Parameters<typeof actual.getOpeningTreeStatus>
    ) => getOpeningTreeStatusMock(...args),
  };
});

vi.mock("react-chessboard", () => ({
  defaultPieces: {
    wK: () => <svg data-testid="piece-wK" />,
    bK: () => <svg data-testid="piece-bK" />,
  },
  Chessboard: ({ options }: { options: Record<string, unknown> }) => {
    boardOptions = options;
    return (
      <div
        data-testid="opening-card-board"
        data-position={options.position as string}
        data-orientation={options.boardOrientation as string}
      />
    );
  },
}));

import OpeningsPage from "./OpeningsPage";

// ---- fixtures --------------------------------------------------------------

function tn(overrides: Partial<TreeNode> & { uci: string }): TreeNode {
  return {
    parent_fen: "parent",
    child_fen: "child",
    san: overrides.uci,
    ply: 1,
    opening_name: null,
    eco: null,
    in_book: true,
    is_navigable: true,
    is_observed: false,
    is_user_selected: false,
    is_prepared: false,
    user_choice_count: 0,
    encounter_count: 0,
    opening_score: 50,
    confidence: 0.5,
    coverage: 0.5,
    sample_size: 10,
    game_count: 10,
    last_practiced_at: null,
    eval_cp: 20,
    eval_mate: null,
    terminal_reason: null,
    drill_opening_key: null,
    is_selected: false,
    ...overrides,
  };
}

function tc(
  ply: number,
  nodes: TreeNode[],
  selectedUci: string | null = null,
): TreeColumn {
  return { position_fen: `pos-${ply}`, ply, selected_uci: selectedUci, nodes };
}

function tr(overrides: Partial<TreeResponse> = {}): TreeResponse {
  return {
    player_color: "white",
    canonical_line: [],
    selected_fen: "sel",
    selected_ply: 0,
    selected_is_terminal: false,
    selected_terminal_reason: null,
    drill_opening_key: null,
    root_eval_cp: 15,
    root_eval_mate: null,
    root_opening_score: null,
    root_coverage: null,
    root_game_count: null,
    root_confidence: null,
    columns: [],
    batch_computed_at: "2026-06-01T00:00:00Z",
    model_version: "v2",
    ...overrides,
  };
}

const WHITE_ROOT = tr({
  canonical_line: [],
  columns: [
    tc(0, [
      tn({ uci: "e2e4", san: "e4", ply: 1, opening_score: 61 }),
      tn({ uci: "d2d4", san: "d4", ply: 1, opening_score: 55 }),
    ]),
  ],
});

const BLACK_ROOT = tr({
  player_color: "black",
  canonical_line: [],
  columns: [
    tc(0, [
      tn({ uci: "e2e4", san: "e4", ply: 1, opening_score: 40 }),
      tn({ uci: "d2d4", san: "d4", ply: 1, opening_score: 38 }),
    ]),
  ],
});

const WHITE_E4 = tr({
  canonical_line: ["e2e4"],
  columns: [
    tc(
      0,
      [
        tn({ uci: "e2e4", san: "e4", ply: 1, opening_score: 61 }),
        tn({ uci: "d2d4", san: "d4", ply: 1, opening_score: 55 }),
      ],
      "e2e4",
    ),
    tc(1, [
      tn({ uci: "c7c5", san: "c5", ply: 2, opening_score: 50 }),
      tn({ uci: "e7e5", san: "e5", ply: 2, opening_score: 52 }),
    ]),
  ],
});

// Resolved response for a legal off-tree first move (1.a3): a3 is a
// user-selected (third type) node alongside the book moves at the root column.
const WHITE_A3 = tr({
  canonical_line: ["a2a3"],
  columns: [
    tc(
      0,
      [
        tn({ uci: "e2e4", san: "e4", ply: 1, opening_score: 61 }),
        tn({ uci: "d2d4", san: "d4", ply: 1, opening_score: 55 }),
        tn({
          uci: "a2a3",
          san: "a3",
          ply: 1,
          is_user_selected: true,
          in_book: false,
          opening_score: null,
        }),
      ],
      "a2a3",
    ),
  ],
});

// e2e4,c7c5 with the deepest (c7c5) node carrying a drill key + a child column.
const WHITE_SICILIAN = tr({
  canonical_line: ["e2e4", "c7c5"],
  drill_opening_key: "deep-line-key",
  selected_is_terminal: false,
  columns: [
    tc(
      0,
      [
        tn({ uci: "e2e4", san: "e4", ply: 1, opening_score: 61 }),
        tn({ uci: "d2d4", san: "d4", ply: 1, opening_score: 55 }),
      ],
      "e2e4",
    ),
    tc(
      1,
      [
        tn({
          uci: "c7c5",
          san: "c5",
          ply: 2,
          opening_score: 50,
          drill_opening_key: "sicilian-key",
        }),
        tn({ uci: "e7e5", san: "e5", ply: 2, opening_score: 52 }),
      ],
      "c7c5",
    ),
    tc(2, [tn({ uci: "g1f3", san: "Nf3", ply: 3, opening_score: 48 })]),
  ],
});

const WHITE_E4_E5 = tr({
  canonical_line: ["e2e4", "e7e5"],
  columns: [
    tc(
      0,
      [
        tn({ uci: "e2e4", san: "e4", ply: 1, opening_score: 61 }),
        tn({ uci: "d2d4", san: "d4", ply: 1, opening_score: 55 }),
      ],
      "e2e4",
    ),
    tc(
      1,
      [
        tn({ uci: "c7c5", san: "c5", ply: 2, opening_score: 50 }),
        tn({ uci: "e7e5", san: "e5", ply: 2, opening_score: 52 }),
      ],
      "e7e5",
    ),
    tc(2, [tn({ uci: "g1f3", san: "Nf3", ply: 3, opening_score: 45 })]),
  ],
});

// e2e4 selected, its edge BOOK-ONLY (in book, never observed) → dashed, base
// width. col1 (ply1) is an unselected frontier replies column.
const WHITE_E4_BOOK_ONLY = tr({
  canonical_line: ["e2e4"],
  columns: [
    tc(
      0,
      [
        tn({
          uci: "e2e4",
          san: "e4",
          ply: 1,
          in_book: true,
          is_observed: false,
          encounter_count: 0,
        }),
        tn({ uci: "d2d4", san: "d4", ply: 1 }),
      ],
      "e2e4",
    ),
    tc(1, [
      tn({ uci: "c7c5", san: "c5", ply: 2 }),
      tn({ uci: "e7e5", san: "e5", ply: 2 }),
    ]),
  ],
});

// e2e4 selected, its edge OBSERVED with 7 encounters → solid, width 2+log2(8)=5.
const WHITE_E4_OBSERVED = tr({
  canonical_line: ["e2e4"],
  columns: [
    tc(
      0,
      [
        tn({
          uci: "e2e4",
          san: "e4",
          ply: 1,
          in_book: true,
          is_observed: true,
          encounter_count: 7,
        }),
        tn({ uci: "d2d4", san: "d4", ply: 1 }),
      ],
      "e2e4",
    ),
    tc(1, [
      tn({ uci: "c7c5", san: "c5", ply: 2 }),
      tn({ uci: "e7e5", san: "e5", ply: 2 }),
    ]),
  ],
});

// ---- harness ---------------------------------------------------------------

function LocationProbe() {
  const location = useLocation();
  return (
    <output
      data-testid="route-location"
      data-state={JSON.stringify(location.state ?? null)}
    >
      {location.pathname}
      {location.search}
    </output>
  );
}

function Nav({ to, label }: { to: string; label: string }) {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(to)}>
      {label}
    </button>
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderAt(entry: string, extra?: React.ReactNode) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <LocationProbe />
      {extra}
      <Routes>
        <Route path="/openings" element={<OpeningsPage />} />
        <Route path="/play" element={<div data-testid="play-stub" />} />
      </Routes>
    </MemoryRouter>,
  );
}

function location(): string {
  return screen.getByTestId("route-location").textContent ?? "";
}

function lineIndexes(): number[] {
  return screen
    .getAllByTestId("tree-column")
    .map((el) => Number(el.getAttribute("data-line-index")));
}

/** Click a compact node card by its SAN move text. */
function clickMove(san: string) {
  const moveSpan = screen.getByText(san, {
    selector: ".tree-node-card__move",
  });
  fireEvent.click(moveSpan.closest("button") as HTMLButtonElement);
}

/** Fire the board's onSquareClick for a square (the click-to-move entry). */
function clickSquare(square: string) {
  act(() => {
    (boardOptions.onSquareClick as (a: { square: string }) => void)({ square });
  });
}

/** The latest squareStyles passed to the board (last-move + legal-move hints). */
function squareStyles(): Record<string, React.CSSProperties> {
  return (
    (boardOptions.squareStyles as Record<string, React.CSSProperties>) ?? {}
  );
}

beforeEach(() => {
  getOpeningTreeMock.mockReset();
  // Default: cache is warm so the page loads the tree directly (no setup poll).
  // The cold-cache initializing flow is covered by its own tests below.
  getOpeningTreeStatusMock.mockReset();
  getOpeningTreeStatusMock.mockResolvedValue({
    player_color: "white",
    state: "warm",
  });
  captureEventMock.mockReset();
  boardOptions = {};
});

// ---- tests -----------------------------------------------------------------

describe("OpeningsPage tree", () => {
  it("renders the board + root column and stays canonical at the root", async () => {
    getOpeningTreeMock.mockResolvedValue(WHITE_ROOT);
    renderAt("/openings?color=white");

    await screen.findByText("e4", { selector: ".tree-node-card__move" });
    expect(lineIndexes()).toEqual([-1, 0]);
    expect(screen.getByTestId("opening-card-board")).toHaveAttribute(
      "data-orientation",
      "white",
    );
    expect(location()).toBe("/openings?color=white");
    expect(getOpeningTreeMock).toHaveBeenCalledTimes(1);
  });

  it("captures opening_explored when a move node is selected", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    getOpeningTreeMock.mockResolvedValueOnce(WHITE_E4);
    clickMove("e4");

    // Await the refetch settling so the route/state updates triggered by the
    // selection are flushed before we assert (keeps the run act()-warning free).
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1]));
    expect(captureEventMock).toHaveBeenCalledWith("opening_explored", {
      from_key: "",
      to_key: "e2e4",
      depth: 1,
      player_color: "white",
    });
  });

  it("restores a deep line and expands only the deepest selected node", async () => {
    getOpeningTreeMock.mockResolvedValue(WHITE_SICILIAN);
    const { container } = renderAt(
      "/openings?color=white&move=e2e4&move=c7c5",
    );

    await screen.findAllByTestId("tree-column");
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1, 2]));

    const expanded = container.querySelectorAll(".tree-node-card--expanded");
    expect(expanded).toHaveLength(1);
    // The single expanded card is the deepest selected node (c5).
    expect(expanded[0].textContent).toContain("c5");
    // URL already canonical → no rewrite, one fetch.
    expect(location()).toBe("/openings?color=white&move=e2e4&move=c7c5");
    expect(getOpeningTreeMock).toHaveBeenCalledTimes(1);
  });

  it("labels each column header with the selected move (Start / move / —)", async () => {
    getOpeningTreeMock.mockResolvedValue(WHITE_SICILIAN);
    renderAt("/openings?color=white&move=e2e4&move=c7c5");
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1, 2]));

    const headers = screen
      .getAllByTestId("tree-column-header")
      .map((el) => el.textContent);
    // Root → "Start"; the two chosen columns → their move-number labels; the
    // frontier column (no selection yet) → "—".
    expect(headers).toEqual(["Start", "1. e4", `1${"…"} c5`, "—"]);
  });

  it("truncates the line when a column header is clicked", async () => {
    getOpeningTreeMock.mockResolvedValue(WHITE_SICILIAN);
    renderAt("/openings?color=white&move=e2e4&move=c7c5");
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1, 2]));

    // Click the e4 column's header → navigate back to just e2e4.
    fireEvent.click(
      screen.getByText("1. e4", {
        selector: ".openings-tree-column__header",
      }),
    );
    await waitFor(() =>
      expect(location()).toBe("/openings?color=white&move=e2e4"),
    );
  });

  it("canonicalizes a non-canonical line by replacing the URL", async () => {
    getOpeningTreeMock.mockResolvedValue(WHITE_E4);
    renderAt("/openings?color=white&move=e2e4&move=z9z9");

    await waitFor(() =>
      expect(location()).toBe("/openings?color=white&move=e2e4"),
    );
    // The truncated canonical line is cached under its canonical key, so the
    // follow-up render is a cache hit (no second network call).
    expect(getOpeningTreeMock).toHaveBeenCalledTimes(1);
  });

  it("selects a sibling: pushes a truncated line and drops deeper columns immediately", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_SICILIAN);
    renderAt("/openings?color=white&move=e2e4&move=c7c5");
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1, 2]));

    // Keep the sibling refetch pending so we can observe the immediate clip.
    const pending = deferred<TreeResponse>();
    getOpeningTreeMock.mockReturnValueOnce(pending.promise);

    clickMove("e5"); // sibling of c5 in column 1

    // URL truncates c7c5 and pushes e7e5.
    expect(location()).toBe("/openings?color=white&move=e2e4&move=e7e5");
    // Column 2 (children of the old pos) drops before the refetch resolves.
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1]));
    expect(screen.getByText("Loading…")).toBeInTheDocument();

    await act(async () => {
      pending.resolve(WHITE_E4_E5);
    });
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1, 2]));
  });

  it("syncs a navigable board drop to the tree and rejects illegal drops", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    // Illegal drop (e2→e5 is not a legal pawn move) → resolveDrop returns null →
    // rejected, board snaps back, no exploration captured.
    let rejected: boolean | undefined;
    act(() => {
      rejected = (
        boardOptions.onPieceDrop as (a: {
          sourceSquare: string;
          targetSquare: string;
        }) => boolean
      )({ sourceSquare: "e2", targetSquare: "e5" });
    });
    expect(rejected).toBe(false);
    expect(location()).toBe("/openings?color=white");
    // A rejected drop never reaches selectLine, so no exploration is captured.
    expect(captureEventMock).not.toHaveBeenCalled();

    // Navigable drop e2→e4 selects the existing node; board follows immediately.
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_E4);
    let accepted: boolean | undefined;
    act(() => {
      accepted = (
        boardOptions.onPieceDrop as (a: {
          sourceSquare: string;
          targetSquare: string;
        }) => boolean
      )({ sourceSquare: "e2", targetSquare: "e4" });
    });
    expect(accepted).toBe(true);
    expect(location()).toBe("/openings?color=white&move=e2e4");
    expect(screen.getByTestId("opening-card-board").getAttribute("data-position"))
      .toMatch(/4P3/);
    // The board-drop path flows through selectLine too, so it captures directly.
    expect(captureEventMock).toHaveBeenCalledWith("opening_explored", {
      from_key: "",
      to_key: "e2e4",
      depth: 1,
      player_color: "white",
    });
    // Let the post-drop refetch settle so its state update doesn't trail the
    // test as an act() warning.
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1]));
  });

  it("extends the line for a legal off-tree board drop (third move type)", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    // a2a3 is legal but not a node in the frontier column. Previously this was
    // rejected (board snapped back); now it extends the line as a user-selected
    // (third type) move the backend resolves and renders (g-obh5).
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_A3);
    let accepted: boolean | undefined;
    act(() => {
      accepted = (
        boardOptions.onPieceDrop as (a: {
          sourceSquare: string;
          targetSquare: string;
        }) => boolean
      )({ sourceSquare: "a2", targetSquare: "a3" });
    });
    expect(accepted).toBe(true);
    expect(location()).toBe("/openings?color=white&move=a2a3");
    // The off-tree drop flows through selectLine, so it captures directly.
    expect(captureEventMock).toHaveBeenCalledWith("opening_explored", {
      from_key: "",
      to_key: "a2a3",
      depth: 1,
      player_color: "white",
    });
    // The refetch settles into a3 as the deepest selected (expanded) node; let
    // it land so the state update doesn't trail as an act() warning.
    await screen.findByText("1. a3", {
      selector: ".tree-node-card__move-label",
    });
    // "Your move" chip flags the third move type on the expanded card.
    expect(screen.getByText("Your move")).toBeInTheDocument();
    expect(lineIndexes()).toEqual([-1, 0]);
  });

  it("off-tree board drop: loading spinner renders inside the frontier column, not a new column", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    // Keep the off-tree (a2a3) refetch pending so the loading state is visible.
    const pending = deferred<TreeResponse>();
    getOpeningTreeMock.mockReturnValueOnce(pending.promise);
    act(() => {
      (
        boardOptions.onPieceDrop as (a: {
          sourceSquare: string;
          targetSquare: string;
        }) => boolean
      )({ sourceSquare: "a2", targetSquare: "a3" });
    });

    // The spinner sits in a footer INSIDE the frontier column (below the book
    // cards), and NO standalone append loading column is spawned (g-42md).
    await waitFor(() =>
      expect(screen.getByText("Loading…")).toBeInTheDocument(),
    );
    const frontier = screen
      .getAllByTestId("tree-column")
      .find((el) => el.getAttribute("data-line-index") === "0");
    expect(
      frontier?.querySelector(".openings-tree-column__loading-footer"),
    ).not.toBeNull();
    expect(
      document.querySelector(".openings-tree-append--loading"),
    ).toBeNull();
    // Still just root + the one frontier column; the book siblings remain.
    expect(lineIndexes()).toEqual([-1, 0]);
    expect(
      screen.getByText("e4", { selector: ".tree-node-card__move" }),
    ).toBeInTheDocument();

    // Resolving settles a3 into that same column as the expanded "Your move"
    // card and clears the footer.
    act(() => pending.resolve(WHITE_A3));
    await screen.findByText("1. a3", {
      selector: ".tree-node-card__move-label",
    });
    expect(screen.getByText("Your move")).toBeInTheDocument();
    expect(
      document.querySelector(".openings-tree-column__loading-footer"),
    ).toBeNull();
  });

  it("switches perspective: flips orientation, preserves the line, refetches at root", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });
    expect(getOpeningTreeMock).toHaveBeenCalledTimes(1);

    getOpeningTreeMock.mockResolvedValueOnce(BLACK_ROOT);
    fireEvent.click(screen.getByRole("button", { name: "Black" }));

    // Orientation flips immediately and the line (root) is preserved.
    expect(location()).toBe("/openings?color=black");
    expect(screen.getByTestId("opening-card-board")).toHaveAttribute(
      "data-orientation",
      "black",
    );
    // Refetch fires even at the same (root) line.
    await waitFor(() => expect(getOpeningTreeMock).toHaveBeenCalledTimes(2));
    expect(getOpeningTreeMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ playerColor: "black", moves: [] }),
      expect.anything(),
    );
    // Color-specific metric re-hydrates (e4 score 61 → 40).
    await screen.findByText("40", { selector: ".tree-node-card__score" });
  });

  it("legacy opening= link never short-circuits a displayed response", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt(
      "/openings?color=white",
      <Nav to="/openings?color=white&opening=somefen" label="go-legacy" />,
    );
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    getOpeningTreeMock.mockResolvedValueOnce(WHITE_E4);
    fireEvent.click(screen.getByText("go-legacy"));

    // The opening= link must hit the network (no []-prefix short-circuit)…
    await waitFor(() => expect(getOpeningTreeMock).toHaveBeenCalledTimes(2));
    expect(getOpeningTreeMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ opening: "somefen" }),
      expect.anything(),
    );
    // …and the URL canonicalizes to the resolved move= line.
    await waitFor(() =>
      expect(location()).toBe("/openings?color=white&move=e2e4"),
    );
  });

  it("prefix back-nav does not snap forward or refetch", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_SICILIAN);
    renderAt(
      "/openings?color=white&move=e2e4&move=c7c5",
      <Nav to="/openings?color=white&move=e2e4" label="go-prefix" />,
    );
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1, 2]));
    expect(getOpeningTreeMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText("go-prefix"));

    // Clipped to e2e4 with no refetch; URL stays at the prefix.
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1]));
    expect(location()).toBe("/openings?color=white&move=e2e4");
    expect(getOpeningTreeMock).toHaveBeenCalledTimes(1);
  });

  it("does not canonicalize backward while a fresh selection is still pending", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    // Keep the e2e4 fetch pending.
    getOpeningTreeMock.mockReturnValueOnce(deferred<TreeResponse>().promise);
    clickMove("e4");

    expect(location()).toBe("/openings?color=white&move=e2e4");
    // The stale (settled) root response must not rewrite the URL back to [].
    await waitFor(() => expect(screen.getByText("Loading…")).toBeInTheDocument());
    expect(location()).toBe("/openings?color=white&move=e2e4");
  });

  it("isolates clipped-view fields: back-nav to root shows no drill/terminal, no refetch", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_SICILIAN);
    renderAt("/openings?color=white&move=e2e4&move=c7c5");
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1, 2]));
    // The deep selected node is drillable.
    expect(
      screen.getByRole("button", { name: /start drill/i }),
    ).toBeInTheDocument();

    clickMove("Start"); // the root column's compact card → selectLine([])

    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0]));
    expect(location()).toBe("/openings?color=white");
    // Root card (now expanded) carries no drill from the deeper response.
    expect(
      screen.queryByRole("button", { name: /start drill/i }),
    ).not.toBeInTheDocument();
    // Reached via the prefix path — no refetch.
    expect(getOpeningTreeMock).toHaveBeenCalledTimes(1);
  });

  it("separates render depth from selection depth on a deep stale URL", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt(
      "/openings?color=white",
      <Nav
        to="/openings?color=white&move=e2e4&move=c7c5"
        label="go-deep"
      />,
    );
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    const pending = deferred<TreeResponse>();
    getOpeningTreeMock.mockReturnValueOnce(pending.promise);
    fireEvent.click(screen.getByText("go-deep"));

    // Renders only through the divergence (ply 0); deeper columns are NOT leaked.
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0]));
    const expanded = document.querySelectorAll(".tree-node-card--expanded");
    expect(expanded).toHaveLength(1);
    expect(expanded[0].textContent).toContain("e4");

    await act(async () => {
      pending.resolve(WHITE_SICILIAN);
    });
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1, 2]));
  });

  it("shows the no-data banner and stays navigable for a book-only tree", async () => {
    getOpeningTreeMock.mockResolvedValue(
      tr({
        batch_computed_at: null,
        columns: [
          tc(0, [
            tn({ uci: "e2e4", san: "e4", ply: 1, opening_score: null }),
          ]),
        ],
      }),
    );
    renderAt("/openings?color=white");

    expect(
      await screen.findByText(/No games for White yet/i),
    ).toBeInTheDocument();
    // Null-metric node still renders and is selectable.
    clickMove("e4");
    expect(location()).toBe("/openings?color=white&move=e2e4");
  });

  it("renders non-navigable boundary nodes as non-clickable cards", async () => {
    getOpeningTreeMock.mockResolvedValue(
      tr({
        columns: [
          tc(0, [
            tn({ uci: "e2e4", san: "e4", is_navigable: true }),
            tn({ uci: "h2h4", san: "h4", is_navigable: false }),
          ]),
        ],
      }),
    );
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    // The navigable move is a selection button…
    expect(
      screen
        .getByText("e4", { selector: ".tree-node-card__move" })
        .closest("button"),
    ).not.toBeNull();
    // …the boundary move renders as a plain (non-button) card.
    const boundary = screen.getByText("h4", {
      selector: ".tree-node-card__move",
    });
    expect(boundary.closest("button")).toBeNull();

    fireEvent.click(boundary);
    expect(location()).toBe("/openings?color=white");
  });

  it("does not mislabel the no-data banner during a color switch", async () => {
    // White is book-only (batch_computed_at === null).
    getOpeningTreeMock.mockResolvedValueOnce(
      tr({
        batch_computed_at: null,
        columns: [tc(0, [tn({ uci: "e2e4", san: "e4" })])],
      }),
    );
    renderAt("/openings?color=white");
    expect(
      await screen.findByText(/No games for White yet/i),
    ).toBeInTheDocument();

    // Switch to black with a pending fetch: the stale white (book-only) response
    // kept on screen must NOT be relabeled "No games for Black yet".
    getOpeningTreeMock.mockReturnValueOnce(deferred<TreeResponse>().promise);
    fireEvent.click(screen.getByRole("button", { name: "Black" }));
    expect(location()).toBe("/openings?color=black");

    await waitFor(() =>
      expect(screen.getByText("Loading…")).toBeInTheDocument(),
    );
    expect(screen.queryByText(/No games for Black yet/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/No games for White yet/i)).not.toBeInTheDocument();
  });

  it("recovers from a page error via Retry", async () => {
    getOpeningTreeMock.mockRejectedValueOnce(new Error("snapshot down"));
    renderAt("/openings?color=white");

    expect(await screen.findByText("snapshot down")).toBeInTheDocument();
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await screen.findByText("e4", { selector: ".tree-node-card__move" });
    expect(lineIndexes()).toEqual([-1, 0]);
  });

  it("shows an append error + Retry while keeping the existing columns", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    getOpeningTreeMock.mockRejectedValueOnce(new Error("append down"));
    clickMove("e4");

    expect(await screen.findByText("append down")).toBeInTheDocument();
    // Existing root columns remain visible.
    expect(lineIndexes()).toEqual([-1, 0]);

    getOpeningTreeMock.mockResolvedValueOnce(WHITE_E4);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1]));
  });

  it("ignores a stale response that settles during a color switch", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    // Switch to black with a pending (slow) black fetch…
    const blackPending = deferred<TreeResponse>();
    getOpeningTreeMock.mockReturnValueOnce(blackPending.promise);
    fireEvent.click(screen.getByRole("button", { name: "Black" }));
    expect(location()).toBe("/openings?color=black");

    // …then switch back to white (cache hit) before black settles.
    fireEvent.click(screen.getByRole("button", { name: "White" }));
    await waitFor(() => expect(location()).toBe("/openings?color=white"));

    // The late black response is dropped by the version guard.
    await act(async () => {
      blackPending.resolve(BLACK_ROOT);
    });
    expect(screen.getByTestId("opening-card-board")).toHaveAttribute(
      "data-orientation",
      "white",
    );
    await screen.findByText("61", { selector: ".tree-node-card__score" });
  });

  it("navigates to the drill with the drill setup from the expanded card", async () => {
    getOpeningTreeMock.mockResolvedValue(WHITE_SICILIAN);
    renderAt("/openings?color=white&move=e2e4&move=c7c5");
    const drillButton = await screen.findByRole("button", {
      name: /start drill/i,
    });

    fireEvent.click(drillButton);

    const probe = screen.getByTestId("route-location");
    expect(probe.textContent).toBe("/play");
    // Card drills carry the target FEN + full UCI line (not a root key); the
    // backend validates the line and synthesizes metadata to match the card.
    expect(JSON.parse(probe.getAttribute("data-state") ?? "null")).toEqual({
      drillSetup: {
        targetFen: "child",
        line: ["e2e4", "c7c5"],
        displayName: null,
        eco: null,
        playerColor: "white",
      },
    });
  });

  it("omits Start Drill on the synthesized root card (no move selected)", async () => {
    getOpeningTreeMock.mockResolvedValue(WHITE_ROOT);
    renderAt("/openings?color=white");
    // No move selected → the root column is the expanded card.
    await screen.findByText("Starting position", {
      selector: ".tree-node-card__move-label",
    });

    // Every expanded MOVE card is drillable, but the root never is.
    expect(
      screen.queryByRole("button", { name: /start drill/i }),
    ).not.toBeInTheDocument();
  });

  it("shows Start Drill on a deep move card even without a drill key", async () => {
    getOpeningTreeMock.mockResolvedValue(WHITE_E4);
    renderAt("/openings?color=white&move=e2e4");
    // e2e4 is the deepest selected node here → expanded (move label "1. e4").
    await screen.findByText("1. e4", {
      selector: ".tree-node-card__move-label",
    });

    // drill_opening_key is null on this node, but the card is still drillable.
    expect(
      screen.getByRole("button", { name: /start drill/i }),
    ).toBeInTheDocument();
  });

  // Data→width contract: connector WIDTH is applied at render from the selected
  // child's metadata, so it reaches the DOM even with zero jsdom geometry.
  // Dashing is NOT a model property — it's reserved for endpoints scrolled
  // off-screen (clamped), which never happens under jsdom's zero geometry — so
  // every connector here stays solid regardless of book/observed status.
  it("draws a solid, base-width connector for a book-only selected edge", async () => {
    getOpeningTreeMock.mockResolvedValue(WHITE_E4_BOOK_ONLY);
    const { container } = renderAt("/openings?color=white&move=e2e4");
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1]));

    const paths = Array.from(
      container.querySelectorAll("path.openings-tree-connector"),
    );
    expect(paths.length).toBeGreaterThan(0);
    // The edge into e2e4 (book-only, 0 encounters) keeps base width 2, and —
    // unlike the old book-only rule — is no longer dashed. Nothing is off-screen
    // under jsdom, so no connector dashes.
    expect(paths.some((p) => p.getAttribute("stroke-width") === "2")).toBe(true);
    expect(paths.every((p) => p.getAttribute("stroke-dasharray") === null)).toBe(
      true,
    );
  });

  it("draws a solid, thicker connector for an observed selected edge", async () => {
    getOpeningTreeMock.mockResolvedValue(WHITE_E4_OBSERVED);
    const { container } = renderAt("/openings?color=white&move=e2e4");
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1]));

    const paths = container.querySelectorAll("path.openings-tree-connector");
    expect(paths.length).toBeGreaterThan(0);
    // The edge into e2e4 (observed, 7 encounters) is solid at width 5.
    const observed = Array.from(paths).find(
      (p) => p.getAttribute("stroke-width") === "5",
    );
    expect(observed).toBeTruthy();
    expect(observed!.getAttribute("stroke-dasharray")).toBeNull();
  });
});

describe("OpeningsPage click-to-move (g-0b6q)", () => {
  it("selects a piece on click, paints legal-move hints, then moves on the second click", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    // First click selects the e2 pawn: legal dots (e3, e4) + a source highlight
    // are painted, and nothing navigates yet.
    clickSquare("e2");
    const hints = squareStyles();
    expect(hints.e2).toBeDefined();
    expect(hints.e3).toBeDefined();
    expect(hints.e4).toBeDefined();
    expect(location()).toBe("/openings?color=white");
    expect(captureEventMock).not.toHaveBeenCalled();

    // Second click on a legal destination makes the move; the in-tree frontier
    // node (e4) is selected just like the equivalent drag.
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_E4);
    clickSquare("e4");
    expect(location()).toBe("/openings?color=white&move=e2e4");
    expect(captureEventMock).toHaveBeenCalledWith("opening_explored", {
      from_key: "",
      to_key: "e2e4",
      depth: 1,
      player_color: "white",
    });
    // Hints clear once the move lands.
    expect(squareStyles().e3).toBeUndefined();
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1]));
  });

  it("extends the line for a legal off-tree square click (third move type)", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    // a2→a3 is legal but not a frontier node → extend the line (g-obh5),
    // routed through the same path as the drag.
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_A3);
    clickSquare("a2");
    clickSquare("a3");
    expect(location()).toBe("/openings?color=white&move=a2a3");
    expect(captureEventMock).toHaveBeenCalledWith("opening_explored", {
      from_key: "",
      to_key: "a2a3",
      depth: 1,
      player_color: "white",
    });
    await screen.findByText("1. a3", {
      selector: ".tree-node-card__move-label",
    });
  });

  it("does not select an opponent piece or an empty square", async () => {
    getOpeningTreeMock.mockResolvedValue(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    // Black pawn (not the side to move) → no hints.
    clickSquare("e7");
    expect(squareStyles().e7).toBeUndefined();
    expect(squareStyles().e5).toBeUndefined();

    // Empty square → no hints.
    clickSquare("e4");
    expect(squareStyles().e4).toBeUndefined();
    expect(location()).toBe("/openings?color=white");
    expect(captureEventMock).not.toHaveBeenCalled();
  });

  it("clears hints (no navigation) when the second click is an illegal destination", async () => {
    getOpeningTreeMock.mockResolvedValue(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    clickSquare("e2");
    expect(squareStyles().e4).toBeDefined();

    // e2→e5 is illegal for a pawn: applyBoardMove refuses, and since e5 is empty
    // there's nothing to (re)select, so the hints clear and no move is made.
    clickSquare("e5");
    expect(squareStyles().e4).toBeUndefined();
    expect(squareStyles().e2).toBeUndefined();
    expect(location()).toBe("/openings?color=white");
    expect(captureEventMock).not.toHaveBeenCalled();
  });

  it("locks the board with a loading overlay while a move-triggered refetch is in flight", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    // Settled → no board overlay.
    expect(
      document.querySelector(".openings-tree-board__loading"),
    ).toBeNull();

    // Keep the e2e4 refetch pending: the board is locked, so the overlay (with
    // its accessible status role) appears on top of it.
    const pending = deferred<TreeResponse>();
    getOpeningTreeMock.mockReturnValueOnce(pending.promise);
    clickMove("e4");

    await waitFor(() =>
      expect(
        document.querySelector(".openings-tree-board__loading"),
      ).not.toBeNull(),
    );
    expect(
      screen.getByRole("status", { name: "Loading next moves" }),
    ).toBeInTheDocument();

    // Resolving settles the view and removes the overlay.
    await act(async () => {
      pending.resolve(WHITE_E4);
    });
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1]));
    expect(
      document.querySelector(".openings-tree-board__loading"),
    ).toBeNull();
  });

  it("clears stale hints when the perspective is switched at the same root FEN", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    // Select e2 at the root (hints painted).
    clickSquare("e2");
    expect(squareStyles().e4).toBeDefined();

    // Switching to Black flips the board but keeps the starting FEN, so the
    // stale White hints must clear even though board.fen is unchanged.
    getOpeningTreeMock.mockResolvedValueOnce(BLACK_ROOT);
    fireEvent.click(screen.getByRole("button", { name: "Black" }));
    expect(squareStyles().e2).toBeUndefined();
    expect(squareStyles().e4).toBeUndefined();
    expect(squareStyles().e3).toBeUndefined();

    // Let the perspective refetch settle so its state update doesn't trail as an
    // act() warning.
    await screen.findByText("40", { selector: ".tree-node-card__score" });
  });

  it("clears stale hints when the board position changes via a tree click", async () => {
    getOpeningTreeMock.mockResolvedValueOnce(WHITE_ROOT);
    renderAt("/openings?color=white");
    await screen.findByText("e4", { selector: ".tree-node-card__move" });

    // Select e2 (hints shown), then navigate via a tree node instead of the
    // board: the position changes, so the e2 hints must not linger.
    clickSquare("e2");
    expect(squareStyles().e4).toBeDefined();

    getOpeningTreeMock.mockResolvedValueOnce(WHITE_E4);
    clickMove("e4");
    await waitFor(() => expect(lineIndexes()).toEqual([-1, 0, 1]));
    expect(squareStyles().e3).toBeUndefined();
  });
});

describe("OpeningsPage cold-cache setup (g-k4z2)", () => {
  it("shows the one-time setup screen while building, then loads the tree once warm", async () => {
    vi.useFakeTimers();
    try {
      // Cold (user, color): the first probe is still bootstrapping, the next is warm.
      getOpeningTreeStatusMock.mockReset();
      getOpeningTreeStatusMock
        .mockResolvedValueOnce({ player_color: "white", state: "building" })
        .mockResolvedValueOnce({ player_color: "white", state: "warm" });
      getOpeningTreeMock.mockResolvedValue(WHITE_ROOT);

      renderAt("/openings?color=white");

      // Explicit one-time setup state — NOT a silent spinner — and no /tree fetch
      // is issued while the bootstrap runs server-side.
      await act(async () => {
        await Promise.resolve();
      });
      expect(
        screen.getByText(/Setting up your white opening tree/i),
      ).toBeInTheDocument();
      expect(getOpeningTreeMock).not.toHaveBeenCalled();

      // The next poll is warm → the tree loads automatically.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      expect(
        screen.getByText("e4", { selector: ".tree-node-card__move" }),
      ).toBeInTheDocument();
      expect(getOpeningTreeMock).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
