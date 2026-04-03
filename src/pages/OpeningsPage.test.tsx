import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { flushSync } from "react-dom";
import { MemoryRouter, useLocation } from "react-router-dom";
import type {
  ChildrenResponse,
  CurrentBranchStats,
  OpeningChildItem,
} from "../utils/api";

const mockLogout = vi.fn();
const getOpeningChildrenMock = vi.fn();
const getOpeningBookMock = vi.fn();

vi.mock("../contexts/useAuth", () => ({
  useAuth: () => ({
    user: {
      id: 1,
      username: "tester",
      isAnonymous: false,
    },
    logout: mockLogout,
  }),
}));

vi.mock("../utils/api", async () => {
  const actual = await vi.importActual<typeof import("../utils/api")>(
    "../utils/api",
  );

  return {
    ...actual,
    getOpeningChildren: (
      ...args: Parameters<typeof actual.getOpeningChildren>
    ) => getOpeningChildrenMock(...args),
  };
});

vi.mock("react-chessboard", () => ({
  Chessboard: ({ options }: { options: Record<string, unknown> }) => (
    <div
      data-testid="opening-card-board"
      data-position={options.position as string}
      data-orientation={options.boardOrientation as string}
    />
  ),
}));

vi.mock("../openings/openingBook", () => ({
  getOpeningBook: () => getOpeningBookMock(),
}));

import AppRoutes from "../AppRoutes";
import OpeningsPage from "./OpeningsPage";

const FEN_ROOT = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -";
const FEN_PARENT = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -";
const FEN_LEAF =
  "rnbqkbnr/pppp1ppp/8/4p3/3PP3/8/PPP2PPP/RNBQKBNR b KQkq -";

function LocationProbe() {
  const location = useLocation();

  return (
    <output data-testid="route-location">
      {location.pathname}
      {location.search}
    </output>
  );
}

function makeBreadcrumb(
  opening_key: string,
  opening_name: string,
  is_current = false,
) {
  return { opening_key, opening_name, is_current };
}

function makeCurrentBranchStats(
  overrides: Partial<CurrentBranchStats> = {},
): CurrentBranchStats {
  return {
    score: 61,
    confidence: 0.72,
    coverage: 0.48,
    sample_size: 56,
    root_count: 4,
    ...overrides,
  };
}

function makeChild(overrides: Partial<OpeningChildItem>): OpeningChildItem {
  const merged: OpeningChildItem = {
    opening_key: "root-1",
    opening_name: "Root 1",
    opening_family: "Root 1",
    eco: null,
    depth: 1,
    child_count: 0,
    subtree_score: 50,
    subtree_confidence: 0.5,
    subtree_coverage: 0.5,
    subtree_sample_size: 10,
    subtree_root_count: 1,
    last_practiced_at: "2026-03-29T10:00:00Z",
    weakest_root_key: "root-1",
    weakest_root_name: "Root 1",
    weakest_root_family: "Root 1",
    weakest_root_score: 50,
    ...overrides,
  };

  return {
    ...merged,
    weakest_root_key: overrides.weakest_root_key ?? merged.opening_key,
    weakest_root_name: overrides.weakest_root_name ?? merged.opening_name,
    weakest_root_family: overrides.weakest_root_family ?? merged.opening_family,
  };
}

function makeResponse(
  overrides: Partial<ChildrenResponse> & {
    children?: OpeningChildItem[];
  },
): ChildrenResponse {
  return {
    player_color: "white",
    parent_key: null,
    parent_name: null,
    canonical_opening_key: null,
    canonical_path: [],
    breadcrumbs: [],
    current_branch_stats: makeCurrentBranchStats(),
    children: [],
    total_children: overrides.children?.length ?? 0,
    computed_at: "2026-03-30T12:00:00Z",
    ...overrides,
  };
}

const whiteTopLevelResponse = makeResponse({
  player_color: "white",
  current_branch_stats: makeCurrentBranchStats({
    score: 63,
    confidence: 0.72,
    coverage: 0.81,
    sample_size: 144,
    root_count: 7,
  }),
  children: [
    makeChild({
      opening_key: "sicilian",
      opening_name: "Sicilian Defense",
      child_count: 1,
      subtree_score: 44,
      subtree_confidence: 0.67,
      subtree_coverage: 0.36,
      subtree_sample_size: 12,
      subtree_root_count: 2,
      weakest_root_name: "Dragon Variation",
      weakest_root_score: 33,
    }),
    makeChild({
      opening_key: "french",
      opening_name: "French Defense",
      child_count: 0,
      subtree_score: 52,
      subtree_confidence: 0.82,
      subtree_coverage: 0.58,
      subtree_sample_size: 26,
      subtree_root_count: 3,
      weakest_root_name: "Winawer Variation",
      weakest_root_score: 33,
    }),
    makeChild({
      opening_key: "caro",
      opening_name: "Caro-Kann Defense",
      child_count: 2,
      subtree_score: 49.2,
      subtree_confidence: 0.71,
      subtree_coverage: 0.44,
      subtree_sample_size: 18,
      subtree_root_count: 2,
      weakest_root_name: "Advance Variation",
      weakest_root_score: 41,
    }),
  ],
});

const blackTopLevelResponse = makeResponse({
  player_color: "black",
  current_branch_stats: makeCurrentBranchStats({
    score: 74,
    confidence: 0.69,
    coverage: 0.62,
    sample_size: 88,
    root_count: 3,
  }),
  children: [
    makeChild({
      opening_key: "kings-indian",
      opening_name: "King's Indian Defense",
      child_count: 1,
      subtree_score: 61,
      subtree_confidence: 0.74,
      subtree_coverage: 0.52,
      subtree_sample_size: 21,
      subtree_root_count: 2,
      weakest_root_name: "Classical Variation",
      weakest_root_score: 47,
    }),
  ],
});

const polishResponse = makeResponse({
  parent_key: "polish",
  parent_name: "Polish Opening",
  canonical_opening_key: "polish",
  canonical_path: [],
  breadcrumbs: [makeBreadcrumb("polish", "Polish Opening", true)],
  current_branch_stats: makeCurrentBranchStats({
    score: 67,
    confidence: 0.59,
    coverage: 0.38,
    sample_size: 23,
    root_count: 2,
  }),
  children: [
    makeChild({
      opening_key: "polish-e6",
      opening_name: "Polish Opening, 1...e6",
      child_count: 1,
      subtree_score: 42,
      subtree_confidence: 0.55,
      subtree_coverage: 0.33,
      subtree_sample_size: 8,
      subtree_root_count: 1,
      weakest_root_key: "polish-e6",
      weakest_root_name: "Polish Opening, 1...e6",
      weakest_root_family: "Polish Opening",
      weakest_root_score: 42,
    }),
  ],
});

const polishE6Response = makeResponse({
  parent_key: "polish-e6",
  parent_name: "Polish Opening, 1...e6",
  canonical_opening_key: "polish-e6",
  canonical_path: ["polish"],
  breadcrumbs: [
    makeBreadcrumb("polish", "Polish Opening"),
    makeBreadcrumb("polish-e6", "Polish Opening, 1...e6", true),
  ],
  current_branch_stats: makeCurrentBranchStats({
    score: 31,
    confidence: 0.44,
    coverage: 0.29,
    sample_size: 9,
    root_count: 1,
  }),
  children: [
    makeChild({
      opening_key: "polish-leaf",
      opening_name: "Polish Leaf",
      child_count: 1,
      subtree_score: 39,
      subtree_confidence: 0.48,
      subtree_coverage: 0.27,
      subtree_sample_size: 5,
      subtree_root_count: 1,
      weakest_root_key: "polish-leaf",
      weakest_root_name: "Polish Leaf",
      weakest_root_family: "Polish Opening",
      weakest_root_score: 39,
    }),
  ],
});

const polishLeafResponse = makeResponse({
  parent_key: "polish-leaf",
  parent_name: "Polish Leaf",
  canonical_opening_key: "polish-leaf",
  canonical_path: ["polish", "polish-e6"],
  breadcrumbs: [
    makeBreadcrumb("polish", "Polish Opening"),
    makeBreadcrumb("polish-e6", "Polish Opening, 1...e6"),
    makeBreadcrumb("polish-leaf", "Polish Leaf", true),
  ],
  current_branch_stats: makeCurrentBranchStats({
    score: 28,
    confidence: 0.33,
    coverage: 0.21,
    sample_size: 4,
    root_count: 1,
  }),
  children: [],
});

function renderPage(path = "/openings?color=white") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <OpeningsPage />
      <LocationProbe />
    </MemoryRouter>,
  );
}

function renderRoute(path = "/openings?color=white") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes />
      <LocationProbe />
    </MemoryRouter>,
  );
}

function createDeferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;

  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });

  return { promise, resolve, reject };
}

function createNeverSettlingPromise<T>() {
  return new Promise<T>(() => undefined);
}

describe("OpeningsPage", () => {
  beforeEach(() => {
    getOpeningChildrenMock.mockReset();
    mockLogout.mockReset();
    getOpeningBookMock.mockReset();
    getOpeningBookMock.mockImplementation(() => createNeverSettlingPromise());
  });

  it("registers a dedicated /openings route and nav link", async () => {
    getOpeningChildrenMock.mockResolvedValueOnce(whiteTopLevelResponse);

    renderRoute("/openings?color=white");

    await waitFor(() => {
      expect(getOpeningChildrenMock).toHaveBeenCalledWith({
        playerColor: "white",
        path: [],
        parentKey: undefined,
      });
    });

    expect(
      screen.getByRole("heading", { name: "OPENING SCOREBOARD" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Openings" })).toHaveAttribute(
      "href",
      "/openings",
    );
    expect(screen.queryByText("Your Stats")).not.toBeInTheDocument();
  });

  it("shows a loading state before the first response", () => {
    getOpeningChildrenMock.mockImplementation(
      () => new Promise(() => undefined),
    );

    renderPage();

    expect(screen.getByText("Loading openings...")).toBeInTheDocument();
    expect(getOpeningChildrenMock).toHaveBeenCalledWith({
      playerColor: "white",
      path: [],
      parentKey: undefined,
    });
  });

  it("shows repertoire-wide hero stats at Start", async () => {
    getOpeningChildrenMock.mockResolvedValueOnce(whiteTopLevelResponse);

    renderPage();

    const heroStats = screen.getByLabelText("Current branch stats");

    await waitFor(() => {
      expect(within(heroStats).getByText("63")).toBeInTheDocument();
      expect(within(heroStats).getByText("81%")).toBeInTheDocument();
      expect(within(heroStats).getByText("144")).toBeInTheDocument();
      expect(within(heroStats).getByText("72%")).toBeInTheDocument();
    });
    expect(within(heroStats).getByText("Repertoire-wide")).toBeInTheDocument();
    expect(heroStats).toHaveClass("openings-shell__stats-card--watch");
  });

  it("renders populated opening cards strongest-first with normalized percentages", async () => {
    getOpeningBookMock.mockResolvedValueOnce({
      entries: [
        { epd: "french", pgn: "1. e4 e6 2. d4 d5" },
        { epd: "sicilian", pgn: "1. e4 c5" },
        { epd: "caro", pgn: "1. e4 c6" },
      ],
    });
    getOpeningChildrenMock.mockResolvedValueOnce(whiteTopLevelResponse);

    renderPage();

    const grid = await screen.findByRole("region", {
      name: "White openings",
    });
    const headings = within(grid)
      .getAllByRole("heading", { level: 2 })
      .map((heading) => heading.textContent);

    expect(headings).toEqual([
      "French Defense",
      "Caro-Kann Defense",
      "Sicilian Defense",
    ]);

    const firstCard = within(grid)
      .getByRole("heading", { name: "French Defense" })
      .closest("article");

    expect(firstCard).not.toBeNull();
    expect(within(firstCard!).getByText(/Moves:/)).toBeInTheDocument();
    expect(within(firstCard!).getByText("1.e4 e6 2.d4 d5")).toBeInTheDocument();
    expect(within(firstCard!).getByText("D")).toBeInTheDocument();
    expect(within(firstCard!).getByText("Games")).toBeInTheDocument();
    expect(within(firstCard!).getByText("82%")).toBeInTheDocument();
    expect(within(firstCard!).getByText("58%")).toBeInTheDocument();
    expect(within(firstCard!).getByText("No children")).toBeInTheDocument();
    expect(within(firstCard!).getByTestId("opening-card-board")).toHaveAttribute(
      "data-position",
      "french",
    );
  });

  it("shows branch hero stats on drill pages instead of reusing the child summary", async () => {
    getOpeningChildrenMock.mockResolvedValueOnce(polishResponse);

    renderPage("/openings?color=white&opening=polish");

    const heroStats = screen.getByLabelText("Current branch stats");

    await waitFor(() => {
      expect(within(heroStats).getByText("67")).toBeInTheDocument();
      expect(within(heroStats).getByText("38%")).toBeInTheDocument();
      expect(within(heroStats).getByText("23")).toBeInTheDocument();
      expect(within(heroStats).getByText("59%")).toBeInTheDocument();
    });
    expect(within(heroStats).queryByText("42")).not.toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
  });

  it("ignores a stale response that settles during a color switch", async () => {
    const whiteDeferred = createDeferred<typeof whiteTopLevelResponse>();
    const blackDeferred = createDeferred<typeof blackTopLevelResponse>();

    getOpeningChildrenMock.mockImplementationOnce(() => whiteDeferred.promise);
    getOpeningChildrenMock.mockImplementationOnce(() => blackDeferred.promise);

    renderPage();

    expect(getOpeningChildrenMock).toHaveBeenCalledWith({
      playerColor: "white",
      path: [],
      parentKey: undefined,
    });

    flushSync(() => {
      fireEvent.click(screen.getByRole("button", { name: "Black" }));
    });

    expect(screen.getByRole("button", { name: "Black" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    whiteDeferred.resolve(whiteTopLevelResponse);
    await Promise.resolve();
    await Promise.resolve();

    expect(screen.getByText("Loading openings...")).toBeInTheDocument();
    expect(screen.queryByText("Sicilian Defense")).not.toBeInTheDocument();

    blackDeferred.resolve(blackTopLevelResponse);

    expect(
      await screen.findByRole("region", { name: "Black openings" }),
    ).toBeInTheDocument();
    expect(screen.getByText("King's Indian Defense")).toBeInTheDocument();
  });

  it("accepts deep links with FEN-shaped opening and repeated path params", async () => {
    getOpeningChildrenMock.mockResolvedValueOnce(
      makeResponse({
        player_color: "black",
        parent_key: FEN_LEAF,
        parent_name: "French Defense: Advance Variation",
        canonical_opening_key: FEN_LEAF,
        canonical_path: [FEN_ROOT, FEN_PARENT],
        breadcrumbs: [
          makeBreadcrumb(FEN_ROOT, "King's Pawn Game"),
          makeBreadcrumb(FEN_PARENT, "King's Pawn Game: ...e5"),
          makeBreadcrumb(FEN_LEAF, "French Defense: Advance Variation", true),
        ],
        children: [
          makeChild({
            opening_key: "fen-child",
            opening_name: "Fen Child",
            child_count: 0,
          }),
        ],
      }),
    );

    renderRoute(
      `/openings?color=black&opening=${encodeURIComponent(FEN_LEAF)}&path=${encodeURIComponent(FEN_ROOT)}&path=${encodeURIComponent(FEN_PARENT)}`,
    );

    await waitFor(() => {
      expect(getOpeningChildrenMock).toHaveBeenCalledWith({
        playerColor: "black",
        parentKey: FEN_LEAF,
        path: [FEN_ROOT, FEN_PARENT],
      });
    });

    expect(
      screen.getAllByText("French Defense: Advance Variation").length,
    ).toBeGreaterThan(0);
  });

  it("card clicks update the URL and grow repeated path params", async () => {
    const user = userEvent.setup();
    getOpeningChildrenMock.mockResolvedValueOnce(
      makeResponse({
        children: [
          makeChild({
            opening_key: "polish",
            opening_name: "Polish Opening",
            child_count: 1,
          }),
        ],
      }),
    );
    getOpeningChildrenMock.mockResolvedValueOnce(polishResponse);
    getOpeningChildrenMock.mockResolvedValueOnce(polishE6Response);

    renderPage();

    await screen.findByRole("region", { name: "White openings" });

    await user.click(screen.getByRole("button", { name: /Polish Opening/ }));
    await waitFor(() => {
      expect(screen.getByTestId("route-location")).toHaveTextContent(
        "/openings?color=white&opening=polish",
      );
    });

    await user.click(screen.getByRole("button", { name: /Polish Opening, 1...e6/ }));

    await waitFor(() => {
      expect(getOpeningChildrenMock).toHaveBeenNthCalledWith(3, {
        playerColor: "white",
        parentKey: "polish-e6",
        path: ["polish"],
      });
    });
    expect(screen.getByTestId("route-location")).toHaveTextContent(
      "/openings?color=white&opening=polish-e6&path=polish",
    );
  });

  it("breadcrumb clicks navigate to an intermediate level", async () => {
    const user = userEvent.setup();
    getOpeningChildrenMock.mockResolvedValueOnce(polishLeafResponse);
    getOpeningChildrenMock.mockResolvedValueOnce(polishResponse);

    renderPage(
      "/openings?color=white&opening=polish-leaf&path=polish&path=polish-e6",
    );

    await waitFor(() => {
      expect(screen.getAllByText("Polish Leaf").length).toBeGreaterThan(0);
    });

    await user.click(
      screen.getByRole("button", { name: "Polish Opening" }),
    );

    await waitFor(() => {
      expect(getOpeningChildrenMock).toHaveBeenNthCalledWith(2, {
        playerColor: "white",
        parentKey: "polish",
        path: [],
      });
    });
    expect(screen.getByTestId("route-location")).toHaveTextContent(
      "/openings?color=white&opening=polish",
    );
  });

  it("invalid color canonicalizes to white", async () => {
    getOpeningChildrenMock.mockResolvedValue(whiteTopLevelResponse);

    renderRoute("/openings?color=chartreuse");

    await screen.findByRole("region", { name: "White openings" });

    expect(getOpeningChildrenMock).toHaveBeenCalledWith({
      playerColor: "white",
      parentKey: undefined,
      path: [],
    });
    expect(screen.getByTestId("route-location")).toHaveTextContent(
      "/openings?color=white",
    );
  });

  it("invalid path canonicalizes to the deepest valid prefix", async () => {
    getOpeningChildrenMock.mockResolvedValueOnce(
      makeResponse({
        parent_key: "polish",
        parent_name: "Polish Opening",
        canonical_opening_key: "polish",
        canonical_path: [],
        breadcrumbs: [makeBreadcrumb("polish", "Polish Opening", true)],
        current_branch_stats: makeCurrentBranchStats({
          score: 68,
          confidence: 0.58,
          coverage: 0.34,
          sample_size: 21,
          root_count: 2,
        }),
        children: [
          makeChild({
            opening_key: "polish-e6",
            opening_name: "Polish Opening, 1...e6",
            child_count: 0,
          }),
        ],
      }),
    );
    getOpeningChildrenMock.mockResolvedValueOnce(polishResponse);

    renderRoute("/openings?color=white&opening=shared&path=polish&path=english");

    await waitFor(() => {
      expect(screen.getByTestId("route-location")).toHaveTextContent(
        "/openings?color=white&opening=polish",
      );
    });
    await waitFor(() => {
      expect(
        within(screen.getByLabelText("Current branch stats")).getByText("67"),
      ).toBeInTheDocument();
    });
    expect(getOpeningChildrenMock).toHaveBeenNthCalledWith(1, {
      playerColor: "white",
      parentKey: "shared",
      path: ["polish", "english"],
    });
    expect(getOpeningChildrenMock).toHaveBeenNthCalledWith(2, {
      playerColor: "white",
      parentKey: "polish",
      path: [],
    });
  });

  it("unknown opening preserves the URL and shows the error state", async () => {
    getOpeningChildrenMock.mockRejectedValueOnce(
      new Error("Unknown opening root"),
    );

    renderRoute("/openings?color=white&opening=missing-root&path=polish");

    await waitFor(() => {
      expect(screen.getByText("Unknown opening root")).toBeInTheDocument();
    });

    expect(screen.getByTestId("route-location")).toHaveTextContent(
      "/openings?color=white&opening=missing-root&path=polish",
    );
  });

  it("retry preserves the current search params", async () => {
    const user = userEvent.setup();
    getOpeningChildrenMock.mockRejectedValueOnce(
      new Error("Opening children cache unavailable"),
    );
    getOpeningChildrenMock.mockResolvedValueOnce(polishLeafResponse);

    renderRoute(
      "/openings?color=white&opening=polish-leaf&path=polish&path=polish-e6",
    );

    await waitFor(() => {
      expect(
        screen.getByText("Opening children cache unavailable"),
      ).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => {
      expect(getOpeningChildrenMock).toHaveBeenNthCalledWith(2, {
        playerColor: "white",
        parentKey: "polish-leaf",
        path: ["polish", "polish-e6"],
      });
    });
    expect(screen.getByTestId("route-location")).toHaveTextContent(
      "/openings?color=white&opening=polish-leaf&path=polish&path=polish-e6",
    );
  });

  it("direct deep links to a structural leaf show the leaf empty state", async () => {
    getOpeningChildrenMock.mockResolvedValueOnce(polishLeafResponse);

    renderRoute(
      "/openings?color=white&opening=polish-leaf&path=polish&path=polish-e6",
    );

    await waitFor(() => {
      expect(
        screen.getByText("No deeper named openings under Polish Leaf."),
      ).toBeInTheDocument();
    });
  });

  it("shows the true no-evidence empty state when computed_at is null and all children are unscored", async () => {
    getOpeningChildrenMock.mockResolvedValueOnce(
      makeResponse({
        computed_at: null,
        current_branch_stats: makeCurrentBranchStats({
          score: null,
          confidence: null,
          coverage: null,
          sample_size: null,
          root_count: 0,
        }),
        children: [
          makeChild({
            opening_key: "polish",
            opening_name: "Polish Opening",
            child_count: 2,
            subtree_score: null,
            subtree_confidence: null,
            subtree_coverage: null,
            subtree_sample_size: 0,
            subtree_root_count: 0,
            weakest_root_key: null,
            weakest_root_name: null,
            weakest_root_family: null,
            weakest_root_score: null,
          }),
        ],
      }),
    );

    renderPage();

    await waitFor(() => {
      expect(
        screen.getByText("No opening evidence for White yet."),
      ).toBeInTheDocument();
    });
    expect(
      within(screen.getByLabelText("Current branch stats")).getAllByText("—"),
    ).toHaveLength(4);
  });

  it("shows the computed snapshot empty state when all returned children are unscored", async () => {
    getOpeningChildrenMock.mockResolvedValueOnce(
      makeResponse({
        computed_at: "2026-03-30T12:00:00Z",
        current_branch_stats: makeCurrentBranchStats({
          score: null,
          confidence: null,
          coverage: null,
          sample_size: null,
          root_count: 0,
        }),
        children: [
          makeChild({
            opening_key: "polish",
            opening_name: "Polish Opening",
            child_count: 2,
            subtree_score: null,
            subtree_confidence: null,
            subtree_coverage: null,
            subtree_sample_size: 0,
            subtree_root_count: 0,
            weakest_root_key: null,
            weakest_root_name: null,
            weakest_root_family: null,
            weakest_root_score: null,
          }),
        ],
      }),
    );

    renderPage();

    await waitFor(() => {
      expect(
        screen.getByText("No scored openings are available for White yet."),
      ).toBeInTheDocument();
    });
  });

  it("keeps the normal content state when the current branch is scored but all listed children are unscored", async () => {
    getOpeningChildrenMock.mockResolvedValueOnce(
      makeResponse({
        parent_key: "polish",
        parent_name: "Polish Opening",
        canonical_opening_key: "polish",
        canonical_path: [],
        breadcrumbs: [makeBreadcrumb("polish", "Polish Opening", true)],
        current_branch_stats: makeCurrentBranchStats({
          score: 61,
          confidence: 0.52,
          coverage: 0.37,
          sample_size: 19,
          root_count: 1,
        }),
        children: [
          makeChild({
            opening_key: "polish-e6",
            opening_name: "Polish Opening, 1...e6",
            child_count: 0,
            subtree_score: null,
            subtree_confidence: null,
            subtree_coverage: null,
            subtree_sample_size: 0,
            subtree_root_count: 0,
            weakest_root_key: null,
            weakest_root_name: null,
            weakest_root_family: null,
            weakest_root_score: null,
          }),
        ],
      }),
    );

    renderPage("/openings?color=white&opening=polish");

    await waitFor(() => {
      expect(
        within(screen.getByLabelText("Current branch stats")).getByText("61"),
      ).toBeInTheDocument();
    });
    expect(
      screen.queryByText("No opening evidence for White yet."),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("No scored openings are available for White yet."),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "White openings" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Polish Opening, 1...e6" }),
    ).toBeInTheDocument();
  });

  it("renders mixed scored and unscored children without switching to the empty state", async () => {
    getOpeningBookMock.mockResolvedValueOnce({
      entries: [
        { epd: "polish", pgn: "1. b4" },
        { epd: "bird", pgn: "1. f4" },
      ],
    });
    getOpeningChildrenMock.mockResolvedValueOnce(
      makeResponse({
        children: [
          makeChild({
            opening_key: "polish",
            opening_name: "Polish Opening",
            child_count: 1,
            subtree_score: 58,
            subtree_confidence: 0.64,
            subtree_coverage: 0.41,
            subtree_sample_size: 14,
            subtree_root_count: 3,
            weakest_root_name: "Polish Opening, 1...e6",
            weakest_root_score: 42,
          }),
          makeChild({
            opening_key: "bird",
            opening_name: "Bird Opening",
            child_count: 0,
            subtree_score: null,
            subtree_confidence: null,
            subtree_coverage: null,
            subtree_sample_size: 0,
            subtree_root_count: 0,
            weakest_root_key: null,
            weakest_root_name: null,
            weakest_root_family: null,
            weakest_root_score: null,
          }),
        ],
      }),
    );

    renderPage();

    const grid = await screen.findByRole("region", { name: "White openings" });
    const headings = within(grid)
      .getAllByRole("heading", { level: 2 })
      .map((heading) => heading.textContent);

    expect(headings).toEqual(["Polish Opening", "Bird Opening"]);
    expect(screen.queryByText("No opening evidence for White yet.")).not.toBeInTheDocument();

    const unscoredCard = within(grid)
      .getByRole("heading", { name: "Bird Opening" })
      .closest("article");
    expect(unscoredCard).not.toBeNull();
    expect(within(unscoredCard!).getByText("No Data")).toBeInTheDocument();
    expect(within(unscoredCard!).getByText("1.f4")).toBeInTheDocument();
    expect(
      within(unscoredCard!).getByText("No scored roots in this subtree yet."),
    ).toBeInTheDocument();
    expect(within(unscoredCard!).getAllByText("—")).toHaveLength(3);
  });
});
