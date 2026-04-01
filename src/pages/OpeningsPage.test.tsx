import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { flushSync } from "react-dom";
import type { ChildrenResponse, OpeningChildItem } from "../utils/api";

const mockLogout = vi.fn();
const getOpeningChildrenMock = vi.fn();

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

import AppRoutes from "../AppRoutes";
import OpeningsPage from "./OpeningsPage";

function makeChild(overrides: Partial<OpeningChildItem>): OpeningChildItem {
  return {
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
    children: [],
    total_children: overrides.children?.length ?? 0,
    computed_at: "2026-03-30T12:00:00Z",
    ...overrides,
  };
}

const whiteTopLevelResponse = makeResponse({
  player_color: "white",
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

function renderPage() {
  return render(
    <MemoryRouter>
      <OpeningsPage />
    </MemoryRouter>,
  );
}

function renderRoute(path = "/openings") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes />
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

describe("OpeningsPage", () => {
  beforeEach(() => {
    getOpeningChildrenMock.mockReset();
    mockLogout.mockReset();
  });

  it("registers a dedicated /openings route and nav link", async () => {
    getOpeningChildrenMock.mockResolvedValueOnce(whiteTopLevelResponse);

    renderRoute("/openings");

    await waitFor(() => {
      expect(getOpeningChildrenMock).toHaveBeenCalledWith("white", undefined);
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
    expect(getOpeningChildrenMock).toHaveBeenCalledWith("white", undefined);
  });

  it("renders populated opening cards strongest-first with normalized percentages", async () => {
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
    expect(within(firstCard!).getByText(/Weakest root:/)).toBeInTheDocument();
    expect(within(firstCard!).getByText("Winawer Variation")).toBeInTheDocument();
    expect(within(firstCard!).getByText("D")).toBeInTheDocument();
    expect(within(firstCard!).getByText("Games")).toBeInTheDocument();
    expect(within(firstCard!).getByText("82%")).toBeInTheDocument();
    expect(within(firstCard!).getByText("58%")).toBeInTheDocument();
    expect(within(firstCard!).getByText("Leaf branch")).toBeInTheDocument();
    expect(within(firstCard!).getByTestId("opening-card-board")).toHaveAttribute(
      "data-position",
      "french",
    );
  });

  it("ignores a stale response that settles during a color switch", async () => {
    const whiteDeferred = createDeferred<typeof whiteTopLevelResponse>();
    const blackDeferred = createDeferred<typeof blackTopLevelResponse>();

    getOpeningChildrenMock.mockImplementationOnce(() => whiteDeferred.promise);
    getOpeningChildrenMock.mockImplementationOnce(() => blackDeferred.promise);

    renderPage();

    expect(getOpeningChildrenMock).toHaveBeenCalledWith("white", undefined);

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

  it("shows the true no-evidence empty state when computed_at is null and all children are unscored", async () => {
    getOpeningChildrenMock.mockResolvedValueOnce(
      makeResponse({
        computed_at: null,
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
  });

  it("shows the computed snapshot empty state when all returned children are unscored", async () => {
    getOpeningChildrenMock.mockResolvedValueOnce(
      makeResponse({
        computed_at: "2026-03-30T12:00:00Z",
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

  it("renders mixed scored and unscored children without switching to the empty state", async () => {
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
    expect(
      within(unscoredCard!).getByText("No scored roots in this subtree yet."),
    ).toBeInTheDocument();
    expect(within(unscoredCard!).getAllByText("—")).toHaveLength(3);
  });

  it("drills down by refetching with parent_key and can navigate back", async () => {
    const user = userEvent.setup();
    getOpeningChildrenMock.mockResolvedValueOnce(
      makeResponse({
        children: [
          makeChild({
            opening_key: "polish",
            opening_name: "Polish Opening",
            child_count: 2,
            subtree_score: 58,
            subtree_confidence: 0.64,
            subtree_coverage: 0.41,
            subtree_sample_size: 14,
            subtree_root_count: 3,
            weakest_root_name: "Polish Opening, 1...e6",
            weakest_root_score: 42,
          }),
        ],
      }),
    );
    getOpeningChildrenMock.mockResolvedValueOnce(
      makeResponse({
        parent_key: "polish",
        parent_name: "Polish Opening",
        children: [
          makeChild({
            opening_key: "polish-e6",
            opening_name: "Polish Opening, 1...e6",
            child_count: 0,
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
      }),
    );
    getOpeningChildrenMock.mockResolvedValueOnce(
      makeResponse({
        children: [
          makeChild({
            opening_key: "polish",
            opening_name: "Polish Opening",
            child_count: 2,
            subtree_score: 58,
            subtree_confidence: 0.64,
            subtree_coverage: 0.41,
            subtree_sample_size: 14,
            subtree_root_count: 3,
            weakest_root_name: "Polish Opening, 1...e6",
            weakest_root_score: 42,
          }),
        ],
      }),
    );

    renderPage();

    await screen.findByRole("region", { name: "White openings" });

    await user.click(screen.getByRole("button", { name: /Polish Opening/ }));

    await waitFor(() => {
      expect(getOpeningChildrenMock).toHaveBeenNthCalledWith(2, "white", "polish");
    });

    expect(await screen.findByText("Polish Opening")).toBeInTheDocument();
    expect(screen.getByText("OPENING SCOREBOARD / Polish Opening")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Back" }));

    await waitFor(() => {
      expect(getOpeningChildrenMock).toHaveBeenNthCalledWith(3, "white", undefined);
    });

    expect(
      await screen.findByRole("region", { name: "White openings" }),
    ).toBeInTheDocument();
  });

  it("switches colors and refetches for the selected side", async () => {
    const user = userEvent.setup();
    getOpeningChildrenMock.mockResolvedValueOnce(whiteTopLevelResponse);
    getOpeningChildrenMock.mockResolvedValueOnce(blackTopLevelResponse);

    renderPage();

    await screen.findByRole("region", { name: "White openings" });

    await user.click(screen.getByRole("button", { name: "Black" }));

    await waitFor(() => {
      expect(getOpeningChildrenMock).toHaveBeenLastCalledWith("black", undefined);
    });

    expect(
      await screen.findByRole("region", { name: "Black openings" }),
    ).toBeInTheDocument();
    expect(screen.getByText("King's Indian Defense")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Black" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("shows fetch failure and retry preserves the active color", async () => {
    const user = userEvent.setup();
    getOpeningChildrenMock.mockResolvedValueOnce(whiteTopLevelResponse);
    getOpeningChildrenMock.mockRejectedValueOnce(
      new Error("Opening children cache unavailable"),
    );
    getOpeningChildrenMock.mockResolvedValueOnce(blackTopLevelResponse);

    renderPage();

    await screen.findByRole("region", { name: "White openings" });

    await user.click(screen.getByRole("button", { name: "Black" }));

    await waitFor(() => {
      expect(
        screen.getByText("Opening children cache unavailable"),
      ).toBeInTheDocument();
    });

    expect(screen.getByRole("button", { name: "Black" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(
      await screen.findByRole("region", { name: "Black openings" }),
    ).toBeInTheDocument();
    expect(getOpeningChildrenMock).toHaveBeenNthCalledWith(1, "white", undefined);
    expect(getOpeningChildrenMock).toHaveBeenNthCalledWith(2, "black", undefined);
    expect(getOpeningChildrenMock).toHaveBeenNthCalledWith(3, "black", undefined);
  });
});
